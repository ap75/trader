"""DVOL Deribit (optbot/dvol.py): докачка, k = IV30/DVOL, перцентиль IV30 по DVOL в сигнале, задача бота."""
import asyncio
import dataclasses
import io
import json
import urllib.parse

import pytest

from optbot import db, dvol
from optbot.fmt import DAY_MS, HOUR_MS
from optbot.signals import SignalEngine, fmt_signal, percentile
from tests.fakes import NOW, set_chain, set_klines
from tests.test_paper import FAR, FAR_MS
from tests.test_signals import add_history

H0 = NOW // HOUR_MS * HOUR_MS


def series(days: int, end_ms: int = H0, f=lambda i: 0.40 + 0.001 * i) -> dict[int, float]:
    """Часовые бары DVOL (начало бара -> доля) до end_ms (последний начался за час до end_ms)."""
    n = days * 24
    return {end_ms - (n - i) * HOUR_MS: f(i) for i in range(n)}


class Deribit:
    """Подставной get_volatility_index_data: до PAGE последних баров с меткой в [start, end]."""
    def __init__(self, data: dict[int, float]):
        self.data, self.calls = data, []

    def __call__(self, url, base, start_ms, end_ms):
        self.calls.append((base, start_ms, end_ms))
        ts = sorted(t for t in self.data if start_ms <= t <= end_ms)[-dvol.PAGE:]
        return [(t, self.data[t]) for t in ts]


def put_dvol(cfg, base, data: dict[int, float]):
    conn = db.account(cfg)
    with conn:
        conn.executemany("INSERT INTO dvol_h1 (base, ts, close) VALUES (?,?,?)", [(base, t, v) for t, v in data.items()])
    conn.close()


@pytest.fixture
def cfg(cfg):
    """Перцентиль по DVOL — в решениях (DVOL_PCTL_ENABLED=1); выключенный — test_dvol_pctl_disabled_*."""
    return dataclasses.replace(cfg, sig=dataclasses.replace(cfg.sig, dvol_pctl_enabled=True))


@pytest.fixture
def dcfg(cfg):
    return dataclasses.replace(cfg, dvol_url="https://example.test/dvol")


# ---------- данные ----------
def test_fetch_page_parses_and_filters(monkeypatch):
    seen = {}

    def urlopen(req, timeout):
        seen["url"] = req.full_url
        body = {"result": {"data": [[1000, 50, 51, 49, 50.5], [2000, 50, 52, 49, 51.0], [9000, 1, 1, 1, 60.0]],
                           "continuation": None}}
        return io.BytesIO(json.dumps(body).encode())
    monkeypatch.setattr(dvol.urllib.request, "urlopen", urlopen)
    got = dvol.fetch_page("https://x.test/api", "BTC", 1000, 5000)
    assert got == [(1000, 0.505), (2000, 0.51)]                    # вне окна — нет; % -> доля
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(seen["url"]).query)
    assert q == {"currency": ["BTC"], "start_timestamp": ["1000"], "end_timestamp": ["5000"], "resolution": ["3600"]}

    monkeypatch.setattr(dvol.urllib.request, "urlopen",
                        lambda req, timeout: io.BytesIO(b'{"error": {"message": "bad"}}'))
    with pytest.raises(ValueError, match="без result"):
        dvol.fetch_page("https://x.test/api", "BTC", 0, 1)


def test_update_pages_back_fills_window_and_increments(dcfg, monkeypatch):
    monkeypatch.setattr(dvol, "PAGE", 100)
    data = series(80, H0 + HOUR_MS)          # последний бар ещё не закрыт к NOW — не сохраняется
    fake = Deribit(data)
    conn = db.account(dcfg)
    res = dvol.update(conn, dcfg, NOW, fake, bases=("BTC",))
    lo = NOW - (dcfg.sig.lookback_days + dvol.SPARE_DAYS) * DAY_MS
    rows = dict(conn.execute("SELECT ts, close FROM dvol_h1 WHERE base = 'BTC'").fetchall())
    want = {t: v for t, v in data.items() if t >= lo and t + HOUR_MS <= NOW}
    assert rows == pytest.approx(want) and res.added == {"BTC": len(want)} and not res.stale
    assert len(fake.calls) > 10                                   # страницами по 100 назад
    # следующий день: только новое — от последнего сохранённого бара
    fake.calls.clear()
    res = dvol.update(conn, dcfg, NOW + DAY_MS, Deribit(series(80, H0 + DAY_MS)), bases=("BTC",))
    assert res.added["BTC"] == 24
    # окно выросло (SIG_LOOKBACK_DAYS больше) — недостающее начало назад от первого бара
    wide = dataclasses.replace(dcfg, sig=dataclasses.replace(dcfg.sig, lookback_days=70))
    first = min(rows)
    lo2 = NOW + DAY_MS - (70 + dvol.SPARE_DAYS) * DAY_MS
    res = dvol.update(conn, wide, NOW + DAY_MS, Deribit(series(80, H0 + DAY_MS)), bases=("BTC",))
    assert res.added["BTC"] == sum(1 for t in series(80, H0 + DAY_MS) if lo2 <= t < first) == 9 * 24
    conn.close()


