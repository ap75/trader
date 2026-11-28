"""История для проверок задним числом: полная цепочка опционов, фондирование и OI по срезам, индекс по дням."""
import io
import json
import sqlite3

import pytest

from optbot import collector as colmod, context
from optbot.bot import check_config
from optbot.collector import Collector, slot_start, stats
from tests.fakes import EXP_MS, set_chain
from tests.helpers import with_cfg

NOW = EXP_MS - 10 * 86_400_000 + 7 * 60_000   # слот 08:00 — час: полная цепочка пишется
DAY = 86_400_000


@pytest.fixture
def hcfg(cfg, tmp_path):
    return with_cfg(cfg, db_path=str(tmp_path / "account.db"), market_db_path=str(tmp_path / "market.db"), chain_snapshot_min=60, chain_max_dte=60,
                    chain_min_delta=0.02, chain_keep_days=0, fng_url="")


def q(cfg, sql, *a):
    conn = sqlite3.connect(cfg.market_db_file)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, a)]
    finally:
        conn.close()


def test_chain_written_on_hour_slot_with_filters(make_client, hcfg, fake):
    set_chain(fake)                                   # BTC: 41 страйк × C/P, экспирация через 10 дн.
    res = Collector(make_client(hcfg), hcfg).collect(NOW)
    rows = q(hcfg, "SELECT * FROM chain_snapshots WHERE base='BTC'")
    assert rows and res.chain >= len(rows)
    assert all(abs(r["delta"]) >= 0.02 for r in rows if r["delta"] is not None)
    n_all = sum(1 for s in fake.tickers if s.startswith("BTC-"))
    assert len(rows) < n_all                           # далёкие крылья с |Δ| < 0.02 отброшены
    r = next(r for r in rows if r["strike"] == 65000 and r["kind"] == "C")
    assert r["ts"] == slot_start(NOW, 15) and r["expiry_ms"] == EXP_MS and r["bid_size"] == 10 and r["iv"] == 0.5
    Collector(make_client(hcfg), hcfg).collect(NOW + 15 * 60_000)     # :15 — за этот час уже есть
    assert len(q(hcfg, "SELECT * FROM chain_snapshots WHERE base='BTC'")) == len(rows)


def test_chain_missed_hour_slot_taken_next(make_client, hcfg, fake):
    set_chain(fake)
    Collector(make_client(hcfg), hcfg).collect(NOW + 15 * 60_000)     # :00 не прошёл — пишет :15
    Collector(make_client(hcfg), hcfg).collect(NOW + 30 * 60_000)
    ts = {r["ts"] for r in q(hcfg, "SELECT DISTINCT ts FROM chain_snapshots WHERE base='BTC'")}
    assert ts == {slot_start(NOW + 15 * 60_000, 15)}


def test_chain_max_dte_off_and_keep_days(make_client, hcfg, fake):
    set_chain(fake)
    c = with_cfg(hcfg, chain_max_dte=5)                # экспирация через 10 дн. — за горизонтом
    Collector(make_client(c), c).collect(NOW)
    assert q(c, "SELECT COUNT(*) n FROM chain_snapshots WHERE base='BTC'")[0]["n"] == 0
    off = with_cfg(hcfg, chain_snapshot_min=0)
    assert Collector(make_client(off), off).collect(NOW).chain == 0
    keep = with_cfg(hcfg, chain_keep_days=1)
    Collector(make_client(keep), keep).collect(NOW - 3 * DAY)
    assert q(keep, "SELECT COUNT(*) n FROM chain_snapshots WHERE ts < ?", NOW - DAY)[0]["n"] > 0
    Collector(make_client(keep), keep).collect(NOW)
    assert q(keep, "SELECT COUNT(*) n FROM chain_snapshots WHERE ts < ?", NOW - DAY)[0]["n"] == 0


def test_perp_context_every_slot(make_client, hcfg, fake):
    Collector(make_client(hcfg), hcfg).collect(NOW)
    Collector(make_client(hcfg), hcfg).collect(NOW + 15 * 60_000)
    rows = q(hcfg, "SELECT * FROM perp_context WHERE base='BTC' ORDER BY ts")
    assert len(rows) == 2 and rows[0]["funding"] == 0.0001 and rows[0]["funding_h"] == 8
    assert rows[0]["oi"] == 100000 and rows[0]["oi_value"] == 6.5e9


