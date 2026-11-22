"""Бумажная книга базиса (optbot/basisbook.py): совпадение с research run_basis, фондирование, отчёты."""
import asyncio
import dataclasses
import math
import random

import pytest

from optbot import basisbook, db, reports
from optbot.config import PaperSettings

DAY = 86_400_000


def gen(n=160, seed=5):
    """Те же данные, на которых посчитан эталон research (run_basis, коридор 25%, защита 1%, start = свеча 10)."""
    rnd = random.Random(seed)
    t0 = 1_780_000_000_000 // DAY * DAY
    spot, perp, px = [], [], 30000.0
    for i in range(n):
        drift = 0.03 if 40 <= i < 70 else (-0.03 if 100 <= i < 125 else 0.0)
        o = px
        c = o * math.exp(drift + rnd.gauss(0, 0.01))
        h, l = max(o, c) * 1.004, min(o, c) * 0.996
        dev = 0.02 if 50 <= i < 62 else rnd.uniform(-0.002, 0.003)
        spot.append((t0 + i * DAY, o, h, l, c))
        perp.append((t0 + i * DAY, o * (1 + dev), h * (1 + dev), l * (1 + dev), c * (1 + dev)))
        px = c
    funding = [(t0 + i * 8 * 3600_000, rnd.uniform(-0.0002, 0.0005)) for i in range(n * 3)]
    return spot, perp, funding


# research: equity после свечи k (капитал + переоценка по закрытию)
RESEARCH_EQ = {10: 9981.768331, 40: 10146.188828, 55: 9922.785515, 62: 10172.860471, 80: 10239.65239,
               110: 10358.459428, 130: 10422.546343, 158: 10548.03085}


class Client:
    def __init__(self, spot, perp, funding):
        self.spot, self.perp, self.funding, self.now = spot, perp, funding, 0

    def spot_ohlc(self, b, iv, limit=200):
        return [x for x in self.spot if x[0] <= self.now][-limit:]

    def perp_ohlc(self, b, iv, limit=200):
        return [x for x in self.perp if x[0] <= self.now][-limit:]

    def perp_funding_history(self, b, s, e, limit=200):
        return sorted([x for x in self.funding if s <= x[0] <= e], reverse=True)[:limit]


@pytest.fixture
def bcfg(cfg):
    return dataclasses.replace(cfg, base_coins=["BTC", "ETH", "SOL"], paper=PaperSettings(enabled=True))


def params(**kw):
    return dataclasses.replace(basisbook.load_params(), **{"bases": ("BTC",), "observe": (), **kw})


def book(cfg):
    conn = db.connect(cfg.db_file)
    try:
        return dict(conn.execute("SELECT * FROM basis_books").fetchone())
    finally:
        conn.close()


def last_eq(cfg):
    conn = db.connect(cfg.db_file)
    try:
        return conn.execute("SELECT equity FROM basis_equity ORDER BY ts DESC LIMIT 1").fetchone()[0]
    finally:
        conn.close()


@pytest.mark.parametrize("every", [1, 6])        # шаг каждый день и с пропусками (бот лежал) — итог тот же
def test_matches_research_run_basis(bcfg, every):
    spot, perp, funding = gen()
    c = Client(spot, perp, funding)
    bb = basisbook.BasisBook(c, bcfg, params())
    c.now = spot[9][0] + 3600_000
    bb.step(c.now)                                   # новая книга: вход — по open следующей свечи
    got = {}
    for k in sorted(set(range(10, 159, every)) | set(RESEARCH_EQ)):
        c.now = spot[k][0] + DAY - 60_000             # конец дня: фондирование 08:00 и 16:00 уже есть
        assert not bb.step(c.now).errors
        got[k] = last_eq(bcfg)
    for k, v in RESEARCH_EQ.items():
        assert got[k] == pytest.approx(v, abs=1e-6)
    b = book(bcfg)
    assert (b["rebalances"], b["skipped"]) == (5, 8)        # 50–61: перп на 2% выше — перестройка отложена


