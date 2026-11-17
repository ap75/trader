"""Сигналы (этап 3): рекомендация стратегии с ограниченным риском на 2–7 дней. Без автоторговли.

Метрики:
- IV7 — ATM IV постоянной дюрации SIG_TARGET_DAYS: линейная интерполяция полной дисперсии σ²·t между
  соседними экспирациями (короче суток не берутся — шумят);
- RV7 / RV14 — реализованная волатильность по закрытиям часовых свечей перпетуала BASEUSDT (годовая, без
  вычитания среднего; только доходности соседних свечей — пропуск не превращается в «часовой» скачок);
- IV percentile — место текущей IV7 в истории сборщика за SIG_LOOKBACK_DAYS; истории меньше
  SIG_MIN_HISTORY_DAYS — «мало истории», решение только по IV/RV;
- ожидаемое движение до экспирации S·IV·√t и цена ATM-стрэддла;
- тренд — EMA(fast/slow) по 4h-свечам: разрыв ≥ SIG_TREND_PCT и наклон медленной EMA за сутки в ту же сторону.

Выбор (только ограниченный риск):
    IV высокая, тренда нет      -> железный кондор
    IV высокая, тренд вверх/вниз -> кредитный пут-спред / колл-спред
    IV низкая, тренд вверх/вниз  -> дебетовый колл-спред / пут-спред
    IV низкая, тренда нет, IV7 < RV7 -> длинный стрэддл
    иначе                        -> «сигнала нет» с причиной
IV высокая: pctl ≥ SIG_PCTL_HIGH и IV7 > RV7·SIG_IV_RV_HIGH (мало истории — только второе).
IV низкая: pctl ≤ SIG_PCTL_LOW при IV7 ≤ RV7·SIG_IV_RV_HIGH, или IV7 < RV7 при pctl < SIG_PCTL_HIGH (IV ниже RV
при высоком pctl — скачок RV, а не дешёвая IV); мало истории — только IV7 < RV7.
Стрэддл — только если его цена (ask) не выше справедливой по RV7: S·RV7·√t·√(2/π) ≈ 0.8·S·RV7·√t.

Подбор: экспирации в окне SIG_DTE_MIN…SIG_DTE_MAX, от ближайшей к SIG_TARGET_DAYS; первая, где все ноги
ликвидны и премия разумна. Цены — по исполнимой стороне (покупка по ask, продажа по bid), mid — для сравнения.
Количество — минимальный лот инструмента.

Запуск: /signals BASE в боте, python -m optbot.cli signals BASE.
"""
from __future__ import annotations

import math
import threading
from decimal import Decimal
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import db
from .bybit_client import OptionsClient, group_by_strike, option_intrinsic, parse_option_symbol, spread_pct
from .collector import _f, expiry_metrics
from .config import Settings, SignalSettings, settings
from .fmt import DAY_MS, env_banner, money, num, pct, px, short_symbol, utc

MIN_INTERP_DTE = 1.0      # экспирации короче суток в IV7 не участвуют
KLINE_COVERAGE = 0.8      # доля ожидаемых свечей, без которой RV не считается
DELTA_TOL = 0.08          # замена неликвидного страйка соседним: |Δ| хуже лучшего не больше чем на столько
WING_TRIES = 3            # крыло: сколько страйков дальше пробовать, если ближайший неликвиден
_POOL = ThreadPoolExecutor(max_workers=6, thread_name_prefix="signals")  # общий: загрузка данных параллельно
STRADDLE_K = math.sqrt(2 / math.pi)  # ATM-стрэддл ≈ S·σ·√t·√(2/π)
HISTORY_STALE_MS = 6 * 3_600_000  # последний срез старше — история «устарела»
TREND_SLOPE_BARS = 6      # наклон медленной EMA: за 6 × 4h = сутки

STRATEGIES = {
    "iron_condor": "Железный кондор",
    "bull_put": "Бычий пут-спред (кредит)",
    "bear_call": "Медвежий колл-спред (кредит)",
    "bull_call": "Бычий колл-спред (дебет)",
    "bear_put": "Медвежий пут-спред (дебет)",
    "long_straddle": "Длинный стрэддл",
}
CREDIT = {"iron_condor", "bull_put", "bear_call"}
TREND_NAMES = {"up": "вверх", "down": "вниз", "flat": "нет"}


class NoTrade(ValueError):
    """Стратегию на этой экспирации не собрать (нет страйка, неликвидно, плохая премия)."""


