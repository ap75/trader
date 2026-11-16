"""Сборщик рыночных данных и база: расчёты, запись срезов, идемпотентность, миграции."""
import asyncio
import os
import sqlite3
from types import SimpleNamespace as NS

import threading

import pytest
from pybit.exceptions import InvalidRequestError

from optbot import db
from optbot.bot import OptionsBot
from optbot.bybit_client import spread_pct
from optbot.collector import (
    Collector, expiry_metrics, iv_at_delta, main, seconds_to_next_slot, slot_start, stats,
)
from tests.fakes import BTC_PUT, EXP, EXP_MS
from tests.helpers import track_symbols, with_cfg

NOW = EXP_MS - 10 * 86_400_000 + 7 * 60_000  # за 10 дней до экспирации, 7 минут в слоте


@pytest.fixture
def cfg_db(cfg, tmp_path):
    return with_cfg(cfg, db_path=str(tmp_path / "market.db"))


def q(cfg, sql, *a):
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, a)]
    finally:
        conn.close()


# ---------- чистые функции ----------
def test_slot_start():
    assert slot_start(15 * 60_000 * 3 + 59_999, 15) == 15 * 60_000 * 3


@pytest.mark.parametrize("now,expected", [(0, 905), (60, 845), (899.5, 5.5)])
def test_seconds_to_next_slot(now, expected):
    assert seconds_to_next_slot(now, 15) == pytest.approx(expected)


def test_iv_at_delta_interpolates():
    pts = [(0.18, 0.38), (0.24, 0.39), (0.31, 0.40)]
    assert iv_at_delta(pts, 0.25) == pytest.approx(0.39 + 0.01 * (0.01 / 0.07))


def test_iv_at_delta_puts_and_bounds():
    puts = [(-0.31, 0.47), (-0.25, 0.49), (-0.21, 0.51)]
    assert iv_at_delta(puts, -0.25) == pytest.approx(0.49)
    assert iv_at_delta(puts, -0.4) is None         # вне диапазона — без экстраполяции
    assert iv_at_delta([(0.2, 0.0), (0.3, 0.4)], 0.25) is None  # нулевая IV отбрасывается


def row(k, cp, iv, delta, bid="0", ask="0", mark="0", oi="1", F="65000"):
    return {"symbol": f"BTC-{EXP}-{k}-{cp}-USDT", "markIv": iv, "delta": delta, "bid1Price": bid,
            "ask1Price": ask, "markPrice": mark, "openInterest": oi, "underlyingPrice": F, "indexPrice": "64990"}


ROWS = [
    row(60000, "P", "0.50", "-0.20"), row(60000, "C", "0.40", "0.85"),
    row(63000, "P", "0.46", "-0.30"), row(63000, "C", "0.41", "0.70"),
    row(65000, "P", "0.42", "-0.48", bid="1000", ask="1020", mark="1010", oi="3"),
    row(65000, "C", "0.40", "0.52", bid="1100", ask="1110", mark="1105", oi="4"),
    row(67000, "C", "0.39", "0.30"), row(67000, "P", "0.41", "-0.70"),
    row(70000, "C", "0.38", "0.20"), row(70000, "P", "0.40", "-0.90"),
]


def test_expiry_metrics():
    m = expiry_metrics(ROWS, NOW, EXP_MS)
    assert m["dte"] == pytest.approx(10 - 7 / 1440)
    assert (m["atm_strike"], m["underlying"], m["index_price"]) == (65000, 65000, 64990)
    assert m["atm_iv"] == pytest.approx(0.41)
    assert m["straddle_mark"] == pytest.approx(2115)
    assert m["straddle_mid"] == pytest.approx(1010 + 1105)
    assert m["iv_c25"] == pytest.approx(0.385)       # между Δ0.20 (0.38) и Δ0.30 (0.39)
    assert m["iv_p25"] == pytest.approx(0.48)        # между Δ-0.30 (0.46) и Δ-0.20 (0.50)
    assert m["rr25"] == pytest.approx(0.385 - 0.48)
    assert m["atm_spread_pct"] == pytest.approx(((20 / 1010) + (10 / 1105)) / 2 * 100)
    assert (m["atm_oi"], m["n_quotes"]) == (7, 2)


