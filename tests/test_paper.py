"""Бумажный движок (E1): издержки, размер, вход, выходы, поставка, календарь, длинный горизонт, тег event."""
import dataclasses
import json
import math
from datetime import datetime, timezone

import pytest

from optbot import db, paper
from optbot.config import PaperSettings, SignalSettings
from optbot.events import Calendar, parse
from optbot.fmt import DAY_MS, HOUR_MS
from optbot.paper import PaperEngine, book_id, delivery_fee, fill_price, im_regular, trade_fee
from optbot.signals import Metrics, Signal, SignalEngine, Trend, make_leg, profile
from tests.fakes import EXP, EXP_MS, NOW, set_chain, set_klines

FAR = "25DEC26"
FAR_MS = int(datetime(2026, 12, 25, 8, tzinfo=timezone.utc).timestamp() * 1000)
PC = PaperSettings(enabled=True, bases=("BTC", "ETH", "SOL"), risk_usdt=100, max_open_per_book=5,
                   max_total_risk_usdt=3000, slippage_pct=1, fee_rate=0.0003, fee_cap_pct=7,
                   delivery_fee_rate={"BTC": 0.00015, "ETH": 0.00015, "SOL": 0.0002}, delivery_fee_cap_pct=12.5,
                   take_pct=50, stop_pct=50, exit_days_short=0, exit_days_long=7, calendar_exit_h=18,
                   calendar_take_pct=35, im_short_pct=10, im_min_pct=5)


@pytest.fixture
def pcfg(cfg, tmp_path):
    ev = tmp_path / "events.csv"
    ev.write_text("# пусто\n")
    return dataclasses.replace(cfg, paper=PC, events_path=str(ev), journal_enabled=True,
                               max_order_qty={"BTC": 0, "ETH": 0, "SOL": 0},
                               max_position={"BTC": 0, "ETH": 0, "SOL": 0})


def eng(make_client, cfg, **pc):
    cfg = dataclasses.replace(cfg, paper=dataclasses.replace(cfg.paper, **pc)) if pc else cfg
    return PaperEngine(make_client(cfg), cfg)


def sym(k, cp, exp=EXP):
    return f"BTC-{exp}-{k}-{cp}-USDT"


def mksig(fake, strategy, legs, horizon="short", expiry=EXP, expiry_ms=EXP_MS, far=None, far_ms=None, em=3000.0,
          now=NOW, qty=0.01):
    ls = [make_leg(fake.tickers[s], side, qty) for s, side in legs]
    m = Metrics(base="BTC", ts_ms=now, horizon=horizon, trend=Trend("flat"))
    sig = Signal(metrics=m, strategy=strategy, reasons=["тест"], regime="high", horizon=horizon, expiry=expiry,
                 expiry_ms=expiry_ms, dte=(expiry_ms - now) / DAY_MS, forward=65000.0, em=em, qty=qty, legs=ls,
                 far_expiry=far, far_expiry_ms=far_ms)
    sig.profile = profile(ls) if strategy != "calendar" else None
    return sig


def condor(fake):
    return mksig(fake, "iron_condor", [(sym(60000, "P"), +1), (sym(62000, "P"), -1),
                                       (sym(68000, "C"), -1), (sym(70000, "C"), +1)])


def bull_put(fake, **kw):
    return mksig(fake, "bull_put", [(sym(62000, "P"), -1), (sym(60000, "P"), +1)], **kw)


def trades(cfg, where="1=1"):
    conn = db.connect(cfg.db_file)
    try:
        return [dict(r) for r in conn.execute(f"SELECT * FROM paper_trades WHERE {where} ORDER BY id")]
    finally:
        conn.close()


def put_marks(cfg, ts, quotes, underlying=65000.0):
    """quotes: {symbol: (bid, ask)} или (bid, ask, mark)."""
    conn = db.connect(cfg.db_file)
    with conn:
        for s, q in quotes.items():
            bid, ask = q[0], q[1]
            mark = q[2] if len(q) > 2 else ((bid or 0) + (ask or 0)) / 2
            conn.execute("INSERT OR REPLACE INTO leg_marks (ts, symbol, mark, bid, ask, underlying, captured_ms) "
                         "VALUES (?,?,?,?,?,?,?)", (ts, s, mark, bid, ask, underlying, ts))
    conn.close()


def step(pe, now, *sigs):
    by = {}
    for s in sigs:
        by.setdefault(s.metrics.base, {})[book_id(s.horizon, s.strategy, s.metrics.base)] = s
    return pe.step(now, by)


# ---------- издержки ----------
def test_fill_price_slippage():
    assert fill_price(PC, +1, 90, 100) == pytest.approx(101)
    assert fill_price(PC, -1, 90, 100) == pytest.approx(89.1)
    assert fill_price(PC, +1, 90, 0) is None              # купить не по чему
    assert fill_price(PC, -1, None, 100) == 0              # длинную без bid — по 0


def test_trade_fee_cap_and_delivery_fee():
    assert trade_fee(PC, 65000, 1000, 0.1) == pytest.approx(0.0003 * 65000 * 0.1)    # ставка от индекса
    assert trade_fee(PC, 65000, 50, 0.1) == pytest.approx(0.07 * 50 * 0.1)           # потолок 7% премии
    assert delivery_fee(PC, "BTC", "C", 70000, 65000, 1) == 0                        # вне денег
    assert delivery_fee(PC, "BTC", "C", 60000, 65000, 1) == pytest.approx(0.00015 * 65000)
    assert delivery_fee(PC, "BTC", "C", 64990, 65000, 1) == pytest.approx(0.125 * 10)  # потолок внутренней
    assert delivery_fee(PC, "SOL", "P", 100, 90, 2) == pytest.approx(min(0.0002 * 90, 0.125 * 10) * 2)


def test_im_regular_short_leg_only():
    legs = [dict(symbol="s", kind="C", strike=67000, side=-1), dict(symbol="l", kind="C", strike=67500, side=+1)]
    # OTM 2000: max(10%·65000 − 2000, 5%·65000) = 4500; + mark 300; купленная — 0
    assert im_regular(PC, legs, {"s": 300, "l": 200}, 65000, 0.01) == pytest.approx((300 + 4500) * 0.01)
    # далеко вне денег — нижняя граница 5%·S
    far = [dict(symbol="s", kind="P", strike=50000, side=-1)]
    assert im_regular(PC, far, {"s": 10}, 65000, 1) == pytest.approx(10 + 3250)


def test_delivery_fee_rate_per_base(monkeypatch):
    from optbot import config
    monkeypatch.setenv("PAPER_DELIVERY_FEE_RATE_ETH", "0.0001")
    got = config._per_base("PAPER_DELIVERY_FEE_RATE", 0.00015, 0, 0.01, {"SOL": 0.0002})
    assert got["BTC"] == 0.00015 and got["ETH"] == 0.0001 and got["SOL"] == 0.0002