# ---------- метрики ----------
def const_maturity_iv(points: list[tuple[float, float]], target_days: float,
                      min_dte: float = MIN_INTERP_DTE) -> float | None:
    """IV постоянной дюрации: [(дней, IV)] -> IV на target_days. Полная дисперсия σ²·t линейна по t
    между соседними экспирациями; за краями — IV крайней экспирации (без экстраполяции наклона)."""
    pts = sorted((t, iv) for t, iv in points if iv and iv > 0 and t >= min_dte)
    if not pts:
        return None
    if target_days <= pts[0][0]:
        return pts[0][1]
    if target_days >= pts[-1][0]:
        return pts[-1][1]
    for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
        if t0 <= target_days <= t1:
            if t1 == t0:
                return v0
            w0, w1 = v0 * v0 * t0, v1 * v1 * t1
            w = w0 + (w1 - w0) * (target_days - t0) / (t1 - t0)
            return math.sqrt(w / target_days) if w > 0 else None
    return None


def realized_vol_window(candles: list[tuple[int, float]], step_ms: int, window_ms: int,
                        coverage: float = KLINE_COVERAGE) -> float | None:
    """RV за окно по [(начало свечи, close)]: доходности только между соседними свечами (разница ровно step);
    доходностей меньше coverage от ожидаемого — None."""
    if not candles:
        return None
    since = candles[-1][0] - window_ms
    pts = [(t, c) for t, c in candles if t >= since and c > 0]
    r = [math.log(c1 / c0) for (t0, c0), (t1, c1) in zip(pts, pts[1:]) if t1 - t0 == step_ms]
    if len(r) < max(2, coverage * window_ms / step_ms):
        return None
    return math.sqrt(sum(x * x for x in r) / len(r) * (365 * DAY_MS / step_ms))


def ema(values: list[float], n: int) -> list[float]:
    """EMA с началом от SMA первых n значений; короче n — пусто."""
    if len(values) < n or n < 1:
        return []
    k = 2 / (n + 1)
    e = sum(values[:n]) / n
    out = [e]
    for v in values[n:]:
        e += k * (v - e)
        out.append(e)
    return out


@dataclass
class Trend:
    direction: str | None          # up | down | flat; None — мало свечей
    ema_fast: float | None = None
    ema_slow: float | None = None
    gap_pct: float | None = None   # EMAfast / EMAslow − 1, %
    slope_pct: float | None = None  # наклон медленной EMA за сутки, %


def trend_of(closes: list[float], fast: int, slow: int, threshold_pct: float) -> Trend:
    ef, es = ema(closes, fast), ema(closes, slow)
    if len(es) <= TREND_SLOPE_BARS or len(closes) < 2 * slow:  # без разгона EMA врёт
        return Trend(None)
    gap = (ef[-1] / es[-1] - 1) * 100
    slope = (es[-1] / es[-1 - TREND_SLOPE_BARS] - 1) * 100
    if gap >= threshold_pct and slope > 0:
        d = "up"
    elif gap <= -threshold_pct and slope < 0:
        d = "down"
    else:
        d = "flat"
    return Trend(d, ef[-1], es[-1], gap, slope)


def percentile(history: list[float], x: float) -> float | None:
    """Место x в истории, 0…100 (равные — пополам)."""
    if not history:
        return None
    below = sum(1 for h in history if h < x)
    equal = sum(1 for h in history if h == x)
    return 100 * (below + equal / 2) / len(history)


def iv7_history(path: str, base: str, since_ms: int, target_days: float) -> list[tuple[int, float]]:
    """IV7 по каждому срезу сборщика с since_ms: [(ts, IV7)] по возрастанию ts."""
    conn = db.connect(path)
    try:
        rows = conn.execute("SELECT ts, dte, atm_iv FROM snapshots WHERE base = ? AND ts >= ? ORDER BY ts",
                            (base, since_ms)).fetchall()
    finally:
        conn.close()
    by_ts: dict[int, list[tuple[float, float]]] = {}
    for r in rows:
        by_ts.setdefault(r["ts"], []).append((r["dte"], r["atm_iv"]))
    out = []
    for ts, pts in by_ts.items():
        v = const_maturity_iv(pts, target_days)
        if v is not None:
            out.append((ts, v))
    return out


REREAD_MS = 2 * 3_600_000  # кэш истории: хвост, перечитываемый каждый раз