def test_expiry_metrics_no_mid_without_quotes():
    rows = [r | {"bid1Price": "0"} for r in ROWS]
    m = expiry_metrics(rows, NOW, EXP_MS)
    assert m["straddle_mid"] is None and m["atm_spread_pct"] is None
    assert m["straddle_mark"] == pytest.approx(2115) and m["n_quotes"] == 2  # ask есть — котировка есть


def test_expiry_metrics_needs_both_atm_legs():
    # одна нога без mark/IV/котировки — ATM-метрики пустые, а не «половина стрэддла»
    rows = [r | {"markPrice": "0", "markIv": "0", "bid1Price": "0"} if r["symbol"].endswith("65000-P-USDT") else r
            for r in ROWS]
    m = expiry_metrics(rows, NOW, EXP_MS)
    assert m["straddle_mark"] is None and m["atm_iv"] is None and m["straddle_mid"] is None
    assert m["atm_spread_pct"] is None and m["rr25"] is not None


@pytest.mark.parametrize("bid,ask,out", [(95, 105, 10), (0, 105, None), (95, None, None), (-1, 5, None)])
def test_spread_pct(bid, ask, out):
    assert spread_pct(bid, ask) == (pytest.approx(out) if out is not None else None)


def test_expiry_metrics_expired_or_empty():
    assert expiry_metrics(ROWS, EXP_MS + 1, EXP_MS) is None
    assert expiry_metrics([], NOW, EXP_MS) is None
    assert expiry_metrics([row(65000, "C", "0.4", "0.5")], NOW, EXP_MS) is None  # нет пары колл/пут


# ---------- запись в БД ----------
def test_collect_writes_snapshots_and_spot(make_client, cfg_db, fake):
    res = Collector(make_client(cfg_db), cfg_db).collect(NOW)
    assert res.snapshots == 3 and not res.errors      # BTC, ETH, SOL — по одной экспирации
    snaps = q(cfg_db, "SELECT * FROM snapshots ORDER BY base")
    assert [s["base"] for s in snaps] == ["BTC", "ETH", "SOL"]
    assert snaps[0]["ts"] == slot_start(NOW, 15) and snaps[0]["expiry"] == EXP
    spot = q(cfg_db, "SELECT * FROM spot WHERE base='BTC'")[0]
    assert (spot["index_price"], spot["perp_price"]) == (64990, 65010)
    assert snaps[0]["expiry_ms"] == EXP_MS              # время экспирации — из справочника (deliveryTime)


def test_collect_same_slot_is_idempotent(make_client, cfg_db):
    c = Collector(make_client(cfg_db), cfg_db)
    c.collect(NOW)
    assert c.collect(NOW + 60_000).snapshots == 0     # тот же 15-минутный слот
    assert c.collect(NOW + 15 * 60_000).snapshots == 3
    assert q(cfg_db, "SELECT COUNT(*) n FROM snapshots")[0]["n"] == 6


def test_collect_error_in_one_base_does_not_stop_others(make_client, cfg_db, fake):
    real = fake.get_tickers

    def flaky(category, baseCoin=None, **kw):
        if baseCoin == "ETH":
            raise RuntimeError("boom")
        return real(category, baseCoin=baseCoin, **kw)
    fake.get_tickers = flaky
    res = Collector(make_client(cfg_db), cfg_db).collect(NOW)
    assert res.snapshots == 2 and len(res.errors) == 1 and "ETH" in res.errors[0]


def test_collect_without_perp(make_client, cfg_db, fake):
    real = fake.get_tickers

    def no_perp(category, **kw):
        if category == "linear":
            raise InvalidRequestError("req", "symbol invalid", 10001, "0", {})
        return real(category, **kw)
    fake.get_tickers = no_perp
    assert Collector(make_client(cfg_db), cfg_db).collect(NOW).snapshots == 3
    assert q(cfg_db, "SELECT perp_price FROM spot WHERE base='BTC'")[0]["perp_price"] is None