def test_update_errors_stale_and_no_url(dcfg, cfg):
    conn = db.account(dcfg)

    def boom(*a):
        raise OSError("нет сети")
    res = dvol.update(conn, dcfg, NOW, boom, bases=("BTC",))
    assert res.errors and "нет сети" in res.errors[0] and res.stale == ["BTC"]
    conn.executemany("INSERT INTO dvol_h1 (base, ts, close) VALUES ('BTC', ?, 0.5)",
                     [(H0 - 5 * DAY_MS,)])
    conn.commit()
    calls = []
    res = dvol.update(conn, cfg, NOW, lambda *a: calls.append(a) or [], bases=("BTC",))   # DVOL_URL пуст
    assert calls == [] and res.stale == ["BTC"] and not res.errors and "устарел" in str(res)
    sol = dvol.update(conn, cfg, NOW, bases=("SOL",))
    assert sol.stale == ["SOL"]                       # SOL не в BASES бота, но проверка не падает
    conn.close()


def test_ratio_own_or_research():
    dv = {H0 - (i + 1) * HOUR_MS: 0.5 for i in range(100)}
    own = [(H0 - i * HOUR_MS + 15 * 60_000, 0.47) for i in range(60)]   # срез в :15 — DVOL прошлого часа
    k, n, src = dvol.ratio(own, dv, "BTC")
    assert k == pytest.approx(0.94) and n == 60 and src == "own"
    k, n, src = dvol.ratio(own[:10], dv, "BTC")
    assert k == dvol.REF_RATIO["BTC"] and n == 10 and src == "research"
    assert dvol.bar_for(H0 + 15 * 60_000) == H0 - HOUR_MS and dvol.bar_for(H0) == H0 - HOUR_MS


# ---------- сигнал ----------
@pytest.fixture
def long_market(fake):
    set_chain(fake, iv=0.6, exp=FAR, exp_ms=FAR_MS, step=1000)
    set_klines(fake, rv=0.4)
    return fake


def test_long_percentile_from_dvol(cfg, make_client, long_market):
    data = series(60)
    put_dvol(cfg, "BTC", data)
    put_dvol(cfg, "BTC", {H0 - 200 * DAY_MS: 0.99})            # вне окна SIG_LOOKBACK_DAYS — не в истории
    se = SignalEngine(make_client(cfg), cfg)
    sig = se.compute("BTC", NOW, horizon="long")
    m = sig.metrics
    k = dvol.REF_RATIO["BTC"]                                   # своих срезов нет — оценка research
    vals = [v for t, v in data.items() if t + HOUR_MS >= NOW - cfg.sig.lookback_days * DAY_MS]
    assert m.pctl_src == "dvol" and m.dvol_ratio == k and m.dvol_ratio_src == "research"
    assert m.pctl == pytest.approx(percentile(vals, m.iv30 / k)) and m.enough_history and not m.history_stale
    assert m.history_days == pytest.approx(len(vals) / 24)
    assert any(r.startswith("pctl ") for r in sig.reasons)       # pctl участвует в решении
    assert "по DVOL Deribit" in fmt_signal(sig, "demo") and "оценка research" in fmt_signal(sig, "demo")
    # короткий горизонт и SOL — по-прежнему по своей истории
    assert se.compute("BTC", NOW).metrics.pctl_src is None


def test_long_percentile_dvol_own_ratio(cfg, make_client, long_market):
    put_dvol(cfg, "BTC", series(60, f=lambda i: 0.64))
    add_history(cfg, "BTC", [0.6] * 72, NOW - 72 * HOUR_MS, step_ms=HOUR_MS, dte=30)
    m = SignalEngine(make_client(cfg), cfg).compute("BTC", NOW, horizon="long").metrics
    assert m.pctl_src == "dvol" and m.dvol_ratio_src == "own" and m.dvol_ratio == pytest.approx(0.6 / 0.64)
    assert m.pctl == pytest.approx(50)                          # IV30 / k = 0.64 — все равны


def test_long_percentile_falls_back(cfg, make_client, long_market, fake):
    put_dvol(cfg, "BTC", series(60, H0 - 4 * DAY_MS))           # устарел (> 3 дн.)
    m = SignalEngine(make_client(cfg), cfg).compute("BTC", NOW, horizon="long").metrics
    assert m.pctl_src is None and not m.enough_history

    # своей истории год и больше — своя
    conn = db.account(cfg)
    conn.execute("DELETE FROM dvol_h1")
    conn.commit()
    conn.close()
    put_dvol(cfg, "BTC", series(60))
    add_history(cfg, "BTC", [0.5], NOW - 400 * DAY_MS, dte=30)
    m = SignalEngine(make_client(cfg), cfg).compute("BTC", NOW, horizon="long").metrics
    assert m.pctl_src is None

    # SOL — DVOL нет
    set_chain(fake, base="SOL", F=150.0, iv=0.6, exp=FAR, exp_ms=FAR_MS, step=5)
    set_klines(fake, base="SOL", rv=0.4)
    put_dvol(cfg, "SOL", series(60))                            # даже если есть строки — SOL не по DVOL
    m = SignalEngine(make_client(cfg), cfg).compute("SOL", NOW, horizon="long").metrics
    assert m.pctl_src is None


