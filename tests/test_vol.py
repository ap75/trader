"""Прогноз RV7 по HAR (optbot/vol.py) и его использование: карточка, журнал сигналов, книги «:har», задача бота."""
import asyncio
import dataclasses
import json
import math
import random

import pytest

from optbot import db, paper, vol
from optbot.fmt import DAY_MS, HOUR_MS
from optbot.signals import SignalEngine, fmt_signal, har_decision, metrics_from_json
from optbot.sigjournal import SignalJournal, fmt_har, fmt_stats, compute_stats
from tests.fakes import EXP_MS, NOW, kline_rows, set_chain, set_klines


def candles_from_daily_vol(day_vols: list[float], start_ms: int = 0, seed: int = 1) -> list[tuple[int, float]]:
    """Часовые свечи с заданной годовой волатильностью каждого дня (доходности ±σ/√8760 — дисперсия дня точная)."""
    rnd = random.Random(seed)
    out, px, t = [], 100.0, start_ms
    out.append((t - HOUR_MS, px))
    for v in day_vols:
        a = v / math.sqrt(vol.YEAR_H)
        for _ in range(24):
            px *= math.exp(a if rnd.random() < 0.5 else -a)
            out.append((t, px))
            t += HOUR_MS
    return out


# ---------- модель ----------
def test_reference_loaded_and_forecast_formula():
    ref = vol.load_reference()
    assert set(ref) == {"BTC", "ETH", "SOL"}
    m = ref["BTC"]
    assert m.b == (-0.353, 0.135, 0.348, 0.271) and m.s2 == 0.359 and m.source == "reference"
    rv1, rv7, rv30 = 0.5, 0.4, 0.3
    y = -0.353 + 0.135 * math.log(0.25) + 0.348 * math.log(0.16) + 0.271 * math.log(0.09)
    assert m.forecast(rv1, rv7, rv30) == pytest.approx(math.sqrt(math.exp(y + 0.359 / 2)))
    assert m.forecast(None, rv7, rv30) is None and m.forecast(0, rv7, rv30) is None


def test_reference_bad_file(tmp_path, caplog):
    p = tmp_path / "x.json"
    p.write_text("{")
    assert vol.load_reference(str(p)) == {}


def test_daily_var_needs_24_returns():
    c = candles_from_daily_vol([0.5, 0.5])
    dv = vol.daily_var(c)
    assert dv[0] == pytest.approx(0.25) and dv[DAY_MS] == pytest.approx(0.25)
    gap = [x for x in c if x[0] != 5 * HOUR_MS]          # пропуск свечи — день без 24 доходностей
    assert 0 not in vol.daily_var(gap) and DAY_MS in vol.daily_var(gap)


def test_ols_exact():
    rnd = random.Random(3)
    X = [[rnd.random(), rnd.random(), rnd.random()] for _ in range(50)]
    y = [0.5 + 1 * a - 2 * b + 0.25 * c for a, b, c in X]
    b, s2 = vol.ols(X, y)
    assert b == pytest.approx([0.5, 1, -2, 0.25], abs=1e-9) and s2 == pytest.approx(0, abs=1e-12)


def test_dataset_alignment():
    dv = {d * DAY_MS: float(d + 1) for d in range(40)}
    X, y = vol.dataset(dv, 7)
    # первый момент — D = 30 (окно RV30 — дни 0…29), цель — дни 30…36; последний — D = 33 (цель до дня 39)
    assert len(y) == 4
    assert X[0] == pytest.approx([math.log(30), math.log(sum(range(24, 31)) / 7), math.log(sum(range(1, 31)) / 30)])
    assert y[0] == pytest.approx(math.log(sum(range(31, 38)) / 7))
    assert len(vol.dataset(dv, 7, until_ms=38 * DAY_MS)[1]) == 2       # цель должна быть известна к until (D ≤ 31)


