"""Рыночный фон: фондирование, открытый интерес, индекс страха и жадности — источники, карточка, журналы."""
import dataclasses
import io
import json

import pytest

from optbot import context, paper, sigjournal
from optbot.context import FearGreed, MarketContext, fmt_context, fng_bucket, funding_bucket, perp_context
from optbot.signals import SignalEngine, fmt_signal
from tests.fakes import NOW, set_chain, set_klines


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fng_payload(v=72, label="Greed", ts=None):
    import time
    return json.dumps({"data": [{"value": str(v), "value_classification": label,
                                 "timestamp": str(int(ts if ts is not None else time.time()))}]}).encode()


class SyncThread:  # фоновое обновление — сразу, в этом же потоке (детерминированно)
    def __init__(self, target, name=None, daemon=None):
        self.target = target

    def start(self):
        self.target()


def test_fear_greed_cache_failure_and_max_age(monkeypatch):
    calls = []
    t = [1000.0]
    monkeypatch.setattr(context.time, "monotonic", lambda: t[0])
    monkeypatch.setattr(context.threading, "Thread", SyncThread)

    def urlopen(req, timeout):
        calls.append(req.full_url)
        assert req.headers["User-agent"] == "options-bot"
        if len(calls) >= 2 and len(calls) != 3:
            raise OSError("down")
        return Resp(fng_payload(72 + len(calls)))
    monkeypatch.setattr(context.urllib.request, "urlopen", urlopen)
    fg = FearGreed("https://example.test/fng")
    assert fg.get() == (73, "Greed")
    t[0] += 100
    assert fg.get() == (73, "Greed") and len(calls) == 1            # кэш
    t[0] += context.FNG_TTL_S
    assert fg.get() == (73, "Greed") and len(calls) == 2            # сбой — прежнее значение
    t[0] += 10
    assert fg.get() == (73, "Greed") and len(calls) == 2            # после сбоя не дёргает каждый расчёт
    t[0] += context.FNG_RETRY_S
    assert fg.get() == (75, "Greed") and len(calls) == 3
    assert FearGreed("").get() is None


def test_fear_greed_stale_source_value_hidden(monkeypatch):
    import time
    monkeypatch.setattr(context.threading, "Thread", SyncThread)
    old = time.time() - context.FNG_MAX_AGE_S - 60                   # источник отдаёт значение трёхдневной давности
    monkeypatch.setattr(context.urllib.request, "urlopen", lambda req, timeout: Resp(fng_payload(50, ts=old)))
    assert FearGreed("https://x").get() is None


def test_fear_greed_first_wait_and_thread_failure(monkeypatch):
    import threading
    import time as _t
    gate = threading.Event()

    def slow(req, timeout):
        gate.wait(2)
        return Resp(fng_payload(40, "Fear"))
    monkeypatch.setattr(context.urllib.request, "urlopen", slow)
    fg = FearGreed("https://x")
    threading.Timer(0.05, gate.set).start()
    assert fg.get(first_wait=2) == (40, "Fear")                       # CLI: первый ответ дождались

    def boom(*a, **k):
        raise RuntimeError("can't start new thread")
    monkeypatch.setattr(context.threading, "Thread", boom)
    fg2 = FearGreed("https://x")
    assert fg2.get() is None and not fg2._busy


def test_fear_greed_does_not_block(monkeypatch):
    started = []
    monkeypatch.setattr(context.threading, "Thread",
                        lambda target, name=None, daemon=None: type("T", (), {"start": lambda self: started.append(1)})())
    fg = FearGreed("https://x")
    assert fg.get() is None and fg.get() is None and started == [1]   # ждать сеть не стал, запрос один


def test_fear_greed_rejects_bad_value(monkeypatch):
    monkeypatch.setattr(context.threading, "Thread", SyncThread)
    monkeypatch.setattr(context.urllib.request, "urlopen", lambda req, timeout: Resp(fng_payload(150)))
    fg = FearGreed("https://x")
    fg.get()
    assert fg.get() is None


def test_perp_context(make_client, cfg, fake):
    ctx = perp_context(make_client(cfg), "BTC")
    assert ctx.funding == 0.0001 and ctx.funding_h == 8 and ctx.oi_value == 6.5e9
    assert ctx.oi_chg_24h == pytest.approx(1.024 / 1 - 1)
    fake.oi_hist = [1.0] * 10                                        # меньше суток истории — изменения нет
    assert perp_context(make_client(cfg), "BTC").oi_chg_24h is None


def test_perp_context_failures_leave_none(make_client, cfg, fake):
    fake.fail["get_tickers"] = OSError("x")
    fake.fail["get_open_interest"] = OSError("y")
    ctx = perp_context(make_client(cfg), "BTC")
    assert ctx == MarketContext()


@pytest.mark.parametrize("v,b", [(None, "нет данных"), (0, "крайний страх"), (24, "крайний страх"), (25, "страх"),
                                 (45, "нейтрально"), (55, "нейтрально"), (56, "жадность"), (76, "крайняя жадность")])
def test_fng_bucket(v, b):
    assert fng_bucket(v) == b


def test_fng_bucket_prefers_source_label():
    assert fng_bucket(55, "Greed") == "жадность" and fng_bucket(55, "что-то") == "нейтрально"


