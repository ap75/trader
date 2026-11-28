"""Сигналы (этап 3): метрики, выплата стратегий, таблица выбора, подбор ног, движок на подставном Bybit."""
import dataclasses
import logging
import math
from types import SimpleNamespace

import pytest

from optbot import cli, db
from optbot.config import SignalSettings
from optbot.fmt import DAY_MS
from optbot.signals import (
    Leg, Metrics, NoTrade, SignalEngine, Trend, build_legs, check_economics, choose, classify_iv,
    const_maturity_iv, ema, fmt_signal, leg_issue, payoff, percentile, profile, realized_vol_window, trend_of,
)
from tests.fakes import EXP, EXP_MS, NOW, kline_rows, set_chain, set_klines

SC = SignalSettings()
YEAR_H = 365 * 24


def add_history(cfg, base, ivs, start_ms, step_ms=DAY_MS, dte=7.0):
    conn = db.account(cfg)
    with conn:
        for i, iv in enumerate(ivs):
            conn.execute("INSERT INTO snapshots (ts, base, expiry, expiry_ms, dte, atm_iv) VALUES (?,?,?,?,?,?)",
                         (start_ms + i * step_ms, base, "X", 0, dte, iv))
    conn.close()


# ---------- метрики ----------
def test_const_maturity_iv_total_variance():
    got = const_maturity_iv([(3, 0.5), (10, 0.6)], 7)
    w = 0.25 * 3 + (0.36 * 10 - 0.25 * 3) * (7 - 3) / (10 - 3)
    assert got == pytest.approx(math.sqrt(w / 7))
    assert const_maturity_iv([(10, 0.6), (3, 0.5)], 7) == pytest.approx(got)  # порядок не важен


def test_const_maturity_iv_edges_and_short_expiries():
    assert const_maturity_iv([(10, 0.6), (20, 0.7)], 7) == 0.6     # короче всех — крайняя
    assert const_maturity_iv([(2, 0.5), (4, 0.55)], 7) == 0.55     # длиннее всех — крайняя
    assert const_maturity_iv([(0.3, 2.0), (10, 0.6)], 7) == 0.6    # меньше суток — не участвует
    assert const_maturity_iv([(5, None), (5, 0)], 7) is None
    assert const_maturity_iv([], 7) is None


def hourly(closes, start=0):
    return [(start + i * 3_600_000, c) for i, c in enumerate(closes)]


def test_realized_vol_window():
    a = 0.004
    alt = hourly([100 * (1 + a if i % 2 else 1) for i in range(7 * 24 + 1)])
    assert realized_vol_window(alt, 3_600_000, 7 * DAY_MS) == pytest.approx(math.log(1 + a) * math.sqrt(YEAR_H))
    assert realized_vol_window(hourly([100] * 200), 3_600_000, 7 * DAY_MS) == 0
    assert realized_vol_window([], 3_600_000, 7 * DAY_MS) is None
    assert realized_vol_window(alt[-50:], 3_600_000, 7 * DAY_MS) is None  # покрытие < 80%


def test_realized_vol_window_skips_gaps():
    """Пропуск свечей: доходность через дыру не считается «часовой» (иначе RV завышена)."""
    a = 0.001
    pts = hourly([100 * (1 + a if i % 2 else 1) for i in range(7 * 24 + 1)])
    gap = pts[:80] + [(t, c * 1.05) for t, c in pts[90:]]  # 10 ч нет, затем скачок на 5%
    rv = realized_vol_window(gap, 3_600_000, 7 * DAY_MS)
    assert rv == pytest.approx(math.log(1 + a) * math.sqrt(YEAR_H))  # скачок через дыру не попал


def test_ema_and_trend():
    assert ema([1, 2, 3, 4], 2) == pytest.approx([1.5, 2.5, 3.5])  # начало — SMA, k = 2/3
    assert ema([5] * 10, 3) == [5] * 8
    assert ema([1, 2], 3) == []
    up = [100 * 1.005 ** i for i in range(200)]
    assert trend_of(up, 12, 48, 1).direction == "up"
    assert trend_of(up[::-1], 12, 48, 1).direction == "down"
    flat = [100 * (1.002 if i % 2 else 1) for i in range(200)]
    t = trend_of(flat, 12, 48, 1)
    assert t.direction == "flat" and abs(t.gap_pct) < 1
    assert trend_of(up[:60], 12, 48, 1).direction is None  # мало свечей для разгона EMA


def test_percentile():
    assert percentile([], 1) is None
    assert percentile([1, 2, 3, 4], 5) == 100
    assert percentile([1, 2, 3, 4], 0) == 0
    assert percentile([1, 2, 3, 4], 2) == pytest.approx(37.5)