def test_tracked_legs(make_client, cfg_db):
    track_symbols(cfg_db.db_path, [BTC_PUT], until_ms=EXP_MS, reason="sig1")
    track_symbols(cfg_db.db_path, [BTC_PUT], until_ms=EXP_MS - 1)   # не сокращает слежение
    res = Collector(make_client(cfg_db), cfg_db).collect(NOW)
    assert res.legs == 1
    leg = q(cfg_db, "SELECT * FROM leg_marks")[0]
    assert (leg["symbol"], leg["mark"], leg["bid"], leg["ask"]) == (BTC_PUT, 100, 95, 105)
    assert q(cfg_db, "SELECT until_ms FROM tracked_symbols")[0]["until_ms"] == EXP_MS
    assert Collector(make_client(cfg_db), cfg_db).collect(EXP_MS + 1).legs == 0  # после экспирации — нет


def test_stats(make_client, cfg_db):
    assert "данных нет" in stats(cfg_db.db_path)
    Collector(make_client(cfg_db), cfg_db).collect(NOW)
    assert "BTC: срезов 1" in stats(cfg_db.db_path)


def test_cli_once_and_stats(monkeypatch, make_client, cfg_db, capsys):
    monkeypatch.setattr("optbot.collector.settings", cfg_db)
    monkeypatch.setattr("optbot.collector.OptionsClient", lambda: make_client(cfg_db))
    assert main(["--once"]) == 0
    assert "срезов" in capsys.readouterr().out
    assert main(["--stats"]) == 0


# ---------- база ----------
def test_migrations_idempotent(tmp_path):
    p = str(tmp_path / "x" / "m.db")
    db.connect(p).close()
    conn = db.connect(p)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.close()


def test_newer_schema_rejected(tmp_path):
    p = str(tmp_path / "m.db")
    conn = sqlite3.connect(p)
    conn.execute(f"PRAGMA user_version={len(db.MIGRATIONS) + 1}")
    conn.close()
    with pytest.raises(RuntimeError, match="новее кода"):
        db.connect(p)


def test_migration_statements_have_no_semicolons():
    # операторы выполняются по одному через execute — «;» внутри (даже в комментарии) разорвал бы схему
    assert all(";" not in st for m in db.MIGRATIONS for st in m)


def test_concurrent_first_open(tmp_path):
    p = str(tmp_path / "m.db")
    errors = []

    def open_db():
        try:
            db.connect(p).close()
        except Exception as e:
            errors.append(e)
    ts = [threading.Thread(target=open_db) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert errors == []


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS + [["CREATE TABLE t (x)", "CREATE TABLE t (x)"]])
    p = str(tmp_path / "m.db")
    with pytest.raises(sqlite3.OperationalError):
        db.connect(p)
    conn = sqlite3.connect(p)
    # все миграции — одна транзакция: при сбое схема откатывается целиком, без полуприменённых таблиц
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    assert not conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    conn.close()


# ---------- настройки и бот ----------
@pytest.mark.parametrize("bad", [0, 7, 45, 61, "15"])
def test_bad_interval_disables_collector_not_bot(cfg, bad, caplog):
    c = with_cfg(cfg, collector_interval_min=bad)       # не бросает: бот и CLI должны запускаться
    assert (c.collector_enabled, c.collector_interval_min) == (False, 15)
    assert "сборщик ВЫКЛЮЧЕН" in caplog.text
    assert with_cfg(cfg, collector_interval_min=30).collector_enabled


@pytest.mark.parametrize("raw,out", [("", 15), (" 30 ", 30), ("15m", 0), ("7", 0), ("120", 0)])
def test_minutes_divisor_env(monkeypatch, raw, out):
    from optbot.config import _minutes_divisor
    monkeypatch.setenv("X_MIN", raw)
    assert _minutes_divisor("X_MIN", 15) == out


def test_db_file_per_env_and_from_project_root(cfg, tmp_path):
    from optbot.config import ROOT
    assert with_cfg(cfg, db_path="").db_file == os.path.join(ROOT, "data", "market-demo.db")
    assert not cfg.db_file.startswith(ROOT)            # тесты никогда не пишут в настоящую БД проекта
    assert with_cfg(cfg, env="mainnet", db_path="").db_file.endswith("market-mainnet.db")  # от итогового env
    assert with_cfg(cfg, db_path="x/my.db").db_file == os.path.join(ROOT, "x", "my.db")
    assert with_cfg(cfg, db_path=str(tmp_path / "a.db")).db_file == str(tmp_path / "a.db")