class IV7History:
    """Кэш IV7 по срезам: прошлые срезы не меняются — из БД дочитываются только новые (ts > последнего).
    Срез актива сборщик пишет одной транзакцией — недописанного среза не бывает."""

    def __init__(self, path: str, target_days: float):
        self.path, self.target_days = path, target_days
        self._data: dict[str, list[tuple[int, float]]] = {}
        self._since: dict[str, int] = {}  # с какого момента загружено полностью
        self._lock = threading.Lock()  # бот считает сигналы в потоках (asyncio.to_thread)

    def get(self, base: str, since_ms: int) -> list[tuple[int, float]]:
        with self._lock:
            cur = [x for x in self._data.get(base, []) if x[0] >= since_ms]
            # пусто или окно начинается раньше загруженного (запрос с более ранним now) — полностью;
            # старые срезы, подлитые в БД задним числом, подхватятся после перезапуска бота
            if not cur or since_ms < self._since.get(base, since_ms):
                cur = iv7_history(self.path, base, since_ms, self.target_days)
                self._since[base] = since_ms
            else:
                # перечитать хвост с запасом: срез с более ранним ts мог записаться позже (ручной --once,
                # долгий проход) — инкремент «только ts > последнего» его потерял бы навсегда
                tail = cur[-1][0] - REREAD_MS
                cur = [x for x in cur if x[0] < tail] + iv7_history(self.path, base, max(tail, since_ms),
                                                                     self.target_days)
            self._data[base] = cur
            return list(cur)


# ---------- ноги и выплата ----------
@dataclass
class Leg:
    symbol: str
    kind: str          # C | P
    strike: float
    side: int          # +1 покупка, −1 продажа
    qty: float
    bid: float = 0.0
    ask: float = 0.0
    mark: float = 0.0
    iv: float | None = None
    delta: float = 0.0
    gamma: float = 0.0
    vega: float = 0.0
    theta: float = 0.0
    oi: float = 0.0

    @property
    def price(self) -> float:
        """Исполнимая сторона: покупка по ask, продажа по bid."""
        return self.ask if self.side > 0 else self.bid

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


def payoff(legs: list[Leg], s: float, prices: list[float] | None = None) -> float:
    """PnL позиции на экспирации при цене поставки s (с количеством), без комиссий."""
    prices = prices if prices is not None else [l.price for l in legs]
    return sum(l.side * l.qty * (option_intrinsic(l.kind, l.strike, s) - p) for l, p in zip(legs, prices))


@dataclass
class Profile:
    max_profit: float          # math.inf — без ограничения
    max_loss: float            # положительное число; math.inf — без ограничения
    breakevens: list[float]


def profile(legs: list[Leg], prices: list[float] | None = None) -> Profile:
    """Выплата кусочно-линейна с изломами на страйках: экстремумы — в 0, на страйках или на бесконечности."""
    ks = sorted({l.strike for l in legs})
    xs = [0.0] + ks
    vs = [payoff(legs, x, prices) for x in xs]
    slope = sum(l.side * l.qty for l in legs if l.kind == "C")  # наклон правее последнего страйка
    # точка правее всех изломов и правее пересечения хвоста с нулём (хвост: vs[-1] + slope·(x − K))
    far = xs[-1] + 1 + (2 * abs(vs[-1] / slope) if slope else 0)
    xs2, vs2 = xs + [far], vs + [vs[-1] + slope * (far - xs[-1])]
    eps = 1e-9 * max(1.0, max(abs(v) for v in vs2))
    sgn = [0 if abs(v) <= eps else (1 if v > 0 else -1) for v in vs2]
    bes: list[float] = []
    for i in range(len(xs2) - 1):
        if sgn[i] * sgn[i + 1] < 0:  # пересечение внутри отрезка
            bes.append(xs2[i] + (xs2[i + 1] - xs2[i]) * -vs2[i] / (vs2[i + 1] - vs2[i]))
    for i in range(1, len(xs2) - 1):  # ровно ноль на изломе — безубыток, только если знак меняется
        if sgn[i] == 0:
            left = next((x for x in reversed(sgn[:i]) if x), 0)
            right = next((x for x in sgn[i + 1:] if x), 0)
            if left * right < 0:
                bes.append(xs2[i])
    return Profile(
        max_profit=math.inf if slope > 1e-12 else max(vs),
        max_loss=math.inf if slope < -1e-12 else max(0.0, -min(vs)),
        breakevens=sorted(bes),
    )


# ---------- подбор ног ----------
Chain = dict[float, dict[str, dict]]  # страйк -> {"C": тикер, "P": тикер}


def _fz(x) -> float:
    """collector._f, но пусто/ноль -> 0.0 вместо None (греки и цены ноги — всегда числа)."""
    return _f(x) or 0.0


def make_leg(row: dict, side: int, qty: float) -> Leg:
    p = parse_option_symbol(row["symbol"])
    return Leg(symbol=row["symbol"], kind=p[3], strike=float(p[2]), side=side, qty=qty,
               bid=_fz(row.get("bid1Price")), ask=_fz(row.get("ask1Price")), mark=_fz(row.get("markPrice")),
               iv=_fz(row.get("markIv")) or None, delta=_fz(row.get("delta")), gamma=_fz(row.get("gamma")),
               vega=_fz(row.get("vega")), theta=_fz(row.get("theta")), oi=_fz(row.get("openInterest")))