def test_waits_for_funding_at_open(bcfg):
    spot, perp, funding = gen(30)
    c = Client(spot, perp, [f for f in funding if f[0] != spot[11][0]])   # метки 00:00 свечи 11 ещё нет
    bb = basisbook.BasisBook(c, bcfg, params())
    c.now = spot[10][0] - 3600_000
    bb.step(c.now)
    c.now = spot[10][0] + basisbook.BAR_LAG_MS
    bb.step(c.now)
    q = book(bcfg)["qty"]
    assert q > 0 and book(bcfg)["last_bar_ms"] == spot[10][0]
    c.now = spot[11][0] + 30 * 60_000                       # фондирования на open 11 нет — свеча 11 ждёт
    bb.step(c.now)
    assert book(bcfg)["last_bar_ms"] == spot[10][0]
    c.now = spot[11][0] + basisbook.FUNDING_WAIT_MS          # не пришло за 2 ч — исполняем без него
    bb.step(c.now)
    assert book(bcfg)["last_bar_ms"] == spot[11][0]


def test_short_receives_positive_funding(bcfg):
    spot = [(i * DAY, 100.0, 101.0, 99.0, 100.0) for i in range(5)]
    perp = [(i * DAY, 100.0, 101.0, 99.0, 100.0) for i in range(5)]
    c = Client(spot, perp, [(2 * DAY + 8 * 3600_000, 0.001), (2 * DAY, 0.0), (3 * DAY, 0.0)])
    bb = basisbook.BasisBook(c, bcfg, params())
    c.now = DAY + 1
    bb.step(c.now)
    c.now = 2 * DAY + basisbook.BAR_LAG_MS
    bb.step(c.now)
    b0 = book(bcfg)
    c.now = 2 * DAY + 9 * 3600_000
    bb.step(c.now)
    b1 = book(bcfg)
    assert b1["funding"] == pytest.approx(b0["qty"] * 100.0 * 0.001)       # шорт получает при ставке > 0
    assert b1["cash"] - b0["cash"] == pytest.approx(b1["funding"])


def test_long_outage_refused_and_missing_params(bcfg, monkeypatch, tmp_path):
    spot, perp, funding = gen(400)
    c = Client(spot, perp, funding)
    bb = basisbook.BasisBook(c, bcfg, params())
    c.now = spot[100][0] + 1
    bb.step(c.now)
    c.now = spot[350][0] + basisbook.FUNDING_WAIT_MS
    res = bb.step(c.now)
    assert res.errors and "начать заново" in res.errors[0]
    monkeypatch.setattr(basisbook, "PARAMS_FILE", str(tmp_path / "none.json"))
    off = basisbook.BasisBook(c, bcfg)
    assert off.p is None and "не прочитаны" in off.step(1).errors[0] and "выключена" in off.status()


def test_params_copied_from_research():
    p = basisbook.load_params()
    assert p.bases == ("BTC", "ETH") and (p.notional, p.rebalance_band, p.max_basis_dev) == (1.0, 0.25, 0.01)
    assert p.observe == ("SOL",) and p.worst_funding_week == {"BTC": -0.006, "ETH": -0.014, "SOL": -0.256}
    assert p.rf_date_ms == 1795046400000                                     # 2026-11-19
    assert (p.spot_taker_fee, p.perp_taker_fee, p.slippage, p.capital_usdt) == (0.001, 0.00055, 0.0005, 10000)
    assert 0 < p.rf_annual < 0.1


def test_status_stats_and_books(bcfg):
    spot, perp, funding = gen()
    c = Client(spot, perp, funding)
    bb = basisbook.BasisBook(c, bcfg, params())
    c.now = spot[9][0] + 3600_000
    bb.step(c.now)
    for k in range(10, 80):
        c.now = spot[k][0] + DAY - 60_000
        bb.step(c.now)
    assert "basis:spot_perp:BTC" in bb.status() and "перестроек" in bb.status()
    start, end = spot[40][0], c.now + 1
    rows = reports.basis_rows(start, end, bcfg, rf=0.04)
    r, = rows
    conn = db.connect(bcfg.db_file)
    before = conn.execute("SELECT equity FROM basis_equity WHERE ts < ? ORDER BY ts DESC LIMIT 1", (start,)).fetchone()[0]
    conn.close()
    assert r["pnl"] == pytest.approx(last_eq(bcfg) - before)            # от последней точки до периода
    assert r["funding"] > 0 and r["rebalances"] >= 1 and r["skipped"] == 8 and r["ann"] is not None
    assert r["excess"] == pytest.approx(r["ann"] - 0.04) and r["sharpe_x"] is not None
    text = reports.fmt_basis(rows, "30 дн.", bb.p)
    assert "сверх ставки 4.00%" in text and "коридор 25%" in text
    rep = reports.books_report(start, end, "x", bcfg)
    assert rep.basis and "Книги базиса" in reports.fmt_books(rep, bcfg)
    assert reports.books_report(start, end, "x", bcfg, horizon="short").basis == []