# ---------- выплата ----------
def L(kind, k, side, price, qty=1.0):
    return Leg(symbol=f"BTC-{EXP}-{k}-{kind}-USDT", kind=kind, strike=k, side=side, qty=qty, bid=price, ask=price)


def test_profile_bull_put_credit():
    legs = [L("P", 60000, -1, 300), L("P", 58000, +1, 100)]  # кредит 200, ширина 2000
    p = profile(legs)
    assert (p.max_profit, p.max_loss) == pytest.approx((200, 1800))
    assert p.breakevens == pytest.approx([59800])
    assert payoff(legs, 70000) == pytest.approx(200)
    assert payoff(legs, 50000) == pytest.approx(-1800)


def test_profile_iron_condor_uses_wider_side():
    legs = [L("P", 60000, -1, 300), L("P", 59000, +1, 100), L("C", 70000, -1, 250), L("C", 72000, +1, 50)]
    p = profile(legs)  # кредит 400; пут-сторона 1000, колл-сторона 2000
    assert p.max_profit == pytest.approx(400)
    assert p.max_loss == pytest.approx(1600)
    assert p.breakevens == pytest.approx([59600, 70400])


def test_profile_debit_spread_and_qty():
    legs = [L("C", 65000, +1, 1200, 0.01), L("C", 67500, -1, 450, 0.01)]
    p = profile(legs)
    assert p.max_loss == pytest.approx(7.5)
    assert p.max_profit == pytest.approx(17.5)
    assert p.breakevens == pytest.approx([65750])


def test_profile_long_straddle_unlimited_upside():
    p = profile([L("C", 65000, +1, 1300), L("P", 65000, +1, 1200)])
    assert math.isinf(p.max_profit)
    assert p.max_loss == pytest.approx(2500)
    assert p.breakevens == pytest.approx([62500, 67500])


def test_profile_short_call_unlimited_loss_and_breakeven_on_strike():
    assert math.isinf(profile([L("C", 65000, -1, 100)]).max_loss)
    # кредит ровно равен ширине — безубыток совпадает со страйком
    p = profile([L("P", 60000, -1, 1000), L("P", 59000, +1, 0)])
    assert p.max_loss == pytest.approx(0)


def test_profile_touching_zero_is_not_breakeven():
    # выплата 0 до 60000, затем растёт — ноль не пересекается
    assert profile([L("C", 60000, +1, 0), L("C", 61000, -1, 0)]).breakevens == []
    # кондор с нулевым итогом: выплата касается 0 на 60000…61000 и уходит в минус с обеих сторон
    legs = [L("P", 60000, -1, 200), L("P", 59000, +1, 0), L("C", 61000, -1, 0), L("C", 62000, +1, 200)]
    assert [payoff(legs, x) for x in (59000, 60000, 61000, 62000)] == [-1000, 0, 0, -1000]
    assert profile(legs).breakevens == []
    # ровно ноль на изломе со сменой знака — безубыток
    assert profile([L("C", 60000, +1, 0), L("P", 60000, -1, 0), L("C", 61000, -1, 0)]).breakevens == [60000]


def test_profile_mid_vs_executable():
    l1 = Leg(symbol="x-1-60000-P", kind="P", strike=60000, side=-1, qty=1, bid=290, ask=310)
    l2 = Leg(symbol="x-1-58000-P", kind="P", strike=58000, side=+1, qty=1, bid=90, ask=110)
    assert profile([l1, l2]).max_profit == pytest.approx(180)                      # 290 − 110
    assert profile([l1, l2], [l1.mid, l2.mid]).max_profit == pytest.approx(200)    # 300 − 100


# ---------- таблица выбора ----------
def M(iv7=0.6, rv7=0.4, pctl=None, enough=False, trend="flat"):
    return Metrics(base="BTC", ts_ms=NOW, iv7=iv7, rv7=rv7, pctl=pctl, enough_history=enough,
                   trend=Trend(trend, 1, 1, 0.0, 0.0))