# ---------- вход и размер ----------
def test_entry_sizes_by_risk_budget_and_tracks_legs(make_client, pcfg, fake):
    set_chain(fake)
    pe = eng(make_client, pcfg)
    sig = condor(fake)
    res = step(pe, NOW, sig)
    assert len(res.opened) == 1, res
    t = trades(pcfg)[0]
    fills = [fill_price(PC, l.side, l.bid, l.ask) for l in sig.legs]
    unit = [dataclasses.replace(l, qty=1.0) for l in sig.legs]
    risk1 = profile(unit, fills).max_loss + sum(trade_fee(PC, 65000, f, 1) for f in fills)
    assert t["qty"] == pytest.approx(math.floor(100 / risk1 / 0.01) * 0.01)
    assert t["max_loss"] == pytest.approx(risk1 * t["qty"]) and t["max_loss"] <= 100
    assert t["entry_net"] > 0 and t["entry_fees"] > 0 and t["status"] == "open" and t["qty_cap"] is None
    assert t["im_regular"] > 0 and t["im_pm"] is None
    assert json.loads(t["metrics"])["pctl_used"] is False
    conn = db.connect(pcfg.db_file)
    tracked = {r["symbol"]: r["reason"] for r in conn.execute("SELECT * FROM tracked_symbols")}
    assert set(tracked) == {l.symbol for l in sig.legs} and set(tracked.values()) == {f"book:{t['id']}"}
    # с фильтром и без × (короткий: RV и HAR — 6 стратегий × 2; длинный: 7)
    # 3 актива × (6 коротких × RV/HAR + 7 длинных) + у BTC/ETH 6 длинных :har30 — × с фильтром и без
    assert conn.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 2 * (3 * (6 * 2 + 7) + 2 * 6)
    conn.close()


def test_one_trade_per_expiry(make_client, pcfg, fake):
    set_chain(fake)
    pe = eng(make_client, pcfg)
    step(pe, NOW, condor(fake))
    assert not step(pe, NOW + 900_000, condor(fake)).opened
    assert len(trades(pcfg)) == 1


def test_qty_capped_by_limits_marked(make_client, pcfg, fake):
    set_chain(fake)
    from decimal import Decimal
    cfg = dataclasses.replace(pcfg, max_order_qty={"BTC": Decimal("0.05")}, max_position={"BTC": Decimal("0.2")})
    pe = PaperEngine(make_client(cfg), cfg)
    step(pe, NOW, condor(fake))
    t = trades(cfg)[0]
    assert t["qty"] == pytest.approx(0.05) and t["qty_cap"] == "MAX_QTY_BTC=0.05" and t["qty_budget"] > 0.05


def test_below_min_lot_skipped_once(make_client, pcfg, fake):
    set_chain(fake)
    pe = eng(make_client, pcfg, risk_usdt=1)
    r1 = step(pe, NOW, condor(fake))
    r2 = step(pe, NOW + 900_000, condor(fake))
    assert not trades(pcfg) and len(r1.skipped) == 1 and "мин. лота" in r1.skipped[0] and not r2.skipped
    conn = db.connect(pcfg.db_file)
    assert conn.execute("SELECT code FROM paper_skips").fetchone()["code"] == "qty"
    conn.close()


def test_max_open_per_book_and_total_risk(make_client, pcfg, fake):
    set_chain(fake)
    set_chain(fake, exp=FAR, exp_ms=FAR_MS, keep=True)
    pe = eng(make_client, pcfg, max_open_per_book=1)
    step(pe, NOW, bull_put(fake))
    far = mksig(fake, "bull_put", [(sym(62000, "P", FAR), -1), (sym(60000, "P", FAR), +1)],
                expiry=FAR, expiry_ms=FAR_MS)
    r = step(pe, NOW + 900_000, far)
    assert not r.opened and "PAPER_MAX_OPEN_PER_BOOK" in r.skipped[0]
    pe = eng(make_client, pcfg, max_total_risk_usdt=150)
    r = step(pe, NOW + 1_800_000, condor(fake))
    assert not r.opened and "PAPER_MAX_TOTAL_RISK_USDT" in r.skipped[0]


# ---------- выходы ----------
def _legs(cfg):
    t = trades(cfg)[0]
    return t, json.loads(t["legs"])


def test_take_profit_exit(make_client, pcfg, fake):
    set_chain(fake)
    pe = eng(make_client, pcfg)
    step(pe, NOW, bull_put(fake))
    t, legs = _legs(pcfg)
    put_marks(pcfg, NOW + 900_000, {l["symbol"]: (0.01, 0.02) for l in legs})   # спред почти ничего не стоит
    res = pe.step(NOW + 900_000, {})
    t2 = trades(pcfg)[0]
    assert res.closed and t2["close_reason"] == "take" and t2["pnl"] > 0
    exit_net = sum(l["side"] * fill_price(PC, -l["side"], 0.01, 0.02) * t["qty"] for l in legs)
    assert t2["pnl"] == pytest.approx(t["entry_net"] + exit_net - t["entry_fees"] - t2["exit_fees"])
    assert t2["r_mult"] == pytest.approx(t2["pnl"] / t["max_loss"])


def test_stop_exit_and_hold_when_short_unbuyable(make_client, pcfg, fake):
    set_chain(fake)
    pe = eng(make_client, pcfg)
    step(pe, NOW, bull_put(fake))
    t, legs = _legs(pcfg)
    short = next(l for l in legs if l["side"] < 0)
    long_ = next(l for l in legs if l["side"] > 0)
    put_marks(pcfg, NOW + 900_000, {short["symbol"]: (1500, None), long_["symbol"]: (100, 120)})
    assert not pe.step(NOW + 900_000, {}).closed            # короткую не откупить — ждём
    put_marks(pcfg, NOW + 1_800_000, {short["symbol"]: (1900, 2000), long_["symbol"]: (100, 120)})
    pe.step(NOW + 1_800_000, {})
    t2 = trades(pcfg)[0]
    assert t2["close_reason"] == "stop" and t2["pnl"] < -0.5 * t["max_loss"]


def test_time_exit_long_horizon(make_client, pcfg, fake):
    set_chain(fake, exp=FAR, exp_ms=FAR_MS)
    pe = eng(make_client, pcfg)
    sig = mksig(fake, "bull_put", [(sym(62000, "P", FAR), -1), (sym(60000, "P", FAR), +1)], horizon="long",
                expiry=FAR, expiry_ms=FAR_MS)
    step(pe, NOW, sig)
    t, legs = _legs(pcfg)
    later = FAR_MS - 7 * DAY_MS
    put_marks(pcfg, later, {l["symbol"]: (fake.tickers[l["symbol"]]["bid1Price"] and float(
        fake.tickers[l["symbol"]]["bid1Price"]), float(fake.tickers[l["symbol"]]["ask1Price"])) for l in legs})
    pe.step(later, {})
    assert trades(pcfg)[0]["close_reason"] == "time"


def test_expiry_settlement_by_delivery_price(make_client, pcfg, fake):
    set_chain(fake)
    pe = eng(make_client, pcfg)
    step(pe, NOW, bull_put(fake))
    t, legs = _legs(pcfg)
    after = EXP_MS + 3600_000
    assert pe.step(after, {}).waiting == 1                # цены поставки нет ни в БД, ни у биржи
    fake.delivery_px["BTC"] = [{"symbol": f"BTC-{EXP}", "deliveryPrice": "61000", "deliveryTime": str(EXP_MS)}]
    pe.step(after + 900_000, {})
    t2 = trades(pcfg)[0]
    payout = -1 * (62000 - 61000) * t["qty"]                # короткий пут 62000 в деньгах на 1000
    dfee = delivery_fee(PC, "BTC", "P", 62000, 61000, t["qty"])
    assert t2["close_reason"] == "expiry" and t2["delivery_price"] == 61000
    assert t2["pnl"] == pytest.approx(t["entry_net"] + payout - t["entry_fees"] - dfee)


def test_void_when_no_delivery_price(make_client, pcfg, fake):
    set_chain(fake)
    pe = eng(make_client, pcfg)
    step(pe, NOW, bull_put(fake))
    pe.step(EXP_MS + paper.PRICE_GIVEUP_MS + DAY_MS, {})
    t = trades(pcfg)[0]
    assert t["status"] == "void" and t["pnl"] is None