def test_fit_recovers_har_process():
    """Синтетический процесс с известными коэффициентами — оценка близка к ним."""
    rnd = random.Random(7)
    b = (-0.3, 0.2, 0.35, 0.3)
    logv = [math.log(0.25)] * 30
    for _ in range(900):
        x1, x7, x30 = (math.log(sum(math.exp(v) for v in logv[-k:]) / k) for k in (1, 7, 30))
        logv.append(b[0] + b[1] * x1 + b[2] * x7 + b[3] * x30 + rnd.gauss(0, 0.4))
    c = candles_from_daily_vol([math.sqrt(math.exp(v)) for v in logv])
    m = vol.fit("btc", c, now_ms=c[-1][0] + HOUR_MS, horizon=1)
    assert m.base == "BTC" and m.source == "fit" and m.n > 800
    assert m.b[1] == pytest.approx(0.2, abs=0.1) and m.b[2] == pytest.approx(0.35, abs=0.15)
    assert vol.fit("BTC", c[: 200 * 24], now_ms=NOW) is None                # меньше MIN_TRAIN моментов


def test_drift_pct():
    ref = vol.HarModel("BTC", (-0.4, 0.1, 0.3, 0.3), 0.3, 1)
    assert vol.drift_pct(vol.HarModel("BTC", (-0.4, 0.105, 0.3, 0.3), 0.3, 1), ref) == pytest.approx(5)
    assert vol.drift_pct(vol.HarModel("BTC", (-0.4, 0.1, 0.36, 0.3), 0.3, 1), ref) == pytest.approx(20)


def test_month_start():
    t = 1795334400000   # 2026-11-22 00:00 UTC
    assert vol.month_start(t + 5 * HOUR_MS) == 1793491200000   # 2026-11-01


# ---------- свечи и переоценка ----------
def _history(fake, base="BTC", days=500, now=NOW):
    """Дни со случайной волатильностью (без памяти — коэффициенты HAR далеки от эталона)."""
    rnd = random.Random(5)
    c = candles_from_daily_vol([rnd.uniform(0.2, 0.9) for _ in range(days)])
    rows = kline_rows([px for _, px in c], 60, now)
    fake.klines[(f"{base}USDT", "60")] = rows
    return rows


def test_update_candles_pages_back_and_increments(cfg, make_client, fake):
    _history(fake, days=100)
    c = make_client(cfg)
    conn = db.connect(cfg.db_file)
    n = vol.update_candles(conn, c, "BTC", NOW)
    assert n == 100 * 24                                           # незакрытая (последняя) не пишется
    pages = sum(1 for x in fake.calls if x[0] == "get_kline")
    assert pages >= 3
    fake.calls.clear()
    assert vol.update_candles(conn, c, "BTC", NOW) == 0           # повтор — одна страница, ничего нового
    assert sum(1 for x in fake.calls if x[0] == "get_kline") == 1
    rows = vol.load_candles(conn, "BTC")
    assert all(t2 - t1 == HOUR_MS for (t1, _), (t2, _) in zip(rows, rows[1:]))
    conn.close()


def test_refit_monthly_drift_and_fallback(cfg, make_client, fake):
    vm = vol.VolModels(cfg)
    c = make_client(cfg)
    assert vm.get("BTC").source == "reference"                   # оценки ещё нет — эталон
    _history(fake, "BTC", days=500)
    fake.klines[("ETHUSDT", "60")] = kline_rows([100.0] * 48, 60, NOW)
    res = vm.refit(c, ["BTC", "ETH"], NOW)
    assert [m.base for m in res.fitted] == ["BTC"]
    assert any("ETH" in e and "мало истории" in e for e in res.errors)
    assert vm.get("BTC").source == "fit" and vm.get("ETH").source == "reference"
    # волатильность без памяти — коэффициенты далеки от эталона: предупреждение
    assert res.drift and res.drift[0][0] == "BTC" and "BTC" in vol.fmt_drift(res)
    assert vm.refit(c, ["BTC"], NOW + DAY_MS).fitted == []        # тот же месяц — не переоценивает
    assert vm.refit(c, ["BTC"], NOW + DAY_MS, force=True).fitted
    assert vm.refit(c, ["BTC"], NOW + 20 * DAY_MS).fitted          # новый месяц (декабрь)
    conn = db.connect(cfg.db_file)
    assert vol.model_at(conn, "BTC", NOW - 1) is None and vol.model_at(conn, "BTC", NOW).fitted_ms == NOW
    conn.close()


# ---------- сигналы ----------
@pytest.fixture
def market(fake):
    set_chain(fake, iv=0.6)
    set_klines(fake, rv=0.4)
    return fake