@pytest.mark.parametrize("m,strategy", [
    (M(0.6, 0.4), "iron_condor"),
    (M(0.6, 0.4, trend="up"), "bull_put"),
    (M(0.6, 0.4, trend="down"), "bear_call"),
    (M(0.35, 0.4, trend="up"), "bull_call"),
    (M(0.35, 0.4, trend="down"), "bear_put"),
    (M(0.35, 0.4), "long_straddle"),
    (M(0.44, 0.4), None),                                  # IV/RV 1.1 — обычная
    (M(0.6, 0.4, pctl=50, enough=True), None),             # IV/RV высокое, но pctl средний
    (M(0.6, 0.4, pctl=80, enough=True), "iron_condor"),
    (M(0.44, 0.4, pctl=10, enough=True, trend="up"), "bull_call"),  # pctl низкий, IV/RV 1.1 — IV низкая
    (M(0.6, 0.4, pctl=10, enough=True, trend="up"), None),   # pctl низкий, но IV/RV 1.5 — премия дорогая
    (M(0.6, 0.4, pctl=10, enough=True), None),             # низкая по истории, но IV > RV — не стрэддл
    (M(0.6, 0.4, pctl=99, enough=False), "iron_condor"),   # мало истории — pctl не используется
    (M(0.6, 0.4, pctl=50, enough=False), "iron_condor"),
    (M(0.35, 0.4, pctl=95, enough=True), None),            # IV < RV, но pctl высокий — скачок RV, не дешёвая IV
    (M(0.35, 0.4, pctl=50, enough=True), "long_straddle"),
    (M(0.35, 0.4, pctl=20, enough=True), "long_straddle"),
    (M(None, 0.4), None),
    (M(0.6, 0.4, trend=None), None),
])
def test_choose_table(m, strategy):
    assert choose(m, SC)[0] == strategy


def test_choose_reason_when_no_iv_but_trend_known():
    s, why = choose(M(None, 0.4, trend="up"), SC)
    assert s is None and why[-1] == "нет IV7 или RV7"


def test_profile_far_right_breakeven():
    # дорогой длинный колл-спред… и непокрытый колл: хвост пересекает ноль далеко правее 2·K
    legs = [L("C", 100, +1, 500)]
    assert profile(legs).breakevens == pytest.approx([600])


def test_choose_normal_iv_reason_not_hidden_by_trend():
    s, why = choose(M(0.44, 0.4, trend=None), SC)
    assert s is None and "обычном диапазоне" in why[-1]


def test_choose_explains():
    s, why = choose(M(0.6, 0.4, pctl=99), SC)
    assert "мало истории" in " ".join(why)
    s, why = choose(M(0.44, 0.4), SC)
    assert s is None and "обычном диапазоне" in why[-1]
    assert classify_iv(M(rv7=0), SC)[0] is None


# ---------- подбор ног ----------
def chain_rows(fake, base="BTC"):
    from optbot.bybit_client import group_by_strike
    return {float(k): v for k, v in group_by_strike(
        [t for s, t in fake.tickers.items() if s.startswith(base + "-")]).items()}


def test_build_iron_condor(fake):
    set_chain(fake)
    ch = chain_rows(fake)
    em = 65000 * 0.5 * math.sqrt(4 / 365)  # ≈ 3400
    legs = build_legs("iron_condor", ch, 65000, em, 0.01, SC)
    sp, lp, sc_, lc = legs
    assert (sp.kind, sp.side, lp.side, sc_.kind, sc_.side, lc.side) == ("P", -1, 1, "C", -1, 1)
    assert abs(abs(sp.delta) - 0.2) < 0.05 and abs(sc_.delta - 0.2) < 0.05
    assert sp.strike - lp.strike >= em * SC.wing_em and lc.strike - sc_.strike >= em * SC.wing_em
    assert sp.strike < 65000 < sc_.strike
    assert all(l.qty == 0.01 for l in legs)


def test_build_debit_spreads_and_straddle(fake):
    set_chain(fake)
    ch = chain_rows(fake)
    lc, scl = build_legs("bull_call", ch, 65000, 3000, 0.01, SC)
    assert lc.side == 1 and scl.side == -1 and scl.strike > lc.strike
    assert abs(lc.delta - 0.45) < 0.05 and abs(scl.delta - 0.25) < 0.05
    lp, sp = build_legs("bear_put", ch, 65000, 3000, 0.01, SC)
    assert lp.side == 1 and sp.side == -1 and sp.strike < lp.strike
    c, p = build_legs("long_straddle", ch, 65000, 3000, 0.01, SC)
    assert (c.kind, p.kind, c.strike, p.strike, c.side, p.side) == ("C", "P", 65000, 65000, 1, 1)


def test_build_no_wing_strike(fake):
    set_chain(fake, n=3)  # цепочка ±1500: крыло на 0.5 × 20000 не найти
    with pytest.raises(NoTrade, match="крыла"):
        build_legs("bull_put", chain_rows(fake), 65000, 20000, 0.01, SC)
    with pytest.raises(NoTrade, match="ATM"):
        build_legs("long_straddle", chain_rows(fake), 12345, 100, 0.01, SC)


