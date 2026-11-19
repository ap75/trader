"""Резервная копия БД, пинг мониторинга, обязательный MAX_QTY, предупреждения конфигурации."""
import asyncio
import dataclasses
import os
import sqlite3
import time
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest

from optbot import backup, db
from optbot.bot import OptionsBot, check_config, config_warnings


@pytest.fixture
def bcfg(cfg, tmp_path):
    return dataclasses.replace(cfg, backup_dir=str(tmp_path / "bk"), backup_keep_days=14)


def test_backup_online_consistent_copy(bcfg):
    conn = db.connect(bcfg.db_file)
    with conn:
        conn.execute("INSERT INTO bot_state (key, value) VALUES ('x', '1')")
    # открытая читающая транзакция другого соединения не мешает (онлайн-копия, не копирование файла)
    other = sqlite3.connect(bcfg.db_file)
    other.execute("BEGIN")
    other.execute("SELECT * FROM bot_state").fetchall()
    path = backup.backup(bcfg)
    other.close()
    conn.close()
    assert os.path.exists(path) and not path.endswith(".part")
    c = sqlite3.connect(path)
    assert c.execute("SELECT value FROM bot_state WHERE key='x'").fetchone()[0] == "1"
    assert c.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    c.close()


def test_backup_prunes_old_keeps_last(bcfg):
    db.connect(bcfg.db_file).close()
    now = time.time()
    old = backup.backup(bcfg, now - 20 * 86400)
    os.utime(old, (now - 20 * 86400, now - 20 * 86400))
    new = backup.backup(bcfg, now)
    assert not os.path.exists(old) and os.path.exists(new)
    os.utime(new, (now - 30 * 86400, now - 30 * 86400))
    assert backup.prune(bcfg, now) == [] and os.path.exists(new)          # последнюю — никогда


def test_backup_cli(bcfg, monkeypatch, capsys):
    db.connect(bcfg.db_file).close()
    monkeypatch.setattr(backup, "settings", bcfg)
    assert backup.main([]) == 0 and "МБ" in capsys.readouterr().out
    monkeypatch.setattr(backup, "backup", lambda c: (_ for _ in ()).throw(RuntimeError("диск")))
    assert backup.main([]) == 1


def _bot(cfg):
    b = OptionsBot.__new__(OptionsBot)
    b.cfg = cfg
    return b


def test_backup_job_alerts_on_failure(bcfg, monkeypatch):
    b = _bot(bcfg)
    sent = []

    async def alert(key, text, quiet=True, retry=False):
        sent.append((key, text))
        return True
    b.admin_alert = alert
    monkeypatch.setattr(backup, "backup", lambda c: (_ for _ in ()).throw(RuntimeError("нет места")))
    asyncio.run(b._backup_job(None))
    assert sent == [("backup", "Резервная копия БД не сделана: нет места")]


def test_health_ping_ok_and_fail(cfg, monkeypatch):
    b = _bot(dataclasses.replace(cfg, healthcheck_url="https://hc-ping.com/abc/"))
    urls = []

    class R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return b"OK"
    monkeypatch.setattr("optbot.bot.urllib.request.urlopen", lambda req, timeout: urls.append(req.full_url) or R())
    asyncio.run(b._ping_health(True))
    asyncio.run(b._ping_health(False))
    assert urls == ["https://hc-ping.com/abc", "https://hc-ping.com/abc/fail"]


def test_health_ping_failure_only_logged_without_url(cfg, monkeypatch, caplog):
    b = _bot(dataclasses.replace(cfg, healthcheck_url="https://hc-ping.com/secret-uuid"))

    def boom(req, timeout):
        raise OSError("network")
    monkeypatch.setattr("optbot.bot.urllib.request.urlopen", boom)
    asyncio.run(b._ping_health(True))
    assert "пинг не отправлен" in caplog.text and "secret-uuid" not in caplog.text


def test_health_ping_after_collect(cfg, monkeypatch):
    b = _bot(dataclasses.replace(cfg, healthcheck_url="https://hc-ping.com/x"))
    got = []

    async def ping(ok):
        got.append(ok)
    b._ping_health = ping
    b._auto_change_on = lambda: False
    b._break_streaks = lambda bases: None
    b.collector = NS(collect=lambda: NS(errors=[], snapshots=2, skipped=0, failed_bases=set()))
    b.cfg = dataclasses.replace(b.cfg, paper=dataclasses.replace(b.cfg.paper, enabled=False))
    asyncio.run(b._collect_job(None))

    def fail():
        raise RuntimeError("биржа")
    b.collector = NS(collect=fail)
    asyncio.run(b._collect_job(None))
    assert got == [True, False]


def test_check_config_requires_max_qty_and_https(cfg):
    bad = dataclasses.replace(cfg, max_order_qty={"BTC": Decimal("0.1"), "ETH": Decimal(0)})
    errs = check_config(bad)
    assert any("MAX_QTY_ETH" in e and "MAX_QTY_SOL" in e and "MAX_QTY_BTC" not in e for e in errs)
    assert any("HEALTHCHECK_URL" in e for e in check_config(dataclasses.replace(cfg, healthcheck_url="http://x")))