def test_signal_card_shows_both_ratios(cfg, make_client, market):
    sig = SignalEngine(make_client(cfg), cfg).compute("BTC", NOW)
    m = sig.metrics
    assert m.har7 and m.har_src == "reference" and m.rv1 and m.rv30
    assert m.har7 == pytest.approx(vol.load_reference()["BTC"].forecast(m.rv1, m.rv7, m.rv30))
    text = fmt_signal(sig, "demo")
    assert "HAR7" in text and "IV7/RV7" in text and "IV7/HAR7" in text and "эталон research" in text
    assert "IV7/RV7 = " in sig.reasons[0]                          # решение карточки — по RV7


def test_har_version_decides_by_har(cfg, make_client, market):
    eng = SignalEngine(make_client(cfg), cfg)
    rv = eng.compute("BTC", NOW)
    har = eng.compute("BTC", NOW, vol="har", metrics=rv.metrics)
    assert har.metrics.rv == har.metrics.har7 and "IV7/HAR7 = " in har.reasons[0]
    assert rv.metrics.vol_model == "rv"                            # исходные метрики не тронуты
    with pytest.raises(ValueError):
        eng.compute("BTC", NOW, vol="x")
    # IV7/HAR7 = 1.05 — по HAR IV обычная, по RV — высокая
    m = dataclasses.replace(rv.metrics, har7=rv.metrics.iv7 / 1.05)
    assert har_decision(m, cfg.sig) is None and rv.strategy == "iron_condor"


def test_har_missing_model_no_signal(cfg, make_client, market, monkeypatch):
    eng = SignalEngine(make_client(cfg), cfg)
    monkeypatch.setattr(eng.vol, "get", lambda base: None)
    s = eng.compute("BTC", NOW, vol="har")
    assert s.metrics.har7 is None and s.strategy is None and "HAR7" in s.no_signal


# ---------- журнал сигналов ----------
def test_journal_records_har_and_retro(cfg, make_client, market):
    sj = SignalJournal(cfg)
    sig = SignalEngine(make_client(cfg), cfg).compute("BTC", NOW)
    sid = sj.record(sig, now_ms=NOW)
    conn = db.connect(cfg.db_file)
    met = json.loads(conn.execute("SELECT metrics FROM signals WHERE id = ?", (sid,)).fetchone()["metrics"])
    assert met["har7"] == pytest.approx(sig.metrics.har7) and met["har_strategy"] == "iron_condor"
    # второй сигнал — «старый», без HAR7 в metrics: итог есть, считается задним числом по perp_h1
    old = {k: v for k, v in met.items() if k not in ("har7", "har_strategy", "har_src", "rv1", "rv30")}
    conn.execute("INSERT INTO signals (ts, last_shown_ms, base, strategy, regime, trend, expiry, expiry_ms, legs_key, "
                 "legs, qty, net, net_mid, max_profit, max_loss, metrics, cfg_hash, cfg, source, settled_ms, pnl) "
                 "SELECT ts, ts, base, strategy, regime, trend, expiry, expiry_ms, legs_key || 'x', legs, qty, net, net_mid, "
                 "max_profit, max_loss, ?, cfg_hash, cfg, source, ?, -5 FROM signals WHERE id = ?",
                 (json.dumps(old), NOW + 1, sid))
    conn.execute("UPDATE signals SET settled_ms = ?, pnl = 12 WHERE id = ?", (NOW + 1, sid))
    a = 0.4 / math.sqrt(8760)
    rows = kline_rows([65000 * (1 + a if i % 2 else 1) for i in range(800)], 60, NOW)
    conn.executemany("INSERT INTO perp_h1 (base, ts, close) VALUES ('BTC', ?, ?)",
                     [(int(r[0]), float(r[4])) for r in rows if int(r[0]) + HOUR_MS <= NOW])
    conn.commit()
    conn.close()
    rows = sj.rows(0, NOW + DAY_MS)
    outs = sj.har_outcomes(rows)
    assert len(outs) == 2 and all(o["known"] for o in outs)
    retro = next(o for o in outs if o["retro"])
    assert retro["har_strategy"] == "iron_condor" and retro["har7"] == pytest.approx(sig.metrics.har7, rel=0.02)
    text = fmt_har(outs)
    assert "прибыльных 1: открыла бы 1 из 1" in text and "убыточных 1: открыла бы 1 из 1" in text
    assert "задним числом" in text and "по эталону research" in text and retro.get("insample")
    full = fmt_stats(compute_stats(rows, "7d", now_ms=NOW + DAY_MS), outs, sj.books_summary(0, NOW + DAY_MS))
    assert "Версия :har" in full and "Бумажные книги, короткий горизонт" in full