def test_build_skips_illiquid_strike(fake):
    set_chain(fake)
    ch = chain_rows(fake)
    best = build_legs("bull_put", ch, 65000, 3400, 0.01, SC, 65000)[0].strike
    fake.tickers[f"BTC-{EXP}-{int(best)}-P-USDT"]["openInterest"] = "0.01"   # лучшая по дельте — неликвидна
    legs = build_legs("bull_put", chain_rows(fake), 65000, 3400, 0.01, SC, 65000)
    assert legs[0].strike != best and abs(legs[0].strike - best) == 500     # соседний страйк
    assert all(leg_issue(l, 65000, SC) is None for l in legs)
    # без forward — только по дельте (неликвидная нога вернётся, причину покажет leg_issue)
    assert build_legs("bull_put", chain_rows(fake), 65000, 3400, 0.01, SC)[0].strike == best


def test_build_wing_skips_illiquid(fake):
    set_chain(fake)
    ch = chain_rows(fake)
    sp, lp = build_legs("bull_put", ch, 65000, 3400, 0.01, SC, 65000)
    fake.tickers[f"BTC-{EXP}-{int(lp.strike)}-P-USDT"]["bid1Price"] = "0"
    sp2, lp2 = build_legs("bull_put", chain_rows(fake), 65000, 3400, 0.01, SC, 65000)
    assert sp2.strike == sp.strike and lp2.strike == lp.strike - 500


def test_leg_issue():
    ok = Leg(symbol="BTC-20NOV26-60000-P-USDT", kind="P", strike=60000, side=1, qty=0.01, bid=95, ask=105, oi=1)
    assert leg_issue(ok, 65000, SC) is None
    assert "нет bid/ask" in leg_issue(dataclasses.replace(ok, bid=0), 65000, SC)
    assert "спред" in leg_issue(dataclasses.replace(ok, bid=50, ask=150), 65000, SC)
    assert "OI" in leg_issue(dataclasses.replace(ok, oi=0.1), 65000, SC)
    assert "нет bid/ask" in leg_issue(dataclasses.replace(ok, bid=float("nan")), 65000, SC)
    assert "OI" in leg_issue(dataclasses.replace(ok, oi=float("nan")), 65000, SC)
    assert "ask" in leg_issue(dataclasses.replace(ok, bid=110, ask=100), 65000, SC)  # перекрёстная котировка


def test_check_economics():
    check_economics("bull_put", [L("P", 60000, -1, 300), L("P", 58000, +1, 100)], SC)  # 200 ≥ 10% × 2000
    with pytest.raises(NoTrade, match=r"кредит 50(\.00)? = 2\.5% ширины 2000 < 10%"):   # кредит и ширина — в причине
        check_economics("bull_put", [L("P", 60000, -1, 150), L("P", 58000, +1, 100)], SC)
    with pytest.raises(NoTrade, match="кредита нет.*кредит -50.*, ширина 2000"):
        check_economics("bull_put", [L("P", 60000, -1, 100), L("P", 58000, +1, 150)], SC)
    check_economics("bull_call", [L("C", 65000, 1, 1000), L("C", 67000, -1, 300)], SC)    # 700 ≤ 60% × 2000
    with pytest.raises(NoTrade, match=r"дебет 1300(\.0+)? = 65\.0% ширины 2000 > 60%"):
        check_economics("bull_call", [L("C", 65000, 1, 1500), L("C", 67000, -1, 200)], SC)
    check_economics("long_straddle", [L("C", 65000, 1, 1500), L("P", 65000, 1, 1500)], SC)
    check_economics("long_straddle", [L("C", 65000, 1, 1500), L("P", 65000, 1, 1500)], SC, fair_rv=3000)
    with pytest.raises(NoTrade, match="справедливой цены по RV7"):
        check_economics("long_straddle", [L("C", 65000, 1, 1600), L("P", 65000, 1, 1500)], SC, fair_rv=3000)


# ---------- движок ----------
def engine(make_client, cfg, **sig):
    cfg = dataclasses.replace(cfg, sig=dataclasses.replace(cfg.sig, **sig)) if sig else cfg
    return SignalEngine(make_client(cfg), cfg)