def test_equity_curve_rows(make_client, pcfg, fake):
    set_chain(fake)
    pe = eng(make_client, pcfg)
    step(pe, NOW, bull_put(fake))
    t, legs = _legs(pcfg)
    put_marks(pcfg, NOW + 900_000, {l["symbol"]: (l["bid"], l["ask"], l["fill"]) for l in legs})  # mark = вход
    pe.step(NOW + 900_000, {})
    conn = db.connect(pcfg.db_file)
    rows = [dict(r) for r in conn.execute("SELECT * FROM book_equity ORDER BY ts")]
    conn.close()
    assert len(rows) == 2 and rows[-1]["book"] == "short:bull_put:BTC" and rows[-1]["open_n"] == 1
    assert rows[-1]["unrealized"] == pytest.approx(-t["entry_fees"])  # mark = цене входа — остаётся комиссия
    assert rows[-1]["open_risk"] == pytest.approx(t["max_loss"]) and rows[-1]["im_regular"] > 0


def test_disabled_does_nothing(make_client, pcfg, fake):
    set_chain(fake)
    pe = eng(make_client, pcfg, enabled=False)
    assert not step(pe, NOW, condor(fake)).opened and not trades(pcfg)


# ---------- календарь ----------
CAL_NOW = EXP_MS - 8 * DAY_MS   # ближняя 27NOV26 — 8 дн. (окно 5–14), дальняя 25DEC26 — 36 дн.


def cal_chain(fake, near_iv=0.66, far_iv=0.5, rv=0.45):
    set_chain(fake, iv=near_iv, now=CAL_NOW)
    set_chain(fake, iv=far_iv, exp=FAR, exp_ms=FAR_MS, now=CAL_NOW, keep=True)
    set_klines(fake, rv=rv, now=CAL_NOW)


def sig_engine(make_client, cfg, **sig):
    cfg = dataclasses.replace(cfg, sig=dataclasses.replace(cfg.sig, **sig)) if sig else cfg
    return SignalEngine(make_client(cfg), cfg)


def test_calendar_signal_on_iv_inversion(make_client, pcfg, fake):
    cal_chain(fake)
    sig = sig_engine(make_client, pcfg).calendar_signal("BTC", CAL_NOW)
    assert sig.strategy == "calendar", sig.no_signal
    assert sig.expiry == EXP and sig.far_expiry == FAR and sig.horizon == "long"
    near, far = sig.legs
    assert near.side < 0 and far.side > 0 and near.strike == far.strike == 65000 and near.kind == far.kind
    assert sig.net < 0 and sig.profile.max_loss == pytest.approx(-sig.net) and math.isnan(sig.profile.max_profit)
    assert any("≥ 1.1" in r for r in sig.reasons)


def test_calendar_needs_inversion_flat_trend_and_calm_rv(make_client, pcfg, fake):
    cal_chain(fake, near_iv=0.52)
    sig = sig_engine(make_client, pcfg).calendar_signal("BTC", CAL_NOW)
    assert sig.strategy is None and "инверсии IV нет" in sig.no_signal
    cal_chain(fake)
    set_klines(fake, rv=0.45, trend_per_bar=0.005, now=CAL_NOW)
    assert "только при боковом" in sig_engine(make_client, pcfg).calendar_signal("BTC", CAL_NOW).no_signal
    cal_chain(fake)
    sig = sig_engine(make_client, pcfg, rv14_rv30_max=1.0).calendar_signal("BTC", CAL_NOW)  # RV14 ≈ RV30 ≥ 1.0
    assert sig.strategy is None and "волатильность растёт" in sig.no_signal


def test_calendar_paper_entry_and_exits(make_client, pcfg, fake):
    cal_chain(fake)
    se = sig_engine(make_client, pcfg)
    sig = se.calendar_signal("BTC", CAL_NOW)
    pe = PaperEngine(make_client(pcfg), pcfg, se)
    step(pe, CAL_NOW, sig)
    t, legs = _legs(pcfg)
    assert t["strategy"] == "calendar" and t["far_expiry"] == FAR and t["book"] == "long:calendar:BTC"
    assert t["max_loss"] == pytest.approx(-t["entry_net"] + t["entry_fees"])
    assert {l["expiry_ms"] for l in legs} == {EXP_MS, FAR_MS}
    conn = db.connect(pcfg.db_file)
    until = {r["symbol"]: r["until_ms"] for r in conn.execute("SELECT * FROM tracked_symbols")}
    conn.close()
    assert sorted(until.values()) == [EXP_MS, FAR_MS]
    q = {l["symbol"]: (float(fake.tickers[l["symbol"]]["bid1Price"]), float(fake.tickers[l["symbol"]]["ask1Price"]))
         for l in legs}
    # цена ушла от страйка дальше ожидаемого движения
    put_marks(pcfg, CAL_NOW + 900_000, q, underlying=65000 + t["em"] + 100)
    pe.step(CAL_NOW + 900_000, {})
    assert trades(pcfg)[0]["close_reason"] == "cal_move"


def test_calendar_time_exit_before_near_expiry(make_client, pcfg, fake):
    cal_chain(fake)
    se = sig_engine(make_client, pcfg)
    pe = PaperEngine(make_client(pcfg), pcfg, se)
    step(pe, CAL_NOW, se.calendar_signal("BTC", CAL_NOW))
    t, legs = _legs(pcfg)
    q = {l["symbol"]: (float(fake.tickers[l["symbol"]]["bid1Price"]), float(fake.tickers[l["symbol"]]["ask1Price"]))
         for l in legs}
    ts = EXP_MS - 18 * HOUR_MS
    put_marks(pcfg, ts, q)
    pe.step(ts, {})
    assert trades(pcfg)[0]["close_reason"] == "cal_time"


def test_calendar_after_near_expiry_settles_near_and_sells_far(make_client, pcfg, fake):
    cal_chain(fake)
    se = sig_engine(make_client, pcfg)
    pe = PaperEngine(make_client(pcfg), pcfg, se)
    step(pe, CAL_NOW, se.calendar_signal("BTC", CAL_NOW))
    t, legs = _legs(pcfg)
    far = next(l for l in legs if l["side"] > 0)
    near = next(l for l in legs if l["side"] < 0)
    after = EXP_MS + HOUR_MS
    fake.delivery_px["BTC"] = [{"symbol": f"BTC-{EXP}", "deliveryPrice": "65500", "deliveryTime": str(EXP_MS)}]
    put_marks(pcfg, after, {far["symbol"]: (3000, 3100)})
    pe.step(after, {})
    t2 = trades(pcfg)[0]
    from optbot.bybit_client import option_intrinsic
    payout = -option_intrinsic(near["kind"], near["strike"], 65500) * t["qty"]
    sell = fill_price(PC, -1, 3000, 3100) * t["qty"]
    fees = delivery_fee(PC, "BTC", near["kind"], near["strike"], 65500, t["qty"]) + trade_fee(PC, 65000, 2970, t["qty"])
    assert t2["close_reason"] == "expiry"
    assert t2["pnl"] == pytest.approx(t["entry_net"] + payout + sell - t["entry_fees"] - fees)


# ---------- длинный горизонт ----------
def test_long_horizon_uses_iv30_rv30(make_client, pcfg, fake):
    set_chain(fake, iv=0.6, exp=FAR, exp_ms=FAR_MS, step=1000)  # крылья 30-дневной серии шире
    set_klines(fake, rv=0.4)
    se = sig_engine(make_client, pcfg)
    sig = se.compute("BTC", NOW, horizon="long")
    m = sig.metrics
    assert m.horizon == "long" and m.iv30 == pytest.approx(0.6) and m.rv30 == pytest.approx(0.4, rel=0.02)
    assert m.iv7 is None and sig.strategy == "iron_condor" and sig.expiry == FAR
    assert any("pctl не используется" in r and "IV30/RV30" in r for r in sig.reasons)
    # в коротком окне 2–7 дн. экспираций нет
    assert se.compute("BTC", NOW).no_signal.startswith("нет экспирации")


