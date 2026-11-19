"""Сборщик рыночных данных (этап 1): каждые 15 минут — срез по каждой экспирации в SQLite.

По каждому активу и экспирации: форвард, ATM-страйк и ATM IV, цена ATM-стрэддла (mark и mid),
25-дельтовый risk reversal (IV колла с Δ≈+0.25 минус IV пута с Δ≈−0.25, линейная интерполяция по дельте),
спред и OI ATM. Плюс индекс и перпетуал актива (цена, фондирование, открытый интерес), котировки отслеживаемых
ног сигналов, раз в CHAIN_SNAPSHOT_MIN — полная цепочка опционов (история для проверок задним числом), раз в
сутки — индекс страха и жадности (при первом запуске — вся история источника).

Запуск: фоновая задача бота (JobQueue) или вручную
    python -m optbot.collector --once     один проход
    python -m optbot.collector --stats    что накоплено
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import db
from .bybit_client import BybitError, OptionsClient, _d, group_by_strike, parse_option_symbol, spread_pct
from .config import Settings, settings
from .context import _f as _num  # число как есть (0 — значение), не число — None
from .context import fng_history
from .fmt import DAY_MS, utc

log = logging.getLogger("optbot.collector")

FNG_HISTORY_RETRY_S = 3600  # индекс по дням: после сбоя источника — не чаще раза в час (не тормозить каждый срез)



def slot_start(now_ms: int, interval_min: int) -> int:
    """Начало слота: срез за слот пишется один раз, повторный проход в том же слоте игнорируется."""
    step = interval_min * 60_000
    return now_ms // step * step


def seconds_to_next_slot(now_s: float, interval_min: int, lag_s: float = 5) -> float:
    """Сколько ждать до начала следующего слота (+ небольшой запас, чтобы не попасть в конец текущего)."""
    step = interval_min * 60
    return step - now_s % step + lag_s


def _f(x) -> float | None:
    v = _d(x)
    return float(v) if v else None



def iv_at_delta(points: list[tuple[float, float]], target: float) -> float | None:
    """IV при заданной дельте: линейная интерполяция между соседними точками (delta, iv).
    Вне диапазона дельт — None (экстраполяция по крыльям ненадёжна)."""
    pts = sorted(p for p in points if p[1] > 0)
    for (d0, v0), (d1, v1) in zip(pts, pts[1:]):
        if d0 <= target <= d1:
            return v0 if d1 == d0 else v0 + (v1 - v0) * (target - d0) / (d1 - d0)
    return None


def expiry_metrics(rows: list[dict], ts_ms: int, expiry_ms: int) -> dict | None:
    """Метрики одной экспирации по строкам тикеров её опционов. None — если считать не из чего."""
    dte = (expiry_ms - ts_ms) / DAY_MS
    if dte <= 0 or not rows:
        return None
    by_strike = {float(k): v for k, v in group_by_strike(rows).items()}
    underlying = _f(rows[0].get("underlyingPrice"))
    index_price = _f(rows[0].get("indexPrice"))
    pairs = [k for k, v in by_strike.items() if "C" in v and "P" in v]
    if not underlying or not pairs:
        return None
    atm = min(pairs, key=lambda k: abs(k - underlying))
    c, p = by_strike[atm]["C"], by_strike[atm]["P"]

    # ATM-метрики — только по ОБЕИМ ногам: подмена среднего одной ногой даёт ложные скачки в ряду
    civ, piv = _f(c.get("markIv")), _f(p.get("markIv"))
    cb, ca, pb, pa = (_f(c.get("bid1Price")), _f(c.get("ask1Price")), _f(p.get("bid1Price")), _f(p.get("ask1Price")))
    cs, ps = spread_pct(cb, ca), spread_pct(pb, pa)
    cm, pm = _f(c.get("markPrice")), _f(p.get("markPrice"))

    calls = [(float(_d(r.get("delta"))), float(_d(r.get("markIv")))) for v in by_strike.values()
             if (r := v.get("C")) and 0 < _d(r.get("delta")) < 1]
    puts = [(float(_d(r.get("delta"))), float(_d(r.get("markIv")))) for v in by_strike.values()
            if (r := v.get("P")) and -1 < _d(r.get("delta")) < 0]
    iv_c25, iv_p25 = iv_at_delta(calls, 0.25), iv_at_delta(puts, -0.25)

    return dict(
        expiry_ms=expiry_ms, dte=dte, underlying=underlying, index_price=index_price, atm_strike=atm,
        atm_iv=(civ + piv) / 2 if civ and piv else None,
        straddle_mark=cm + pm if cm and pm else None,
        straddle_mid=(cb + ca) / 2 + (pb + pa) / 2 if all((cb, ca, pb, pa)) else None,
        rr25=iv_c25 - iv_p25 if iv_c25 is not None and iv_p25 is not None else None,
        iv_c25=iv_c25, iv_p25=iv_p25,
        atm_spread_pct=(cs + ps) / 2 if cs is not None and ps is not None else None,
        atm_oi=float(_d(c.get("openInterest")) + _d(p.get("openInterest"))),
        n_quotes=sum(1 for r in rows if _d(r.get("bid1Price")) > 0 or _d(r.get("ask1Price")) > 0),
    )


@dataclass
class RunResult:
    ts: int
    snapshots: int = 0
    skipped: int = 0          # слот уже был записан (повторный запуск) — данные не перезаписываются
    legs_missing: list[str] = field(default_factory=list)  # на слежении, но котировки нет
    legs: int = 0
    chain: int = 0            # строк полной цепочки
    fng: int = 0              # новых дней индекса страха и жадности
    errors: list[str] = field(default_factory=list)
    failed_bases: set[str] = field(default_factory=set)  # активы без среза в этом проходе (ошибка загрузки/записи)
    seconds: float = 0.0

    def __str__(self) -> str:
        err = f", ошибки: {'; '.join(self.errors)}" if self.errors else ""
        dup = f", пропущено {self.skipped} (слот {utc(self.ts)} UTC уже записан)" if self.skipped else ""
        miss = f", нет котировок ног: {', '.join(self.legs_missing)}" if self.legs_missing else ""
        extra = (f", цепочка {self.chain}" if self.chain else "") + (f", индекс страха и жадности +{self.fng} дн."
                                                                       if self.fng else "")
        return f"срезов {self.snapshots}, ног {self.legs}{extra} за {self.seconds:.1f} с{dup}{miss}{err}"


class Collector:
    def __init__(self, client: OptionsClient, cfg: Settings = settings):
        self.c = client
        self.cfg = cfg
        self._fng_tried = -1e18  # monotonic: попытка загрузки индекса (после сбоя — не чаще FNG_HISTORY_RETRY_S)
        self._fng_pending: list | None = None  # загружено, но не записано (сбой БД) — записать без новой загрузки

    def collect(self, now_ms: int | None = None) -> RunResult:
        """Один проход: данные по активам запрашиваются параллельно, запись — последовательно.
        Ошибка по активу не прерывает остальные."""
        t0 = time.monotonic()
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        res = RunResult(ts=slot_start(now_ms, self.cfg.collector_interval_min))
        bases = list(self.cfg.base_coins)
        with ThreadPoolExecutor(max_workers=len(bases) or 1) as pool:
            fetched = list(pool.map(self._fetch, bases))
        conn = db.connect(self.cfg.db_file)
        try:
            tracked = {r["symbol"] for r in conn.execute(
                "SELECT symbol FROM tracked_symbols WHERE until_ms > ?", (now_ms,))}
            stored: set[str] = set()  # символы активов, чья запись прошла (а не откатилась)
            for base, (rows, expiries, perp, err) in zip(bases, fetched):
                if err is not None:
                    # ошибки биржи — без трассировки, неожиданные — с ней
                    log.warning("Сборщик: %s — %s", base, err,
                                exc_info=None if isinstance(err, BybitError) else err)
                    res.errors.append(f"{base}: {err}")
                    res.failed_bases.add(base)
                    continue
                try:
                    self._store_base(conn, base, rows, expiries, perp, res, now_ms, tracked)
                    stored.update(r.get("symbol") for r in rows)
                except Exception as e:
                    log.warning("Сборщик: запись %s — %s", base, e, exc_info=e)
                    res.errors.append(f"{base}: {e}")
                    res.failed_bases.add(base)
            res.legs_missing = sorted(tracked - stored)
            if res.legs_missing:  # актив убран из BASE_COINS, сбой загрузки или инструмент снят с торгов
                log.warning("Сборщик: нет котировок ног на слежении: %s", ", ".join(res.legs_missing))
            if self.cfg.chain_keep_days:  # и при выключенном сборе цепочки — старая история не копится
                try:  # история — не срез: её сбой только в лог
                    with conn:
                        conn.execute("DELETE FROM chain_snapshots WHERE ts < ?",
                                     (res.ts - self.cfg.chain_keep_days * DAY_MS,))
                except sqlite3.Error as e:
                    log.warning("Сборщик: старая цепочка не удалена — %s", e)
        finally:
            conn.close()
        res.seconds = time.monotonic() - t0
        return res

    def _chain_due(self, conn: sqlite3.Connection, base: str, ts: int) -> bool:
        """Цепочки за текущий период CHAIN_SNAPSHOT_MIN ещё нет (обычно — срез :00; не прошёл — следующий)."""
        n = self.cfg.chain_snapshot_min
        if not n:
            return False
        start = ts // (n * 60_000) * (n * 60_000)
        try:
            return conn.execute("SELECT 1 FROM chain_snapshots WHERE base = ? AND ts >= ? LIMIT 1",
                                (base, start)).fetchone() is None
        except sqlite3.Error as e:  # сбой истории не роняет срез актива
            log.warning("Сборщик: цепочка %s — %s", base, e)
            return False

    def update_fear_greed(self, now_ms: int | None = None) -> int:
        """Индекс по дням -> новых дней. Таблица пуста — вся история источника, иначе пропущенные дни; раз в
        сутки (значение дня у источника появляется после 00:00 UTC). Отдельно от среза (бот — своей задачей
        после него): сеть не задерживает срез и следующие за ним шаги. Сбой — только лог."""
        if not self.cfg.fng_url:
            return 0
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        try:
            conn = db.connect(self.cfg.db_file)
            try:
                last = conn.execute("SELECT MAX(ts) FROM fear_greed").fetchone()[0]
            finally:
                conn.close()
        except sqlite3.Error as e:
            log.warning("Сборщик: индекс страха и жадности — %s", e)
            return 0
        today = now_ms // DAY_MS * DAY_MS
        if last is not None and last >= today:
            return 0
        if time.monotonic() - self._fng_tried < FNG_HISTORY_RETRY_S:
            return 0
        self._fng_tried = time.monotonic()
        rows, self._fng_pending = self._fng_pending, None
        if rows is None:
            gap = None if last is None else (today - last) // DAY_MS + 2
            try:
                rows = fng_history(self.cfg.fng_url, 0 if gap is None or gap > 1000 else gap)
            except Exception as e:
                log.warning("Сборщик: индекс страха и жадности — %s", type(e).__name__)
                return 0
        try:
            conn = db.connect(self.cfg.db_file)
            try:
                with conn:
                    cur = conn.executemany("INSERT OR IGNORE INTO fear_greed (ts, value, label) VALUES (?,?,?)",
                                           rows)
                return max(cur.rowcount, 0)
            finally:
                conn.close()
        except sqlite3.Error as e:
            self._fng_tried, self._fng_pending = -1e18, rows  # записать в следующем срезе, без новой загрузки
            log.warning("Сборщик: индекс страха и жадности не записан — %s", e)
            return 0

    def _fetch(self, base: str) -> tuple[list[dict], dict[str, int], dict | None, Exception | None]:
        """Тикеры опционов, время экспираций из справочника (deliveryTime), тикер перпетуала."""
        try:
            rows = self.c.tickers(base)
            expiries = {i.expiry_code: i.expiry_ms for i in self.c.load_instruments(base)}
        except Exception as e:
            return [], {}, None, e
        return rows, expiries, self._perp(base), None  # перпетуал отдельно: его сбой не теряет опционы

    def _store_base(self, conn: sqlite3.Connection, base: str, rows: list[dict], expiries: dict[str, int],
                    perp: dict | None, res: RunResult, now_ms: int, tracked: set[str]):
        by_exp: dict[str, list[dict]] = {}
        for r in rows:
            p = parse_option_symbol(r.get("symbol", ""))
            if p:  # нестандартный символ пропускаем, а не роняем весь актив
                by_exp.setdefault(p[1], []).append(r)

        snaps = []
        for exp, exp_rows in by_exp.items():
            if exp not in expiries:
                continue  # нет в справочнике среди торгуемых (снят с торгов / истёк)
            m = expiry_metrics(exp_rows, now_ms, expiries[exp])
            if m:
                snaps.append((exp, m))
        legs = [r for r in rows if r.get("symbol") in tracked]
        chain = []
        if self._chain_due(conn, base, res.ts):
            horizon = now_ms + self.cfg.chain_max_dte * DAY_MS
            for r in rows:
                p = parse_option_symbol(r.get("symbol", ""))
                exp_ms = expiries.get(p[1]) if p else None
                if not exp_ms or not now_ms < exp_ms <= horizon:
                    continue
                delta = _num(r.get("delta"))  # 0 — значение (далёкое крыло), а не «нет данных»
                if delta is not None and abs(delta) < self.cfg.chain_min_delta:
                    continue  # далёкие крылья: котировок почти нет, а строк — больше всего
                # цены и IV 0 — котировки нет (None); размеры и OI 0 — значение
                chain.append((res.ts, r["symbol"], base, exp_ms, float(p[2]), p[3], _f(r.get("bid1Price")),
                              _f(r.get("ask1Price")), _num(r.get("bid1Size")), _num(r.get("ask1Size")),
                              _f(r.get("markPrice")), _f(r.get("markIv")), _f(r.get("bid1Iv")),
                              _f(r.get("ask1Iv")), delta, _num(r.get("openInterest")),
                              _f(r.get("underlyingPrice"))))

        with conn:
            for exp, m in snaps:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO snapshots (ts, base, expiry, expiry_ms, dte, underlying, index_price, "
                    "atm_strike, atm_iv, straddle_mark, straddle_mid, rr25, iv_c25, iv_p25, atm_spread_pct, atm_oi, "
                    "n_quotes, captured_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (res.ts, base, exp, m["expiry_ms"], m["dte"], m["underlying"], m["index_price"], m["atm_strike"],
                     m["atm_iv"], m["straddle_mark"], m["straddle_mid"], m["rr25"], m["iv_c25"], m["iv_p25"],
                     m["atm_spread_pct"], m["atm_oi"], m["n_quotes"], now_ms))
                res.snapshots += cur.rowcount
                res.skipped += 1 - cur.rowcount
            index = next((_f(r.get("indexPrice")) for r in rows if r.get("indexPrice")), None)
            conn.execute("INSERT OR IGNORE INTO spot (ts, base, index_price, perp_price, captured_ms) "
                         "VALUES (?,?,?,?,?)", (res.ts, base, index, _f(perp.get("markPrice")) if perp else None,
                                                now_ms))
            for r in legs:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO leg_marks (ts, symbol, mark, bid, ask, iv, underlying, captured_ms) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (res.ts, r["symbol"], _f(r.get("markPrice")), _f(r.get("bid1Price")), _f(r.get("ask1Price")),
                     _f(r.get("markIv")), _f(r.get("underlyingPrice")), now_ms))
                res.legs += cur.rowcount
        if perp:  # история — отдельной транзакцией: её сбой не откатывает срез актива
            try:
                with conn:
                    conn.execute(
                        "INSERT OR IGNORE INTO perp_context (ts, base, funding, funding_h, oi, oi_value, "
                        "captured_ms) VALUES (?,?,?,?,?,?,?)",
                        # ставка 0 — значение, а не «нет данных» (_f превращает 0 в None)
                        (res.ts, base, _num(perp.get("fundingRate")), _f(perp.get("fundingIntervalHour")),
                         _f(perp.get("openInterest")), _f(perp.get("openInterestValue")), now_ms))
            except sqlite3.Error as e:
                log.warning("Сборщик: фондирование %s не записано — %s", base, e)
        if chain:  # отдельной транзакцией: сбой необязательной истории не откатывает срез актива
            try:
                with conn:
                    cur = conn.executemany(
                        "INSERT OR IGNORE INTO chain_snapshots (ts, symbol, base, expiry_ms, strike, kind, bid, ask, "
                        "bid_size, ask_size, mark, iv, bid_iv, ask_iv, delta, oi, underlying) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", chain)
                res.chain += max(cur.rowcount, 0)
            except sqlite3.Error as e:
                log.warning("Сборщик: цепочка %s не записана — %s", base, e)

    def _perp(self, base: str) -> dict | None:
        try:
            return self.c.perp_ticker(base) or None
        except Exception as e:  # без перпетуала срез всё равно полезен; неожиданное — с трассировкой
            log.warning("Сборщик: перпетуал %sUSDT — %s", base, e,
                        exc_info=None if isinstance(e, BybitError) else e)
            return None


def track(conn: sqlite3.Connection, symbols: list[str], until_ms: int, reason: str = "",
          now_ms: int | None = None) -> None:
    """Поставить ноги на слежение в открытой транзакции (журнал сигналов пишет сигнал и слежение вместе)."""
    conn.executemany(
        "INSERT INTO tracked_symbols (symbol, added_ms, until_ms, reason) VALUES (?,?,?,?) "
        "ON CONFLICT(symbol) DO UPDATE SET until_ms = MAX(until_ms, excluded.until_ms), "
        "reason = CASE WHEN excluded.until_ms > until_ms THEN excluded.reason ELSE reason END",
        [(s, now_ms if now_ms is not None else int(time.time() * 1000), until_ms, reason) for s in symbols])


def stats(path: str) -> str:
    conn = db.connect(path)
    try:
        lines = []
        for r in conn.execute(
                "SELECT base, COUNT(DISTINCT ts) slots, COUNT(*) n, MIN(ts) first, MAX(ts) last "
                "FROM snapshots GROUP BY base ORDER BY base"):
            lines.append(f"{r['base']}: срезов {r['slots']} ({r['n']} строк), "
                         f"{utc(r['first'])} … {utc(r['last'])} UTC")
        legs = conn.execute("SELECT COUNT(*) FROM leg_marks").fetchone()[0]
        tracked = conn.execute("SELECT COUNT(*) FROM tracked_symbols").fetchone()[0]
        lines = lines or ["данных нет"]
        lines.append(f"ног на слежении {tracked}, котировок ног {legs}")
        for r in conn.execute("SELECT base, COUNT(DISTINCT ts) slots, COUNT(*) n, MIN(ts) first, MAX(ts) last "
                              "FROM chain_snapshots GROUP BY base ORDER BY base"):
            lines.append(f"цепочка {r['base']}: срезов {r['slots']} ({r['n']} строк), "
                         f"{utc(r['first'])} … {utc(r['last'])} UTC")
        r = conn.execute("SELECT COUNT(*) n, MIN(ts) first, MAX(ts) last FROM perp_context").fetchone()
        if r["n"]:
            lines.append(f"фондирование и OI: {r['n']} строк, {utc(r['first'])} … {utc(r['last'])} UTC")
        r = conn.execute("SELECT COUNT(*) n, MIN(ts) first, MAX(ts) last FROM fear_greed").fetchone()
        if r["n"]:
            last = conn.execute("SELECT value, label FROM fear_greed ORDER BY ts DESC LIMIT 1").fetchone()
            lines.append(f"индекс страха и жадности: {r['n']} дн., {utc(r['first'])[:10]} … {utc(r['last'])[:10]}, "
                         f"последний {last['value']} ({last['label']})")
        return "\n".join(lines)
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    ap = argparse.ArgumentParser(prog="python -m optbot.collector", description="Сборщик рыночных данных")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--once", action="store_true", help="один проход")
    g.add_argument("--stats", action="store_true", help="что накоплено")
    a = ap.parse_args(argv)
    if a.stats:
        print(stats(settings.db_file))
        return 0
    col = Collector(OptionsClient(), settings)  # settings — текущий (подменяемый) объект модуля
    res = col.collect()
    res.fng = col.update_fear_greed()
    print(f"[{settings.env}] {settings.db_file}: {res}")
    return 1 if res.errors and not res.snapshots else 0


if __name__ == "__main__":
    sys.exit(main())