def test_har_outcomes_without_candles(cfg, make_client, market):
    sj = SignalJournal(cfg)
    r = dict(id=1, base="BTC", ts=NOW, settled_ms=NOW + 1, pnl=3.0, strategy="iron_condor",
             metrics=json.dumps(dict(iv7=0.6, rv7=0.4, trend="flat")))
    outs = sj.har_outcomes([r])
    assert outs[0]["known"] is False
    assert "HAR7 не посчитать 1" in fmt_har(outs)


def test_metrics_from_json_roundtrip(cfg, make_client, market):
    sig = SignalEngine(make_client(cfg), cfg).compute("BTC", NOW)
    m = sig.metrics
    d = dict(iv7=m.iv7, rv7=m.rv7, pctl=m.pctl, enough_history=m.enough_history, trend=m.trend.direction,
             ema_gap_pct=m.trend.gap_pct, har7=m.har7)
    assert har_decision(metrics_from_json("BTC", NOW, d), cfg.sig) == har_decision(m, cfg.sig)


# ---------- книги «:har» ----------
def test_paper_har_books(make_client, cfg, tmp_path, market):
    from optbot.config import PaperSettings
    ev = tmp_path / "events.csv"
    ev.write_text("# пусто\n")
    pcfg = dataclasses.replace(cfg, paper=PaperSettings(enabled=True, bases=("BTC",)), events_path=str(ev),
                               journal_enabled=True, max_order_qty={"BTC": 0, "ETH": 0, "SOL": 0},
                               max_position={"BTC": 0, "ETH": 0, "SOL": 0})
    ids = [x[0] for x in paper.all_books(pcfg)]
    assert "short:iron_condor:BTC:har" in ids and not any(i.startswith("long:") and ":har" in i for i in ids)
    assert paper.is_har_book("short:iron_condor:BTC:har:cost") and paper.is_cost_book("short:iron_condor:BTC:har:cost")
    assert paper.version_name("short:iron_condor:BTC:har:cost") == "HAR, с фильтром издержек"
    pe = paper.PaperEngine(make_client(pcfg), pcfg)
    pe.step(NOW)
    conn = db.connect(pcfg.db_file)
    books = {r["book"] for r in conn.execute("SELECT book FROM paper_trades")}
    kinds = {r["kind"] for r in conn.execute("SELECT kind FROM paper_signals")}
    met = json.loads(conn.execute("SELECT metrics FROM paper_trades WHERE book = 'short:iron_condor:BTC:har'")
                     .fetchone()["metrics"])
    conn.close()
    assert {"short:iron_condor:BTC", "short:iron_condor:BTC:har"} <= books and "short_har" in kinds
    assert met["vol_model"] == "har" and met["har7"] > 0
    freq = pe.signal_freq(0, NOW + 1)
    assert freq[("short", "BTC")] == (1, 1) and freq[("short_har", "BTC")] == (1, 1)
    text = paper.fmt_stats([], "7d", None, freq)
    assert "RV 1 из 1 (100%) · HAR 1 из 1 (100%)" in text


def test_fmt_versions_and_risk_limit_per_version():
    rows = [dict(id=1, book="short:bull_put:BTC", horizon="short", tags=None, pnl=1.0, r_mult=0.1, status="closed"),
            dict(id=2, book="short:bull_put:BTC:har", horizon="short", tags=None, pnl=-2.0, r_mult=-0.2,
                 status="closed")]
    text = paper.fmt_stats(rows, "x")
    assert "IV7/RV7 против IV7/HAR7" in text and " RV: 1 сд." in text and " HAR: 1 сд." in text
    assert "книги HAR: 1" in text