def _first_ok(ranked: list[float], kind: str, chain: Chain, ok) -> float:
    """Первый ликвидный из упорядоченных по пригодности страйков; ликвидных нет — лучший (причину покажет
    leg_issue)."""
    if ok is not None:
        for k in ranked:
            if ok(chain[k][kind]):
                return k
    return ranked[0]


def by_delta(chain: Chain, kind: str, target: float, beyond: float | None = None, ok=None) -> float:
    """Страйк с |Δ|, ближайшей к target; beyond — колл строго выше / пут строго ниже этого страйка.
    ok(тикер) — ликвидность: неликвидный пропускается в пользу соседнего, пока |Δ| в пределах DELTA_TOL."""
    cands = []
    for k, v in chain.items():
        r = v.get(kind)
        if not r:
            continue
        d = abs(_fz(r.get("delta")))
        if not 0 < d < 1 or beyond is not None and (k <= beyond if kind == "C" else k >= beyond):
            continue
        cands.append((abs(d - target), k))
    if not cands:
        raise NoTrade(f"нет {'колла' if kind == 'C' else 'пута'} с |Δ|≈{target:g}")
    cands.sort()
    return _first_ok([k for dist, k in cands if dist <= cands[0][0] + DELTA_TOL], kind, chain, ok)


def wing(chain: Chain, kind: str, short_k: float, width: float, ok=None) -> float:
    """Длинное крыло: ближайший страйк не ближе width от короткого (колл — выше, пут — ниже);
    неликвидный — следующий дальше (до WING_TRIES страйков)."""
    if kind == "C":
        ks = sorted(k for k, v in chain.items() if kind in v and k > short_k and k >= short_k + width)
    else:
        ks = sorted((k for k, v in chain.items() if kind in v and k < short_k and k <= short_k - width), reverse=True)
    if not ks:
        raise NoTrade(f"нет страйка для крыла {'колла' if kind == 'C' else 'пута'} на ±{width:.4g} от {num(short_k)}")
    return _first_ok(ks[:WING_TRIES], kind, chain, ok)


def build_legs(strategy: str, chain: Chain, atm_strike: float, em: float, qty: float,
               sc: SignalSettings, forward: float | None = None) -> list[Leg]:
    """forward — проверять ликвидность при подборе страйков (без него — только по дельте/ширине)."""
    def leg(k: float, kind: str, side: int) -> Leg:
        return make_leg(chain[k][kind], side, qty)

    ok = None if forward is None else (lambda row: leg_issue(make_leg(row, 1, qty), forward, sc) is None)
    w = em * sc.wing_em
    if strategy in ("bull_put", "iron_condor"):
        sp = by_delta(chain, "P", sc.short_delta, ok=ok)
        put_legs = [leg(sp, "P", -1), leg(wing(chain, "P", sp, w, ok), "P", +1)]
    if strategy in ("bear_call", "iron_condor"):
        sc_k = by_delta(chain, "C", sc.short_delta, ok=ok)
        call_legs = [leg(sc_k, "C", -1), leg(wing(chain, "C", sc_k, w, ok), "C", +1)]
    if strategy == "bull_put":
        return put_legs
    if strategy == "bear_call":
        return call_legs
    if strategy == "iron_condor":
        if put_legs[0].strike >= call_legs[0].strike:
            raise NoTrade("короткие пут и колл кондора сошлись — слишком узкая цепочка")
        return put_legs + call_legs
    if strategy == "bull_call":
        lk = by_delta(chain, "C", sc.debit_long_delta, ok=ok)
        return [leg(lk, "C", +1), leg(by_delta(chain, "C", sc.debit_short_delta, beyond=lk, ok=ok), "C", -1)]
    if strategy == "bear_put":
        lk = by_delta(chain, "P", sc.debit_long_delta, ok=ok)
        return [leg(lk, "P", +1), leg(by_delta(chain, "P", sc.debit_short_delta, beyond=lk, ok=ok), "P", -1)]
    if strategy == "long_straddle":
        if "C" not in chain.get(atm_strike, {}) or "P" not in chain.get(atm_strike, {}):
            raise NoTrade("нет пары колл/пут на ATM-страйке")
        return [leg(atm_strike, "C", +1), leg(atm_strike, "P", +1)]
    raise ValueError(f"неизвестная стратегия {strategy}")


