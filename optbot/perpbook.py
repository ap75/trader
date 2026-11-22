"""Бумажная книга перпетуалов: EMA 4h лонг/шорт (research, этап 3) по BTC/ETH/SOL — только бумага, ордеров нет.

Параметры — копия из research (config/perp_ema.json, без подбора): EMA 10/200 по закрытиям 4h-свечей перпетуала
BASEUSDT; быстрая выше медленной — лонг, ниже — шорт (всегда в рынке, как в research). Как движок research
(rlab/backtest.py):
- решение — по закрытию свечи i−1, исполнение — по open свечи i с проскальзыванием (покупка дороже, продажа дешевле);
- размер: риск 1% капитала / (3·ATR20), не больше 2× капитала по цене входа; смена знака — закрыть и открыть;
- комиссия тейкера на каждую сторону; фондирование — по факту (история Bybit), по цене open свечи, в которую
  попала метка; метка ровно на open свечи — до смены позиции (платит прежняя);
- капитал — у каждой монеты своя книга, условно 10 000 USDT (как у research: отдельный бэктест на монету).

Шаг — после шага опционных книг (бот: после каждого среза сборщика) и `--step`. Пропущенные свечи (бот лежал)
проходятся по порядку по историческим open — итог тот же, что при работе без перерыва. Новая книга начинает с
ближайшей следующей свечи (без входа по уже прошедшему open).

Таблицы (миграция 11): perp_books (состояние), perp_trades (сделки; R = PnL / риск при входе), perp_equity
(капитал на каждом шаге — для дневных итогов и корреляции с опционными книгами).

    python -m optbot.perpbook --step | --status | --stats 30d
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import statistics
import sys
import threading
import time
from dataclasses import dataclass, field

from . import db
from .config import Settings, settings
from .fmt import DAY_MS

log = logging.getLogger("optbot.perpbook")

PARAMS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "perp_ema.json")
BARS = 1000                 # 4h-свечей на шаг: EMA200 прогрета (вес начала < 0.1%), ATR20 — тем более
MIN_CORR_DAYS = 5           # корреляция дневных итогов — не меньше стольких общих дней
FUNDING_PAGE = 200
WARMUP_BARS = 600           # первая исполняемая свеча — не раньше 600-й загруженной (EMA200: 3 периода прогрева)
BAR_LAG_MS = 10 * 60_000    # свеча исполняется не раньше чем через 10 мин после open: фондирование с меткой на этом
                            # open уже в истории биржи и платится прежней позицией (а не новой — как в research)


@dataclass(frozen=True)
class Params:
    strategy: str
    interval_min: int
    fast: int
    slow: int
    atr_period: int
    risk_atr_mult: float
    risk_per_trade: float
    max_leverage: float
    capital_usdt: float
    taker_fee: float
    slippage: float


def load_params(path: str | None = None) -> Params:
    with open(path or PARAMS_FILE, encoding="utf-8") as f:
        raw = json.load(f)
    return Params(**{k: raw[k] for k in Params.__dataclass_fields__})


def book_id(p: Params, base: str) -> str:
    return f"perp:{p.strategy}:{base.upper()}"


# ---------- индикаторы (как pandas ewm(adjust=False, min_periods) в research) ----------
def ewm(values: list[float], alpha: float, min_periods: int) -> list[float | None]:
    out: list[float | None] = []
    e = None
    for i, v in enumerate(values):
        e = v if e is None else e + alpha * (v - e)
        out.append(e if i + 1 >= min_periods else None)
    return out


def ema(closes: list[float], n: int) -> list[float | None]:
    return ewm(closes, 2 / (n + 1), n)


def atr(h: list[float], l: list[float], c: list[float], n: int) -> list[float | None]:
    """ATR Уайлдера: true range (первый — high − low), сглаживание alpha = 1/n, первые n−1 — None."""
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]
    return ewm(tr, 1 / n, n)


def targets(bars: list[tuple], p: Params) -> tuple[list[float | None], list[float | None]]:
    """По закрытым свечам [(t, o, h, l, c)] -> (знак EMAfast − EMAslow или None, расстояние риска 3·ATR или None)."""
    c = [b[4] for b in bars]
    ef, es = ema(c, p.fast), ema(c, p.slow)
    a = atr([b[2] for b in bars], [b[3] for b in bars], c, p.atr_period)
    tgt = [None if f is None or s is None else float((f > s) - (f < s)) for f, s in zip(ef, es)]
    return tgt, [None if x is None else p.risk_atr_mult * x for x in a]


@dataclass
class StepResult:
    ts: int
    opened: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    funding: int = 0
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        parts = [f"открыто {len(self.opened)}", f"закрыто {len(self.closed)}", f"фондирований {self.funding}"]
        return "перпетуалы: " + ", ".join(parts) + (f", ошибки: {'; '.join(self.errors)}" if self.errors else "")


class PerpBook:
    def __init__(self, client, cfg: Settings = settings, params: Params | None = None):
        self.c = client
        self.cfg = cfg
        self._lock = threading.Lock()
        self.p_error = None
        try:  # нет или кривой config/perp_ema.json — без книги перпетуалов, но бот работает
            self.p = params or load_params()
        except (OSError, KeyError, TypeError, ValueError) as e:
            self.p, self.p_error = None, f"параметры {PARAMS_FILE} не прочитаны: {e}"
            log.error("Перпетуалы: %s — книга выключена", self.p_error)

    def _db(self):
        return db.connect(self.cfg.db_file)

    def bases(self) -> list[str]:
        return [b for b in self.cfg.paper.bases if b in self.cfg.base_coins]

    # ---------- шаг ----------
    def step(self, now_ms: int | None = None) -> StepResult:
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        res = StepResult(now_ms)
        if not self.cfg.paper.enabled:
            return res
        if self.p is None:
            res.errors.append(self.p_error)
            return res
        with self._lock:
            conn = self._db()
            try:
                for base in self.bases():
                    try:
                        self._step_base(conn, base, now_ms, res)
                    except Exception as e:
                        log.exception("Перпетуалы: шаг %s не выполнен", base)
                        res.errors.append(f"{base}: {e}")
            finally:
                conn.close()
        return res

    def _funding(self, base: str, after_ms: int, now_ms: int) -> list[tuple[int, float]]:
        """Ставки фондирования с меткой в (after_ms, now_ms] — по возрастанию (листаем назад по endTime)."""
        out: dict[int, float] = {}
        if after_ms >= now_ms:
            return []
        end = now_ms
        for _ in range(50):
            rows = self.c.perp_funding_history(base, after_ms + 1, end, FUNDING_PAGE)
            for t, r in rows:
                if after_ms < t <= now_ms:
                    out[t] = r
            if len(rows) < FUNDING_PAGE:
                break
            oldest = min(t for t, _ in rows)
            if oldest <= after_ms + 1:
                break
            end = oldest - 1
        return sorted(out.items())

    def _step_base(self, conn, base: str, now_ms: int, res: StepResult) -> None:
        p = self.p
        step_ms = p.interval_min * 60_000
        bid = book_id(p, base)
        bars = self.c.perp_ohlc(base, p.interval_min, BARS)          # от старых к новым, последняя — текущая
        bars = [b for b in bars if b[0] <= now_ms]
        if len(bars) < 2:
            raise RuntimeError("нет свечей")
        pre = conn.execute("SELECT last_funding_ms FROM perp_books WHERE id = ?", (bid,)).fetchone()
        # сеть — до блокировки записи; под блокировкой берётся только то, что новее состояния книги
        funding = self._funding(base, pre["last_funding_ms"], now_ms) if pre else []
        closed = [b for b in bars if b[0] + step_ms <= now_ms]
        tgt, dist = targets(closed, p)
        by_t = {b[0]: i for i, b in enumerate(bars)}
        opened, closed_msgs = [], []
        with conn:
            # ручной --step одновременно с ботом: состояние книги — читать под блокировкой записи
            conn.execute("BEGIN IMMEDIATE")
            book = conn.execute("SELECT * FROM perp_books WHERE id = ?", (bid,)).fetchone()
            if book is None:  # новая книга: первое исполнение — по open следующей свечи
                conn.execute("INSERT INTO perp_books (id, base, strategy, capital, cash, qty, entry, last_bar_ms, "
                             "last_funding_ms, created_ms, params) VALUES (?,?,?,?,?,0,0,?,?,?,?)",
                             (bid, base, p.strategy, p.capital_usdt, p.capital_usdt, bars[-1][0], now_ms, now_ms,
                              json.dumps(p.__dict__)))
                book = conn.execute("SELECT * FROM perp_books WHERE id = ?", (bid,)).fetchone()
            st = dict(cash=book["cash"], qty=book["qty"], entry=book["entry"], last_bar=book["last_bar_ms"],
                      last_f=book["last_funding_ms"])
            trade = conn.execute("SELECT * FROM perp_trades WHERE book = ? AND status = 'open'", (bid,)).fetchone()
            trade = dict(trade) if trade else None
            events = [(b[0], 1, "bar", b) for b in bars if b[0] > st["last_bar"] and b[0] + BAR_LAG_MS <= now_ms]
            if events and by_t[events[0][0]] < WARMUP_BARS:
                raise RuntimeError(f"перерыв длиннее {(len(bars) - WARMUP_BARS) * step_ms // DAY_MS} дн.: EMA "
                                   f"{p.slow} не прогреть по {BARS} свечам — книгу {bid} начать заново "
                                   "(удалить строку perp_books)")
            last_t = max([e[0] for e in events], default=st["last_bar"])
            # фондирование — до последней исполняемой свечи (позже — следующим шагом, по порядку); метка на open
            # свечи — до исполнения на этом open (как в research)
            events += [(t, 0, "fund", r) for t, r in funding if t > st["last_f"] and t < last_t + step_ms]
            events.sort(key=lambda e: (e[0], e[1]))
            for t, _, kind, x in events:
                if kind == "fund":
                    price = bars[max(0, _bar_at(bars, t))][1]
                    if t <= st["last_bar"]:
                        # метка опоздала: свеча на этом open уже исполнена — платит позиция, державшаяся в t
                        cash0 = st["cash"]
                        trade = self._late_funding(conn, bid, st, trade, t, x, price)
                        res.funding += st["cash"] != cash0
                    elif st["qty"]:
                        amt = -st["qty"] * price * x                   # лонг платит при положительной ставке
                        st["cash"] += amt
                        trade["funding"] += amt
                        trade["pnl"] += amt
                        res.funding += 1
                    st["last_f"] = max(st["last_f"], t)
                    continue
                k = by_t[t]
                g = k - 1                                                # решение по закрытию предыдущей свечи
                want = tgt[g] if 0 <= g < len(tgt) else None
                o = x[1]
                if want is not None and want != _sign(st["qty"]):
                    if st["qty"]:
                        trade = self._close(conn, st, trade, o, t, "signal", closed_msgs)
                    if want:
                        d = dist[g]
                        fill = o * (1 + p.slippage * want)
                        size = p.risk_per_trade * st["cash"] / d if d and d > 0 else 0.0
                        size = min(size, p.max_leverage * st["cash"] / fill)
                        if size > 0 and st["cash"] > 0:
                            fees = size * fill * p.taker_fee
                            st["cash"] -= fees
                            st["qty"], st["entry"] = want * size, fill
                            risk = size * d
                            cur = conn.execute(
                                "INSERT INTO perp_trades (book, base, side, status, opened_ms, entry_px, qty, risk, "
                                "fees, funding, pnl) VALUES (?,?,?,'open',?,?,?,?,?,0,?)",
                                (bid, base, int(want), t, fill, size, risk, fees, -fees))
                            trade = dict(id=cur.lastrowid, fees=fees, funding=0.0, pnl=-fees, risk=risk, qty=size)
                            opened.append(f"{bid} {'лонг' if want > 0 else 'шорт'} {size:.4g} по {fill:,.2f}")
                st["last_bar"] = t
            if trade:
                conn.execute("UPDATE perp_trades SET fees = ?, funding = ?, pnl = ? WHERE id = ?",
                             (trade["fees"], trade["funding"], trade["pnl"], trade["id"]))
            price = bars[-1][4]                                           # последняя цена (текущая свеча)
            equity = st["cash"] + st["qty"] * (price - st["entry"])
            conn.execute("UPDATE perp_books SET cash = ?, qty = ?, entry = ?, last_bar_ms = ?, last_funding_ms = ? "
                         "WHERE id = ?", (st["cash"], st["qty"], st["entry"], st["last_bar"], st["last_f"], bid))
            conn.execute("INSERT OR REPLACE INTO perp_equity (ts, book, equity, cash, qty, price) VALUES (?,?,?,?,?,?)",
                         (now_ms, bid, equity, st["cash"], st["qty"], price))
        res.opened += opened            # после фиксации: откат шага не оставит в итоге несуществующих сделок
        res.closed += closed_msgs

    @staticmethod
    def _late_funding(conn, bid: str, st: dict, trade: dict | None, t: int, rate: float, price: float):
        """Фондирование с меткой t, пришедшее после исполнения свечи на t: на сделку, открытую до t и не закрытую
        раньше t (свеча на open t сначала платит, потом меняет позицию)."""
        row = conn.execute("SELECT * FROM perp_trades WHERE book = ? AND opened_ms < ? AND (closed_ms IS NULL OR "
                           "closed_ms >= ?) ORDER BY opened_ms DESC LIMIT 1", (bid, t, t)).fetchone()
        if row is None:
            return trade
        amt = -row["side"] * row["qty"] * price * rate
        st["cash"] += amt
        if trade is not None and trade["id"] == row["id"]:
            trade["funding"] += amt
            trade["pnl"] += amt
            return trade
        pnl = row["pnl"] + amt
        conn.execute("UPDATE perp_trades SET funding = funding + ?, pnl = ?, r_mult = ? WHERE id = ?",
                     (amt, pnl, pnl / row["risk"] if row["risk"] else None, row["id"]))
        return trade

    def _close(self, conn, st: dict, trade: dict | None, px_raw: float, t: int, reason: str, msgs: list[str]):
        p = self.p
        side = _sign(st["qty"])
        fill = px_raw * (1 - p.slippage * side)
        fees = abs(st["qty"]) * fill * p.taker_fee
        pnl = st["qty"] * (fill - st["entry"])
        st["cash"] += pnl - fees
        if trade is not None:
            total = trade["pnl"] + pnl - fees
            conn.execute("UPDATE perp_trades SET status = 'closed', closed_ms = ?, exit_px = ?, fees = ?, funding = ?, "
                         "pnl = ?, r_mult = ?, reason = ? WHERE id = ?",
                         (t, fill, trade["fees"] + fees, trade["funding"], total,
                          total / trade["risk"] if trade["risk"] else None, reason, trade["id"]))
            msgs.append(f"#{trade['id']} {total:+.2f} USDT")
        st["qty"], st["entry"] = 0.0, 0.0
        return None

    # ---------- отчёты ----------
    def status(self) -> str:
        conn = self._db()
        try:
            books = conn.execute("SELECT * FROM perp_books ORDER BY id").fetchall()
            if self.p is None:
                return f"Книга перпетуалов выключена: {self.p_error}"
            if not books:
                return "Книги перпетуалов ещё не начаты (первый шаг — после среза сборщика)"
            lines = [f"Книги перпетуалов ({self.p.strategy}: EMA {self.p.fast}/{self.p.slow} 4h, риск "
                     f"{self.p.risk_per_trade:.0%}, капитал {self.p.capital_usdt:,.0f} USDT на монету):"]
            for b in books:
                eq = conn.execute("SELECT equity, price FROM perp_equity WHERE book = ? ORDER BY ts DESC LIMIT 1",
                                  (b["id"],)).fetchone()
                pos = ("вне рынка" if not b["qty"] else
                       f"{'лонг' if b['qty'] > 0 else 'шорт'} {abs(b['qty']):.4g} от {b['entry']:,.2f}")
                e = f", капитал {eq['equity']:,.2f} ({eq['equity'] / b['capital'] - 1:+.2%})" if eq else ""
                lines.append(f" {b['id']}: {pos}{e}")
            return "\n".join(lines)
        finally:
            conn.close()

    def closed(self, start_ms: int, end_ms: int) -> list[dict]:
        conn = self._db()
        try:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM perp_trades WHERE status = 'closed' AND closed_ms >= ? AND closed_ms < ? ORDER BY id",
                (start_ms, end_ms))]
        finally:
            conn.close()

    def correlations(self, start_ms: int, end_ms: int) -> dict[str, dict[str, tuple[float | None, int]]]:
        """{опционная книга: {книга перпетуалов: (корреляция дневных итогов, общих дней)}}."""
        conn = self._db()
        try:
            perp = daily_pnl(conn.execute("SELECT ts, book, equity v FROM perp_equity WHERE ts < ? ORDER BY ts",
                                          (end_ms,)).fetchall(), start_ms)
            opt = daily_pnl(conn.execute("SELECT ts, book, realized + unrealized v FROM book_equity WHERE ts < ? "
                                         "ORDER BY ts", (end_ms,)).fetchall(), start_ms)
        finally:
            conn.close()
        return {ob: {pb: corr(oser, pser) for pb, pser in perp.items()} for ob, oser in opt.items()}


def _sign(x: float) -> float:
    return float((x > 0) - (x < 0))


def _bar_at(bars: list[tuple], t: int) -> int:
    """Индекс свечи, в которую попадает метка t (последняя с началом ≤ t)."""
    lo, hi = 0, len(bars) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if bars[mid][0] <= t:
            lo = mid
        else:
            hi = mid - 1
    return lo if bars[lo][0] <= t else -1


def daily_pnl(rows, start_ms: int) -> dict[str, dict[int, float]]:
    """[(ts, book, значение)] -> {книга: {день UTC: изменение за день}} — по последнему значению каждого дня;
    первый день книги (нет конца предыдущего) не считается."""
    last: dict[str, dict[int, float]] = {}
    for r in rows:
        last.setdefault(r["book"], {})[r["ts"] // DAY_MS * DAY_MS] = r["v"]
    out: dict[str, dict[int, float]] = {}
    for book, days in last.items():
        ds = sorted(days)
        out[book] = {d: days[d] - days[p] for p, d in zip(ds, ds[1:]) if d - p == DAY_MS and d >= start_ms}
    return out


def corr(a: dict[int, float], b: dict[int, float]) -> tuple[float | None, int]:
    days = sorted(set(a) & set(b))
    if len(days) < MIN_CORR_DAYS:
        return None, len(days)
    x, y = [a[d] for d in days], [b[d] for d in days]
    try:
        return statistics.correlation(x, y), len(days)
    except statistics.StatisticsError:   # постоянный ряд (книга без открытых сделок)
        return None, len(days)


def fmt_stats(rows: list[dict], label: str, corrs: dict | None = None) -> str:
    """Те же метрики, что у опционных книг (сделки, прибыльные, PnL, ΣR, ср. R, худшая) + корреляция."""
    from .paper import summarize
    lines = [f"Книги перпетуалов (EMA 4h лонг/шорт, бумага), {label}: закрытых {len(rows)}"]
    for book in sorted({r["book"] for r in rows}):
        part = [r for r in rows if r["book"] == book]
        s = summarize(part)
        avg = "-" if s["avg_r"] is None else f"{s['avg_r']:+.2f}"
        worst = "-" if s["worst"] is None else f"{s['worst']:+.2f}"
        fund = sum(r["funding"] or 0 for r in part)
        fees = sum(r["fees"] or 0 for r in part)
        lines.append(f" {book}: {s['n']} сд., прибыльных {s['wins']}, PnL {s['pnl']:+.2f}, ΣR {s['sum_r']:+.2f}, "
                     f"ср. R {avg}, худшая {worst} (комиссии {fees:.2f}, фондирование {fund:+.2f})")
    if corrs:
        perps = sorted({pb for v in corrs.values() for pb in v})
        shown = [(ob, v) for ob, v in sorted(corrs.items()) if any(c is not None for c, _ in v.values())]
        lines.append("Корреляция дневных итогов с опционными книгами (" + " / ".join(p.split(":")[-1] for p in perps)
                     + f"; не меньше {MIN_CORR_DAYS} общих дней):")
        if not shown:
            lines.append(" пока мало общих дней")
        for ob, v in shown:
            cells = [("-" if v.get(pb, (None, 0))[0] is None else f"{v[pb][0]:+.2f}") for pb in perps]
            n = max(x[1] for x in v.values())
            lines.append(f" {ob}: " + " / ".join(cells) + f" ({n} дн.)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    ap = argparse.ArgumentParser(prog="python -m optbot.perpbook", description="Бумажная книга перпетуалов (EMA 4h)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--step", action="store_true", help="шаг: пройти новые 4h-свечи и фондирование")
    g.add_argument("--status", action="store_true", help="позиции и капитал")
    g.add_argument("--stats", nargs="?", const="30d", metavar="ПЕРИОД", help="итоги закрытых и корреляция")
    a = ap.parse_args(argv)
    from .bybit_client import OptionsClient
    from .journal import parse_period
    pb = PerpBook(OptionsClient(settings), settings)
    if a.status:
        print(pb.status())
        return 0
    if a.stats:
        start, end, label = parse_period([a.stats])
        print(fmt_stats(pb.closed(start, end), label, pb.correlations(start, end)))
        return 0
    res = pb.step()
    print(f"[{settings.env}] {res}")
    for x in res.opened + res.closed:
        print(" ", x)
    return 1 if res.errors else 0


if __name__ == "__main__":
    sys.exit(main())