def test_dvol_db_error_falls_back(cfg, make_client, long_market, monkeypatch):
    def boom(*a):
        raise RuntimeError("испорчено")
    monkeypatch.setattr(dvol, "bars", boom)
    m = SignalEngine(make_client(cfg), cfg).compute("BTC", NOW, horizon="long").metrics
    assert m.pctl_src is None and m.iv30


def test_journal_records_dvol(cfg, make_client, long_market):
    from optbot.sigjournal import SignalJournal
    cfg = dataclasses.replace(cfg, journal_enabled=True)
    put_dvol(cfg, "BTC", series(60, f=lambda i: 0.30 + 0.0001 * i))   # IV30 / k выше всей истории — pctl 100
    sig = SignalEngine(make_client(cfg), cfg).compute("BTC", NOW, horizon="long")
    assert sig.metrics.pctl == 100 and sig.strategy == "iron_condor", sig.no_signal
    sid = SignalJournal(cfg).record(sig, now_ms=NOW)
    conn = db.account(cfg)
    met = json.loads(conn.execute("SELECT metrics FROM signals WHERE id = ?", (sid,)).fetchone()["metrics"])
    conn.close()
    assert met["pctl_src"] == "dvol" and met["dvol_ratio"] == dvol.REF_RATIO["BTC"] and met["har30"] > 0


# ---------- бот ----------
def test_bot_dvol_job_alerts_when_stale(cfg, monkeypatch):
    from optbot import bot as bot_mod
    from optbot.bot import OptionsBot
    sent = []
    bot = OptionsBot.__new__(OptionsBot)
    bot.cfg = cfg

    async def alert(key, text, **k):
        sent.append((key, text))
        return True
    bot.admin_alert = alert
    monkeypatch.setattr(bot_mod.dvol_mod, "update", lambda conn, c: dvol.UpdateResult(stale=["BTC"]))
    asyncio.run(bot._dvol_job(None))
    assert sent and sent[0][0] == "dvol_stale" and "BTC" in sent[0][1]
    sent.clear()
    monkeypatch.setattr(bot_mod.dvol_mod, "update", lambda conn, c: dvol.UpdateResult(added={"BTC": 24}))
    asyncio.run(bot._dvol_job(None))
    assert sent == []


def test_check_config_dvol_url(cfg):
    from optbot.bot import check_config
    bad = dataclasses.replace(cfg, dvol_url="http://x")
    assert any("DVOL_URL" in e for e in check_config(bad))


def test_dvol_pctl_disabled_reference_only(cfg, make_client, long_market):
    off = dataclasses.replace(cfg, sig=dataclasses.replace(cfg.sig, dvol_pctl_enabled=False))
    put_dvol(cfg, "BTC", series(60, f=lambda i: 0.30 + 0.0001 * i))
    sig = SignalEngine(make_client(off), off).compute("BTC", NOW, horizon="long")
    m = sig.metrics
    assert m.pctl_src is None and m.pctl is None and not m.enough_history       # решение — как без DVOL
    assert m.dvol_pctl == 100 and m.dvol_ratio == dvol.REF_RATIO["BTC"]
    assert any("pctl не используется" in r for r in sig.reasons)
    assert "по DVOL 100 — справочно" in fmt_signal(sig, "demo")
    on = SignalEngine(make_client(cfg), cfg).compute("BTC", NOW, horizon="long").metrics
    assert on.pctl_src == "dvol" and on.pctl == on.dvol_pctl == 100


def test_dvol_flag_changes_journal_version_only_when_on(cfg):
    from optbot.config import SignalSettings
    from optbot.sigjournal import cfg_version
    off = dataclasses.replace(cfg, sig=dataclasses.replace(cfg.sig, dvol_pctl_enabled=False))
    h_off, raw_off = cfg_version(off)
    h_on, raw_on = cfg_version(cfg)
    assert h_off != h_on and "dvol_pctl_enabled" not in raw_off and '"dvol_pctl_enabled": true' in raw_on
    # выключенный флаг — та же версия, что до появления флага (прежние записи журнала не отделяются)
    import hashlib
    import json
    from dataclasses import asdict
    from optbot.config import SIG_LONG_FIELDS
    old = {k: v for k, v in asdict(off.sig).items() if k not in SIG_LONG_FIELDS and k != "dvol_pctl_enabled"}
    assert h_off == hashlib.sha1(json.dumps(old, sort_keys=True).encode()).hexdigest()[:8]
    assert SignalSettings().dvol_pctl_enabled is False                            # по умолчанию выключен