def test_db_recreated_if_deleted(tmp_path):
    p = str(tmp_path / "m.db")
    db.connect(p).close()
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(p + suffix):
            os.remove(p + suffix)
    conn = db.connect(p)
    assert conn.execute("SELECT COUNT(*) FROM tracked_symbols").fetchone()[0] == 0
    conn.close()


def test_chain_without_parsable_symbols(client, fake):
    from optbot.bybit_client import BybitError
    fake.tickers.clear()
    fake.tickers["BTC-27NOV26-x-Q-USDT"] = {"symbol": "BTC-27NOV26-x-Q-USDT", "underlyingPrice": "1"}
    with pytest.raises(BybitError, match="Нет данных"):
        client.chain("BTC", EXP)


def test_perp_unexpected_error_keeps_options(make_client, cfg_db, monkeypatch):
    c = make_client(cfg_db)
    monkeypatch.setattr(c, "perp_ticker", lambda base: 1 / 0)
    assert Collector(c, cfg_db).collect(NOW).snapshots == 3


def test_missing_tracked_legs_reported(make_client, cfg_db, caplog):
    track_symbols(cfg_db.db_path, ["XRP-27NOV26-1-C-USDT"], until_ms=EXP_MS)
    res = Collector(make_client(cfg_db), cfg_db).collect(NOW)
    assert res.legs_missing == ["XRP-27NOV26-1-C-USDT"] and "нет котировок ног" in str(res)


def test_track_reason_follows_longer_tracking(cfg_db):
    track_symbols(cfg_db.db_path, [BTC_PUT], until_ms=100, reason="A")
    track_symbols(cfg_db.db_path, [BTC_PUT], until_ms=50, reason="B")
    assert q(cfg_db, "SELECT reason FROM tracked_symbols")[0]["reason"] == "A"
    track_symbols(cfg_db.db_path, [BTC_PUT], until_ms=200, reason="C")
    assert q(cfg_db, "SELECT reason, until_ms FROM tracked_symbols")[0] == {"reason": "C", "until_ms": 200}


def test_rerun_same_slot_reports_skipped(make_client, cfg_db):
    c = Collector(make_client(cfg_db), cfg_db)
    c.collect(NOW)
    res = c.collect(NOW + 60_000)
    assert res.snapshots == 0 and res.skipped == 3 and "уже записан" in str(res)
    assert q(cfg_db, "SELECT captured_ms FROM snapshots LIMIT 1")[0]["captured_ms"] == NOW  # первая запись цела


def test_bad_symbol_does_not_abort_base(make_client, cfg_db, fake):
    fake.tickers["BTC-WEIRD"] = {"symbol": "BTC-WEIRD", "markPrice": "1"}
    fake.tickers["BTC-27NOV26-abc-C-USDT"] = {"symbol": "BTC-27NOV26-abc-C-USDT", "markPrice": "1"}
    res = Collector(make_client(cfg_db), cfg_db).collect(NOW)
    assert res.snapshots == 3 and not res.errors


def test_parse_option_symbol_and_group():
    from decimal import Decimal
    from optbot.bybit_client import group_by_strike, parse_option_symbol
    assert parse_option_symbol("BTC-27NOV26-60000-P-USDT") == ("BTC", "27NOV26", Decimal("60000"), "P")
    for bad in ("BTC-WEIRD", "BTC-27NOV26-x-C", "BTC-27NOV26-1-X", ""):
        assert parse_option_symbol(bad) is None
    g = group_by_strike([{"symbol": "BTC-27NOV26-1-C"}, {"symbol": "BTC-27NOV26-1-P"}, {"symbol": "bad"}])
    assert list(g) == [Decimal(1)] and set(g[Decimal(1)]) == {"C", "P"}