def test_perp_context_zero_funding_is_value(make_client, hcfg, fake):
    real = fake.get_tickers

    def zero(category, **kw):
        res = real(category, **kw)
        if category == "linear":
            res["result"]["list"][0]["fundingRate"] = rate[0]
        return res
    rate = ["0"]
    fake.get_tickers = zero
    Collector(make_client(hcfg), hcfg).collect(NOW)
    assert q(hcfg, "SELECT funding FROM perp_context WHERE base='BTC'")[0]["funding"] == 0.0
    rate[0] = "-"                                                    # не число — не 0, а пусто
    Collector(make_client(hcfg), hcfg).collect(NOW + 15 * 60_000)
    assert q(hcfg, "SELECT funding FROM perp_context WHERE base='BTC' ORDER BY ts")[1]["funding"] is None


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fng_rows(days):
    return [{"value": str(40 + i), "value_classification": "Fear", "timestamp": str(d // 1000)}
            for i, d in enumerate(days)]


def test_fear_greed_backfill_then_daily(make_client, hcfg, monkeypatch):
    c = with_cfg(hcfg, fng_url="https://api.example/fng/?limit=1")
    today = NOW // DAY * DAY
    urls = []
    payload = {"limit=0": fng_rows([today - 2 * DAY, today - DAY]) + [{"value": "", "timestamp": "x"}],
               "limit=3": fng_rows([today - DAY, today])}            # битая строка — пропущена

    def urlopen(req, timeout):
        urls.append(req.full_url)
        key = next(k for k in payload if k in req.full_url)
        return Resp(json.dumps({"data": payload[key]}).encode())
    monkeypatch.setattr(context.urllib.request, "urlopen", urlopen)
    col = Collector(make_client(c), c)
    assert col.update_fear_greed(NOW) == 2 and "limit=0" in urls[0] and urls[0].count("limit=") == 1
    monkeypatch.setattr(colmod.time, "monotonic", lambda: 1e12)      # прошло больше FNG_RETRY_S
    assert col.update_fear_greed(NOW + 15 * 60_000) == 1 and "limit=3" in urls[1]   # пропуск дней + 2
    col._fng_tried = -1e18
    assert col.update_fear_greed(NOW + 30 * 60_000) == 0 and len(urls) == 2   # сегодняшний день уже есть
    assert [r["value"] for r in q(c, "SELECT value FROM fear_greed ORDER BY ts")] == [40, 41, 41]
    assert "индекс страха и жадности: 3 дн." in stats(c)


def test_fear_greed_failure_throttled_and_not_fatal(make_client, hcfg, monkeypatch):
    c = with_cfg(hcfg, fng_url="https://api.example/fng/")
    calls = []

    def boom(req, timeout):
        calls.append(1)
        raise OSError("down")
    monkeypatch.setattr(context.urllib.request, "urlopen", boom)
    col = Collector(make_client(c), c)
    res = col.collect(NOW)
    assert res.snapshots and not res.errors and calls == []         # срез источник не трогает
    assert col.update_fear_greed(NOW) == 0
    col.update_fear_greed(NOW + 15 * 60_000)
    assert calls == [1]                                              # повтор — не раньше FNG_RETRY_S


def test_stats_lists_history(make_client, hcfg, fake):
    set_chain(fake)
    Collector(make_client(hcfg), hcfg).collect(NOW)
    s = stats(hcfg)
    assert "цепочка BTC: срезов 1" in s and "фондирование и OI:" in s


def test_chain_period_not_multiple_of_interval_ok(make_client, hcfg, fake, cfg):
    assert not any("CHAIN_SNAPSHOT_MIN" in e for e in check_config(with_cfg(cfg, chain_snapshot_min=25)))
    set_chain(fake)
    c = with_cfg(hcfg, chain_snapshot_min=25)                        # период 25 мин при срезах раз в 15
    col = Collector(make_client(c), c)
    for i in range(4):
        col.collect(NOW + i * 15 * 60_000)
    assert len(q(c, "SELECT DISTINCT ts FROM chain_snapshots WHERE base='BTC'")) >= 2


def test_chain_zero_delta_filtered_and_zero_oi_kept(make_client, hcfg, fake):
    set_chain(fake)
    s65 = next(s for s in fake.tickers if "-65000-C" in s)
    s75 = next(s for s in fake.tickers if "-75000-C" in s)
    fake.tickers[s75]["delta"] = "0"                                  # далёкое крыло — отбросить
    fake.tickers[s65]["openInterest"] = "0"
    Collector(make_client(hcfg), hcfg).collect(NOW)
    syms = {r["symbol"]: r for r in q(hcfg, "SELECT * FROM chain_snapshots")}
    assert s75 not in syms and syms[s65]["oi"] == 0.0


def test_fear_greed_write_failure_keeps_rows(make_client, hcfg, monkeypatch):
    from optbot import db
    c = with_cfg(hcfg, fng_url="https://api.example/fng/")
    calls = []

    def urlopen(req, timeout):
        calls.append(1)
        return Resp(json.dumps({"data": fng_rows([NOW // DAY * DAY])}).encode())
    monkeypatch.setattr(context.urllib.request, "urlopen", urlopen)
    col = Collector(make_client(c), c)
    real = db.connect
    fail = [True]

    class Bad:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, k):
            return getattr(self.conn, k)

        def __enter__(self):
            return self.conn.__enter__()

        def __exit__(self, *a):
            return self.conn.__exit__(*a)

        def executemany(self, *a):
            import sqlite3 as s3
            raise s3.OperationalError("database is locked")
    monkeypatch.setattr(colmod.db, "connect", lambda *a, **k: Bad(real(*a, **k)) if fail[0] else real(*a, **k))
    assert col.update_fear_greed(NOW) == 0
    fail[0] = False
    assert col.update_fear_greed(NOW) == 1 and calls == [1]          # записано без повторной загрузки


def test_chain_failure_does_not_lose_snapshot(make_client, hcfg, fake, monkeypatch):
    set_chain(fake)
    col = Collector(make_client(hcfg), hcfg)
    orig = col._chain_due
    col._chain_due = lambda conn, base, ts: True
    from optbot import db
    c = db.account(hcfg)
    c.execute("DROP TABLE chain_snapshots")                          # запись цепочки упадёт
    c.commit()
    c.close()
    res = col.collect(NOW)
    assert res.snapshots == 3 and not res.errors and res.chain == 0
    col._chain_due = orig
    assert not col._chain_due(db.account(hcfg), "BTC", NOW)   # SELECT по пропавшей таблице — False


def test_fear_greed_db_error_not_fatal(make_client, hcfg):
    from optbot import db
    c = with_cfg(hcfg, fng_url="https://api.example/fng/")
    conn = db.account(c)
    conn.execute("DROP TABLE fear_greed")
    conn.commit()
    conn.close()
    assert Collector(make_client(c), c).update_fear_greed(NOW) == 0


def test_keep_days_prunes_with_chain_off(make_client, hcfg, fake):
    set_chain(fake)
    Collector(make_client(hcfg), hcfg).collect(NOW - 3 * DAY)
    off = with_cfg(hcfg, chain_snapshot_min=0, chain_keep_days=1)
    Collector(make_client(off), off).collect(NOW)
    assert q(off, "SELECT COUNT(*) n FROM chain_snapshots")[0]["n"] == 0


def test_perp_context_failure_keeps_snapshot(make_client, hcfg):
    from optbot import db
    conn = db.account(hcfg)
    conn.execute("DROP TABLE perp_context")
    conn.commit()
    conn.close()
    res = Collector(make_client(hcfg), hcfg).collect(NOW)
    assert res.snapshots == 3 and not res.errors


def test_bot_runs_fng_after_collect(cfg, monkeypatch):
    import asyncio
    from types import SimpleNamespace as NS
    from optbot.bot import OptionsBot
    b = OptionsBot.__new__(OptionsBot)
    b.cfg = with_cfg(cfg, fng_url="https://x", healthcheck_url="")
    b.cfg = with_cfg(b.cfg, paper=__import__("dataclasses").replace(b.cfg.paper, enabled=False))
    b._fng_task = None
    b._auto_change_on = lambda: False
    got = []
    b.collector = NS(collect=lambda: NS(errors=[], snapshots=1, skipped=0, failed_bases=set()),
                     update_fear_greed=lambda: got.append(1) or 2)

    async def go():
        await b._collect_job(None)
        await b._fng_task
    asyncio.run(go())
    assert got == [1]
