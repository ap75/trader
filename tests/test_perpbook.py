"""Бумажная книга перпетуалов EMA 4h (optbot/perpbook.py): совпадение с движком research, фондирование, отчёты."""
import asyncio
import dataclasses
import math
import random

import pytest

from optbot import db, perpbook
from optbot.config import PaperSettings, Settings
from optbot.fmt import DAY_MS

H4 = 4 * 3600_000


def gen_bars(n=1500, seed=11):
    """Те же синтетические свечи и фондирование, на которых посчитан эталон research (rlab/backtest.run)."""
    rnd = random.Random(seed)
    t0 = 1_700_000_000_000 // H4 * H4
    bars, px = [], 30000.0
    for i in range(n):
        o = px
        c = o * math.exp(0.004 * math.sin(i / 60) + rnd.gauss(0, 0.01))
        h = max(o, c) * (1 + abs(rnd.gauss(0, 0.004)))
        l = min(o, c) * (1 - abs(rnd.gauss(0, 0.004)))
        bars.append((t0 + i * H4, o, h, l, c))
        px = c
    funding = [(t0 + i * 8 * 3600_000, rnd.uniform(-0.0003, 0.0006)) for i in range(n * 4 // 8)]
    return bars, funding


# research: run(bars, ema_cross(bars, 10, 200), funding, start=idx[1001]) — закрытые по сигналу сделки
# [сторона, вход, выход, количество, комиссии, фондирование, PnL]
RESEARCH = [[1, 56636.040668, 52015.559441, 0.04017997, 2.40109, -2.045553, -190.097421],
            [-1, 52015.559441, 44664.882882, 0.03955574, 2.103347, 27.289918, 315.94804],
            [1, 44664.882882, 53272.099053, 0.05077077, 2.734785, -35.274075, 398.986143]]


class Client:
    def __init__(self, bars, funding, limit=1000):
        self.bars, self.funding, self.limit, self.now = bars, funding, limit, 0

    def perp_ohlc(self, base, iv, limit=1000):
        return [b for b in self.bars if b[0] <= self.now][-min(limit, self.limit):]

    def perp_funding_history(self, base, s, e, limit=200):
        return sorted([x for x in self.funding if s <= x[0] <= e], reverse=True)[:limit]


@pytest.fixture
def pcfg(cfg):
    return dataclasses.replace(cfg, base_coins=["BTC"], paper=PaperSettings(enabled=True, bases=("BTC",)))


def run_steps(pb, c, bars, start, end, every=1, lag=perpbook.BAR_LAG_MS + 60_000):
    c.now = bars[start - 1][0] + 1
    pb.step(c.now)                                      # новая книга: первое исполнение — следующая свеча
    for i in range(start, end, every):
        c.now = bars[i][0] + lag
        assert not pb.step(c.now).errors


def trades(cfg):
    conn = db.account(cfg)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM perp_trades ORDER BY id")]
    finally:
        conn.close()


def as_row(t):
    return [t["side"], round(t["entry_px"], 6), round(t["exit_px"], 6), round(t["qty"], 8), round(t["fees"], 6),
            round(t["funding"], 6), round(t["pnl"], 6)]


@pytest.mark.parametrize("every", [1, 7])       # шаг каждую свечу и с пропусками (бот лежал) — итог тот же
def test_matches_research_engine(pcfg, every):
    bars, funding = gen_bars()
    c = Client(bars, funding)
    pb = perpbook.PerpBook(c, pcfg)
    run_steps(pb, c, bars, 1001, len(bars), every)
    if (len(bars) - 1001) % every != 1:          # дойти до последней свечи
        c.now = bars[-1][0] + perpbook.BAR_LAG_MS + 60_000
        pb.step(c.now)
    got = trades(pcfg)
    closed = [as_row(t) for t in got if t["status"] == "closed"]
    assert closed == RESEARCH
    assert got[-1]["status"] == "open" and got[-1]["side"] == -1
    for t in got[:-1]:
        assert t["r_mult"] == pytest.approx(t["pnl"] / t["risk"])


def test_new_book_waits_for_next_bar_and_lag(pcfg):
    bars, funding = gen_bars(1100)
    c = Client(bars, funding)
    pb = perpbook.PerpBook(c, pcfg)
    c.now = bars[1050][0] + 3600_000                   # середина свечи 1050
    pb.step(c.now)
    assert trades(pcfg) == []                          # по уже прошедшему open не входим
    c.now = bars[1051][0] + 60_000                     # open 1051 — меньше BAR_LAG: ещё ждём
    pb.step(c.now)
    assert trades(pcfg) == []
    c.now = bars[1051][0] + perpbook.BAR_LAG_MS
    pb.step(c.now)
    t = trades(pcfg)
    assert len(t) == 1 and t[0]["opened_ms"] == bars[1051][0]
    assert t[0]["entry_px"] == pytest.approx(bars[1051][1] * (1 + 0.0005 * t[0]["side"]))


def test_funding_sign_and_price(pcfg):
    """Лонг при положительной ставке платит qty × open свечи × ставку; метка на open — прежней позицией."""
    p = perpbook.load_params()
    bars = [(i * H4, 100.0 + i * 0.01, 101.0 + i * 0.01, 99.0 + i * 0.01, 100.0 + i * 0.01) for i in range(900)]
    c = Client(bars, [])
    pb = perpbook.PerpBook(c, pcfg, p)
    run_steps(pb, c, bars, 800, 802)
    t = trades(pcfg)[0]
    assert t["side"] == 1                                # рост — лонг
    c.funding = [(bars[802][0] + 3600_000, 0.001)]       # внутри свечи 802
    c.now = bars[802][0] + 2 * 3600_000
    pb.step(c.now)
    t2 = trades(pcfg)[0]
    assert t2["funding"] == pytest.approx(-t["qty"] * bars[802][1] * 0.001)
    assert t2["pnl"] == pytest.approx(-t["fees"] + t2["funding"])


def test_late_funding_goes_to_position_held_then(pcfg):
    """Метка на open свечи опубликована после исполнения этой свечи — платит позиция, державшаяся до неё."""
    bars, _ = gen_bars()
    c = Client(bars, [])
    pb = perpbook.PerpBook(c, pcfg)
    run_steps(pb, c, bars, 1001, 1100)
    rows = trades(pcfg)
    flip = next(r for r in rows if r["opened_ms"] > rows[0]["opened_ms"])     # вторая сделка: смена на этом open
    t = flip["opened_ms"]
    c.funding = [(t, 0.001)]                                                    # пришла поздно
    c.now = bars[1100][0] + perpbook.BAR_LAG_MS + 60_000
    pb.step(c.now)
    prev = next(r for r in trades(pcfg) if r["closed_ms"] == t)
    old = next(r for r in rows if r["id"] == prev["id"])
    i = next(k for k, b in enumerate(bars) if b[0] == t)
    amt = -prev["side"] * prev["qty"] * bars[i][1] * 0.001
    assert prev["funding"] == pytest.approx(old["funding"] + amt)
    assert prev["pnl"] == pytest.approx(old["pnl"] + amt) and prev["r_mult"] == pytest.approx(prev["pnl"] / prev["risk"])
    assert next(r for r in trades(pcfg) if r["id"] == flip["id"])["funding"] == flip["funding"]  # новая не платит


def test_long_outage_refused(pcfg):
    bars, funding = gen_bars()
    c = Client(bars, funding)
    pb = perpbook.PerpBook(c, pcfg)
    c.now = bars[700][0] + 1
    pb.step(c.now)
    c.now = bars[1450][0] + perpbook.BAR_LAG_MS                                # 750 свечей спустя
    res = pb.step(c.now)
    assert res.errors and "начать заново" in res.errors[0] and trades(pcfg) == []


def test_missing_params_keeps_bot(pcfg, monkeypatch, tmp_path):
    monkeypatch.setattr(perpbook, "PARAMS_FILE", str(tmp_path / "none.json"))
    pb = perpbook.PerpBook(Client([], []), pcfg)
    assert pb.p is None and "не прочитаны" in pb.step(1).errors[0] and "выключена" in pb.status()


def test_parallel_step_no_double_trade(pcfg):
    """Два шага одновременно (бот и ручной --step) — сделка одна: состояние читается под блокировкой записи."""
    import threading
    bars, funding = gen_bars(1100)
    c = Client(bars, funding)
    pb1, pb2 = perpbook.PerpBook(c, pcfg), perpbook.PerpBook(c, pcfg)
    c.now = bars[1050][0] + 1
    pb1.step(c.now)
    c.now = bars[1060][0] + perpbook.BAR_LAG_MS
    ths = [threading.Thread(target=x.step, args=(c.now,)) for x in (pb1, pb2)]
    for th in ths:
        th.start()
    for th in ths:
        th.join()
    single = trades(pcfg)
    assert len([r for r in single if r["status"] == "open"]) == 1


def test_indicators():
    assert perpbook.ema([1.0, 2.0, 3.0], 3)[:2] == [None, None]
    e = perpbook.ema([1.0, 2.0, 3.0], 3)[2]
    assert e == pytest.approx(1 + 0.5 * (2 - 1) + 0.5 * (3 - 1.5))      # ewm(span=3, adjust=False)
    a = perpbook.atr([2, 3, 4], [1, 1, 2], [1.5, 2.5, 3], 2)
    assert a[0] is None and a[1] == pytest.approx(1 + 0.5 * (2 - 1)) and a[2] == pytest.approx(1.5 + 0.5 * (2 - 1.5))


def test_daily_pnl_and_corr():
    rows = [dict(ts=d * DAY_MS + h, book="b", v=float(v)) for d, v in enumerate([10, 12, 11, 15, 15, 20, 18])
            for h in (0, 5)]
    pnl = perpbook.daily_pnl(rows, 0)["b"]
    assert pnl == {DAY_MS: 2, 2 * DAY_MS: -1, 3 * DAY_MS: 4, 4 * DAY_MS: 0, 5 * DAY_MS: 5, 6 * DAY_MS: -2}
    assert perpbook.corr(pnl, pnl)[0] == pytest.approx(1) and perpbook.corr(pnl, {k: -v for k, v in pnl.items()})[0] == pytest.approx(-1)
    assert perpbook.corr(pnl, {DAY_MS: 1.0}) == (None, 1)
    assert perpbook.corr(pnl, {k: 1.0 for k in pnl}) == (None, 6)       # постоянный ряд


def test_stats_and_correlation_with_option_books(pcfg):
    bars, funding = gen_bars()
    c = Client(bars, funding)
    pb = perpbook.PerpBook(c, pcfg)
    run_steps(pb, c, bars, 1001, 1200)
    conn = db.account(pcfg)
    eq = conn.execute("SELECT ts, equity FROM perp_equity ORDER BY ts").fetchall()
    with conn:  # опционная книга с тем же дневным ходом, что у перпетуалов (корреляция +1)
        conn.executemany("INSERT INTO book_equity (ts, book, realized, unrealized, open_n, open_risk, im_regular) "
                         "VALUES (?, 'short:bull_put:BTC', ?, 0, 0, 0, 0)", [(r["ts"], r["equity"]) for r in eq])
    conn.close()
    start, end = bars[1001][0], bars[1200][0]
    corrs = pb.correlations(start, end)
    rho, n = corrs["short:bull_put:BTC"]["perp:ema_4h_ls:BTC"]
    assert rho == pytest.approx(1) and n >= 20
    text = perpbook.fmt_stats(pb.closed(start, end), "x", corrs)
    assert "perp:ema_4h_ls:BTC:" in text and "ΣR" in text and "фондирование" in text
    assert "short:bull_put:BTC: +1.00" in text
    assert "лонг" in pb.status() or "шорт" in pb.status()


def test_disabled_and_errors(pcfg):
    off = dataclasses.replace(pcfg, paper=PaperSettings(enabled=False, bases=("BTC",)))
    assert perpbook.PerpBook(Client([], []), off).step(1).opened == []
    res = perpbook.PerpBook(Client([], []), pcfg).step(1)
    assert res.errors and "нет свечей" in res.errors[0]


def test_params_copied_from_research():
    p = perpbook.load_params()
    assert (p.fast, p.slow, p.interval_min, p.atr_period, p.risk_atr_mult) == (10, 200, 240, 20, 3.0)
    assert (p.risk_per_trade, p.max_leverage, p.capital_usdt, p.taker_fee, p.slippage) == (0.01, 2.0, 10000, 0.00055, 0.0005)


def test_bot_runs_perp_after_paper(monkeypatch):
    from optbot.bot import OptionsBot
    calls = []
    bot = OptionsBot.__new__(OptionsBot)
    bot.paper = type("P", (), {"step": lambda self: calls.append("paper") or perpbook.StepResult(0)})()
    bot.perp = type("Q", (), {"step": lambda self: calls.append("perp") or perpbook.StepResult(0)})()
    from optbot.basisbook import StepResult as BasisStep
    bot.basis = type("B", (), {"step": lambda self: BasisStep(0)})()
    asyncio.run(bot._paper_job())
    assert calls == ["paper", "perp"]


def test_client_methods(cfg, make_client, fake):
    from tests.fakes import NOW, kline_rows
    fake.klines[("BTCUSDT", "240")] = kline_rows([100.0, 101.0, 102.0], 240, NOW)
    c = make_client(cfg)
    rows = c.perp_ohlc("BTC", 240, 10)
    assert [r[4] for r in rows] == [100.0, 101.0, 102.0] and rows[0][0] < rows[-1][0]
    fake.funding_hist["BTCUSDT"] = [(NOW - 3 * 8 * 3600_000, 0.0001), (NOW - 8 * 3600_000, -0.0002)]
    assert c.perp_funding_history("BTC", NOW - DAY_MS - 1, NOW) == [(NOW - 8 * 3600_000, -0.0002),
                                                                    (NOW - 3 * 8 * 3600_000, 0.0001)]
