"""Прогноз реализованной волатильности на 7 и 30 дней — HAR (research/reports/stage6; копия эталона — config/).

HAR7 — короткий горизонт (книги «:har»), HAR30 — длинный (книги «:har30», BTC/ETH; research этап 7).
Модель (как в research): log V_h(D) = b0 + b1·log RV1 + b2·log RV7 + b3·log RV30, V и RV — дисперсии в годовых
(часовые лог-доходности перпетуала BASEUSDT, без вычитания среднего, ×8760); прогноз дисперсии — exp(ŷ + s²/2)
(s² — дисперсия остатков обучения), HAR_h = √.

Переоценка (как в research, `last_fit`): МНК по каждой монете на всех моментах D = 00:00 UTC с известной целью
(D + h дн ≤ сейчас), расширяющееся окно, не меньше MIN_TRAIN моментов. Дисперсия дня — среднее r² часовых
доходностей свечей 00:00…23:00 UTC × 8760, только между соседними свечами; день без 24 доходностей — пропуск;
окно RV_h и цель — только при всех днях окна. История часовых свечей — таблица perp_h1 (докачивается с Bybit).
Раз в месяц (бот: при первом проходе в новом месяце) — новая строка har_models на каждый горизонт; коэффициенты отличаются от эталона
config/har_reference.json больше чем на DRIFT_PCT% — предупреждение администратору. Модели ещё нет — эталон.

В сигнале (signals.py) признаки — RV1, RV7, RV30 по последним часовым свечам, как бот считает RV сейчас
(скользящие окна до последней закрытой свечи).

    python -m optbot.vol --fit [--base BTC]    # докачать свечи и переоценить сейчас
    python -m optbot.vol --show                # модели в работе, расхождение с эталоном, текущие HAR7 и HAR30
"""
from __future__ import annotations

import argparse
import calendar
import json
import logging
import math
import os
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass, field

from . import db
from .config import Settings, settings
from .fmt import DAY_MS, HOUR_MS

log = logging.getLogger("optbot.vol")

HORIZON = 7                  # дней — короткий горизонт сигналов
LONG_HORIZON = 30            # дней — длинный горизонт (HAR30, книги «:har30»)
HORIZONS = (HORIZON, LONG_HORIZON)
LAGS = (1, 7, 30)
MIN_TRAIN = 365              # моментов обучения (research: MIN_TRAIN)
DRIFT_PCT = 10               # расхождение коэффициента с эталоном больше — предупреждение
YEAR_H = 8760
REFERENCE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config",
                              "har_reference.json")
KLINE_PAGE = 1000
MAX_PAGES = 200              # 200 000 часов ≈ 22 года — защита от зацикливания


@dataclass
class HarModel:
    base: str
    b: tuple[float, float, float, float]
    s2: float
    n: int
    source: str = "reference"          # reference | fit
    fitted_ms: int | None = None
    data_end_ms: int | None = None
    horizon: int = HORIZON

    def forecast(self, rv1: float | None, rv7: float | None, rv30: float | None) -> float | None:
        """HAR-прогноз волатильности (годовой, доля) по RV1/RV7/RV30 (годовые, доли); чего-то нет — None."""
        if not all(x and x > 0 and math.isfinite(x) for x in (rv1, rv7, rv30)):
            return None
        y = self.b[0] + sum(bi * math.log(v * v) for bi, v in zip(self.b[1:], (rv1, rv7, rv30)))
        return math.sqrt(math.exp(y + self.s2 / 2))