def test_client_spot_daily_interval(cfg, make_client, fake):
    c = make_client(cfg)
    fake.klines[("BTCUSDT", "D")] = [[str(1000 + i), "1", "2", "0.5", "1.5", "0", "0"] for i in range(3)]
    rows = c.spot_ohlc("BTC", 1440, 10)
    call = fake.called("get_kline")[-1]
    assert call["category"] == "spot" and call["interval"] == "D" and len(rows) == 3 and rows[0][0] == 1000


def test_bot_runs_basis_after_perp():
    from optbot.bot import OptionsBot
    from optbot import perpbook
    calls = []
    bot = OptionsBot.__new__(OptionsBot)
    bot.paper = type("P", (), {"step": lambda self: calls.append("paper") or perpbook.StepResult(0)})()
    bot.perp = type("Q", (), {"step": lambda self: calls.append("perp") or perpbook.StepResult(0)})()
    bot.basis = type("B", (), {"step": lambda self: calls.append("basis") or basisbook.StepResult(0)})()
    asyncio.run(bot._paper_job())
    assert calls == ["paper", "perp", "basis"]


def test_late_funding_owed_by_quantity_before_open(bcfg):
    """Метка 00:00 не пришла за 2 ч — свеча исполнена (вход), метка пришла после 08:00 — платит прежнее кол-во."""
    spot = [(i * DAY, 100.0, 101.0, 99.0, 100.0) for i in range(6)]
    perp = [(i * DAY, 100.0, 101.0, 99.0, 100.0) for i in range(6)]
    c = Client(spot, perp, [(2 * DAY, 0.0), (2 * DAY + 8 * 3600_000, 0.001)])
    bb = basisbook.BasisBook(c, bcfg, params())
    c.now = 1 * DAY + 1
    bb.step(c.now)
    c.now = 2 * DAY + basisbook.FUNDING_WAIT_MS                # вход на open 2 (метка 2·DAY есть)
    bb.step(c.now)
    q = book(bcfg)["qty"]
    perp[3] = (3 * DAY, 110.0, 111.0, 109.0, 110.0)
    spot[3] = (3 * DAY, 110.0, 111.0, 109.0, 110.0)
    c.now = 3 * DAY + basisbook.FUNDING_WAIT_MS               # метки 3·DAY нет — исполняем, долг по ней
    bb.step(c.now)
    b = book(bcfg)
    assert (b["owed_ms"], b["owed_qty"], b["owed_px"]) == (3 * DAY, q, 110.0)
    c.funding += [(3 * DAY + 8 * 3600_000, 0.0), (3 * DAY, 0.002)]   # опоздавшая пришла вместе со следующей
    c.now = 3 * DAY + 9 * 3600_000
    f0 = b["funding"]
    bb.step(c.now)
    b = book(bcfg)
    assert b["funding"] - f0 == pytest.approx(q * 110.0 * 0.002) and b["owed_ms"] is None


def test_rebalance_cost_is_fees_and_slippage_only(bcfg):
    spot, perp, funding = gen()
    c = Client(spot, perp, funding)
    bb = basisbook.BasisBook(c, bcfg, params())
    c.now = spot[9][0] + 3600_000
    bb.step(c.now)
    for k in range(10, 159):
        c.now = spot[k][0] + DAY - 60_000
        bb.step(c.now)
    conn = db.connect(bcfg.db_file)
    rows = conn.execute("SELECT * FROM basis_events WHERE kind = 'rebalance' ORDER BY ts").fetchall()
    prev = {r["ts"]: r for r in conn.execute("SELECT * FROM basis_events WHERE kind IN ('enter', 'rebalance')")}
    conn.close()
    p = params()
    qs = sorted(prev.items())
    for r in rows:
        q_before = [v["qty"] for t, v in qs if t < r["ts"]][-1]
        dq = abs(r["qty"] - q_before)
        want = dq * (r["spot"] * (p.slippage + p.spot_taker_fee) + r["perp"] * (p.slippage + p.perp_taker_fee))
        assert r["amount"] == pytest.approx(-want)


def test_outage_refused_even_without_new_bars(bcfg):
    spot, perp, funding = gen(400)
    c = Client(spot, perp, funding)
    bb = basisbook.BasisBook(c, bcfg, params())
    c.now = spot[100][0] + 1
    bb.step(c.now)
    c.now = spot[350][0] + 60_000                              # новая свеча ещё в BAR_LAG — событий нет
    res = bb.step(c.now)
    assert res.errors and "начать заново" in res.errors[0] and book(bcfg)["funding"] == 0