def test_long_horizon_rv_rising_blocks_selling(make_client, pcfg, fake):
    set_chain(fake, iv=0.6, exp=FAR, exp_ms=FAR_MS, step=1000)  # крылья 30-дневной серии шире
    set_klines(fake, rv=0.4)
    sig = sig_engine(make_client, pcfg, rv14_rv30_max=1.0).compute("BTC", NOW, horizon="long")
    assert sig.strategy is None and "волатильность растёт" in sig.no_signal and sig.regime == "high"


def test_long_horizon_percentile_needs_iv_pct_min_days(make_client, pcfg, fake):
    from tests.test_signals import add_history
    set_chain(fake, iv=0.6, exp=FAR, exp_ms=FAR_MS, step=1000)  # крылья 30-дневной серии шире
    set_klines(fake, rv=0.4)
    add_history(pcfg, "BTC", [0.5] * (20 * 24), NOW - 20 * DAY_MS, step_ms=HOUR_MS, dte=30)
    se = sig_engine(make_client, pcfg)
    m = se.compute("BTC", NOW, horizon="long").metrics
    assert m.history_days >= 19 and not m.enough_history          # 20 дн. < IV_PCT_MIN_DAYS=30
    assert se.compute("BTC", NOW).metrics.enough_history           # короткому хватает SIG_MIN_HISTORY_DAYS=14


# ---------- тег event ----------
def test_event_tag_and_stats_split(make_client, pcfg, fake, tmp_path):
    ev = tmp_path / "ev.csv"
    when = datetime.fromtimestamp((NOW + DAY_MS) / 1000, timezone.utc)
    ev.write_text(f"{when:%Y-%m-%d},{when:%H:%M},high,FOMC\n2030-01-01,00:00,low,Мелочь\n")
    cfg = dataclasses.replace(pcfg, events_path=str(ev))
    set_chain(fake, iv=0.6)
    set_klines(fake, rv=0.4)
    se = sig_engine(make_client, cfg)
    sig = se.compute("BTC", NOW)
    assert sig.tags == ["event"] and sig.events[0].name == "FOMC"
    from optbot.signals import fmt_signal
    assert "event: FOMC" in fmt_signal(sig, "demo")
    pe = PaperEngine(make_client(cfg), cfg, se)
    step(pe, NOW, sig, bull_put(fake))
    rows = trades(cfg)
    assert [r["tags"] for r in rows] == ["event", None]
    for r in rows:
        r.update(pnl=1.0, r_mult=0.1, closed_ms=NOW)
    text = paper.fmt_stats(rows, "тест")
    assert "без event: 1" in text and "с event: 1" in text and "[event]" in text
    assert "short:iron_condor:BTC" in paper.to_csv(rows)


def test_events_parse_and_reload(tmp_path):
    assert [e.name for e in parse("# c\n2026-12-09,19:00,high,FOMC\nплохо\n")] == ["FOMC"]
    p = tmp_path / "e.csv"
    c = Calendar(str(p))
    assert c.events() == []                      # нет файла — событий нет
    p.write_text("2026-12-09,19:00,high,FOMC\n")
    assert len(c.between(0, 2 ** 62)) == 1 and c.covered_until() is not None


def test_repo_events_file_parses():
    from optbot.config import ROOT
    import os
    with open(os.path.join(ROOT, "events.csv"), encoding="utf-8") as f:
        ev = parse(f.read())
    assert len(ev) >= 10 and all(e.importance == "high" for e in ev)


# ---------- отчёты и бот ----------
def test_status_and_cli(make_client, pcfg, fake, monkeypatch, capsys):
    set_chain(fake)
    pe = eng(make_client, pcfg)
    assert "нет" in pe.status()
    step(pe, NOW, condor(fake))
    assert "short:iron_condor:BTC" in pe.status()
    monkeypatch.setattr(paper, "settings", pcfg)
    monkeypatch.setattr(paper, "OptionsClient", lambda: make_client(pcfg))
    assert paper.main(["--status"]) == 0 and "iron_condor" in capsys.readouterr().out
    assert paper.main(["--stats", "7d"]) == 0 and "закрытых сделок нет" in capsys.readouterr().out


def test_bot_kicks_paper_after_collect(monkeypatch, cfg):
    import asyncio
    from types import SimpleNamespace as NS
    from optbot.bot import OptionsBot
    b = OptionsBot.__new__(OptionsBot)
    b.cfg = dataclasses.replace(cfg, paper=PC)
    b.collector = NS(collect=lambda: NS(errors=[], snapshots=3, skipped=0, failed_bases=set()))
    b._paper_task = None
    calls = []
    b.paper = NS(step=lambda: calls.append(1) or paper.StepResult(ts=0))
    b._auto_change_on = lambda: False

    async def go():
        await b._collect_job(None)
        await b._paper_task
    asyncio.run(go())
    assert calls == [1]


def test_step_computes_signals_from_exchange(make_client, pcfg, fake, monkeypatch, capsys, tmp_path):
    """Без signals — расчёт по бирже: короткий кондор по высокой IV; ошибка по активу не мешает остальным."""
    set_chain(fake, iv=0.6)
    set_klines(fake, rv=0.4)
    cfg = dataclasses.replace(pcfg, paper=dataclasses.replace(PC, bases=("BTC", "ETH")))
    pe = PaperEngine(make_client(cfg), cfg)
    res = pe.step(NOW)
    assert any("short:iron_condor:BTC" in x for x in res.opened), res
    assert any("ETH" in e for e in res.errors) or "ETH" not in str(res.opened)
    monkeypatch.setattr(paper, "settings", cfg)
    monkeypatch.setattr(paper, "OptionsClient", lambda: make_client(cfg))
    paper.main(["--step"])
    assert "[" in capsys.readouterr().out
    f = tmp_path / "s.csv"
    assert paper.main(["--stats", "30d", "--csv", str(f)]) == 0 and f.read_text().startswith("id,book")


def test_unbounded_risk_not_entered(make_client, pcfg, fake):
    set_chain(fake)
    pe = eng(make_client, pcfg)
    naked = mksig(fake, "bear_call", [(sym(68000, "C"), -1)])
    r = step(pe, NOW, naked)
    assert not r.opened and "без ограничения" in r.skipped[0]


# ---------- ревью E1 ----------
def test_stop_not_triggered_by_spread_alone(make_client, pcfg, fake):
    """Стоп — по mark: широкий bid/ask сам по себе не закрывает сделку (календарь: риск — только дебет)."""
    cal_chain(fake)
    se = sig_engine(make_client, pcfg)
    pe = PaperEngine(make_client(pcfg), pcfg, se)
    step(pe, CAL_NOW, se.calendar_signal("BTC", CAL_NOW))
    t, legs = _legs(pcfg)
    # mark = цене входа, а bid/ask разъехались: исполнимый PnL глубоко в минусе
    put_marks(pcfg, CAL_NOW + 900_000, {l["symbol"]: (l["fill"] * 0.5, l["fill"] * 1.5, l["fill"]) for l in legs})
    pe.step(CAL_NOW + 900_000, {})
    assert trades(pcfg)[0]["status"] == "open"


