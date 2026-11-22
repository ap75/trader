"""Бумажная книга базиса: спот в лонг + перпетуал в шорт на одинаковое количество (research, доработка базиса).

Повторяет `research/rlab/backtest.run_basis` для опорного варианта (`config/basis.json`, копия из research, без
подбора): всегда в позиции, номинал 1x капитала, дневные свечи UTC; вход и перестройка — по open свечи с
проскальзыванием 0.05% на ногу и тейкерскими комиссиями (спот 0.1%, перп 0.055%); перестройка — когда номинал
отклонился от 1x капитала больше чем на 25%, но не когда |перп/спот − 1| > 1% (рынок разъехался — отложить до
следующей свечи). Шорт получает фондирование при положительной ставке: количество × open перпа свечи, в которую
попала метка, × ставка. Метка на open свечи (00:00 UTC) платится прежним количеством — до перестройки на этом open.
Только бумага, ордеров нет. BTC и ETH; SOL — книга «наблюдение» (ведётся, в итог базиса не входит): на SOL research
базис не сработал (отрицательное фондирование в крах FTX).

Доход сравнивается со ставкой векселей США (FRED DTB3 раз в сутки — `rates.py`; запасное — `rf_annual` в
config/basis.json): капитал базиса занят целиком и процента на USDT не получает.

    python -m optbot.basisbook --step | --status | --stats [ПЕРИОД] | --rf
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field

from . import db
from .config import Settings, settings
from .fmt import DAY_MS

log = logging.getLogger("optbot.basisbook")

PARAMS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "basis.json")
BARS = 200                       # дневных свечей на шаг (перерыв дольше — книгу начать заново)
FUNDING_PAGE = 200
BAR_LAG_MS = 10 * 60_000         # open свечи исполняется не раньше чем через 10 мин …
FUNDING_WAIT_MS = 2 * 3600_000   # … и после фондирования с меткой на этом open (нет его 2 ч — исполняем без него)


@dataclass(frozen=True)
class Params:
    strategy: str
    bases: tuple[str, ...]
    interval_min: int
    notional: float
    rebalance_band: float
    max_basis_dev: float
    capital_usdt: float
    spot_taker_fee: float
    perp_taker_fee: float
    slippage: float
    rf_annual: float
    rf_date_ms: int = 0
    observe: tuple[str, ...] = ()                          # книги «наблюдение»: ведутся, в итоги не входят
    worst_funding_week: dict = field(default_factory=dict)  # актив -> худшие 7 дней фондирования (доля номинала)


def load_params(path: str | None = None) -> Params:
    import calendar
    from datetime import datetime
    with open(path or PARAMS_FILE, encoding="utf-8") as f:
        raw = json.load(f)
    kw = {k: raw[k] for k in Params.__dataclass_fields__ if k in raw}
    kw["bases"] = tuple(b.upper() for b in kw["bases"])
    kw["observe"] = tuple(b.upper() for b in raw.get("observe", ()))
    kw["worst_funding_week"] = {k.upper(): float(v) for k, v in raw.get("worst_funding_week", {}).items()}
    if raw.get("rf_date"):
        kw["rf_date_ms"] = calendar.timegm(datetime.strptime(raw["rf_date"], "%Y-%m-%d").timetuple()) * 1000
    return Params(**kw)


def book_id(p: Params, base: str) -> str:
    return f"basis:{p.strategy}:{base.upper()}"


def is_observe(p: Params | None, book: str) -> bool:
    return p is not None and book.split(":")[-1] in p.observe


@dataclass
class State:
    cash: float
    qty: float
    s_in: float
    p_in: float
    fees: float = 0.0
    funding: float = 0.0
    rebalances: int = 0
    skipped: int = 0


def enter(st: State, p: Params, so: float, po: float) -> float:
    """Вход по open (как run_basis): -> комиссии."""
    st.s_in, st.p_in = so * (1 + p.slippage), po * (1 - p.slippage)
    st.qty = p.notional * st.cash / st.s_in
    fees = st.qty * (st.s_in * p.spot_taker_fee + st.p_in * p.perp_taker_fee)
    st.cash -= fees
    st.fees += fees
    return fees


def rebalance(st: State, p: Params, so: float, po: float) -> tuple[str, float] | None:
    """Довести номинал до цели по open, если отклонение больше коридора: -> ('rebalance', издержки: комиссии и
    проскальзывание, без переоценки ног) | ('skip', 0) | None."""
    e_open = st.cash + st.qty * (so - st.s_in) - st.qty * (po - st.p_in)
    if e_open <= 0 or abs(st.qty * so / e_open - p.notional) <= p.rebalance_band * p.notional:
        return None
    if abs(po / so - 1) > p.max_basis_dev:
        st.skipped += 1
        return "skip", 0.0
    st.cash += st.qty * (so - st.s_in) - st.qty * (po - st.p_in)        # переоценка по open — в капитал
    st.s_in, st.p_in = so, po
    new_q = p.notional * e_open / so
    dq = abs(new_q - st.qty)
    cost = dq * (so * (p.slippage + p.spot_taker_fee) + po * (p.slippage + p.perp_taker_fee))
    st.cash -= cost
    st.fees += dq * (so * p.spot_taker_fee + po * p.perp_taker_fee)   # как research: в «комиссиях» без проскальзывания
    st.qty = new_q
    st.rebalances += 1
    return "rebalance", cost


def receive(st: State, rate: float, price: float) -> float:
    amt = st.qty * price * rate
    st.cash += amt
    st.funding += amt
    return amt


def equity(st: State, spot: float, perp: float) -> float:
    return st.cash + st.qty * (spot - st.s_in) - st.qty * (perp - st.p_in)


def _bar_at(bars: list[tuple], t: int) -> int:
    lo, hi = 0, len(bars) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if bars[mid][0] <= t:
            lo = mid
        else:
            hi = mid - 1
    return lo if bars and bars[lo][0] <= t else -1


@dataclass
class StepResult:
    ts: int
    events: list[str] = field(default_factory=list)
    funding: int = 0
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        parts = [f"событий {len(self.events)}", f"фондирований {self.funding}"]
        return "базис: " + ", ".join(parts) + (f", ошибки: {'; '.join(self.errors)}" if self.errors else "")


class BasisBook:
    def __init__(self, client, cfg: Settings = settings, params: Params | None = None):
        self.c = client
        self.cfg = cfg
        self._lock = threading.Lock()
        self.p_error = None
        try:  # нет или кривой config/basis.json — без книги базиса, но бот работает
            self.p = params or load_params()
        except (OSError, KeyError, TypeError, ValueError) as e:
            self.p, self.p_error = None, f"параметры {PARAMS_FILE} не прочитаны: {e}"
            log.error("Базис: %s — книга выключена", self.p_error)

    def _db(self):
        return db.connect(self.cfg.db_file)

    def bases(self) -> list[str]:
        if not self.p:
            return []
        return [b for b in dict.fromkeys(self.p.bases + self.p.observe) if b in self.cfg.base_coins]

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
                        log.exception("Базис: шаг %s не выполнен", base)
                        res.errors.append(f"{base}: {e}")
            finally:
                conn.close()
        return res

    def _funding(self, base: str, after_ms: int, now_ms: int) -> list[tuple[int, float]]:
        """Ставки с меткой в (after_ms, now_ms] — по возрастанию."""
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
        spot = {b[0]: b for b in self.c.spot_ohlc(base, p.interval_min, BARS) if b[0] <= now_ms}
        perp = [b for b in self.c.perp_ohlc(base, p.interval_min, BARS) if b[0] <= now_ms]
        bars = [b for b in perp if b[0] in spot]          # свечи, где есть обе ноги
        if len(bars) < 2:
            raise RuntimeError("нет свечей спота и перпа")
        pre = conn.execute("SELECT last_funding_ms, owed_ms FROM basis_books WHERE id = ?", (bid,)).fetchone()
        # сеть — до блокировки; долг по опоздавшей метке — тоже ищем (она раньше last_funding_ms)
        after = min(pre["last_funding_ms"], pre["owed_ms"] - 1) if pre and pre["owed_ms"] else \
            (pre["last_funding_ms"] if pre else None)
        funding = self._funding(base, after, now_ms) if pre else []
        fund_ts = {t for t, _ in funding}
        events, msgs = [], []
        with conn:
            conn.execute("BEGIN IMMEDIATE")          # ручной --step одновременно с ботом — состояние под блокировкой
            book = conn.execute("SELECT * FROM basis_books WHERE id = ?", (bid,)).fetchone()
            if book is None:  # новая книга: вход — по open следующей свечи
                conn.execute("INSERT INTO basis_books (id, base, capital, cash, qty, s_in, p_in, last_bar_ms, "
                             "last_funding_ms, created_ms, params) VALUES (?,?,?,?,0,0,0,?,?,?,?)",
                             (bid, base, p.capital_usdt, p.capital_usdt, bars[-1][0], now_ms, now_ms,
                              json.dumps({**p.__dict__, "bases": list(p.bases)})))
                book = conn.execute("SELECT * FROM basis_books WHERE id = ?", (bid,)).fetchone()
            st = State(book["cash"], book["qty"], book["s_in"], book["p_in"], book["fees"], book["funding"],
                       book["rebalances"], book["skipped"])
            last_bar, last_f = book["last_bar_ms"], book["last_funding_ms"]
            owed = (book["owed_ms"], book["owed_qty"], book["owed_px"]) if book["owed_ms"] else None
            if bars[0][0] > last_bar + step_ms:
                raise RuntimeError(f"перерыв длиннее {len(bars)} свечей — книгу {bid} начать заново "
                                   "(удалить её строку basis_books; старые basis_equity и basis_events в отчёты "
                                   "новой книги не попадут)")
            for b in bars:
                t = b[0]
                if t <= last_bar or t + BAR_LAG_MS > now_ms:
                    continue
                if st.qty and t not in fund_ts and t > last_f and now_ms < t + FUNDING_WAIT_MS:
                    break           # ждём фондирование с меткой на этом open: его платит прежнее количество
                events.append((t, 1, "bar", b))
            last_t = max([e[0] for e in events], default=last_bar)
            events += [(t, 0, "fund", r) for t, r in funding
                       if (t > last_f or (owed and t == owed[0])) and t < last_t + step_ms]
            events.sort(key=lambda e: (e[0], e[1]))
            for t, _, kind, x in events:
                if kind == "fund":
                    k = _bar_at(bars, t)
                    if owed and t == owed[0]:          # опоздала: платит количество до того open, по его цене
                        q_now, st.qty = st.qty, owed[1]
                        amt = receive(st, x, owed[2])
                        st.qty = q_now
                        conn.execute("INSERT INTO basis_events (book, ts, kind, qty, perp, amount) VALUES "
                                     "(?,?,'funding',?,?,?)", (bid, t, owed[1], owed[2], amt))
                        res.funding += 1
                        owed = None
                    elif t <= last_bar and t not in {e[0] for e in events if e[2] == "bar"}:
                        log.warning("Базис: %s — метка %s пришла после своей свечи, долга по ней нет — пропущена",
                                    bid, t)
                    elif st.qty and k >= 0:
                        amt = receive(st, x, bars[k][1])
                        conn.execute("INSERT INTO basis_events (book, ts, kind, qty, perp, amount) VALUES "
                                     "(?,?,'funding',?,?,?)", (bid, t, st.qty, bars[k][1], amt))
                        res.funding += 1
                    last_f = max(last_f, t)
                    continue
                so, po = spot[t][1], x[1]
                if st.qty and t not in fund_ts and t > last_f:   # фондирование на этом open не пришло — долг
                    if owed:
                        log.warning("Базис: %s — долг по метке %s не закрыт, заменён меткой %s", bid, owed[0], t)
                    owed = (t, st.qty, po)
                if not st.qty and st.cash > 0:
                    fees = enter(st, p, so, po)
                    slip = st.qty * (so + po) * p.slippage          # издержки — как у перестройки: и проскальзывание
                    conn.execute("INSERT INTO basis_events (book, ts, kind, qty, spot, perp, amount) VALUES "
                                 "(?,?,'enter',?,?,?,?)", (bid, t, st.qty, st.s_in, st.p_in, -fees - slip))
                    msgs.append(f"{bid}: вход {st.qty:.6g} по споту {st.s_in:,.2f} / перпу {st.p_in:,.2f}")
                got = rebalance(st, p, so, po) if st.qty else None
                if got:
                    what, cost = got
                    conn.execute("INSERT INTO basis_events (book, ts, kind, qty, spot, perp, amount) VALUES "
                                 "(?,?,?,?,?,?,?)", (bid, t, what, st.qty, so, po,
                                                     None if what == "skip" else -cost))
                    msgs.append(f"{bid}: " + (f"перестройка до {st.qty:.6g}" if what == "rebalance" else
                                              f"перестройка отложена: перп/спот {po / so - 1:+.2%}"))
                last_bar = t
            s_last, p_last = spot[bars[-1][0]][4], bars[-1][4]
            conn.execute("UPDATE basis_books SET cash = ?, qty = ?, s_in = ?, p_in = ?, last_bar_ms = ?, "
                         "last_funding_ms = ?, fees = ?, funding = ?, rebalances = ?, skipped = ?, owed_ms = ?, "
                         "owed_qty = ?, owed_px = ? WHERE id = ?",
                         (st.cash, st.qty, st.s_in, st.p_in, last_bar, last_f, st.fees, st.funding, st.rebalances,
                          st.skipped, *(owed or (None, None, None)), bid))
            conn.execute("INSERT OR REPLACE INTO basis_equity (ts, book, equity, cash, qty, spot, perp) "
                         "VALUES (?,?,?,?,?,?,?)", (now_ms, bid, equity(st, s_last, p_last), st.cash, st.qty,
                                                    s_last, p_last))
        res.events += msgs

    # ---------- отчёты ----------
    def status(self) -> str:
        if self.p is None:
            return f"Книга базиса выключена: {self.p_error}"
        conn = self._db()
        try:
            books = conn.execute("SELECT * FROM basis_books ORDER BY id").fetchall()
            if not books:
                return "Книги базиса ещё не начаты (первый шаг — после среза сборщика)"
            lines = [f"Книги базиса (спот лонг + перп шорт, номинал {self.p.notional:g}x, коридор "
                     f"{self.p.rebalance_band:.0%}, капитал {self.p.capital_usdt:,.0f} USDT на монету):"]
            for b in books:
                eq = conn.execute("SELECT * FROM basis_equity WHERE book = ? ORDER BY ts DESC LIMIT 1",
                                  (b["id"],)).fetchone()
                if not b["qty"]:
                    lines.append(f" {b['id']}{' [наблюдение]' if is_observe(self.p, b['id']) else ''}: ждёт входа (open следующей дневной свечи)")
                    continue
                e = (f", капитал {eq['equity']:,.2f} ({eq['equity'] / b['capital'] - 1:+.2%}), номинал "
                     f"{b['qty'] * eq['spot'] / eq['equity']:.2f}x, перп/спот {eq['perp'] / eq['spot'] - 1:+.3%}"
                     if eq else "")
                obs = " [наблюдение]" if is_observe(self.p, b["id"]) else ""
                lines.append(f" {b['id']}{obs}: {b['qty']:.6g}{e}; фондирование {b['funding']:+.2f}, комиссии "
                             f"{b['fees']:.2f}, перестроек {b['rebalances']} (отложено {b['skipped']})")
            return "\n".join(lines)
        finally:
            conn.close()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    ap = argparse.ArgumentParser(prog="python -m optbot.basisbook",
                                 description="Бумажная книга базиса (спот лонг + перпетуал шорт)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--step", action="store_true", help="шаг: новые дневные свечи и фондирование")
    g.add_argument("--status", action="store_true", help="позиции, капитал, фондирование")
    g.add_argument("--stats", nargs="?", const="30d", metavar="ПЕРИОД",
                   help="итоги за период: годовых, сверх ставки, просадка, Шарп (по умолчанию 30d)")
    g.add_argument("--rf", action="store_true", help="взять ставку векселей с FRED (RF_URL) и показать")
    a = ap.parse_args(argv)
    from .bybit_client import OptionsClient
    bb = BasisBook(OptionsClient(), settings)
    if a.status:
        print(bb.status())
        return 0
    if a.stats:
        from .journal import parse_period
        from .reports import basis_rows, fmt_basis
        start, end, label = parse_period([a.stats])
        print(fmt_basis(basis_rows(start, end, settings), label, bb.p))
        return 0
    if a.rf:
        from . import rates
        conn = db.connect(settings.db_file)
        try:
            res = rates.update(conn, settings)
        finally:
            conn.close()
        print(res)
        return 1 if res.error or res.stale else 0
    res = bb.step()
    print(f"[{settings.env}] {res}")
    for x in res.events:
        print(" ", x)
    return 1 if res.errors else 0


if __name__ == "__main__":
    sys.exit(main())
