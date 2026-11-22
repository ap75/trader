"""Отчёты бумажных книг (этап E3): `/books`, `/stress`, еженедельный отчёт в группы.

Метрики книги (PLAN, E3 п. 1): сделки, доля прибыльных, PnL, ΣR и средний R, худшая сделка, доходность на IM
обычного режима (PnL / сумма IM сделок на входе; PM для бумажных книг не известна — «н/д»), максимальная просадка
кривой и Шарп / Сортино по дневным изменениям (`book_equity`: realized + unrealized по mark; у перпетуалов —
`perp_equity`). Проценты — от условного капитала: у опционных книг — PAPER_CAPITAL_USDT на систему (RV, HAR, с
фильтром издержек и без — параллельные системы, длинный горизонт входит и в RV, и в HAR), у перпетуалов —
капитал книги из `config/perp_ema.json`.

Стресс — мгновенный сдвиг рынка без хода времени: BTC ±10% / ±20% (ETH и SOL — на бету к BTC по дневным
доходностям перпетуалов за 90 дней, `perp_h1`), IV +20 / +50 пунктов у всех ног. Ноги переоцениваются по Black-76
(r = 0, форвард — underlying ноги из среза сборщика, IV ноги — её mark IV; улыбка — «по страйку»). Изменение —
от модельной цены при текущих F и IV, без комиссий. Перпетуалы — позиция × цена × сдвиг; к IV не чувствительны.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import math
import statistics
from dataclasses import dataclass, field

from . import db
from .config import Settings, settings
from .fmt import DAY_MS
from .paper import (all_books, cost_rejects, fmt_rejects, fmt_versions, in_system, is_cost_book, is_har_book,
                    leg_marks, signal_freq, summarize)
from .perpbook import corr, daily_pnl

log = logging.getLogger("optbot.reports")
YEAR_MS = 365 * DAY_MS
BETA_DAYS = 90
BETA_MIN_DAYS = 30              # меньше общих дней — бета 1 (с пометкой)
MIN_RATIO_DAYS = 5              # Шарп и Сортино — не меньше стольких дневных изменений
# (имя, сдвиг BTC, сдвиг IV в пунктах: 0.20 = +20); отдельные, затем совмещённые
SINGLE = [("−20%", -0.20, 0.0), ("−10%", -0.10, 0.0), ("+10%", 0.10, 0.0), ("+20%", 0.20, 0.0),
          ("IV+20", 0.0, 0.20), ("IV+50", 0.0, 0.50)]
COMBINED = [("−10/+20", -0.10, 0.20), ("−20/+50", -0.20, 0.50), ("+10/+10", 0.10, 0.10), ("+20/+10", 0.20, 0.10)]
SCENARIOS = SINGLE + COMBINED
SCEN_NAMES = [n for n, _, _ in SCENARIOS]
N_SINGLE = len(SINGLE)


def scen_label(i: int) -> str:
    _, s, dv = SCENARIOS[i]
    return " & ".join(x for x in (f"BTC {100 * s:+.0f}%" if s else "", f"IV {100 * dv:+.0f}" if dv else "") if x)


def leg_iv_shift(kind: str, price_shock: float, dv: float, put_mult: float) -> float:
    """Сдвиг IV ноги: при падении у путов — в put_mult раз сильнее, чем у коллов (перекос растёт)."""
    return dv * put_mult if price_shock < 0 and kind.upper().startswith("P") else dv
TG_LIMIT = 3900                 # сообщение <pre> — с запасом до 4096


def systems(cfg: Settings, present=()) -> list[tuple[str, bool, bool]]:
    """[(имя, HAR, с фильтром издержек)] — по книгам из all_books и по книгам с данными (present: id), чтобы
    выключенная в настройках версия со сделками не выпала из итогов."""
    books = all_books(cfg)
    hars = (False, True) if any(b[4] for b in books) or any(is_har_book(b) for b in present) else (False,)
    costs = (False, True) if any(b[5] for b in books) or any(is_cost_book(b) for b in present) else (False,)
    return [(("HAR" if h else "RV") + (", с фильтром издержек" if c else ""), h, c) for h in hars for c in costs]


# ---------- Black-76 ----------
def _ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _npdf(x: float) -> float:
    return math.exp(-x * x / 2) / math.sqrt(2 * math.pi)


def b76(kind: str, f: float, k: float, t: float, sig: float) -> tuple[float, float, float, float, float]:
    """(цена, дельта, гамма, вега на 1 пункт IV, тета в день) опциона на форвард, r = 0; t — в годах."""
    call = kind.upper().startswith("C")
    if t <= 0 or sig <= 0 or f <= 0 or k <= 0:
        intr = max(f - k, 0.0) if call else max(k - f, 0.0)
        d = (1.0 if f > k else 0.0) if call else (-1.0 if f < k else 0.0)
        return intr, d, 0.0, 0.0, 0.0
    st = sig * math.sqrt(t)
    d1 = (math.log(f / k) + st * st / 2) / st
    d2 = d1 - st
    if call:
        price, delta = f * _ncdf(d1) - k * _ncdf(d2), _ncdf(d1)
    else:
        price, delta = k * _ncdf(-d2) - f * _ncdf(-d1), _ncdf(d1) - 1
    gamma = _npdf(d1) / (f * st)
    vega = f * _npdf(d1) * math.sqrt(t) / 100
    theta = -f * _npdf(d1) * sig / (2 * math.sqrt(t)) / 365
    return price, delta, gamma, vega, theta


# ---------- бета к BTC ----------
def daily_closes(conn, base: str, lo_ms: int, hi_ms: int) -> dict[int, float]:
    """{день UTC: закрытие свечи 23:00} по perp_h1; день без неё (свечи докачаны не до конца дня) — пропущен."""
    return {r["ts"] // DAY_MS * DAY_MS: r["close"] for r in conn.execute(
        "SELECT ts, close FROM perp_h1 WHERE base = ? AND ts >= ? AND ts < ? AND ts % ? = ? ORDER BY ts",
        (base.upper(), lo_ms, hi_ms, DAY_MS, DAY_MS - 3600_000))}


def beta(conn, base: str, now_ms: int, days: int = BETA_DAYS) -> tuple[float, int]:
    """(бета дневных лог-доходностей base к BTC за days дней, общих дней); мало данных — (1.0, n)."""
    if base.upper() == "BTC":
        return 1.0, days
    hi = now_ms // DAY_MS * DAY_MS          # только полные дни: сегодняшний неполный — не доходность дня
    lo = hi - (days + 1) * DAY_MS
    a, b = daily_closes(conn, base, lo, hi), daily_closes(conn, "BTC", lo, hi)

    def rets(x: dict[int, float]) -> dict[int, float]:
        return {d: math.log(x[d] / x[d - DAY_MS]) for d in x if d - DAY_MS in x and x[d] > 0 and x[d - DAY_MS] > 0}
    ra, rb = rets(a), rets(b)
    days_ = sorted(set(ra) & set(rb))[-days:]
    if len(days_) < BETA_MIN_DAYS:
        return 1.0, len(days_)
    xa, xb = [ra[d] for d in days_], [rb[d] for d in days_]
    vb = statistics.pvariance(xb)
    if vb <= 0:
        return 1.0, len(days_)
    mb, ma = statistics.fmean(xb), statistics.fmean(xa)
    return sum((x - ma) * (y - mb) for x, y in zip(xa, xb)) / len(xa) / vb, len(days_)


# ---------- стресс и греки ----------
@dataclass
class Greeks:
    delta: float = 0.0       # в монете
    delta_usdt: float = 0.0
    gamma_usdt: float = 0.0  # изменение дельты в USDT на 1% хода
    vega: float = 0.0        # USDT на 1 пункт IV
    theta: float = 0.0       # USDT в день

    def add(self, o: "Greeks") -> None:
        for k in self.__dataclass_fields__:
            setattr(self, k, getattr(self, k) + getattr(o, k))


@dataclass
class Stress:
    now_ms: int
    betas: dict[str, tuple[float, int]]
    books: dict[str, list[float]] = field(default_factory=dict)        # книга -> изменение по сценариям, USDT
    greeks: dict[tuple[str, str], Greeks] = field(default_factory=dict)  # (книга, актив) -> греки
    perp: dict[str, list[float]] = field(default_factory=dict)
    perp_greeks: dict[str, Greeks] = field(default_factory=dict)        # актив -> греки (дельта)
    perp_capital: float = 0.0
    missing: list[str] = field(default_factory=list)                   # сделки без котировок / IV
    trades: int = 0
    # книга базиса -> (номинал USDT, худшая историческая неделя фондирования: доля, потеря USDT, капитал, наблюдение)
    basis: dict[str, tuple[float, float, float, float, bool]] = field(default_factory=dict)


def stress(cfg: Settings = settings, now_ms: int | None = None, conn=None) -> Stress:
    import time
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    own = conn is None
    conn = conn or db.connect(cfg.db_file)
    try:
        trades = conn.execute("SELECT * FROM paper_trades WHERE status = 'open' ORDER BY id").fetchall()
        try:
            perp = conn.execute("SELECT * FROM perp_books ORDER BY id").fetchall()
        except Exception:
            perp = []
        bases = sorted({"BTC"} | {t["base"] for t in trades} | {b["base"] for b in perp})
        res = Stress(now_ms, {b: beta(conn, b, now_ms) for b in bases})
        for t in trades:
            legs = [l for l in json.loads(t["legs"]) if l["expiry_ms"] > now_ms]
            if not legs:
                continue   # все ноги истекли — итог по цене поставки уже определён
            marks = leg_marks(conn, [l["symbol"] for l in legs], now_ms)
            if not marks or any(not (marks[l["symbol"]].get("iv") or 0) > 0
                                 or not ((marks[l["symbol"]].get("underlying") or t["forward"] or 0) > 0) for l in legs):
                res.missing.append(f"#{t['id']} {t['book']}")
                continue
            bt = res.betas.get(t["base"], (1.0, 0))[0]
            delta = [0.0] * len(SCEN_NAMES)
            g = Greeks()
            for l in legs:
                m = marks[l["symbol"]]
                f = m["underlying"] or t["forward"]
                iv = m["iv"]
                tt = (l["expiry_ms"] - now_ms) / YEAR_MS
                q = l["side"] * t["qty"]
                p0, d0, g0, v0, th0 = b76(l["kind"], f, l["strike"], tt, iv)
                scen = [(f * (1 + bt * s), iv + leg_iv_shift(l["kind"], bt * s, dv, cfg.paper.stress_put_iv_mult))
                        for _, s, dv in SCENARIOS]
                for i, (f1, iv1) in enumerate(scen):
                    delta[i] += q * (b76(l["kind"], f1, l["strike"], tt, iv1)[0] - p0)
                g.add(Greeks(q * d0, q * d0 * f, q * g0 * f * f * 0.01, q * v0, q * th0))
            res.trades += 1
            acc = res.books.setdefault(t["book"], [0.0] * len(SCEN_NAMES))
            for i, x in enumerate(delta):
                acc[i] += x
            res.greeks.setdefault((t["book"], t["base"]), Greeks()).add(g)
        for b in perp:
            res.perp_capital += b["capital"]
            px = conn.execute("SELECT price FROM perp_equity WHERE book = ? ORDER BY ts DESC LIMIT 1",
                              (b["id"],)).fetchone()
            if not b["qty"] or px is None:
                res.perp.setdefault(b["id"], [0.0] * len(SCEN_NAMES))
                continue
            bt = res.betas.get(b["base"], (1.0, 0))[0]
            notional = b["qty"] * px["price"]
            res.perp[b["id"]] = [notional * bt * s for _, s, _ in SCENARIOS]
            res.perp_greeks.setdefault(b["base"], Greeks()).add(Greeks(b["qty"], notional))
        try:
            from .basisbook import is_observe, load_params
            bp = load_params()
            bbooks = conn.execute("SELECT * FROM basis_books ORDER BY id").fetchall()
        except Exception:
            bp, bbooks = None, []
        for b in bbooks:
            row = conn.execute("SELECT spot FROM basis_equity WHERE book = ? ORDER BY ts DESC LIMIT 1",
                               (b["id"],)).fetchone()
            wk = bp.worst_funding_week.get(b["base"])
            if not b["qty"] or row is None or wk is None:
                continue
            notional = b["qty"] * row["spot"]
            res.basis[b["id"]] = (notional, wk, notional * wk, b["capital"], is_observe(bp, b["id"]))
        return res
    finally:
        if own:
            conn.close()


MIN_W = 18  # ширина колонки книги не меньше (шире — по самому длинному имени: узкий экран телефона)


def _cells(v: list[float]) -> str:
    return " ".join(f"{x:+7.0f}" for x in v)


def _pcts(v: list[float], cap: float) -> str:
    return " ".join(f"{100 * x / cap:+7.1f}" for x in v)


def system_totals(st: Stress, cfg: Settings = settings) -> list[tuple[str, list[float]]]:
    """[(система, изменение по всем сценариям)] — системы с открытыми позициями."""
    out = []
    for name, har, cost in systems(cfg, list(st.books)):
        part = [v for b, v in st.books.items() if in_system(b, har, cost)]
        if part:
            out.append((name, [sum(x) for x in zip(*part)]))
    return out


def worst_combined(st: Stress, cfg: Settings = settings) -> str:
    """Одна строка: худший совмещённый сценарий по каждой системе (USDT, % капитала, сценарий)."""
    cap = cfg.paper.capital_usdt
    cells = []
    for name, tot in system_totals(st, cfg):
        i = min(range(N_SINGLE, len(SCENARIOS)), key=lambda k: tot[k])
        cells.append(f"{name} {tot[i]:+,.0f} ({tot[i] / cap:+.1%}, {scen_label(i)})")
    if st.perp and any(any(v) for v in st.perp.values()):
        tot = [sum(x) for x in zip(*st.perp.values())]
        i = min(range(N_SINGLE, len(SCENARIOS)), key=lambda k: tot[k])
        cells.append(f"перпетуалы {tot[i]:+,.0f} ({tot[i] / st.perp_capital:+.1%}, {scen_label(i)})"
                     if st.perp_capital else f"перпетуалы {tot[i]:+,.0f} ({scen_label(i)})")
    if st.missing:
        cells.append(f"без оценки {len(st.missing)} сд.")
    return "Худший совмещённый сценарий: " + ("; ".join(cells) if cells else "открытых позиций нет")


def fmt_stress(st: Stress, cfg: Settings = settings) -> str:
    cap = cfg.paper.capital_usdt
    W = max([MIN_W] + [len(b) + 1 for b in [*st.books, *st.perp]])
    bt = ", ".join(f"{b} β {v:.2f}" + ("" if n >= BETA_MIN_DAYS else f" (мало данных: {n} дн., взята 1)")
                   for b, (v, n) in st.betas.items() if b != "BTC")
    lines = [f"Стресс открытых бумажных позиций (мгновенно; ETH/SOL — по бете к BTC за {BETA_DAYS} дн.: {bt or '-'})"]
    if not st.books and not st.perp and not st.basis:
        lines.append(" открытых позиций нет")
    totals = system_totals(st, cfg)
    for title, lo, hi in (("Изменение PnL, USDT — отдельные сценарии (сдвиг BTC; IV — пункты у всех ног):", 0, N_SINGLE),
                          (f"Совмещённые (сдвиг BTC % / IV пунктов; при падении IV путов — ×"
                           f"{cfg.paper.stress_put_iv_mult:g}):", N_SINGLE, len(SCENARIOS))):
        if not st.books and not st.perp:
            break
        lines += [title, f"{'':{W}}" + " ".join(f"{n:>7}" for n in SCEN_NAMES[lo:hi])]
        for book in sorted(st.books):
            v = st.books[book]
            lines.append(f"{book:{W}}{_cells(v[lo:hi])}  ({min(v[lo:hi]) / cap:+.1%})")
        for name, tot in totals:
            lines.append(f"{'Σ ' + name.replace(', с фильтром издержек', ':cost'):{W}}{_cells(tot[lo:hi])}")
            lines.append(f"{'  % от ' + f'{cap:,.0f}':{W}}{_pcts(tot[lo:hi], cap)}")
        if st.perp:
            for book in sorted(st.perp):
                lines.append(f"{book:{W}}{_cells(st.perp[book][lo:hi])}")
            tot = [sum(x) for x in zip(*st.perp.values())]
            lines.append(f"{'Σ перпетуалы':{W}}{_cells(tot[lo:hi])}")
            if st.perp_capital:
                lines.append(f"{'  % от ' + f'{st.perp_capital:,.0f}':{W}}{_pcts(tot[lo:hi], st.perp_capital)}")
    if st.basis:
        lines.append("Базис (дельта-нейтрален — сдвиг цены ≈ 0): худшая историческая неделя фондирования для шорта "
                     "(research), на текущий номинал:")
        for book, (notional, wk, loss, cap_b, obs) in sorted(st.basis.items()):
            lines.append(f" {book}{' [наблюдение]' if obs else ''}: номинал {notional:,.0f} × {wk:+.1%} = "
                         f"{loss:+,.0f} USDT ({loss / cap_b:+.1%} капитала)")
        main = [v for v in st.basis.values() if not v[4]]
        if len(main) > 1:
            tot, cap_t = sum(v[2] for v in main), sum(v[3] for v in main)
            lines.append(f" Σ базис (без наблюдения): {tot:+,.0f} USDT ({tot / cap_t:+.1%} от {cap_t:,.0f}; худшие "
                         "недели у монет — в разные даты)")
    if st.books or st.perp:
        lines.append(f"Σ — по системам (длинный горизонт — и в RV, и в HAR); % — от условного капитала; "
                     f"в скобках у книги — худший сценарий таблицы, % от {cap:,.0f}")
    if st.missing:
        lines.append("Без оценки (нет котировки или IV ноги в последнем срезе): " + ", ".join(st.missing))
    present = [*st.books, *(b for b, _ in st.greeks)]
    lines.append("")
    lines.append("Греки открытых: Δ — в монете (в USDT), Γ — изменение Δ в USDT на 1% хода, вега — USDT на пункт IV, "
                 "Θ — USDT в день")
    for name, har, cost in systems(cfg, present):
        by_base: dict[str, Greeks] = {}
        for (book, base), g in st.greeks.items():
            if in_system(book, har, cost):
                by_base.setdefault(base, Greeks()).add(g)
        if by_base:
            lines.append(f" {name}:")
            lines += [f"  {b}: {_fmt_greeks(g)}" for b, g in sorted(by_base.items())]
    if st.perp_greeks:
        lines.append(" перпетуалы:")
        lines += [f"  {b}: Δ {g.delta:+.4g} ({g.delta_usdt:+,.0f})" for b, g in sorted(st.perp_greeks.items())]
    if not st.greeks and not st.perp_greeks:
        lines.append(" позиций нет")
    return "\n".join(lines)


def _fmt_greeks(g: Greeks) -> str:
    return (f"Δ {g.delta:+.4g} ({g.delta_usdt:+,.0f}), Γ {g.gamma_usdt:+,.1f}, вега {g.vega:+,.2f}, "
            f"Θ {g.theta:+,.2f}")


# ---------- /books ----------
def curve_metrics(series: list[tuple[int, float]], start_ms: int, cap: float) -> dict:
    """series — [(ts, значение кривой)] по времени, с последней точкой до start_ms (если есть): просадка (USDT, %
    капитала) за период, Шарп и Сортино по дневным изменениям (годовые, √365). Дни — сутки UTC; первые и последние
    сутки периода «Nd» неполные; сутки без точек (бот не работал) — без изменения: оно в метрики не входит."""
    peak, dd = None, 0.0
    for _, v in series:         # первая точка — последняя до периода (если есть): от неё и считается просадка
        peak = v if peak is None else max(peak, v)
        dd = min(dd, v - peak)
    days = _days(series, start_ms)[1]
    sharpe = sortino = None
    if len(days) >= MIN_RATIO_DAYS:
        m = statistics.fmean(days)
        sd = statistics.pstdev(days)
        sharpe = m / sd * math.sqrt(365) if sd > 0 else None
        down = math.sqrt(statistics.fmean([min(x, 0.0) ** 2 for x in days]))
        sortino = m / down * math.sqrt(365) if down > 0 else None
    return dict(dd=dd, dd_pct=dd / cap, sharpe=sharpe, sortino=sortino, days=len(days))


def _series(conn, q: str, args, start_ms: int, end_ms: int) -> dict[str, list[tuple[int, float]]]:
    """{книга: [(ts, значение)]} — точки периода и последняя до него (начало просадки и первого дня)."""
    out: dict[str, list[tuple[int, float]]] = {}
    for r in conn.execute(q, args):
        if r["ts"] < end_ms:
            out.setdefault(r["book"], []).append((r["ts"], r["v"]))
    for k, v in out.items():
        before = [x for x in v if x[0] < start_ms]
        out[k] = before[-1:] + [x for x in v if x[0] >= start_ms]
    return out


def _book_filter(book: str, horizon: str | None, base: str | None) -> bool:
    parts = book.split(":")
    return (horizon is None or parts[0] == horizon) and (base is None or parts[2] == base)


@dataclass
class BookRow:
    book: str
    n: int
    wins: int
    pnl: float
    sum_r: float
    avg_r: float | None
    worst: float | None
    im_ret: float | None       # PnL / сумма IM обычного режима на входе
    cap: float
    dd: float
    dd_pct: float
    sharpe: float | None
    sortino: float | None
    days: int
    open_n: int = 0

    def line(self) -> str:
        f2 = lambda x: "-" if x is None else f"{x:+.2f}"   # noqa: E731
        win = f"{self.wins / self.n:.0%}" if self.n else "-"
        im = "-" if self.im_ret is None else f"{self.im_ret:+.1%}"
        opened = f", открыто {self.open_n}" if self.open_n else ""
        return (f" {self.book}: {self.n} сд. (приб. {win}){opened}, PnL {self.pnl:+.2f} ({self.pnl / self.cap:+.2%}), "
                f"ΣR {self.sum_r:+.2f}, ср. R {f2(self.avg_r)}, худшая {f2(self.worst)}, на IM {im} (PM н/д), "
                f"просадка {self.dd:+.2f} ({self.dd_pct:+.2%}), Шарп {f2(self.sharpe)}, Сортино {f2(self.sortino)}")

    def csv_row(self) -> list:
        return [self.book, self.n, self.wins, round(self.pnl, 4), round(self.sum_r, 4),
                None if self.avg_r is None else round(self.avg_r, 4), self.worst,
                None if self.im_ret is None else round(self.im_ret, 6), round(self.dd, 4), round(self.dd_pct, 6),
                None if self.sharpe is None else round(self.sharpe, 3),
                None if self.sortino is None else round(self.sortino, 3), self.days, self.open_n]


CSV_HEAD = ["book", "trades", "wins", "pnl", "sum_r", "avg_r", "worst", "im_return", "max_dd", "max_dd_pct",
            "sharpe", "sortino", "days", "open"]


def _row(book: str, trades: list[dict], series: list[tuple[int, float]], start_ms: int, cap: float,
         open_n: int, im: bool = True) -> BookRow:
    s = summarize(trades)
    # IM 0 — законно (стрэддл без короткой ноги); нет IM у какой-то сделки — доходность на IM не считаем
    ims = [t["im_regular"] for t in trades if t.get("im_regular") is not None] if im else []
    im_ret = s["pnl"] / sum(ims) if ims and len(ims) == len(trades) and sum(ims) > 0 else None
    c = curve_metrics(series, start_ms, cap)
    return BookRow(book, s["n"], s["wins"], s["pnl"], s["sum_r"], s["avg_r"], s["worst"], im_ret, cap,
                   c["dd"], c["dd_pct"], c["sharpe"], c["sortino"], c["days"], open_n)


@dataclass
class BooksReport:
    label: str
    rows: list[BookRow]
    systems: list[BookRow]
    perp: list[BookRow]
    perp_corr: dict[str, dict[str, tuple[float | None, int]]]   # книга перпетуалов -> {система: (ρ, дней)}
    open_lines: list[str]
    versions: list[str]
    rejects: list[str]
    basis: list[dict] = field(default_factory=list)
    basis_corr: dict[str, dict[str, tuple[float | None, int]]] = field(default_factory=dict)


def books_report(start_ms: int, end_ms: int, label: str, cfg: Settings = settings, horizon: str | None = None,
                 base: str | None = None, conn=None) -> BooksReport:
    own = conn is None
    conn = conn or db.connect(cfg.db_file)
    cap = cfg.paper.capital_usdt
    try:
        closed = [dict(r) for r in conn.execute(
            "SELECT * FROM paper_trades WHERE status = 'closed' AND closed_ms >= ? AND closed_ms < ? ORDER BY id",
            (start_ms, end_ms)) if _book_filter(r["book"], horizon, base)]
        opened = [dict(r) for r in conn.execute("SELECT * FROM paper_trades WHERE status = 'open'")
                  if _book_filter(r["book"], horizon, base)]
        eq = _series(conn, "SELECT ts, book, realized + unrealized v FROM book_equity ORDER BY ts", (), start_ms, end_ms)
        eq = {k: v for k, v in eq.items() if _book_filter(k, horizon, base)}
        books = sorted({t["book"] for t in closed} | {t["book"] for t in opened}
                       | {k for k, v in eq.items() if len({x[1] for x in v}) > 1})
        rows = [_row(b, [t for t in closed if t["book"] == b], eq.get(b, []), start_ms, cap,
                     sum(t["book"] == b for t in opened)) for b in books]
        sys_rows, sys_daily = [], {}
        present = [*eq, *(t["book"] for t in closed), *(t["book"] for t in opened)]
        for name, har, cost in systems(cfg, present):
            members = [b for b in eq if in_system(b, har, cost)]
            trades = [t for t in closed if in_system(t["book"], har, cost)]
            if not members and not trades:
                continue
            tot: dict[int, float] = {}
            for b in members:   # срез пишет все книги разом — сумма по одинаковым ts; нет точки — последняя известная
                for ts, v in eq[b]:
                    tot[ts] = 0.0
            for b in members:
                pts, i, last = eq[b], 0, 0.0
                for ts in sorted(tot):
                    while i < len(pts) and pts[i][0] <= ts:
                        last = pts[i][1]
                        i += 1
                    tot[ts] += last
            series = sorted(tot.items())
            r = _row(name, trades, series, start_ms, cap, sum(in_system(t["book"], har, cost) for t in opened))
            sys_rows.append(r)
            sys_daily[name] = dict(zip(*_days(series, start_ms))) if series else {}
        perp_rows, perp_corr = [], {}
        if horizon is None:
            try:
                pbooks = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM perp_books ORDER BY id")}
            except Exception:
                pbooks = {}
            ptrades = [dict(r) for r in conn.execute(
                "SELECT * FROM perp_trades WHERE status = 'closed' AND closed_ms >= ? AND closed_ms < ?",
                (start_ms, end_ms))] if pbooks else []
            peq = _series(conn, "SELECT ts, book, equity v FROM perp_equity ORDER BY ts", (), start_ms, end_ms) \
                if pbooks else {}
            for bid, b in pbooks.items():
                if base is not None and b["base"] != base:
                    continue
                series = peq.get(bid, [])
                perp_rows.append(_row(bid, [t for t in ptrades if t["book"] == bid], series, start_ms, b["capital"],
                                      int(bool(b["qty"])), im=False))
                pd = dict(zip(*_days(series, start_ms))) if series else {}
                perp_corr[bid] = {name: corr(pd, sd) for name, sd in sys_daily.items()}
        freq = {k: v for k, v in signal_freq(conn, start_ms, end_ms).items() if base is None or k[1] == base}
        rej = {k: v for k, v in cost_rejects(conn, start_ms, end_ms).items() if _book_filter(k, horizon, base)}
        open_lines = []
        for name, har, cost in systems(cfg, present):
            part = [t for t in opened if in_system(t["book"], har, cost)]
            if part:
                mark = sum(t["mark_pnl"] if t["mark_pnl"] is not None else -t["entry_fees"] for t in part)
                open_lines.append(f" {name}: {len(part)}, риск {sum(t['max_loss'] for t in part):.2f}, "
                                  f"по mark {mark:+.2f} USDT")
        basis, basis_corr = [], {}
        if horizon is None:
            try:
                basis = basis_rows(start_ms, end_ms, cfg, base, conn)
                beq = _series(conn, BASIS_EQ_SQL, (), start_ms, end_ms)
            except Exception:
                log.exception("Отчёт книг: итоги базиса не посчитаны")
                basis, beq = [], {}
            for r_ in basis:
                series = beq.get(r_["book"], [])
                bd = dict(zip(*_days(series, start_ms))) if series else {}
                basis_corr[r_["book"]] = {name: corr(bd, sd) for name, sd in sys_daily.items()}
        return BooksReport(label, rows, sys_rows, perp_rows, perp_corr, open_lines,
                           fmt_versions(closed if horizon in (None, "short") else [], freq if horizon in (None, "short") else None),
                           fmt_rejects(rej), basis, basis_corr)
    finally:
        if own:
            conn.close()


def _days(series: list[tuple[int, float]], start_ms: int) -> tuple[list[int], list[float]]:
    """Дневные изменения за период. Последняя точка до начала — как конец предыдущего дня: так первый (неполный)
    день периода — изменение от неё, а не выпадает (начало «7d» — посреди дня)."""
    day0 = start_ms // DAY_MS * DAY_MS
    pts = [(day0 - 1 if ts < start_ms else ts, v) for ts, v in series]
    d = daily_pnl([dict(ts=ts, book="x", v=v) for ts, v in pts], day0).get("x", {})
    return list(d.keys()), list(d.values())


# ---------- книга базиса ----------
# кривая — только с начала текущей книги (книгу начали заново после перерыва — старые точки не в счёт)
BASIS_EQ_SQL = ("SELECT e.ts, e.book, e.equity v FROM basis_equity e JOIN basis_books b ON b.id = e.book "
                "WHERE e.ts >= b.created_ms ORDER BY e.ts")
def basis_rows(start_ms: int, end_ms: int, cfg: Settings = settings, base: str | None = None, conn=None,
               rf: float | None = None) -> list[dict]:
    """По книгам базиса за период: PnL (изменение капитала), фондирование, издержки, перестройки, годовых и сверх
    ставки (rf — годовая; по умолчанию последняя с FRED, иначе из config/basis.json), просадка, Шарп и Шарп сверх
    ставки, номинал / капитал книги на конец периода; observe — книга «наблюдение» (в итог базиса не входит)."""
    from .basisbook import is_observe, load_params
    own = conn is None
    conn = conn or db.connect(cfg.db_file)
    try:
        try:
            p = load_params()
        except Exception:
            p = None
        rf_label = None
        if rf is None:
            try:
                from . import rates
                r_ = rates.current(conn)
                rf, rf_label = r_.rate, r_.label()
            except Exception:
                log.exception("Ставка для базиса не прочитана — сверх ставки считаю от 0")
                rf = 0.0
        books = [dict(r) for r in conn.execute("SELECT * FROM basis_books ORDER BY id")
                 if base is None or r["base"] == base]
        eq = _series(conn, BASIS_EQ_SQL, (), start_ms, end_ms)
        out = []
        for b in books:
            series = eq.get(b["id"], [])
            ev = {r["kind"]: (r["n"], r["amt"]) for r in conn.execute(
                "SELECT kind, COUNT(*) n, COALESCE(SUM(amount), 0) amt FROM basis_events WHERE book = ? AND ts >= ? "
                "AND ts < ? GROUP BY kind", (b["id"], max(start_ms, b["created_ms"]), end_ms))}
            first = series[0] if series else None
            obs = is_observe(p, b["id"])
            last = conn.execute("SELECT equity, qty, spot FROM basis_equity WHERE book = ? AND ts < ? AND ts >= ? "
                                "ORDER BY ts DESC LIMIT 1", (b["id"], end_ms, b["created_ms"])).fetchone()
            lev = last["qty"] * last["spot"] / last["equity"] if last and last["equity"] > 0 else None
            if first is None:
                out.append(dict(book=b["id"], days=0, pnl=0.0, capital=b["capital"], funding=0.0, costs=0.0,
                                rebalances=0, skipped=0, ann=None, excess=None, rf=rf, rf_label=rf_label, dd=0.0,
                                dd_pct=0.0, sharpe=None, sharpe_x=None, lev=lev, observe=obs, start_v=b["capital"]))
                continue
            if first[0] < start_ms:                     # точка до периода — от неё
                start_v, t0 = first[1], start_ms
            elif b["created_ms"] >= start_ms:           # книга начата в периоде — от капитала, с первого входа
                ent = conn.execute("SELECT MIN(ts) FROM basis_events WHERE book = ? AND kind = 'enter' AND ts >= ?",
                                   (b["id"], b["created_ms"])).fetchone()[0]
                start_v, t0 = b["capital"], ent or b["created_ms"]
            else:                                       # до периода точек нет (бот не работал) — от первой в периоде
                start_v, t0 = first[1], first[0]
            pnl = series[-1][1] - start_v
            days = max((series[-1][0] - t0) / DAY_MS, 0.0)
            c = curve_metrics(series, start_ms, b["capital"])
            ann = pnl / start_v * 365 / days if days >= 1 and start_v > 0 else None   # на капитал в начале периода
            dl = _days(series, start_ms)[1]
            sx = None
            if len(dl) >= MIN_RATIO_DAYS:
                x = [v - start_v * rf / 365 for v in dl]
                sd = statistics.pstdev(x)
                sx = statistics.fmean(x) / sd * math.sqrt(365) if sd > 0 else None
            out.append(dict(book=b["id"], days=days, pnl=pnl, capital=b["capital"],
                            funding=ev.get("funding", (0, 0.0))[1],
                            costs=ev.get("enter", (0, 0.0))[1] + ev.get("rebalance", (0, 0.0))[1],
                            rebalances=ev.get("rebalance", (0, 0))[0], skipped=ev.get("skip", (0, 0))[0],
                            ann=ann, excess=None if ann is None else ann - rf, rf=rf, rf_label=rf_label, dd=c["dd"],
                            dd_pct=c["dd_pct"], sharpe=c["sharpe"], sharpe_x=sx, lev=lev, observe=obs,
                            start_v=start_v))
        return out
    finally:
        if own:
            conn.close()


def fmt_basis(rows: list[dict], label: str, p=None) -> str:
    head = "Книги базиса (спот лонг + перп шорт, бумага"
    if p is not None:
        head += f", номинал {p.notional:g}x, коридор {p.rebalance_band:.0%}"
    lines = [head + f"), {label}:"]
    if not rows:
        lines.append(" ещё не начаты")
    f2 = lambda x: "-" if x is None else f"{x:+.2f}"     # noqa: E731
    pc = lambda x: "-" if x is None else f"{x:+.1%}"     # noqa: E731
    for r in rows:
        lev = "-" if r.get("lev") is None else f"{r['lev']:.2f}x"
        lines.append(f" {r['book']}{' [наблюдение]' if r.get('observe') else ''}: PnL {r['pnl']:+.2f} "
                     f"({r['pnl'] / (r.get('start_v') or r['capital']):+.2%} от капитала в начале периода) за "
                     f"{r['days']:.1f} дн., номинал/капитал {lev}, "
                     f"фондирование {r['funding']:+.2f}, издержки {r['costs']:+.2f}, перестроек {r['rebalances']}"
                     + (f" (отложено {r['skipped']})" if r["skipped"] else "")
                     + f"; годовых {pc(r['ann'])}, сверх ставки {r['rf']:.2%}: {pc(r['excess'])}; просадка "
                     f"{r['dd']:+.2f} ({r['dd_pct']:+.2%}), Шарп {f2(r['sharpe'])}, сверх ставки {f2(r['sharpe_x'])}")
    main = [r for r in rows if not r.get("observe")]
    if len(main) > 1:
        cap = sum(r["capital"] for r in main)
        lines.append(f" Σ базис (без наблюдения): PnL {sum(r['pnl'] for r in main):+.2f} "
                     f"({sum(r['pnl'] for r in main) / cap:+.2%} от {cap:,.0f}), фондирование "
                     f"{sum(r['funding'] for r in main):+.2f}, издержки {sum(r['costs'] for r in main):+.2f}")
    if rows and rows[0].get("rf_label"):
        lines.append(f" ставка: {rows[0]['rf_label']}")
    if any(r["days"] and r["days"] < 30 for r in rows):
        lines.append(" годовых за короткий срок — грубая оценка (research: BTC/ETH ≈ +3…5% сверх ставки)")
    return "\n".join(lines)


def fmt_books(r: BooksReport, cfg: Settings = settings, filt: str = "") -> str:
    cap = cfg.paper.capital_usdt
    lines = [f"Бумажные книги{filt}, {r.label} (проценты — от условного капитала {cap:,.0f} USDT на систему; "
             "у перпетуалов — от капитала книги)"]
    lines.append("Открытые:" if r.open_lines else "Открытых сделок нет")
    lines += r.open_lines
    if r.systems:
        lines.append("По системам (длинный горизонт — и в RV, и в HAR):")
        lines += [x.line() for x in r.systems]
    if r.rows:
        lines.append("По книгам:")
        lines += [x.line() for x in r.rows]
    elif not r.systems:
        lines.append("Сделок за период нет")
    lines += r.versions + r.rejects
    if r.perp:
        lines.append("Книги перпетуалов (EMA 4h, бумага):")
        lines += [x.line().replace(", на IM - (PM н/д)", "") for x in r.perp]
        names = sorted({n for v in r.perp_corr.values() for n in v})
        if names:
            lines.append("Корреляция дневных итогов с системами опционных книг (" + " / ".join(names) + "):")
            for pb, v in sorted(r.perp_corr.items()):
                cells = ["-" if v[n][0] is None else f"{v[n][0]:+.2f}" for n in names]
                lines.append(f" {pb}: " + " / ".join(cells) + f" ({max(x[1] for x in v.values())} дн.)")
    if r.basis:
        lines += fmt_basis(r.basis, r.label).splitlines()
        names = sorted({n for v in r.basis_corr.values() for n in v})
        if names and any(c is not None for v in r.basis_corr.values() for c, _ in v.values()):
            lines.append("Корреляция дневных итогов базиса с системами опционных книг (" + " / ".join(names) + "):")
            for bb, v in sorted(r.basis_corr.items()):
                cells = ["-" if v[n][0] is None else f"{v[n][0]:+.2f}" for n in names]
                lines.append(f" {bb}: " + " / ".join(cells) + f" ({max(x[1] for x in v.values())} дн.)")
    return "\n".join(lines)


def books_csv(r: BooksReport) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_HEAD)
    for x in r.systems + r.rows + r.perp:
        w.writerow(x.csv_row())
    return buf.getvalue().encode("utf-8-sig")


def split_text(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Текст -> части не длиннее limit по границам строк (длинная строка — обрезается)."""
    out, cur = [], ""
    for line in text.split("\n"):
        line = line if len(line) <= limit else line[:limit - 1] + "…"
        if cur and len(cur) + 1 + len(line) > limit:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse
    from .journal import parse_period
    ap = argparse.ArgumentParser(prog="python -m optbot.reports", description="Отчёты бумажных книг (E3)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--books", nargs="?", const="30d", metavar="ПЕРИОД", help="итоги книг (по умолчанию 30d)")
    g.add_argument("--stress", action="store_true", help="стресс-сценарии и греки открытых позиций")
    ap.add_argument("--horizon", choices=("short", "long"), help="с --books: только горизонт")
    ap.add_argument("--base", help="с --books: только актив")
    ap.add_argument("--csv", metavar="ФАЙЛ", help="с --books: метрики книг в CSV")
    a = ap.parse_args(argv)
    if a.stress:
        print(fmt_stress(stress(settings)))
        return 0
    start, end, label = parse_period([a.books])
    base = a.base.upper() if a.base else None
    rep = books_report(start, end, label, settings, a.horizon, base)
    print(fmt_books(rep, settings, "".join(f" · {x}" for x in (a.horizon, base) if x)))
    if a.csv:
        with open(a.csv, "wb") as f:
            f.write(books_csv(rep))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