def test_calendar_both_legs_expired_each_at_own_delivery(make_client, pcfg, fake):
    cal_chain(fake)
    se = sig_engine(make_client, pcfg)
    pe = PaperEngine(make_client(pcfg), pcfg, se)
    step(pe, CAL_NOW, se.calendar_signal("BTC", CAL_NOW))
    t, legs = _legs(pcfg)
    near = next(l for l in legs if l["side"] < 0)
    far = next(l for l in legs if l["side"] > 0)
    fake.delivery_px["BTC"] = [
        {"symbol": f"BTC-{EXP}", "deliveryPrice": "65000", "deliveryTime": str(EXP_MS)},
        {"symbol": f"BTC-{FAR}", "deliveryPrice": "70000", "deliveryTime": str(FAR_MS)}]
    pe.step(FAR_MS + HOUR_MS, {})
    t2 = trades(pcfg)[0]
    from optbot.bybit_client import option_intrinsic
    payout = (-option_intrinsic(near["kind"], 65000, 65000) + option_intrinsic(far["kind"], 65000, 70000)) * t["qty"]
    assert t2["close_reason"] == "expiry" and t2["exit_net"] == pytest.approx(payout)


def test_delivery_prices_fetched_once_per_base_for_all_expiries(make_client, pcfg, fake):
    cfg = dataclasses.replace(pcfg, journal_enabled=False)     # в БД цены не пишутся — только ответ биржи
    set_chain(fake)
    set_chain(fake, exp=FAR, exp_ms=FAR_MS, keep=True)
    pe = PaperEngine(make_client(cfg), cfg)
    step(pe, NOW, bull_put(fake))
    far = mksig(fake, "bull_put", [(sym(62000, "P", FAR), -1), (sym(60000, "P", FAR), +1)],
                expiry=FAR, expiry_ms=FAR_MS)
    step(pe, NOW, far)
    fake.delivery_px["BTC"] = [
        {"symbol": f"BTC-{EXP}", "deliveryPrice": "65000", "deliveryTime": str(EXP_MS)},
        {"symbol": f"BTC-{FAR}", "deliveryPrice": "65000", "deliveryTime": str(FAR_MS)}]
    n0 = sum(1 for c in fake.calls if c[0] == "get_new_delivery_price")
    pe.step(FAR_MS + HOUR_MS, {})
    assert [t["close_reason"] for t in trades(cfg)] == ["expiry", "expiry"]
    assert sum(1 for c in fake.calls if c[0] == "get_new_delivery_price") - n0 == 1


def test_entry_inside_exit_window_skipped(make_client, pcfg, fake):
    set_chain(fake)
    pe = eng(make_client, pcfg, exit_days_short=5)            # до экспирации 4 дня < 5
    r = step(pe, NOW, bull_put(fake))
    assert not r.opened and "раннего выхода" in r.skipped[0]


def test_void_counted_in_stats(make_client, pcfg, fake):
    set_chain(fake)
    pe = eng(make_client, pcfg)
    step(pe, NOW, bull_put(fake))
    end = EXP_MS + paper.PRICE_GIVEUP_MS + DAY_MS
    pe.step(end, {})
    rows = pe.closed(0, end + 1)
    assert len(rows) == 1 and "без итога (нет цены поставки) 1" in paper.fmt_stats(rows, "x")


def test_one_signal_failure_keeps_others(make_client, pcfg, fake, monkeypatch):
    set_chain(fake, iv=0.6)
    set_klines(fake, rv=0.4)
    cfg = dataclasses.replace(pcfg, paper=dataclasses.replace(PC, bases=("BTC",)))
    pe = PaperEngine(make_client(cfg), cfg)

    def boom(*a, **kw):
        raise ZeroDivisionError("кривые данные")
    monkeypatch.setattr(pe.engine, "calendar_signal", boom)
    res = pe.step(NOW)
    assert any("calendar BTC" in e for e in res.errors) and any("iron_condor" in x for x in res.opened)


# ---------- ревью E1, раунд 2 ----------
def test_long_leg_without_bid_but_with_mark_waits(make_client, pcfg, fake):
    set_chain(fake, exp=FAR, exp_ms=FAR_MS)
    pe = eng(make_client, pcfg)
    sig = mksig(fake, "bull_put", [(sym(62000, "P", FAR), -1), (sym(60000, "P", FAR), +1)], horizon="long",
                expiry=FAR, expiry_ms=FAR_MS)
    step(pe, NOW, sig)
    t, legs = _legs(pcfg)
    later = FAR_MS - 7 * DAY_MS
    q = {l["symbol"]: ((None, 6000, 3000) if l["side"] > 0 else (500, 520)) for l in legs}  # нет bid, дорогая
    put_marks(pcfg, later, q)
    pe.step(later, {})
    assert trades(pcfg)[0]["status"] == "open"
    q = {l["symbol"]: ((None, 1, 2) if l["side"] > 0 else (500, 520)) for l in legs}  # копейки — по 0 можно
    put_marks(pcfg, later + 900_000, q)
    pe.step(later + 900_000, {})
    assert trades(pcfg)[0]["close_reason"] == "time"


def test_marks_from_one_slot_only(make_client, pcfg, fake):
    set_chain(fake)
    pe = eng(make_client, pcfg)
    step(pe, NOW, bull_put(fake))
    t, legs = _legs(pcfg)
    a, b = legs
    put_marks(pcfg, NOW + 900_000, {a["symbol"]: (0.01, 0.02), b["symbol"]: (0.01, 0.02)})
    put_marks(pcfg, NOW + 1_800_000, {a["symbol"]: (0.01, 0.02)})          # вторая нога в этом срезе не пришла
    conn = db.connect(pcfg.db_file)
    got = pe._marks(conn, [a["symbol"], b["symbol"]], NOW + 1_800_000)
    conn.close()
    assert {m["ts"] for m in got.values()} == {NOW + 900_000}


def test_missing_mark_does_not_trigger_stop(make_client, pcfg, fake):
    set_chain(fake)
    pe = eng(make_client, pcfg)
    step(pe, NOW, mksig(fake, "bull_call", [(sym(65000, "C"), +1), (sym(67000, "C"), -1)]))
    t, legs = _legs(pcfg)
    conn = db.connect(pcfg.db_file)
    with conn:
        for l in legs:
            conn.execute("INSERT INTO leg_marks (ts, symbol, mark, bid, ask, underlying) VALUES (?,?,?,?,?,?)",
                         (NOW + 900_000, l["symbol"], None, l["bid"], l["ask"], 65000))
    conn.close()
    pe.step(NOW + 900_000, {})
    assert trades(pcfg)[0]["status"] == "open"


def test_delivery_price_used_when_db_write_fails(make_client, pcfg, fake, monkeypatch):
    set_chain(fake)
    pe = eng(make_client, pcfg)
    step(pe, NOW, bull_put(fake))
    fake.delivery_px["BTC"] = [{"symbol": f"BTC-{EXP}", "deliveryPrice": "65000", "deliveryTime": str(EXP_MS)}]

    def locked(self, base, rows):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(paper.Journal, "record_delivery_prices", locked)
    pe.step(EXP_MS + HOUR_MS, {})
    assert trades(pcfg)[0]["close_reason"] == "expiry"


def test_compute_min_dte_skips_expiries_inside_exit_window(make_client, pcfg, fake):
    set_chain(fake, iv=0.6)
    set_chain(fake, iv=0.6, exp="29NOV26", exp_ms=EXP_MS + 2 * DAY_MS, keep=True)
    set_klines(fake, rv=0.4)
    se = sig_engine(make_client, pcfg, target_days=3)
    assert se.compute("BTC", NOW).expiry == EXP                       # 4 дн. — ближе к цели 3
    assert se.compute("BTC", NOW, min_dte=5).expiry == "29NOV26"      # 4 дн. — внутри окна выхода