def test_engine_iron_condor_on_high_iv(make_client, cfg, fake):
    set_chain(fake, iv=0.6)
    set_klines(fake, rv=0.4)
    sig = engine(make_client, cfg).compute("btc", NOW)
    m = sig.metrics
    assert m.iv7 == pytest.approx(0.6)
    assert m.rv7 == pytest.approx(0.4, rel=0.01) and m.rv14 == pytest.approx(0.4, rel=0.01)
    assert m.trend.direction == "flat" and not m.enough_history and m.pctl is None
    assert sig.strategy == "iron_condor" and len(sig.legs) == 4 and sig.expiry == EXP
    assert sig.dte == pytest.approx(4) and sig.qty == 0.01
    assert sig.net > 0 and sig.net_mid > sig.net                # кредит; по mid лучше
    assert sig.profile.max_loss > 0 and len(sig.profile.breakevens) == 2
    assert sig.em == pytest.approx(65000 * 0.6 * math.sqrt(4 / 365))
    text = fmt_signal(sig, "demo")
    for s in ("Железный кондор", "SELL", "BUY", "Кредит", "Макс. риск", "Безубыток", "мало истории", "[demo]"):
        assert s in text


def test_engine_trend_and_low_iv(make_client, cfg, fake):
    set_chain(fake, iv=0.35)
    set_klines(fake, rv=0.5, trend_per_bar=0.005)
    sig = engine(make_client, cfg).compute("BTC", NOW)
    assert sig.metrics.trend.direction == "up"
    assert sig.strategy == "bull_call" and sig.net < 0
    assert "Дебет" in fmt_signal(sig, "demo")


def test_engine_uses_history_percentile(make_client, cfg, fake):
    set_chain(fake, iv=0.6)
    set_klines(fake, rv=0.4)
    n = 15 * 96  # 15 дней срезов по 15 мин; IV 0.6 — дешевле всей истории
    add_history(cfg, "BTC", [0.7 + i * 1e-5 for i in range(n)], NOW - 15 * DAY_MS, step_ms=900_000)
    sig = engine(make_client, cfg).compute("BTC", NOW)
    m = sig.metrics
    assert m.enough_history and m.history_n == n and m.pctl == 0
    assert sig.strategy is None  # IV/RV высокое, но по истории IV низкая и выше RV — сигнала нет
    assert "Сигнала нет" in fmt_signal(sig, "demo")


def test_engine_history_gap_is_not_enough(make_client, cfg, fake):
    set_chain(fake, iv=0.6)
    set_klines(fake, rv=0.4)
    # размах 30 дней, но всего 40 срезов по 15 мин (10 ч) — истории мало
    add_history(cfg, "BTC", [0.7] * 20, NOW - 30 * DAY_MS, step_ms=900_000)
    add_history(cfg, "BTC", [0.7] * 20, NOW - DAY_MS, step_ms=900_000)
    m = engine(make_client, cfg).compute("BTC", NOW).metrics
    assert m.history_n == 40 and m.history_days == pytest.approx(10 / 24) and not m.enough_history


def test_engine_stale_history_not_used(make_client, cfg, fake):
    set_chain(fake, iv=0.6)
    set_klines(fake, rv=0.4)
    # 20 дней плотной истории, но закончилась 2 дня назад (сборщик стоит)
    add_history(cfg, "BTC", [0.9] * (20 * 96), NOW - 22 * DAY_MS, step_ms=900_000)
    sig = engine(make_client, cfg).compute("BTC", NOW)
    m = sig.metrics
    assert m.history_days == pytest.approx(20) and m.history_stale and not m.enough_history
    assert sig.strategy == "iron_condor"                     # решение — по IV/RV, а не по устаревшему pctl
    text = fmt_signal(sig, "demo")
    assert "история устарела" in text and "последний срез" in text


def test_engine_history_counts_hours_not_rows(make_client, cfg, fake):
    set_chain(fake, iv=0.6)
    set_klines(fake, rv=0.4)
    add_history(cfg, "BTC", [0.7] * 60, NOW - 3_600_000, step_ms=60_000)  # 60 ручных срезов за час — это 1 час
    m = engine(make_client, cfg).compute("BTC", NOW).metrics
    assert m.history_n == 60 and m.history_days == pytest.approx(1 / 24)


def test_history_cache_reads_only_new_slots(make_client, cfg, fake, monkeypatch):
    set_chain(fake, iv=0.6)
    set_klines(fake, rv=0.4)
    add_history(cfg, "BTC", [0.5] * 10, NOW - 10 * 900_000, step_ms=900_000)
    e = engine(make_client, cfg)
    calls = []
    import optbot.signals as sm
    real = sm.iv7_history
    monkeypatch.setattr(sm, "iv7_history", lambda p, b, since, t: calls.append(since) or real(p, b, since, t))
    assert e.compute("BTC", NOW).metrics.history_n == 10
    add_history(cfg, "BTC", [0.55], NOW, step_ms=900_000)                   # сборщик дописал срез
    assert e.compute("BTC", NOW + 60_000).metrics.history_n == 11
    assert calls[1] == NOW - 900_000 - 2 * 3_600_000                        # дочитан только хвост (2 ч)
    assert e.compute("BTC", NOW + 61 * DAY_MS).metrics.history_n == 0       # окно ушло — всё выпало