def test_lock_error_not_masked_by_rollback(tmp_path):
    p = str(tmp_path / "m.db")
    holder = sqlite3.connect(p, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")  # другой процесс держит запись
    conn = sqlite3.connect(p, timeout=0.05)
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            db.migrate(conn)
    finally:
        conn.close()
        holder.execute("ROLLBACK")
        holder.close()


def test_connect_closes_on_migration_failure(tmp_path, monkeypatch):
    closed = []
    real_connect = sqlite3.connect

    class Conn:
        def __init__(self, *a, **k):
            self._c = real_connect(*a, **k)

        def __getattr__(self, name):
            return getattr(self._c, name)

        def __setattr__(self, name, value):
            if name == "_c":
                object.__setattr__(self, name, value)
            else:
                setattr(self._c, name, value)

        def close(self):
            closed.append(True)
            self._c.close()

    monkeypatch.setattr(db.sqlite3, "connect", Conn)
    monkeypatch.setattr(db, "migrate", lambda conn: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        db.connect(str(tmp_path / "m.db"))
    assert closed == [True]


def test_migration_checked_once_per_file(tmp_path, monkeypatch):
    calls = []
    real = db.migrate
    monkeypatch.setattr(db, "migrate", lambda conn: calls.append(1) or real(conn))
    p = str(tmp_path / "m.db")
    db.connect(p).close()
    db.connect(p).close()
    assert calls == [1]


def test_bot_without_job_queue_still_starts(make_client, cfg_db, monkeypatch, caplog):
    b = OptionsBot(make_client(cfg_db), cfg_db)
    app = b.build_app()
    monkeypatch.setattr(type(app), "job_queue", property(lambda self: None))
    monkeypatch.setattr(type(app.bot), "set_my_commands", lambda self, *a, **k: asyncio.sleep(0))
    monkeypatch.setattr("optbot.bot.ExecutionWatcher", lambda *a: NS(start=lambda: None, stop=lambda: None))
    asyncio.run(app.post_init(app))
    assert "нет JobQueue" in caplog.text


def test_bot_collect_job_survives_errors(make_client, cfg_db, caplog):
    b = OptionsBot(make_client(cfg_db), cfg_db)
    asyncio.run(b._collect_job(NS()))
    assert q(cfg_db, "SELECT COUNT(*) n FROM snapshots")[0]["n"] == 3
    b.collector.collect = lambda: 1 / 0
    asyncio.run(b._collect_job(NS()))                 # не бросает
    assert "проход не выполнен" in caplog.text


def test_bot_schedules_collector(make_client, cfg_db, monkeypatch):
    b = OptionsBot(make_client(cfg_db), cfg_db)
    app = b.build_app()
    jobs = []
    monkeypatch.setattr(type(app.job_queue), "run_repeating",
                        lambda self, cb, interval, first, name: jobs.append((cb, interval, first, name)))
    monkeypatch.setattr(type(app.bot), "set_my_commands", lambda self, *a, **k: asyncio.sleep(0))
    monkeypatch.setattr("optbot.bot.ExecutionWatcher", lambda *a: NS(start=lambda: None, stop=lambda: None))
    monkeypatch.setattr("optbot.bot.time.time", lambda: 899.5)
    asyncio.run(app.post_init(app))
    assert (b._collect_job, 900, pytest.approx(5.5), "collector") in jobs
    assert (b._sync_job, 3600, 10, "journal_sync") in jobs   # сверка журнала: при старте и каждый час
    assert (b._sigeval_job, 3600, 120, "signals_eval") in jobs  # итоги сигналов — с тем же периодом


def test_bot_collector_disabled(make_client, cfg_db, monkeypatch, caplog):
    cfg_off = with_cfg(cfg_db, collector_enabled=False)
    b = OptionsBot(make_client(cfg_off), cfg_off)
    app = b.build_app()
    jobs = []
    monkeypatch.setattr(type(app.job_queue), "run_repeating", lambda self, *a, **k: jobs.append(a))
    monkeypatch.setattr(type(app.bot), "set_my_commands", lambda self, *a, **k: asyncio.sleep(0))
    monkeypatch.setattr("optbot.bot.ExecutionWatcher", lambda *a: NS(start=lambda: None, stop=lambda: None))
    asyncio.run(app.post_init(app))
    assert [j for j in jobs if j and j[0] == b._collect_job] == []
    assert "без просадки" in caplog.text           # итоги сигналов будут, путь — нет: предупредить


def test_bot_journal_disabled_no_signal_eval(make_client, cfg_db, monkeypatch):
    cfg_off = with_cfg(cfg_db, journal_enabled=False)
    b = OptionsBot(make_client(cfg_off), cfg_off)
    app = b.build_app()
    jobs = []
    monkeypatch.setattr(type(app.job_queue), "run_repeating", lambda self, *a, **k: jobs.append(a))
    monkeypatch.setattr(type(app.bot), "set_my_commands", lambda self, *a, **k: asyncio.sleep(0))
    monkeypatch.setattr("optbot.bot.ExecutionWatcher", lambda *a: NS(start=lambda: None, stop=lambda: None))
    asyncio.run(app.post_init(app))
    assert [j for j in jobs if j and j[0] == b._sigeval_job] == []


def test_store_failure_reports_legs_missing(make_client, cfg_db, monkeypatch):
    track_symbols(cfg_db.db_path, [BTC_PUT], until_ms=EXP_MS)
    col = Collector(make_client(cfg_db), cfg_db)
    real = col._store_base

    def fail_btc(conn, base, *a):
        if base == "BTC":
            raise sqlite3.OperationalError("disk I/O")
        return real(conn, base, *a)
    monkeypatch.setattr(col, "_store_base", fail_btc)
    res = col.collect(NOW)
    assert res.legs_missing == [BTC_PUT] and res.snapshots == 2


def test_expiry_not_in_instruments_skipped(make_client, cfg_db, fake):
    sym = "BTC-4DEC26-65000-C-USDT"
    fake.tickers[sym] = dict(fake.tickers[next(iter(fake.tickers))], symbol=sym)
    assert Collector(make_client(cfg_db), cfg_db).collect(NOW).snapshots == 3


@pytest.mark.parametrize("sym", ["BTC-27NOV26-NaN-C-USDT", "BTC-27NOV26-sNaN-P", "BTC-27NOV26-Infinity-C",
                                 "BTC-27NOV26--1-C", "BTC-27NOV26-0-P"])
def test_parse_rejects_non_finite_strikes(sym):
    from optbot.bybit_client import parse_option_symbol
    assert parse_option_symbol(sym) is None


def test_instruments_lock_not_held_during_fetch(client, fake):
    import threading as th
    started, release = th.Event(), th.Event()
    real = fake.get_instruments_info

    def slow(**kw):
        started.set()
        release.wait(5)
        return real(**kw)
    fake.get_instruments_info = slow
    t = th.Thread(target=client.load_instruments, args=("ETH",), kwargs={"force": True})
    t.start()
    assert started.wait(5)
    got = client._instr_lock.acquire(timeout=1)  # загрузка идёт — блокировка свободна
    if got:
        client._instr_lock.release()
    release.set()
    t.join()
    assert got and any(i.base == "ETH" for i in client._instruments.values())


def test_bad_instrument_symbol_skipped(client, fake):
    real = fake.get_instruments_info

    def with_bad(**kw):
        r = real(**kw)
        r["result"]["list"].append({"symbol": "BTC-WEIRD", "status": "Trading", "deliveryTime": "1"})
        return r
    fake.get_instruments_info = with_bad
    assert all(i.symbol != "BTC-WEIRD" for i in client.load_instruments("BTC", force=True))


def test_set_wal_retries_while_locked(monkeypatch):
    calls = []

    class Conn:
        def execute(self, sql):
            calls.append(sql)
            if len(calls) < 3:
                raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(db.time, "sleep", lambda s: None)
    db._set_wal(Conn())
    assert len(calls) == 3


def test_set_wal_gives_up_and_other_errors(monkeypatch):
    class Locked:
        def execute(self, sql):
            raise sqlite3.OperationalError("database is locked")

    class Broken:
        def execute(self, sql):
            raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(db.time, "sleep", lambda s: None)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        db._set_wal(Locked(), timeout=0)
    with pytest.raises(sqlite3.OperationalError, match="disk"):
        db._set_wal(Broken())


@pytest.mark.parametrize("raw,out", [("", 60), ("30", 30), ("4", 60), ("2000", 60), ("abc", 60)])
def test_int_in_env(monkeypatch, caplog, raw, out):
    from optbot.config import _int_in
    monkeypatch.setenv("X_SYNC", raw)
    assert _int_in("X_SYNC", 60, 5, 1440) == out
    assert ("нужно целое" in caplog.text) == (raw not in ("", "30"))