def test_null_mark_without_bid_is_zero():
    assert paper._mark({"mark": None, "bid": None}) == 0.0
    assert paper._mark({"mark": None, "bid": 5}) is None
    assert paper._mark({"mark": 3, "bid": None}) == 3


def test_iv_pct_min_days_capped_by_lookback():
    assert SignalSettings(iv_pct_min_days=60, lookback_days=60).iv_pct_min_days == 48
    assert SignalSettings(iv_pct_min_days=30, lookback_days=60).iv_pct_min_days == 30


def test_calendar_near_expiry_inside_exit_window_not_offered(make_client, pcfg, fake):
    cal_chain(fake)
    se = sig_engine(make_client, pcfg)
    assert se.calendar_signal("BTC", CAL_NOW).strategy == "calendar"
    sig = se.calendar_signal("BTC", CAL_NOW, min_near_dte=9)       # ближняя 8 дн. — внутри окна выхода
    assert sig.strategy is None and "нет пары экспираций" in sig.no_signal



# ---------- фильтр издержек ----------
def test_cost_pct_estimate():
    from optbot.signals import Leg, cost_pct
    legs = [Leg(symbol="a", kind="P", strike=60000, side=-1, qty=1, bid=300, ask=320),
            Leg(symbol="b", kind="P", strike=59000, side=+1, qty=1, bid=100, ask=120)]
    prem = 310 - 110
    fees = sum(min(0.0003 * 65000, 0.07 * p) for p in (300, 120, 320, 100))  # вход по bid/ask, выход — обратно
    assert cost_pct(legs, 65000, 0.0003, 7) == pytest.approx(100 * (fees + 20 + 20) / prem)
    held = sum(min(0.0003 * 65000, 0.07 * p) for p in (300, 120)) + 10 + 10   # держать: только вход
    assert cost_pct(legs, 65000, 0.0003, 7, exit_trade=False) == pytest.approx(100 * held / prem)
    slip = 0.01 * (300 + 120)
    assert cost_pct(legs, 65000, 0.0003, 7, 1, exit_trade=False) == pytest.approx(100 * (held + slip) / prem)
    flat = [dataclasses.replace(legs[0], side=+1), legs[0]]                      # премия по mid — 0
    assert cost_pct(flat, 65000, 0.0003, 7) is None


def test_cost_filter_books_skip_costly_and_plain_books_enter(make_client, pcfg, fake):
    set_chain(fake)
    pe = eng(make_client, pcfg)
    sig = bull_put(fake)
    sig.cost_pct = 40.0                                     # порог — из настроек (SIG_MAX_COST_PCT=15)
    by = {"BTC": {"short:bull_put:BTC": sig, "short:bull_put:BTC:cost": sig}}
    r = pe.step(NOW, by)
    assert [t["book"] for t in trades(pcfg)] == ["short:bull_put:BTC"]
    assert any("SIG_MAX_COST_PCT" in x for x in r.skipped)
    assert trades(pcfg)[0]["cost_pct"] == 40.0
    assert json.loads(trades(pcfg)[0]["cfg"])["sig_max_cost_pct"] == pcfg.sig.max_cost_pct
    sig2 = condor(fake)
    sig2.cost_pct = 10.0
    pe.step(NOW, {"BTC": {"short:iron_condor:BTC:cost": sig2}})
    assert "short:iron_condor:BTC:cost" in [t["book"] for t in trades(pcfg)]


def test_total_risk_limit_per_variant(make_client, pcfg, fake):
    set_chain(fake)
    pe = eng(make_client, pcfg, max_total_risk_usdt=120)
    s1 = bull_put(fake)
    pe.step(NOW, {"BTC": {"short:bull_put:BTC": s1}})
    s2 = bull_put(fake)
    s2.cost_pct = 5.0
    r = pe.step(NOW, {"BTC": {"short:bull_put:BTC:cost": s2}})      # другая версия — свой лимит
    assert r.opened


def test_signal_card_shows_costs(make_client, pcfg, fake):
    set_chain(fake, iv=0.6)
    set_klines(fake, rv=0.4)
    sig = sig_engine(make_client, pcfg, max_cost_pct=1).compute("BTC", NOW)
    from optbot.signals import fmt_signal
    assert sig.cost_pct > 1 and sig.costly and "книги с фильтром не открыли бы" in fmt_signal(sig, "demo")
    sig = sig_engine(make_client, pcfg, max_cost_pct=0).compute("BTC", NOW)
    assert not sig.costly and "Издержки ≈" in fmt_signal(sig, "demo")


def test_no_cost_books_when_filter_off(pcfg):
    cfg = dataclasses.replace(pcfg, sig=dataclasses.replace(pcfg.sig, max_cost_pct=0))
    assert not any(v for *_, v in paper.all_books(cfg))


def test_stats_split_by_cost_variant():
    rows = [dict(id=1, book="short:bull_put:BTC", tags=None, pnl=1.0, r_mult=0.1, status="closed"),
            dict(id=2, book="short:bull_put:BTC:cost", tags=None, pnl=2.0, r_mult=0.2, status="closed")]
    text = paper.fmt_stats(rows, "x")
    assert "книги RV: 1" in text and "книги RV, с фильтром издержек: 1" in text


# ---------- комиссия поставки: сверка с биржей ----------
def _delivery(cfg, sym_, pos, strike, price, fee, t=EXP_MS):
    conn = db.connect(cfg.db_file)
    with conn:
        conn.execute("INSERT INTO deliveries (symbol, delivery_time, side, position, strike, delivery_price, fee) "
                     "VALUES (?,?,?,?,?,?,?)", (sym_, t, "Sell", pos, strike, price, fee))
    conn.close()


def test_delivery_fee_check_reports_first_match_and_mismatches(pcfg):
    from optbot.paper import check_delivery_fees, fmt_delivery_check
    s1 = sym(60000, "C")
    _delivery(pcfg, s1, 0.01, 60000, 65000, 0.00015 * 65000 * 0.01)           # совпадает
    conn = db.connect(pcfg.db_file)
    got = check_delivery_fees(pcfg, conn)
    assert len(got) == 1 and got[0]["ok"] and got[0]["report"] and "совпала" in fmt_delivery_check(got[0])
    assert check_delivery_fees(pcfg, conn) == []                              # уже проверена
    _delivery(pcfg, sym(61000, "C"), 0.01, 61000, 65000, 0.00015 * 65000 * 0.01, EXP_MS + 1)
    got = check_delivery_fees(pcfg, conn)
    assert got[0]["ok"] and not got[0]["report"]                              # второе совпадение — молча
    _delivery(pcfg, sym(64990, "C"), 0.01, 64990, 65000, 0.05, EXP_MS + 2)     # формула: потолок 12.5%·10·0.01
    got = check_delivery_fees(pcfg, conn)
    assert not got[0]["ok"] and got[0]["report"] and "РАСХОЖДЕНИЕ" in fmt_delivery_check(got[0])
    assert len(check_delivery_fees(pcfg, conn, mark=False)) == 3              # CLI: все, без отметки
    conn.close()


def test_bot_alerts_delivery_fee_check(cfg, monkeypatch):
    import asyncio
    from optbot.bot import OptionsBot
    b = OptionsBot.__new__(OptionsBot)
    b.cfg = dataclasses.replace(cfg, paper=PC)
    _delivery(b.cfg, sym(60000, "C"), 0.01, 60000, 65000, 1.0)
    sent = []

    async def alert(key, text, quiet=True, retry=False):
        sent.append((key, text, quiet, retry))
        return True
    b.admin_alert = alert
    asyncio.run(b._check_delivery_fees())
    assert len(sent) == 1 and "РАСХОЖДЕНИЕ" in sent[0][1] and sent[0][2] is False