def leg_issue(l: Leg, forward: float, sc: SignalSettings) -> str | None:
    """Почему ногу не взять (или None): нет двусторонней котировки, широкий спред, мало OI."""
    name = short_symbol(l.symbol)
    if not (l.bid > 0 and l.ask > 0):  # так, а не «<= 0»: NaN тоже отсекается
        return f"{name}: нет bid/ask"
    if l.ask < l.bid:  # перекрёстная/устаревшая котировка: премия по ней неисполнима
        return f"{name}: ask {px(l.ask)} < bid {px(l.bid)}"
    sp = spread_pct(l.bid, l.ask)
    if not (sp is not None and sp <= sc.max_leg_spread_pct):
        return f"{name}: спред {sp:.0f}% > {sc.max_leg_spread_pct:g}%"
    if not l.oi * forward >= sc.min_oi_usd:
        return f"{name}: OI {l.oi * forward:,.0f} < {sc.min_oi_usd:,.0f} USDT"
    return None


def spread_width(legs: list[Leg]) -> float:
    """Наибольшая ширина вертикали (у кондора — худшая из двух сторон), на единицу актива."""
    out = 0.0
    for kind in ("C", "P"):
        ks = [l.strike for l in legs if l.kind == kind]
        if len(ks) == 2:
            out = max(out, abs(ks[0] - ks[1]))
    return out


def check_economics(strategy: str, legs: list[Leg], sc: SignalSettings, fair_rv: float | None = None) -> None:
    """fair_rv — справедливая цена ATM-стрэддла при волатильности RV7 (≈0.8·S·RV·√t): покупать дороже — платить
    больше, чем стоит фактическая волатильность."""
    net = sum(-l.side * l.price for l in legs)  # на единицу: > 0 — кредит
    width = spread_width(legs)
    if strategy in CREDIT:
        if net <= 0:
            raise NoTrade("кредита нет (по исполнимым ценам)")
        if width and net < sc.min_credit_ratio * width:
            raise NoTrade(f"кредит {px(net)} < {sc.min_credit_ratio:.0%} ширины {num(width)}")
    elif width:  # дебетовые вертикали
        if -net > sc.max_debit_ratio * width:
            raise NoTrade(f"дебет {px(-net)} > {sc.max_debit_ratio:.0%} ширины {num(width)}")
    elif strategy == "long_straddle" and fair_rv is not None and -net > fair_rv:
        raise NoTrade(f"стрэддл {px(-net)} дороже справедливой цены по RV7 {px(fair_rv)}")


# ---------- выбор стратегии ----------
@dataclass
class Metrics:
    base: str
    ts_ms: int
    iv7: float | None = None
    rv7: float | None = None
    rv14: float | None = None
    pctl: float | None = None
    history_days: float = 0.0
    history_n: int = 0
    history_last_ms: int | None = None
    history_stale: bool = False        # сборщик давно не писал — pctl по старым данным не используется
    enough_history: bool = False
    trend: Trend = field(default_factory=lambda: Trend(None))


def classify_iv(m: Metrics, sc: SignalSettings) -> tuple[str | None, list[str]]:
    """-> ('high' | 'low' | 'normal' | None, пояснения)."""
    if m.iv7 is None or m.rv7 is None or m.rv7 <= 0:
        return None, ["нет IV7 или RV7"]
    ratio = m.iv7 / m.rv7
    why = [f"IV7/RV7 = {m.iv7 * 100:.1f}/{m.rv7 * 100:.1f} = {ratio:.2f}"]
    if m.enough_history and m.pctl is not None:
        why.append(f"pctl {m.pctl:.0f}")
        if m.pctl >= sc.pctl_high and ratio > sc.iv_rv_high:
            return "high", why + [f"pctl ≥ {sc.pctl_high:g} и IV/RV > {sc.iv_rv_high:g}"]
        # IV < RV при высоком pctl — скачок RV, а не дешёвая IV; низкий pctl при IV ≫ RV — премия дорогая
        # относительно факта: в обоих случаях покупать премию нельзя
        if m.pctl <= sc.pctl_low and ratio <= sc.iv_rv_high or ratio < 1 and m.pctl < sc.pctl_high:
            return "low", why + [f"pctl ≤ {sc.pctl_low:g}" if m.pctl <= sc.pctl_low else "IV7 < RV7"]
    else:
        why.append("история устарела — только IV/RV" if m.history_stale else "мало истории — только IV/RV")
        if ratio > sc.iv_rv_high:
            return "high", why + [f"IV/RV > {sc.iv_rv_high:g}"]
        if ratio < 1:
            return "low", why + ["IV7 < RV7"]
    return "normal", why