def test_config_warnings(cfg):
    w = config_warnings(dataclasses.replace(cfg, tg_admin_ids=set(), healthcheck_url=""))
    assert any("TELEGRAM_ADMIN_IDS пуст" in x for x in w) and any("HEALTHCHECK_URL" in x for x in w)
    assert config_warnings(dataclasses.replace(cfg, tg_admin_ids={1}, healthcheck_url="https://x",
                                               collector_enabled=True, collector_interval_min=15)) == []


def test_order_refused_without_max_qty(make_client, cfg, fake):
    from optbot.bybit_client import OrderRequest, RiskError
    from tests.fakes import BTC_PUT
    c = make_client(dataclasses.replace(cfg, max_order_qty={"BTC": Decimal(0)}))
    with pytest.raises(RiskError, match="MAX_QTY_BTC не задан"):
        c.prepare_order(OrderRequest(symbol=BTC_PUT, side="Buy", qty=Decimal("0.01"), price=Decimal("100")))


def test_backup_missing_db_and_failure_cleanup(bcfg, monkeypatch):
    with pytest.raises(FileNotFoundError):
        backup.backup(bcfg)
    assert not os.path.exists(bcfg.db_file)                               # пустую базу не создала
    db.connect(bcfg.db_file).close()

    class Bad:
        def __init__(self, c):
            self.c = c

        def backup(self, out):
            raise OSError("disk full")

        def close(self):
            self.c.close()
    real = sqlite3.connect
    monkeypatch.setattr(backup.sqlite3, "connect",
                        lambda p, **k: Bad(real(p, **k)) if "mode=ro" in p else real(p, **k))
    with pytest.raises(OSError):
        backup.backup(bcfg)
    assert [f for f in os.listdir(bcfg.backup_dir_abs) if f.endswith(".part")] == []


def test_prune_removes_stale_part(bcfg):
    os.makedirs(bcfg.backup_dir_abs)
    name = os.path.splitext(os.path.basename(bcfg.db_file))[0]
    p = os.path.join(bcfg.backup_dir_abs, f"{name}-20260101-0320.db.1.part")
    open(p, "w").close()
    now = time.time()
    os.utime(p, (now - 2 * 86400, now - 2 * 86400))
    backup.prune(bcfg, now)
    assert not os.path.exists(p)


def test_warning_healthcheck_without_collector(cfg):
    w = config_warnings(dataclasses.replace(cfg, tg_admin_ids={1}, healthcheck_url="https://x",
                                            collector_enabled=False))
    assert any("сборщик выключен" in x for x in w)


def test_prune_tolerates_concurrent_removal_and_failure_not_fatal(bcfg, monkeypatch):
    db.connect(bcfg.db_file).close()
    real = os.path.getmtime
    monkeypatch.setattr(backup.os.path, "getmtime",
                        lambda f: (_ for _ in ()).throw(FileNotFoundError(f)) if f.endswith("gone.db") else real(f))
    os.makedirs(bcfg.backup_dir_abs)
    name = os.path.splitext(os.path.basename(bcfg.db_file))[0]
    open(os.path.join(bcfg.backup_dir_abs, f"{name}-0gone.db"), "w").close()
    assert backup.prune(bcfg) == []
    monkeypatch.setattr(backup, "prune", lambda c, now=None: (_ for _ in ()).throw(PermissionError("ro")))
    assert os.path.exists(backup.backup(bcfg))                          # копия есть — чистка не роняет


def test_warning_long_collector_interval(cfg):
    w = config_warnings(dataclasses.replace(cfg, tg_admin_ids={1}, healthcheck_url="https://x",
                                            collector_enabled=True, collector_interval_min=60))
    assert any("COLLECTOR_INTERVAL_MIN=60" in x for x in w)


def test_health_fail_with_query(cfg, monkeypatch):
    b = _bot(dataclasses.replace(cfg, healthcheck_url="https://hc-ping.com/abc?rid=1"))
    urls = []

    class R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return b""
    monkeypatch.setattr("optbot.bot.urllib.request.urlopen", lambda req, timeout: urls.append(req.full_url) or R())
    asyncio.run(b._ping_health(False))
    assert urls == ["https://hc-ping.com/abc/fail?rid=1"]


def test_prune_ignores_other_db_with_same_prefix(bcfg):
    os.makedirs(bcfg.backup_dir_abs)
    name = os.path.splitext(os.path.basename(bcfg.db_file))[0]
    other = os.path.join(bcfg.backup_dir_abs, f"{name}-demo-20200101-0320.db")
    open(other, "w").close()
    os.utime(other, (0, 0))
    backup.prune(bcfg)
    assert os.path.exists(other)