def test_engine_reloads_instruments_for_new_strike(make_client, cfg, fake):
    set_chain(fake, iv=0.6)
    set_klines(fake, rv=0.4)
    e = engine(make_client, cfg)
    e.compute("BTC", NOW)
    n = len(fake.called("get_instruments_info"))
    e.compute("BTC", NOW)
    assert len(fake.called("get_instruments_info")) == n                    # справочник из кэша
    sym = f"BTC-{EXP}-99000-C-USDT"                                          # новый страйк на бирже
    fake.tickers[sym] = dict(fake.tickers[f"BTC-{EXP}-65000-C-USDT"], symbol=sym)
    e.compute("BTC", NOW)
    assert len(fake.called("get_instruments_info")) == n + 1                 # перечитан


def test_engine_straddle_rejected_when_dear_vs_rv(make_client, cfg, fake):
    set_klines(fake, rv=0.36)
    set_chain(fake, iv=0.35, spread=0.15)            # ask ≈ 0.35·1.075 ≈ 0.376 «по IV» > справедливой по RV 0.36
    sig = engine(make_client, cfg).compute("BTC", NOW)
    assert sig.strategy == "long_straddle" and not sig.legs and "справедливой цены по RV7" in sig.rejected[0]
    set_chain(fake, iv=0.35, spread=0.02)
    assert engine(make_client, cfg).compute("BTC", NOW).legs


def test_engine_qty_from_chosen_legs(make_client, cfg, fake):
    set_chain(fake, iv=0.6)
    set_klines(fake, rv=0.4)
    far = next(i for i in fake.instruments if i["symbol"] == f"BTC-{EXP}-55000-P-USDT")
    far["lotSizeFilter"] = dict(far["lotSizeFilter"], minOrderQty="0.1")  # дальний страйк не в сделке
    sig = engine(make_client, cfg).compute("BTC", NOW)
    assert sig.legs and all("55000" not in l.symbol for l in sig.legs)
    assert sig.qty == 0.01 and all(l.qty == 0.01 for l in sig.legs)


def test_engine_forced_reload_throttled(make_client, cfg, fake):
    set_chain(fake, iv=0.6)
    set_klines(fake, rv=0.4)
    sym = f"BTC-{EXP}-99000-C-USDT"   # тикер есть, а в справочнике (Trading) его нет никогда — как при поставке
    fake.tickers[sym] = dict(fake.tickers[f"BTC-{EXP}-65000-C-USDT"], symbol=sym)
    e = engine(make_client, cfg)
    e.compute("BTC", NOW)
    n = len(fake.called("get_instruments_info"))
    e.compute("BTC", NOW)
    e.compute("BTC", NOW)
    assert len(fake.called("get_instruments_info")) == n  # не перечитывается на каждом расчёте


def test_history_cache_picks_late_written_slot(make_client, cfg, fake):
    set_chain(fake, iv=0.6)
    set_klines(fake, rv=0.4)
    add_history(cfg, "BTC", [0.5] * 4, NOW - 4 * 900_000, step_ms=900_000)
    e = engine(make_client, cfg)
    assert e.compute("BTC", NOW).metrics.history_n == 4
    add_history(cfg, "BTC", [0.5], NOW - 4 * 900_000 + 300_000)   # срез с более ранним ts записан позже
    assert e.compute("BTC", NOW).metrics.history_n == 5


def test_engine_forced_reload_retried_after_failure(make_client, cfg, fake):
    import requests
    set_chain(fake, iv=0.6)
    set_klines(fake, rv=0.4)
    e = engine(make_client, cfg)
    e.compute("BTC", NOW)
    sym = f"BTC-{EXP}-99000-C-USDT"
    fake.tickers[sym] = dict(fake.tickers[f"BTC-{EXP}-65000-C-USDT"], symbol=sym)
    fake.fail["get_instruments_info"] = requests.exceptions.ConnectionError("down")
    with pytest.raises(Exception):
        e.compute("BTC", NOW)
    n = len(fake.called("get_instruments_info"))
    e.compute("BTC", NOW)
    assert len(fake.called("get_instruments_info")) == n + 1  # сбой не заблокировал повтор на TTL