# ---------- задача бота ----------
def test_bot_vol_job_alerts_on_drift(monkeypatch):
    from optbot.bot import OptionsBot
    sent = []

    class VM:
        def refit(self, client):
            r = vol.RefitResult()
            r.drift.append(("BTC", 25.0))
            return r

    bot = OptionsBot.__new__(OptionsBot)
    bot.c = None
    bot.signals = type("S", (), {"vol": VM()})()

    async def alert(key, text, **k):
        sent.append((key, text))
        return True
    bot.admin_alert = alert
    asyncio.run(bot._vol_job(None))
    assert sent and sent[0][0] == "har_drift" and "BTC 25%" in sent[0][1]


def test_update_candles_resumes_after_failure(cfg, make_client, fake, monkeypatch):
    _history(fake, days=100)
    c = make_client(cfg)
    conn = db.connect(cfg.db_file)
    real = c.perp_klines
    calls = []

    def flaky(base, iv, end, limit=1000):
        calls.append(end)
        if len(calls) == 2:
            raise RuntimeError("10016")
        return real(base, iv, end, limit)
    monkeypatch.setattr(c, "perp_klines", flaky)
    with pytest.raises(RuntimeError):
        vol.update_candles(conn, c, "BTC", NOW)
    got = len(vol.load_candles(conn, "BTC"))
    assert got == 999                                   # первая страница сохранена до сбоя
    vol.update_candles(conn, c, "BTC", NOW)             # продолжение — с самой ранней свечи назад
    rows = vol.load_candles(conn, "BTC")
    assert len(rows) == 100 * 24 and all(t2 - t1 == HOUR_MS for (t1, _), (t2, _) in zip(rows, rows[1:]))
    conn.close()


def test_refit_skips_when_running(cfg, make_client):
    vm = vol.VolModels(cfg)
    vm._refit_lock.acquire()
    try:
        assert vm.refit(make_client(cfg), ["BTC"], NOW).candles == 0
    finally:
        vm._refit_lock.release()


def test_system_risk_long_in_both_systems():
    rows = [dict(book="short:bull_put:BTC", max_loss=500), dict(book="short:bull_put:BTC:har", max_loss=400),
            dict(book="long:iron_condor:ETH", max_loss=100), dict(book="short:bull_put:BTC:cost", max_loss=50)]
    assert paper.system_risk(rows, "short:x:SOL") == 600            # RV + длинный
    assert paper.system_risk(rows, "short:x:SOL:har") == 500        # HAR + длинный
    assert paper.system_risk(rows, "long:x:SOL") == 600             # длинный — худшая из двух систем, не сумма
    assert paper.system_risk(rows, "short:x:SOL:cost") == 50


def test_update_candles_continues_past_gap(cfg, make_client, fake, monkeypatch):
    """Пропуск свечей внутри истории (короткая страница у Bybit) — не начало: докачка идёт до пустой страницы."""
    _history(fake, days=100)
    rows = fake.klines[("BTCUSDT", "60")]
    gap = {r[0] for r in rows[1500:1530]}                       # 30 часов без свечей в середине
    c = make_client(cfg)
    real = c.perp_klines

    def windowed(base, iv, end, limit=1000):                    # как Bybit: окно — limit интервалов до end
        return [x for x in real(base, iv, end, limit) if x[0] > end - limit * HOUR_MS and str(x[0]) not in gap]
    monkeypatch.setattr(c, "perp_klines", windowed)
    conn = db.connect(cfg.db_file)
    vol.update_candles(conn, c, "BTC", NOW)
    assert len(vol.load_candles(conn, "BTC")) == 100 * 24 - 30
    conn.close()


def test_refit_fits_stored_history_when_download_fails(cfg, make_client, fake, monkeypatch):
    _history(fake, "BTC", days=500)
    c = make_client(cfg)
    conn = db.connect(cfg.db_file)
    vol.update_candles(conn, c, "BTC", NOW)
    conn.close()

    def boom(*a, **k):
        raise RuntimeError("10016: svc error: Get kline failed")
    monkeypatch.setattr(c, "perp_klines", boom)
    res = vol.VolModels(cfg).refit(c, ["BTC"], NOW)
    assert [m.base for m in res.fitted] == ["BTC"] and any("не докачаны" in e for e in res.errors)