def choose(m: Metrics, sc: SignalSettings,
           cls: tuple[str | None, list[str]] | None = None) -> tuple[str | None, list[str]]:
    """-> (стратегия или None, пояснения). cls — уже посчитанный classify_iv (чтобы не считать дважды)."""
    regime, why = cls if cls is not None else classify_iv(m, sc)
    why = list(why)
    if regime is None:  # причина — последней строкой (её карточка показывает как «Сигнала нет: …»)
        return None, why
    if regime == "normal":  # тренд ничего не изменит — не прятать причину за «тренд не определён»
        return None, why + ["IV в обычном диапазоне — преимущества нет"]
    t = m.trend
    if t.direction is None:
        return None, why + ["тренд не определён (мало свечей)"]
    why.append(f"тренд {TREND_NAMES[t.direction]} (EMA {sc.ema_fast}/{sc.ema_slow}: {t.gap_pct:+.2f}%, "
               f"наклон {t.slope_pct:+.2f}%/сут)")
    if regime == "high":
        return {"flat": "iron_condor", "up": "bull_put", "down": "bear_call"}[t.direction], why
    if regime == "low":
        if t.direction != "flat":
            return {"up": "bull_call", "down": "bear_put"}[t.direction], why
        if m.iv7 < m.rv7:
            return "long_straddle", why
        return None, why + ["IV низкая по истории, но не ниже RV — стрэддл не оправдан"]
    raise AssertionError(f"неизвестный режим IV: {regime}")


# ---------- сигнал ----------
@dataclass
class Signal:
    metrics: Metrics
    strategy: str | None
    reasons: list[str]
    no_signal: str | None = None       # почему не выдан (если strategy is None или не собрался)
    rejected: list[str] = field(default_factory=list)  # экспирации, где не собралось, с причинами
    regime: str | None = None          # high | low | normal | None — режим IV (для статистики, этап 4)
    expiry: str | None = None
    expiry_ms: int | None = None
    dte: float | None = None
    forward: float | None = None
    exp_iv: float | None = None
    em: float | None = None            # ожидаемое движение до экспирации, ±
    straddle: float | None = None
    qty: float | None = None
    legs: list[Leg] = field(default_factory=list)
    profile: Profile | None = None
    id: int | None = None              # номер в журнале сигналов (sigjournal), если записан
    journal_first_ms: int | None = None  # повторный показ: когда записан первый (цены журнала — оттуда)
    journal_net: float | None = None     # повторный показ: премия в журнале (от первого показа)

    @property
    def net(self) -> float:
        """Премия на всю позицию по исполнимым ценам: > 0 — кредит, < 0 — дебет."""
        return sum(-l.side * l.qty * l.price for l in self.legs)

    @property
    def net_mid(self) -> float:
        return sum(-l.side * l.qty * l.mid for l in self.legs)

    def greek(self, name: str) -> float:
        return sum(l.side * l.qty * getattr(l, name) for l in self.legs)