def test_cli_check_delivery_fees(pcfg, monkeypatch, capsys, make_client):
    monkeypatch.setattr(paper, "settings", pcfg)
    monkeypatch.setattr(paper, "OptionsClient", lambda: make_client(pcfg))
    assert paper.main(["--check-delivery-fees"]) == 0 and "в журнале нет" in capsys.readouterr().out
    _delivery(pcfg, sym(60000, "C"), 0.01, 60000, 65000, 1.0)
    assert paper.main(["--check-delivery-fees"]) == 1 and "РАСХОЖДЕНИЕ" in capsys.readouterr().out



def test_cost_book_rejects_signal_without_premium(make_client, pcfg, fake):
    set_chain(fake)
    pe = eng(make_client, pcfg)
    sig = bull_put(fake)
    sig.cost_pct = None                                     # премии по mid нет — издержки к ней не ограничены
    r = pe.step(NOW, {"BTC": {"short:bull_put:BTC:cost": sig}})
    assert not r.opened and "премии нет" in r.skipped[0]


def test_status_risk_per_variant(make_client, pcfg, fake):
    set_chain(fake)
    pe = eng(make_client, pcfg)
    s1, s2 = bull_put(fake), condor(fake)
    s2.cost_pct = 1.0
    pe.step(NOW, {"BTC": {"short:bull_put:BTC": s1, "short:iron_condor:BTC:cost": s2}})
    st = pe.status()
    assert "RV 1, риск" in st and "RV, с фильтром издержек 1" in st


def test_delivery_check_grouped_and_skipped_when_paper_off(cfg):
    import asyncio
    from optbot.bot import OptionsBot
    b = OptionsBot.__new__(OptionsBot)
    b.cfg = dataclasses.replace(cfg, paper=PC)
    for i in range(7):
        _delivery(b.cfg, sym(60000 + i * 100, "C"), 0.01, 60000 + i * 100, 65000, 1.0, EXP_MS + i)
    sent = []

    async def alert(key, text, quiet=True, retry=False):
        sent.append(text)
        return True
    b.admin_alert = alert
    asyncio.run(b._check_delivery_fees())
    assert len(sent) == 1 and sent[0].count("РАСХОЖДЕНИЕ") == 3          # не больше DELIVERY_FEE_MAX_BAD
    b.cfg = dataclasses.replace(b.cfg, paper=dataclasses.replace(PC, enabled=False))
    _delivery(b.cfg, sym(59000, "C"), 0.01, 59000, 65000, 1.0, EXP_MS + 100)
    asyncio.run(b._check_delivery_fees())
    assert len(sent) == 1



def test_delivery_check_late_same_expiry_and_zero_fee_itm(pcfg):
    from optbot.paper import check_delivery_fees
    conn = db.connect(pcfg.db_file)
    _delivery(pcfg, sym(60000, "C"), 0.01, 60000, 65000, 0.00015 * 65000 * 0.01)
    assert len(check_delivery_fees(pcfg, conn)) == 1
    _delivery(pcfg, sym(61000, "C"), 0.01, 61000, 65000, 0.0)               # та же экспирация, позже; fee 0 в деньгах
    _delivery(pcfg, sym(62000, "C"), 0.01, 62000, 65000, None)              # комиссии в записи нет — не сверяем
    _delivery(pcfg, sym(70000, "C"), 0.01, 70000, 65000, 0.0)               # вне денег — не сверяется
    got = check_delivery_fees(pcfg, conn)
    assert [x["symbol"] for x in got] == [sym(61000, "C")] and not got[0]["ok"]
    assert check_delivery_fees(pcfg, conn) == []
    conn.close()


def test_delivery_check_caps_mismatch_reports_and_abs_fee(pcfg):
    from optbot.paper import check_delivery_fees
    conn = db.connect(pcfg.db_file)
    _delivery(pcfg, sym(60000, "C"), 0.01, 60000, 65000, -0.00015 * 65000 * 0.01)   # знак — не важен
    assert check_delivery_fees(pcfg, conn)[0]["ok"]
    for i in range(5):
        _delivery(pcfg, sym(61000 + i * 100, "C"), 0.01, 61000 + i * 100, 65000, 0.0, EXP_MS + 1 + i)
    got = check_delivery_fees(pcfg, conn)
    assert [x["report"] for x in got] == [True, True, True, False, False]
    conn.close()


def test_stats_cost_reject_share(make_client, pcfg, fake):
    set_chain(fake)
    set_chain(fake, exp=FAR, exp_ms=FAR_MS, keep=True)
    pe = eng(make_client, pcfg)
    bad = bull_put(fake)
    bad.cost_pct = 40.0
    pe.step(NOW, {"BTC": {"short:bull_put:BTC:cost": bad}})
    ok = mksig(fake, "bull_put", [(sym(62000, "P", FAR), -1), (sym(60000, "P", FAR), +1)], expiry=FAR, expiry_ms=FAR_MS)
    ok.cost_pct = 5.0
    pe.step(NOW, {"BTC": {"short:bull_put:BTC:cost": ok}})
    rej = pe.cost_rejects(0, NOW + 1)
    assert rej == {"short:bull_put:BTC:cost": (1, 1)}
    text = paper.fmt_stats([], "x", rej)
    assert "short:bull_put:BTC:cost: 1 из 2 (50%)" in text and "всего: 1 из 2 (50%)" in text


def test_cost_reject_then_opened_not_counted(make_client, pcfg, fake):
    set_chain(fake)
    pe = eng(make_client, pcfg)
    s = bull_put(fake)
    s.cost_pct = 40.0
    pe.step(NOW, {"BTC": {"short:bull_put:BTC:cost": s}})
    s2 = bull_put(fake)
    s2.cost_pct = 5.0                                         # издержки упали — та же экспирация открылась
    pe.step(NOW + 1, {"BTC": {"short:bull_put:BTC:cost": s2}})
    assert pe.cost_rejects(0, NOW + 2) == {"short:bull_put:BTC:cost": (0, 1)}


def test_cost_reject_window_counts_reject_opened_after_window(make_client, pcfg, fake):
    set_chain(fake)
    pe = eng(make_client, pcfg)
    s = bull_put(fake)
    s.cost_pct = 40.0
    pe.step(NOW, {"BTC": {"short:bull_put:BTC:cost": s}})
    s2 = bull_put(fake)
    s2.cost_pct = 5.0
    pe.step(NOW + 10, {"BTC": {"short:bull_put:BTC:cost": s2}})
    assert pe.cost_rejects(0, NOW + 5) == {"short:bull_put:BTC:cost": (1, 0)}   # в том окне — ещё отказ


def test_signal_outcomes_logged_and_skips_report(make_client, pcfg, fake, monkeypatch, capsys):
    set_chain(fake, iv=0.6)
    set_klines(fake, rv=0.4)
    pe = eng(make_client, pcfg)
    pe.step(NOW)
    conn = db.connect(pcfg.db_file)
    rows = [dict(r) for r in conn.execute("SELECT * FROM paper_signals")]
    conn.close()
    assert {(r["base"], r["kind"]) for r in rows} >= {("BTC", "short"), ("BTC", "long"), ("BTC", "calendar")}
    assert all(r["outcome"] in ("signal", "not_built", "none", "error") for r in rows)
    assert any(r["outcome"] != "signal" and r["reason"] for r in rows)
    sigs, skips = pe.skips(0, NOW + 1)
    text = paper.fmt_skips(sigs, skips, "7d")
    assert "короткий BTC: шагов 1" in text and "длинный BTC" in text and "календарь BTC" in text