def test_restarted_book_ignores_old_history(bcfg):
    spot, perp, funding = gen()
    c = Client(spot, perp, funding)
    bb = basisbook.BasisBook(c, bcfg, params())
    c.now = spot[9][0] + 3600_000
    bb.step(c.now)
    for k in range(10, 40):
        c.now = spot[k][0] + DAY - 60_000
        bb.step(c.now)
    conn = db.connect(bcfg.db_file)
    with conn:
        conn.execute("DELETE FROM basis_books")
    conn.close()
    c.now = spot[40][0] + 3600_000
    bb.step(c.now)                                       # начата заново
    r, = reports.basis_rows(spot[20][0], c.now + 1, bcfg)
    assert r["pnl"] == pytest.approx(0) and r["funding"] == 0 and r["costs"] == 0 and r["dd"] == 0


def run_days(cfg, p, n=40):
    spot, perp, funding = gen()
    c = Client(spot, perp, funding)
    bb = basisbook.BasisBook(c, cfg, p)
    c.now = spot[9][0] + 3600_000
    bb.step(c.now)
    for k in range(10, 10 + n):
        c.now = spot[k][0] + DAY - 60_000
        bb.step(c.now)
    return bb, spot, c


def test_sol_observation_book_outside_totals(bcfg):
    bb, spot, c = run_days(bcfg, params(bases=("BTC", "ETH"), observe=("SOL",)))
    assert bb.bases() == ["BTC", "ETH", "SOL"] and "basis:spot_perp:SOL [наблюдение]" in bb.status()
    rows = reports.basis_rows(spot[20][0], c.now + 1, bcfg, rf=0.04)
    by = {r["book"]: r for r in rows}
    assert by["basis:spot_perp:SOL"]["observe"] and not by["basis:spot_perp:BTC"]["observe"]
    assert by["basis:spot_perp:BTC"]["lev"] == pytest.approx(1.0, abs=0.3)
    text = reports.fmt_basis(rows, "x", bb.p)
    tot = next(x for x in text.splitlines() if "Σ базис (без наблюдения)" in x)
    want = by["basis:spot_perp:BTC"]["pnl"] + by["basis:spot_perp:ETH"]["pnl"]
    assert f"PnL {want:+.2f}" in tot and "номинал/капитал" in text and "[наблюдение]" in text


def test_stress_worst_funding_week(bcfg):
    bb, spot, c = run_days(bcfg, params(bases=("BTC", "ETH"), observe=("SOL",)), 5)
    st = reports.stress(bcfg, c.now)
    n, wk, loss, cap, obs = st.basis["basis:spot_perp:SOL"]
    assert wk == -0.256 and obs and loss == pytest.approx(n * -0.256)
    assert st.basis["basis:spot_perp:BTC"][1] == -0.006
    text = reports.fmt_stress(st, bcfg)
    assert "худшая историческая неделя фондирования" in text and "basis:spot_perp:SOL [наблюдение]" in text
    tot = next(x for x in text.splitlines() if x.startswith(" Σ базис"))
    assert f"{st.basis['basis:spot_perp:BTC'][2] + st.basis['basis:spot_perp:ETH'][2]:+,.0f}" in tot


def test_period_without_point_before_starts_from_first_point(bcfg):
    bb, spot, c = run_days(bcfg, params(), 40)
    conn = db.connect(bcfg.db_file)
    with conn:   # бот «не работал» 10 дней до периода: точек кривой нет
        conn.execute("DELETE FROM basis_equity WHERE ts < ?", (spot[30][0],))
    first = conn.execute("SELECT equity FROM basis_equity WHERE ts >= ? ORDER BY ts LIMIT 1", (spot[30][0],)).fetchone()[0]
    conn.close()
    r, = reports.basis_rows(spot[25][0], c.now + 1, bcfg, rf=0.04)              # книга создана до периода
    assert r["pnl"] == pytest.approx(last_eq(bcfg) - first)


def test_new_book_days_from_first_entry(bcfg):
    bb, spot, c = run_days(bcfg, params(), 3)
    r, = reports.basis_rows(spot[0][0], c.now + 1, bcfg, rf=0.04)
    assert r["days"] == pytest.approx((c.now - spot[10][0]) / DAY)      # не от создания книги (за сутки до входа)