class SignalEngine:
    def __init__(self, client: OptionsClient, cfg: Settings = settings):
        self.c = client
        self.cfg = cfg
        self.history = IV7History(cfg.db_file, cfg.sig.target_days)
        self._reloaded: dict[str, float] = {}  # актив -> когда справочник перечитан из-за нового символа

    def _fetch(self, base: str) -> tuple[list[dict], dict, list, list]:
        """Тикеры, справочник, 1h- и 4h-свечи — параллельно (независимые запросы)."""
        sc = self.cfg.sig
        pool = _POOL
        f_rows = pool.submit(self.c.tickers, base)
        f_h1 = pool.submit(self.c.perp_closes, base, 60, 14 * 24 + 1)
        f_h4 = pool.submit(self.c.perp_closes, base, 240, min(max(200, 3 * sc.ema_slow), 999))
        insts = {i.symbol: i for i in self.c.load_instruments(base)}
        rows = f_rows.result()
        # новые страйки/экспирация появились, а справочник ещё в кэше — перечитать, но не чаще его TTL:
        # не-Trading (поставка, предлистинг) в справочник не попадут никогда — иначе перечитывали бы каждый раз
        now = time.monotonic()
        if (now - self._reloaded.get(base, -1e9) > self.c.INSTRUMENTS_TTL
                and any(r.get("symbol") not in insts and parse_option_symbol(r.get("symbol", "")) for r in rows)):
            insts = {i.symbol: i for i in self.c.load_instruments(base, force=True)}
            self._reloaded[base] = now  # после успеха: сбой перечитывания не блокирует повтор на TTL
        return rows, insts, f_h1.result(), f_h4.result()

    def check_base(self, base: str) -> str:
        base = base.upper()
        if base not in self.cfg.base_coins:
            raise ValueError(f"Базовый актив {base} не в списке BASE_COINS")
        return base

    def compute(self, base: str, now_ms: int | None = None) -> Signal:
        base = self.check_base(base)
        sc = self.cfg.sig
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)

        rows, insts, h1, h4c = self._fetch(base)
        exp_ms = {i.expiry_code: i.expiry_ms for i in insts.values()}
        by_exp: dict[str, list[dict]] = {}
        for r in rows:
            p = parse_option_symbol(r.get("symbol", ""))
            if p and p[1] in exp_ms and r["symbol"] in insts:
                by_exp.setdefault(p[1], []).append(r)
        exp_m = {e: m for e, rs in by_exp.items() if (m := expiry_metrics(rs, now_ms, exp_ms[e]))}

        m = Metrics(base=base, ts_ms=now_ms)
        m.iv7 = const_maturity_iv([(x["dte"], x["atm_iv"]) for x in exp_m.values()], sc.target_days)

        m.rv7 = realized_vol_window(h1, 3_600_000, 7 * DAY_MS)
        m.rv14 = realized_vol_window(h1, 3_600_000, 14 * DAY_MS)
        m.trend = trend_of([c for _, c in h4c], sc.ema_fast, sc.ema_slow, sc.trend_pct)

        hist = self.history.get(base, now_ms - sc.lookback_days * DAY_MS)
        if hist:
            m.history_n = len(hist)
            m.history_last_ms = hist[-1][0]
            # не размах, а покрытие — часы, в которые были срезы: пропуски в сборе не выдают себя за историю,
            # а смена COLLECTOR_INTERVAL_MIN и ручные срезы не искажают счёт
            m.history_days = len({ts // 3_600_000 for ts, _ in hist}) / 24
            m.history_stale = now_ms - m.history_last_ms > HISTORY_STALE_MS
            m.enough_history = m.history_days >= sc.min_history_days and not m.history_stale
            if m.iv7 is not None:
                m.pctl = percentile([v for _, v in hist], m.iv7)

        cls = classify_iv(m, sc)
        strategy, why = choose(m, sc, cls)
        sig = Signal(metrics=m, strategy=strategy, reasons=why, regime=cls[0])
        if strategy is None:
            sig.no_signal = why[-1]
            return sig

        cands = sorted((e for e, x in exp_m.items() if sc.dte_min <= x["dte"] <= sc.dte_max),
                       key=lambda e: abs(exp_m[e]["dte"] - sc.target_days))
        if not cands:
            sig.no_signal = f"нет экспирации в окне {sc.dte_min:g}–{sc.dte_max:g} дн."
            return sig
        for e in cands:
            x = exp_m[e]
            if not x["atm_iv"]:
                sig.rejected.append(f"{e}: нет ATM IV")
                continue
            chain: Chain = {float(k): v for k, v in group_by_strike(by_exp[e]).items()}
            em = x["underlying"] * x["atm_iv"] * math.sqrt(x["dte"] / 365)
            try:
                legs = build_legs(strategy, chain, x["atm_strike"], em, 1.0, sc, x["underlying"])
                qty = max(float(insts[l.symbol].min_qty) for l in legs)  # минимальный лот, общий для всех ног
                q = Decimal(str(qty))
                if any(insts[l.symbol].qty_step and q % insts[l.symbol].qty_step for l in legs):
                    raise NoTrade(f"лот {num(qty)} не кратен шагу количества одной из ног")
                for l in legs:
                    l.qty = qty
                issues = [i for l in legs if (i := leg_issue(l, x["underlying"], sc))]
                if issues:
                    raise NoTrade("; ".join(issues))
                fair_rv = x["underlying"] * m.rv7 * math.sqrt(x["dte"] / 365) * STRADDLE_K if m.rv7 else None
                check_economics(strategy, legs, sc, fair_rv)
            except NoTrade as err:
                sig.rejected.append(f"{e}: {err}")
                continue
            sig.expiry, sig.dte, sig.forward, sig.exp_iv = e, x["dte"], x["underlying"], x["atm_iv"]
            sig.expiry_ms = exp_ms[e]
            sig.em, sig.straddle, sig.qty, sig.legs = em, x["straddle_mark"], qty, legs
            sig.profile = profile(legs)
            return sig
        sig.no_signal = "ни на одной экспирации окна стратегия не собралась"
        return sig


# ---------- карточка ----------
def _usd(x: float) -> str:
    return "без ограничения" if math.isinf(x) else f"{money(x)} USDT"


def fmt_signal(sig: Signal, env: str, head: str = "") -> str:
    """head — строки сразу под баннером окружения (например, «рекомендация сменилась»)."""
    m = sig.metrics
    t = m.trend
    lines = [env_banner(env), *([head] if head else []), f"📊 {m.base} — сигнал на {utc(m.ts_ms)} UTC"]
    if sig.strategy and sig.legs:
        num_ = f"#{sig.id} " if sig.id else ""
        lines.append(f"{num_}{STRATEGIES[sig.strategy]} · {sig.expiry} ({sig.dte:.1f} дн.)")
        # повтор — запись старше этого расчёта или с другими ценами (свой же показ из кэша — не повтор)
        if sig.id and sig.journal_first_ms and (
                sig.journal_first_ms < m.ts_ms
                or (sig.journal_net is not None and abs(sig.journal_net - sig.net) > 1e-9)):
            lines.append(f"Повтор #{sig.id}: в журнале — цены первого показа {utc(sig.journal_first_ms)} UTC")
    elif sig.strategy:
        lines.append(f"Подходит: {STRATEGIES[sig.strategy]} — но не собрать: {sig.no_signal}")
    else:
        lines.append(f"Сигнала нет: {sig.no_signal}")

    if sig.legs:
        lines.append(f"\nНоги (кол-во {num(sig.qty)}):")
        for l in sig.legs:
            act = "BUY " if l.side > 0 else "SELL"
            lines.append(f" {act} {short_symbol(l.symbol):<22} {px(l.price):>6}  mid {px(l.mid)}  Δ{l.delta:+.2f}")
        net, mid = sig.net, sig.net_mid
        kind = "Кредит" if net > 0 else "Дебет"
        lines.append(f"{kind}: {abs(net):,.2f} USDT (по mid {abs(mid):,.2f})")
        p = sig.profile
        lines.append(f"Макс. прибыль: {_usd(p.max_profit)}")
        lines.append(f"Макс. риск:    {_usd(p.max_loss)}")
        lines.append("Безубыток: " + (" / ".join(num(round(b, 2)) for b in p.breakevens) or "-"))
        lines.append(f"Греки: Δ {sig.greek('delta'):+.4f} Γ {sig.greek('gamma'):+.2g} "
                     f"Vega {sig.greek('vega'):+.2f} Θ {sig.greek('theta'):+.2f}/сут")
        lines.append(f"Ожидаемое движение до {sig.expiry}: ±{px(sig.em)} "
                     f"({sig.em / sig.forward * 100:.1f}%) · стрэддл {px(sig.straddle or 0)} · F {px(sig.forward)}")
    for r in sig.rejected:
        lines.append(f"  ✗ {r}")

    def pc(x):
        return pct(x) + ("%" if x else "")

    lines.append("\nМетрики:")
    lines.append(f" IV7 {pc(m.iv7)} · RV7 {pc(m.rv7)} · RV14 {pc(m.rv14)}")
    if m.pctl is None:
        hist = "истории нет"
    else:
        hist = f"pctl {m.pctl:.0f}"
    if m.history_stale:
        flag = f" — история устарела (последний срез {utc(m.history_last_ms)} UTC)"
    else:
        flag = "" if m.enough_history else " — мало истории"
    lines.append(f" {hist} ({m.history_days:.1f} дн., {m.history_n} срезов){flag}")
    if t.direction:
        lines.append(f" тренд 4h: {TREND_NAMES[t.direction]} · EMA {num(round(t.ema_fast))}/{num(round(t.ema_slow))}")
    lines.append("Почему: " + "; ".join(sig.reasons))
    lines.append("\nЦены — по исполнимой стороне (покупка ask, продажа bid), без комиссий. "
                 "Не торговый совет: решение за трейдером.")
    return "\n".join(lines)


def signal_key(sig: Signal) -> str | None:
    """Что рекомендовано: стратегия, если сделка собралась; иначе None («сигнала нет»)."""
    return sig.strategy if sig.strategy and sig.legs else None


def strategy_name(key: str | None) -> str:
    return STRATEGIES[key] if key else "сигнала нет"


def fmt_signal_brief(sig: Signal) -> str:
    """Несколько строк для сводки по активам: стратегия, ноги, премия и риск — или почему сигнала нет."""
    base = sig.metrics.base
    if not signal_key(sig):
        if sig.strategy:
            return f"{base}: подходит {STRATEGIES[sig.strategy]}, но не собрать — {sig.no_signal}"
        return f"{base}: сигнала нет — {sig.no_signal}"
    legs = [f"  {'BUY ' if l.side > 0 else 'SELL'} {short_symbol(l.symbol).removeprefix(base + '-'):<16} {px(l.price):>7}"
            for l in sig.legs]
    net, p = sig.net, sig.profile
    profit = "прибыль без ограничения" if math.isinf(p.max_profit) else f"прибыль до {_usd(p.max_profit)}"
    return "\n".join([f"{base}: {STRATEGIES[sig.strategy]} · {sig.expiry} ({sig.dte:.1f} дн.), кол-во {num(sig.qty)}",
                      *legs,
                      f"  {'Кредит' if net > 0 else 'Дебет'} {_usd(abs(net))} · {profit} · риск {_usd(p.max_loss)}"])