def test_fmt_skips_groups():
    sigs = [dict(ts=1, base="BTC", kind="short", outcome="none", strategy=None, regime="normal", trend="flat",
                 reason="IV7/RV7 = 1.02 — в норме"),
            dict(ts=2, base="BTC", kind="short", outcome="none", strategy=None, regime="normal", trend="flat",
                 reason="IV7/RV7 = 1.05 — в норме"),
            dict(ts=3, base="BTC", kind="short", outcome="signal", strategy="iron_condor", regime="high",
                 trend="flat", reason=None),
            dict(ts=3, base="BTC", kind="long", outcome="not_built", strategy="bull_put", regime="high",
                 trend="up", reason="нет экспирации 14–45 дн.")]
    skips = [dict(book="short:iron_condor:BTC:cost", expiry="27NOV26", code="cost", ts=3, last_ts=NOW,
                  reason="издержки 30%")]
    text = paper.fmt_skips(sigs, skips, "7d")
    assert "короткий BTC: шагов 3 — сигнал 1, нет стратегии 2" in text
    assert "IV normal, тренд нет, IVN/RVN = N — в норме: 2 — последняя причина: IV7/RV7 = 1.05 — в норме" in text
    assert "Бычий пут-спред (кредит) не собрался: 1 — последняя причина: нет экспирации 14–45 дн." in text
    from optbot.fmt import utc
    assert f"short:iron_condor:BTC:cost: издержки 1 — последняя ({utc(NOW)} UTC): 27NOV26 издержки 30%" in text
    assert "Итогов расчёта" in paper.fmt_skips([], [], "7d")


def test_paper_cli_skips(make_client, pcfg, monkeypatch, capsys):
    monkeypatch.setattr(paper, "settings", pcfg)
    monkeypatch.setattr(paper, "OptionsClient", lambda: make_client(pcfg))
    assert paper.main(["--skips", "7d"]) == 0
    assert "сигналы и отказы" in capsys.readouterr().out


def test_skip_repeat_updates_last_ts_and_window(make_client, pcfg, fake):
    set_chain(fake)
    pe = eng(make_client, pcfg)
    s = bull_put(fake)
    s.cost_pct = 40.0
    pe.step(NOW, {"BTC": {"short:bull_put:BTC:cost": s}})
    pe.step(NOW + 10 * DAY_MS, {"BTC": {"short:bull_put:BTC:cost": s}})
    _, skips = pe.skips(NOW + 9 * DAY_MS, NOW + 11 * DAY_MS)         # первый отказ — до окна, повтор — в нём
    assert [r["code"] for r in skips] == ["cost"] and skips[0]["last_ts"] == NOW + 10 * DAY_MS
    assert pe.cost_rejects(0, NOW + 11 * DAY_MS) == {"short:bull_put:BTC:cost": (1, 0)}   # счёт — по первому


def test_traded_expiry_recorded_as_skip(make_client, pcfg, fake):
    set_chain(fake)
    pe = eng(make_client, pcfg)
    pe.step(NOW, {"BTC": {"short:bull_put:BTC": bull_put(fake)}})
    conn = db.connect(pcfg.db_file)
    with conn:
        conn.execute("UPDATE paper_trades SET status = 'closed', closed_ms = ?", (NOW + 1,))
    conn.close()
    pe.step(NOW + 2, {"BTC": {"short:bull_put:BTC": bull_put(fake)}})
    _, skips = pe.skips(0, NOW + 3)
    assert [r["code"] for r in skips] == ["traded"]
    assert "экспирация уже торговалась 1" in paper.fmt_skips([], skips, "7d")


def test_calendar_inversion_not_built_logged_as_not_built(make_client, pcfg, fake, monkeypatch):
    from optbot.signals import Signal as S
    pe = eng(make_client, pcfg)
    set_chain(fake)
    m = Metrics(base="BTC", ts_ms=NOW, horizon="long", trend=Trend("flat"))
    monkeypatch.setattr(pe.engine, "calendar_signal", lambda *a, **k: S(
        metrics=m, strategy="calendar", reasons=[], horizon="long", no_signal="инверсия IV есть, но календарь не собрался"))
    pe.step(NOW)
    conn = db.connect(pcfg.db_file)
    r = conn.execute("SELECT * FROM paper_signals WHERE base='BTC' AND kind='calendar'").fetchone()
    conn.close()
    assert r["outcome"] == "not_built" and r["strategy"] == "calendar"


def test_skips_excludes_reject_later_opened(make_client, pcfg, fake):
    set_chain(fake)
    pe = eng(make_client, pcfg)
    s = bull_put(fake)
    s.cost_pct = 40.0
    pe.step(NOW, {"BTC": {"short:bull_put:BTC:cost": s}})
    s2 = bull_put(fake)
    s2.cost_pct = 5.0
    pe.step(NOW + 1, {"BTC": {"short:bull_put:BTC:cost": s2}})
    assert pe.skips(0, NOW + 2)[1] == []                             # как в --stats: не отказ
    assert len(pe.skips(0, NOW + 1)[1]) == 1                         # в окне до открытия — ещё отказ



def test_settlement_with_day_start_delivery_time(make_client, pcfg, fake):
    set_chain(fake)
    pe = eng(make_client, pcfg)
    step(pe, NOW, bull_put(fake))
    fake.delivery_px["BTC"] = [{"deliveryPrice": "61000", "deliveryTime": str(EXP_MS // DAY_MS * DAY_MS)}]
    pe.step(EXP_MS + 3600_000, {})
    t = trades(pcfg)[0]
    assert t["close_reason"] == "expiry" and t["delivery_price"] == 61000
    pe2 = eng(make_client, pcfg)                                      # второй раз — из БД (окно с начала дня)
    assert pe2._delivery_price(db.connect(pcfg.db_file), "BTC", EXP_MS) == 61000


def test_outcome_reason_lists_rejected_expiries():
    m = Metrics(base="BTC", ts_ms=NOW)
    sig = Signal(metrics=m, strategy="bear_call", reasons=[],
                 no_signal="ни на одной экспирации окна стратегия не собралась",
                 rejected=["27NOV26: кредит 50 = 2.5% ширины 2000 < 10%", "04DEC26: нога без bid/ask"])
    r = paper.outcome_reason(sig)
    assert r == ("ни на одной экспирации окна стратегия не собралась; ✗ 27NOV26: кредит 50 = 2.5% ширины 2000 < 10%; "
                 "✗ 04DEC26: нога без bid/ask")
    sig.rejected = [f"E{i}: " + "x" * 100 for i in range(50)]
    assert len(paper.outcome_reason(sig)) == paper.REASON_MAX and paper.outcome_reason(sig).endswith("…")
    assert paper.outcome_reason(Signal(metrics=m, strategy=None, reasons=[], no_signal="нет")) == "нет"


def test_step_logs_rejections_per_expiry(make_client, pcfg, fake):
    set_chain(fake, iv=0.6)
    set_klines(fake, rv=0.4)
    pe = eng(make_client, dataclasses.replace(pcfg, sig=dataclasses.replace(pcfg.sig, min_credit_ratio=0.9)))
    pe.step(NOW)
    conn = db.connect(pcfg.db_file)
    r = conn.execute("SELECT reason FROM paper_signals WHERE kind = 'short' AND outcome = 'not_built'").fetchone()
    conn.close()
    assert r and "не собралась" in r["reason"] and f"✗ {EXP}: кредит " in r["reason"]
    assert "ширины" in r["reason"] and "< 90%" in r["reason"]
    conn = db.connect(pcfg.db_file)
    none = [x["reason"] for x in conn.execute("SELECT reason FROM paper_signals WHERE outcome = 'none'")]
    conn.close()
    assert all("✗" not in (x or "") for x in none)                    # у «нет сигнала» — только итог