def load_reference(path: str = REFERENCE_FILE, horizon: int = HORIZON) -> dict[str, HarModel]:
    """Эталон из config/har_reference.json -> {актив: HarModel}; файла нет или кривой — {} (и ошибка в лог)."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)["horizons"][str(horizon)]
        return {b.upper(): HarModel(b.upper(), tuple(float(x) for x in v["b"]), float(v["s2"]), int(v["n"]),
                                    horizon=horizon) for b, v in raw.items()}
    except (OSError, KeyError, TypeError, ValueError) as e:
        log.error("HAR: эталон %s не прочитан — %s", path, e)
        return {}


def drift_pct(m: HarModel, ref: HarModel) -> float:
    """Наибольшее относительное расхождение коэффициентов b0…b3 с эталоном, %."""
    return max(abs(a - r) / abs(r) * 100 if r else (0.0 if a == 0 else math.inf) for a, r in zip(m.b, ref.b))


# ---------- данные ----------
def daily_var(candles: list[tuple[int, float]]) -> dict[int, float]:
    """{начало дня UTC, мс: дисперсия дня в годовых}: r — доходность к свече t от предыдущей (ровно час назад),
    относится к дню свечи t; дней с 24 доходностями — только они."""
    sums: dict[int, list] = {}
    prev = None
    for t, c in candles:
        if prev is not None and t - prev[0] == HOUR_MS and c > 0 and prev[1] > 0:
            r = math.log(c / prev[1])
            s = sums.setdefault(t // DAY_MS * DAY_MS, [0.0, 0])
            s[0] += r * r
            s[1] += 1
        prev = (t, c)
    return {d: s / n * YEAR_H for d, (s, n) in sums.items() if n == 24}


def dataset(dv: dict[int, float], horizon: int = HORIZON, until_ms: int | None = None):
    """Строки обучения: ([log RV1, log RV7, log RV30], log V_h) для D с полными окнами; D + h ≤ until_ms."""
    X, y = [], []
    if not dv:
        return X, y
    days = sorted(dv)
    for d in range(days[0], days[-1] + DAY_MS, DAY_MS):
        if until_ms is not None and d + horizon * DAY_MS > until_ms:
            break
        past = []
        for lag in LAGS:
            w = [dv.get(d - k * DAY_MS) for k in range(1, lag + 1)]
            if any(v is None for v in w) or sum(w) <= 0:
                break
            past.append(math.log(sum(w) / lag))
        if len(past) < len(LAGS):
            continue
        fut = [dv.get(d + k * DAY_MS) for k in range(horizon)]
        if any(v is None for v in fut) or sum(fut) <= 0:
            continue
        X.append(past)
        y.append(math.log(sum(fut) / horizon))
    return X, y


def ols(X: list[list[float]], y: list[float]) -> tuple[list[float], float]:
    """МНК с константой (нормальные уравнения, Гаусс с выбором главного) -> (b, s² с поправкой на степени свободы)."""
    k = len(X[0]) + 1
    A = [[1.0, *row] for row in X]
    M = [[sum(a[i] * a[j] for a in A) for j in range(k)] + [sum(a[i] * t for a, t in zip(A, y))] for i in range(k)]
    for c in range(k):
        p = max(range(c, k), key=lambda r: abs(M[r][c]))
        if abs(M[p][c]) < 1e-12:
            raise ValueError("вырожденная матрица признаков")
        M[c], M[p] = M[p], M[c]
        for r in range(k):
            if r != c:
                f = M[r][c] / M[c][c]
                M[r] = [x - f * z for x, z in zip(M[r], M[c])]
    b = [M[i][k] / M[i][i] for i in range(k)]
    res = [t - sum(bi * ai for bi, ai in zip(b, a)) for a, t in zip(A, y)]
    return b, sum(e * e for e in res) / max(1, len(y) - k)


def fit(base: str, candles: list[tuple[int, float]], now_ms: int, horizon: int = HORIZON) -> HarModel | None:
    """Оценка HAR по истории часовых свечей; моментов меньше MIN_TRAIN — None."""
    X, y = dataset(daily_var(candles), horizon, now_ms)
    if len(y) < MIN_TRAIN:
        return None
    b, s2 = ols(X, y)
    return HarModel(base.upper(), tuple(b), s2, len(y), "fit", now_ms, candles[-1][0] if candles else None, horizon)


def rv_at(candles: list[tuple[int, float]], end_ms: int) -> tuple[float | None, float | None, float | None]:
    """RV1, RV7, RV30 так же, как бот считает RV (signals.realized_vol_window) — по свечам, закрытым к end_ms."""
    from .signals import realized_vol_window
    done = [(t, c) for t, c in candles if t + HOUR_MS <= end_ms]
    return tuple(realized_vol_window(done, HOUR_MS, lag * DAY_MS) for lag in LAGS)


# ---------- БД ----------
def update_candles(conn, client, base: str, now_ms: int | None = None) -> int:
    """Докачать закрытые часовые свечи перпетуала в perp_h1 (пусто — вся история Bybit) -> строк добавлено."""
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    base = base.upper()
    last = conn.execute("SELECT MAX(ts) t FROM perp_h1 WHERE base = ?", (base,)).fetchone()["t"]
    first = conn.execute("SELECT MIN(ts) t FROM perp_h1 WHERE base = ?", (base,)).fetchone()["t"]
    n = 0

    def page_back(end: int, stop: int | None) -> bool:
        """Назад от end до stop (None — до начала истории); каждая страница пишется сразу — сбой посередине
        не теряет скачанное (первая загрузка — десятки страниц). -> дошли ли до начала истории биржи."""
        nonlocal n
        for _ in range(MAX_PAGES):
            page = client.perp_klines(base, 60, end, KLINE_PAGE)
            if not page:
                return True
            with conn:
                n += max(0, conn.executemany(
                    "INSERT OR IGNORE INTO perp_h1 (base, ts, close) VALUES (?,?,?)",
                    [(base, t, c) for t, c in page if t + HOUR_MS <= now_ms and c > 0]).rowcount)   # незакрытую — нет
            oldest = min(t for t, _ in page)
            if stop is not None and oldest <= stop:
                return False
            if oldest > end:                             # биржа отдала не то окно — не зацикливаться
                return True
            # неполная страница — не начало истории: у Bybit окно без startTime — limit интервалов до end, и
            # пропуск свечей внутри истории даёт короткую страницу; начало — только пустая страница
            end = oldest - 1
        log.warning("HAR: %s — свечи докачаны не полностью (больше %d страниц)", base, MAX_PAGES)
        return False

    done_key = f"perp_h1_complete2:{base}"   # 2: прежний признак ставился и на пропуске внутри истории
    if last is not None:
        page_back(now_ms, last)             # новые — до последней сохранённой
    if conn.execute("SELECT 1 FROM market_state WHERE key = ?", (done_key,)).fetchone() is None:
        # начало истории ещё не загружено (первый раз или прошлая загрузка оборвалась) — с самой ранней свечи назад
        if page_back(first - 1 if first is not None else now_ms, None):   # не дошли — продолжим в следующий раз
            with conn:
                conn.execute("INSERT OR REPLACE INTO market_state (key, value) VALUES (?, '1')", (done_key,))
    return n


def load_candles(conn, base: str, lo_ms: int | None = None, hi_ms: int | None = None) -> list[tuple[int, float]]:
    q, a = "SELECT ts, close FROM perp_h1 WHERE base = ?", [base.upper()]
    if lo_ms is not None:
        q += " AND ts >= ?"
        a.append(lo_ms)
    if hi_ms is not None:
        q += " AND ts < ?"
        a.append(hi_ms)
    return [(r["ts"], r["close"]) for r in conn.execute(q + " ORDER BY ts", a)]


def save_model(conn, m: HarModel) -> None:
    with conn:
        conn.execute("INSERT OR REPLACE INTO har_models (base, horizon, fitted_ms, b0, b1, b2, b3, s2, n, data_end_ms) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?)", (m.base, m.horizon, m.fitted_ms, *m.b, m.s2, m.n, m.data_end_ms))


def model_at(conn, base: str, at_ms: int | None = None, horizon: int = HORIZON) -> HarModel | None:
    """Последняя оценка, сделанная не позже at_ms (None — последняя вообще); нет — None."""
    q = "SELECT * FROM har_models WHERE base = ? AND horizon = ?"
    a: list = [base.upper(), horizon]
    if at_ms is not None:
        q += " AND fitted_ms <= ?"
        a.append(at_ms)
    r = conn.execute(q + " ORDER BY fitted_ms DESC LIMIT 1", a).fetchone()
    if r is None:
        return None
    return HarModel(r["base"], (r["b0"], r["b1"], r["b2"], r["b3"]), r["s2"], r["n"], "fit", r["fitted_ms"],
                    r["data_end_ms"], r["horizon"])


def month_start(ms: int) -> int:
    """Начало месяца UTC, мс."""
    t = time.gmtime(ms / 1000)
    return calendar.timegm((t.tm_year, t.tm_mon, 1, 0, 0, 0)) * 1000


@dataclass
class RefitResult:
    fitted: list[HarModel] = field(default_factory=list)
    drift: list[tuple[str, float]] = field(default_factory=list)      # («BTC HAR30», %) — больше DRIFT_PCT
    errors: list[str] = field(default_factory=list)
    candles: int = 0

    def __str__(self) -> str:
        parts = [f"свечей +{self.candles}"]
        parts += [f"{m.base} HAR{m.horizon}: b={'/'.join(f'{x:+.3f}' for x in m.b)} s²={m.s2:.3f} n={m.n}"
                  for m in self.fitted]
        parts += [f"{b}: расхождение с эталоном {d:.0f}%" for b, d in self.drift]
        parts += [f"ошибка: {e}" for e in self.errors]
        return "HAR: " + "; ".join(parts)


class VolModels:
    """Модели HAR по активам и горизонтам (7, 30 дн.) для сигналов: последняя оценка из БД, иначе эталон;
    кэш на CACHE_S."""
    CACHE_S = 600

    def __init__(self, cfg: Settings = settings):
        self.cfg = cfg
        self.refs = {h: load_reference(horizon=h) for h in HORIZONS}
        self.ref = self.refs[HORIZON]
        self._cache: dict[tuple[str, int], tuple[float, HarModel | None]] = {}
        self._refit_lock = threading.Lock()   # задача при старте и ежедневная не качают и не оценивают дважды

    def get(self, base: str, horizon: int = HORIZON) -> HarModel | None:
        base = base.upper()
        hit = self._cache.get((base, horizon))
        if hit and time.monotonic() - hit[0] < self.CACHE_S:
            return hit[1]
        m = None
        try:
            conn = db.market(self.cfg)
            try:
                m = model_at(conn, base, horizon=horizon)
            finally:
                conn.close()
        except (sqlite3.Error, OSError) as e:
            log.warning("HAR%d: модель %s не прочитана из БД — %s (эталон)", horizon, base, e)
        m = m or self.refs.get(horizon, {}).get(base)
        self._cache[(base, horizon)] = (time.monotonic(), m)
        return m

    def refit(self, client, bases: list[str] | None = None, now_ms: int | None = None,
              force: bool = False) -> RefitResult:
        """Докачать свечи и, если в этом месяце оценки ещё не было (или force), переоценить (оба горизонта)."""
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        res = RefitResult()
        if not self._refit_lock.acquire(blocking=False):
            log.info("HAR: переоценка уже идёт — пропуск")
            return res
        try:
            return self._refit(client, bases, now_ms, force, res)
        finally:
            self._refit_lock.release()

    def _refit(self, client, bases, now_ms: int, force: bool, res: RefitResult) -> RefitResult:
        conn = db.market(self.cfg)
        try:
            for base in bases or self.cfg.base_coins:
                base = base.upper()
                try:  # сбой докачки (10016 у биржи) — оценка по уже сохранённой истории; докачка — в следующий раз
                    res.candles += update_candles(conn, client, base, now_ms)
                except Exception as e:
                    log.warning("HAR: %s — свечи не докачаны: %s", base, e)
                    res.errors.append(f"{base}: свечи не докачаны — {e}")
                candles = None
                for h in HORIZONS:
                    try:
                        cur = model_at(conn, base, horizon=h)
                        if not force and cur is not None and cur.fitted_ms >= month_start(now_ms):
                            continue
                        if candles is None:
                            candles = load_candles(conn, base)
                        m = fit(base, candles, now_ms, h)
                        if m is None:
                            res.errors.append(f"{base} HAR{h}: мало истории (< {MIN_TRAIN} дней) — остаётся эталон")
                            continue
                        save_model(conn, m)
                        res.fitted.append(m)
                        if (ref := self.refs[h].get(base)) is not None and (d := drift_pct(m, ref)) > DRIFT_PCT:
                            res.drift.append((f"{base} HAR{h}", d))
                        self._cache.pop((base, h), None)
                    except Exception as e:
                        log.exception("HAR%d: %s не переоценён", h, base)
                        res.errors.append(f"{base} HAR{h}: {e}")
        finally:
            conn.close()
        return res

    def describe(self, base: str, horizon: int = HORIZON) -> str:
        m = self.get(base, horizon)
        if m is None:
            return f"{base} HAR{horizon}: модели нет (ни оценки, ни эталона)"
        ref = self.refs.get(horizon, {}).get(base.upper())
        src = (f"оценка {time.strftime('%Y-%m-%d', time.gmtime(m.fitted_ms / 1000))}, n={m.n}" if m.source == "fit"
               else "эталон research")
        d = f", расхождение с эталоном {drift_pct(m, ref):.1f}%" if ref and m.source == "fit" else ""
        return f"{base} HAR{horizon}: b={'/'.join(f'{x:+.3f}' for x in m.b)} s²={m.s2:.3f} ({src}{d})"


def fmt_drift(res: RefitResult) -> str:
    return ("HAR: коэффициенты новой оценки отличаются от эталона research больше чем на "
            f"{DRIFT_PCT}%: " + ", ".join(f"{b} {d:.0f}%" for b, d in res.drift)
            + ". Книги :har (HAR7) и :har30 (HAR30) используют новую оценку; проверьте данные "
              "(python -m optbot.vol --show).")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    ap = argparse.ArgumentParser(prog="python -m optbot.vol", description="Прогноз RV7 и RV30 (HAR)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--fit", action="store_true", help="докачать часовые свечи и переоценить сейчас")
    g.add_argument("--show", action="store_true", help="модели в работе и текущие HAR7, HAR30")
    ap.add_argument("--base", help="только этот актив")
    a = ap.parse_args(argv)
    from .bybit_client import OptionsClient
    vm = VolModels(settings)
    bases = [a.base.upper()] if a.base else list(settings.base_coins)
    if a.fit:
        res = vm.refit(OptionsClient(settings), bases, force=True)
        print(f"[{settings.env}] {res}")
        if res.drift:
            print(fmt_drift(res))
        return 1 if res.errors else 0
    c = OptionsClient(settings)
    for b in bases:
        line = "\n".join(vm.describe(b, h) for h in HORIZONS)
        try:
            from .signals import RV_HOURS
            h1 = c.perp_closes(b, 60, RV_HOURS)
            rv = rv_at(h1, int(time.time() * 1000))
            line += "\n  RV1/RV7/RV30 " + " / ".join("-" if x is None else f"{x * 100:.1f}%" for x in rv)
            for h in HORIZONS:
                m = vm.get(b, h)
                har = m.forecast(*rv) if m else None
                line += f" → HAR{h} {har * 100:.1f}%" if har else f" → HAR{h} -"
        except Exception as e:
            line += f"\n  текущий HAR не посчитан: {e}"
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