def test_engine_rejects_qty_not_multiple_of_step(make_client, cfg, fake):
    set_chain(fake, iv=0.6)
    set_klines(fake, rv=0.4)
    legs = engine(make_client, cfg).compute("BTC", NOW).legs
    lots = {legs[0].symbol: ("0.03", "0.03"), legs[1].symbol: ("0.02", "0.02")}  # общий лот 0.03 не кратен 0.02
    for i in fake.instruments:
        if i["symbol"] in lots:
            step, mn = lots[i["symbol"]]
            i["lotSizeFilter"] = {"qtyStep": step, "minOrderQty": mn, "maxOrderQty": "100"}
    sig = engine(make_client, cfg).compute("BTC", NOW)
    assert not sig.legs and "не кратен шагу" in sig.rejected[0]


def test_history_cache_reloads_for_earlier_window(make_client, cfg, fake):
    set_chain(fake, iv=0.6)
    set_klines(fake, rv=0.4)
    add_history(cfg, "BTC", [0.5] * 4, NOW - 4 * 900_000, step_ms=900_000)
    e = engine(make_client, cfg)
    assert e.compute("BTC", NOW).metrics.history_n == 4
    add_history(cfg, "BTC", [0.5] * 3, NOW - 3 * DAY_MS, step_ms=900_000)    # старые срезы задним числом
    assert e.compute("BTC", NOW).metrics.history_n == 4                        # окно то же — кэш
    assert e.history.get("BTC", NOW - 61 * DAY_MS) and len(e.history.get("BTC", NOW - 61 * DAY_MS)) == 7  # раньше — заново


def test_card_small_prices_not_rounded(make_client, cfg, fake):
    set_chain(fake, base="SOL", F=150.0, iv=0.6, step=2, oi="100000", min_qty="1")
    set_klines(fake, base="SOL", rv=0.4)
    sig = engine(make_client, cfg).compute("SOL", NOW)
    assert sig.legs
    text = fmt_signal(sig, "demo")
    assert f"±{sig.em:.2f}" in text and "F 150" in text


def test_engine_kline_limit_with_slow_ema(make_client, cfg, fake):
    set_chain(fake, iv=0.6)
    set_klines(fake, rv=0.4)
    engine(make_client, cfg, ema_slow=300).compute("BTC", NOW)
    assert max(kw["limit"] for kw in fake.called("get_kline")) <= 1000


def test_engine_history_outside_lookback_ignored(make_client, cfg, fake):
    set_chain(fake, iv=0.6)
    set_klines(fake, rv=0.4)
    add_history(cfg, "BTC", [0.9] * 5, NOW - 100 * DAY_MS)
    add_history(cfg, "ETH", [0.9] * 5, NOW - 5 * DAY_MS)  # другой актив
    m = engine(make_client, cfg).compute("BTC", NOW).metrics
    assert m.history_n == 0 and m.pctl is None


def test_engine_no_expiry_in_window(make_client, cfg, fake):
    set_chain(fake, iv=0.6)
    set_klines(fake, rv=0.4, now=EXP_MS - 10 * DAY_MS)
    sig = engine(make_client, cfg).compute("BTC", EXP_MS - 10 * DAY_MS)
    assert sig.strategy == "iron_condor" and not sig.legs
    assert "нет экспирации в окне" in sig.no_signal
    assert "не собрать" in fmt_signal(sig, "demo")


def test_engine_illiquid_legs_rejected(make_client, cfg, fake):
    set_chain(fake, iv=0.6, oi="0.1")
    set_klines(fake, rv=0.4)
    sig = engine(make_client, cfg).compute("BTC", NOW)
    assert not sig.legs and sig.rejected and "OI" in sig.rejected[0]
    assert "✗" in fmt_signal(sig, "demo")
    assert engine(make_client, cfg, min_oi_usd=0).compute("BTC", NOW).legs  # порог из настроек


def test_engine_without_klines(make_client, cfg, fake):
    set_chain(fake, iv=0.6)
    fake.klines[("BTCUSDT", "60")] = []
    fake.klines[("BTCUSDT", "240")] = []
    sig = engine(make_client, cfg).compute("BTC", NOW)
    assert sig.metrics.rv7 is None and sig.strategy is None and sig.no_signal == "нет IV7 или RV7"


def test_engine_unknown_base(make_client, cfg):
    with pytest.raises(ValueError, match="BASE_COINS"):
        engine(make_client, cfg).compute("DOGE", NOW)