def test_oi_change_needs_exact_24h_point(make_client, cfg, fake):
    from optbot.context import perp_oi_change
    real = fake.get_open_interest

    def gap(category, symbol, intervalTime, limit=50, **kw):   # пропущен час ровно сутки назад
        res = real(category, symbol, intervalTime, limit)
        res["result"]["list"] = [r for i, r in enumerate(res["result"]["list"]) if i != 24]
        return res
    fake.get_open_interest = gap
    assert perp_oi_change(make_client(cfg), "BTC") is None

    def shifted(category, symbol, intervalTime, limit=50, **kw):   # новейшая метка не круглая (+10 мин)
        res = real(category, symbol, intervalTime, limit)
        res["result"]["list"][0]["timestamp"] = str(int(res["result"]["list"][0]["timestamp"]) + 600_000)
        return res
    fake.get_open_interest = shifted
    assert perp_oi_change(make_client(cfg), "BTC") == pytest.approx(1.024 - 1)


def test_funding_bucket():
    assert funding_bucket(None) == "нет данных"
    assert funding_bucket(-0.00001) == "отрицательное"
    assert funding_bucket(0.0001) == "обычное (≤ 0.01%/8ч)"
    assert funding_bucket(0.0002) == "высокое (> 0.01%/8ч)"
    assert funding_bucket(0.0001, 4) == "высокое (> 0.01%/8ч)"   # за 4 ч = 0.02% за 8 ч


def test_fmt_context():
    assert fmt_context(None) is None and fmt_context(MarketContext()) is None
    s = fmt_context(MarketContext(funding=0.0001, funding_h=8, oi_value=1.23e9, oi_chg_24h=0.031, fng=72,
                                  fng_label="Greed"))
    assert s == " фондирование +0.0100%/8ч · OI 1.23 млрд $ (+3.1% за 24ч) · страх/жадность 72 (Greed)"
    assert "OI 45.0 млн $" in fmt_context(MarketContext(oi_value=4.5e7))


@pytest.fixture
def sig_with_ctx(make_client, cfg, fake, monkeypatch):
    set_chain(fake, iv=0.6)
    set_klines(fake, rv=0.4)
    monkeypatch.setattr("optbot.signals.fear_greed", lambda url: type("F", (), {"get": lambda self, first_wait=0: (20, "Extreme Fear")})())
    sig = SignalEngine(make_client(cfg), cfg).compute("BTC", NOW)
    assert sig.legs, sig.no_signal
    return sig


def test_card_shows_context(sig_with_ctx):
    c = sig_with_ctx.metrics.ctx
    assert c.fng == 20 and c.funding == 0.0001
    text = fmt_signal(sig_with_ctx, "demo")
    assert "страх/жадность 20 (Extreme Fear)" in text and "фондирование" in text and "фон, справочно" in text


def test_sigjournal_stores_context_and_splits_stats(cfg, sig_with_ctx):
    sj = sigjournal.SignalJournal(cfg)
    sid = sj.record(sig_with_ctx, now_ms=NOW)
    row = sj.rows(0, NOW + 1)[0]
    m = json.loads(row["metrics"])
    assert row["id"] == sid and m["fng"] == 20 and m["funding"] == 0.0001 and m["oi_chg_24h"] is not None
    base = dict(settled_ms=NOW, expiry_ms=NOW, pnl=10.0, r_mult=0.5, half_ms=None, max_dd=None, max_loss=20.0,
                strategy="iron_condor", regime="high")
    rows = [dict(base, metrics=json.dumps({"fng": 20, "funding": 0.0001})),
            dict(base, pnl=-5.0, r_mult=-0.25, metrics=json.dumps({"fng": 80, "funding": -0.0001})),
            dict(base, metrics=None)]
    text = sigjournal.fmt_stats(sigjournal.compute_stats(rows, "7d", now_ms=NOW))
    assert "По индексу страха и жадности" in text and "крайний страх" in text and "крайняя жадность" in text
    assert "По фондированию" in text and "отрицательное" in text and "нет данных" in text
    old = sigjournal.fmt_stats(sigjournal.compute_stats([dict(base, metrics="{}")], "7d", now_ms=NOW))
    assert "страха" not in old                                       # до записи фона — без пустой таблицы


def test_paper_stores_context_and_splits_stats(make_client, cfg, sig_with_ctx, tmp_path):
    from tests.test_paper import PC
    ev = tmp_path / "events.csv"
    ev.write_text("# пусто\n")
    pcfg = dataclasses.replace(cfg, paper=PC, events_path=str(ev), max_order_qty={"BTC": 0, "ETH": 0, "SOL": 0},
                               max_position={"BTC": 0, "ETH": 0, "SOL": 0})
    pe = paper.PaperEngine(make_client(pcfg), pcfg)
    bid = paper.book_id(sig_with_ctx.horizon, sig_with_ctx.strategy, "BTC")
    r = pe.step(NOW, {"BTC": {bid: sig_with_ctx}})
    assert r.opened, r.skipped
    conn = __import__("optbot.db", fromlist=["db"]).connect(pcfg.db_file)
    row = dict(conn.execute("SELECT * FROM paper_trades").fetchone())
    conn.close()
    assert json.loads(row["metrics"])["fng"] == 20
    closed = [dict(row, status="closed", pnl=5.0, r_mult=0.2, closed_ms=NOW)]
    text = paper.fmt_stats(closed, "7d")
    assert "По индексу страха и жадности" in text and "крайний страх: 1 сд." in text
    assert "обычное (≤ 0.01%/8ч): 1 сд." in text
    csv_text = paper.to_csv(closed)
    head, line = csv_text.splitlines()[:2]
    cols = head.split(",")
    assert line.split(",")[cols.index("fng")] == "20"