def test_perp_closes_drops_open_candle(client, fake):
    fake.klines[("BTCUSDT", "60")] = kline_rows([1, 2, 3, 4], 60, NOW)
    got = client.perp_closes("BTC", 60, 10)
    assert [c for _, c in got] == [1, 2, 3]                    # 4 — текущая незакрытая
    assert got == sorted(got)
    assert [c for _, c in client.perp_closes("BTC", 60, 2)] == [2, 3]
    # первая строка Bybit отбрасывается всегда — даже если по своим часам она «закрыта» (часы спешат)
    fake.klines[("BTCUSDT", "60")] = kline_rows([1, 2, 3, 4], 60, NOW - 3_600_000)
    assert [c for _, c in client.perp_closes("BTC", 60, 10)] == [1, 2, 3]
    assert fake.called("get_kline")[1]["limit"] == 3  # limit + 1: незакрытая отбрасывается


# ---------- настройки ----------
def test_sig_settings_swapped_pairs_fall_back(caplog):
    with caplog.at_level(logging.WARNING, "optbot.config"):
        s = SignalSettings(ema_fast=50, ema_slow=20, pctl_low=40, pctl_high=40, dte_min=5, dte_max=5)
    assert (s.ema_fast, s.ema_slow, s.pctl_low, s.pctl_high) == (12, 48, 30, 70)
    assert (s.dte_min, s.dte_max) == (5, 5)  # равные границы окна допустимы
    assert "SIG_EMA_FAST" in caplog.text
    s = SignalSettings(min_history_days=30, lookback_days=7)  # история не может набраться — оба по умолчанию
    assert (s.min_history_days, s.lookback_days) == (14, 60)
    s = SignalSettings(min_history_days=60, lookback_days=60)  # без запаса на пропуски сбора — тоже
    assert (s.min_history_days, s.lookback_days) == (14, 60)
    assert SignalSettings(min_history_days=48, lookback_days=60).min_history_days == 48  # 80% — допустимо


def test_sig_pair_resets_only_env_value(monkeypatch):
    monkeypatch.setenv("SIG_LOOKBACK_DAYS", "15")   # задан только он; 14 × 1.25 > 15
    s = SignalSettings(lookback_days=15)
    assert (s.min_history_days, s.lookback_days) == (14, 60)
    monkeypatch.delenv("SIG_LOOKBACK_DAYS")
    monkeypatch.setenv("SIG_DTE_MIN", "5")          # задан только DTE_MIN, DTE_MAX по умолчанию 7 — пара в порядке
    monkeypatch.setenv("SIG_DTE_MAX", "")
    s = SignalSettings(dte_min=8, dte_max=20)         # пользователь: min 8 (env), max 20 по умолчанию — согласовано
    assert (s.dte_min, s.dte_max) == (8, 20)
    s = SignalSettings(dte_min=10, dte_max=7)         # min из env испорчен — сбрасывается только он
    assert (s.dte_min, s.dte_max) == (2, 7)
    monkeypatch.delenv("SIG_DTE_MIN")
    monkeypatch.setenv("SIG_DTE_MAX", "1")            # задан только DTE_MAX и он меньше DTE_MIN=3
    s = SignalSettings(dte_min=3, dte_max=1)
    assert (s.dte_min, s.dte_max) == (3, 7)           # DTE_MIN сохранён, сброшен только DTE_MAX


def test_sig_target_outside_window_warns(caplog):
    with caplog.at_level(logging.WARNING, "optbot.config"):
        s = SignalSettings(target_days=30)
    assert s.target_days == 30 and "SIG_TARGET_DAYS" in caplog.text  # не сброс — только предупреждение


def test_settings_share_one_sig_instance():
    from optbot.config import Settings
    assert Settings().sig is Settings().sig  # SIG_* разбираются (и предупреждают) один раз


def test_sig_env_parsing(monkeypatch, caplog):
    from optbot import config
    monkeypatch.setenv("X_SIG", "0,25")
    assert config._float_in("X_SIG", 0.2, 0.05, 0.45) == 0.25
    monkeypatch.setenv("X_SIG", "abc")
    with caplog.at_level(logging.WARNING, "optbot.config"):
        assert config._float_in("X_SIG", 0.2, 0.05, 0.45) == 0.2
    monkeypatch.setenv("X_SIG", "9")
    assert config._float_in("X_SIG", 0.2, 0.05, 0.45) == 0.2


# ---------- CLI ----------
def test_cli_signals(client, fake, capsys, monkeypatch):
    set_chain(fake, iv=0.6)
    set_klines(fake, rv=0.4, now=NOW)
    monkeypatch.setattr("optbot.signals.time", SimpleNamespace(time=lambda: NOW / 1000, monotonic=__import__("time").monotonic))
    cli.run(cli.build_parser().parse_args(["signals", "BTC"]), client)
    assert "Железный кондор" in capsys.readouterr().out
    assert fake.called("place_order") == []
