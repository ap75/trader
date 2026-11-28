"""Рыночная БД отдельно от БД счёта: роли файлов, ATTACH, защита старой общей БД, разделение --split."""
import dataclasses
import os
import sqlite3

import pytest

from optbot import db, market, rates
from optbot.bot import check_db
from tests.helpers import track_symbols

LEGACY_TABLES = set(db.MARKET_TABLES) | set(db.ACCOUNT_TABLES)


def tables(path: str) -> set[str]:
    c = sqlite3.connect(path)
    try:
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}
    finally:
        c.close()


def legacy_db(path: str) -> None:
    """Общая БД как у прежней версии: схема 1–13 без роли, с данными рынка и счёта."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    c = sqlite3.connect(path)
    for m in db.MIGRATIONS:
        for st in m:
            c.execute(st)
    c.execute(f"PRAGMA user_version={db.LEGACY_VERSION}")
    c.execute("PRAGMA journal_mode=WAL")
    with c:
        c.execute("INSERT INTO snapshots (ts, base, expiry, expiry_ms, dte, captured_ms) VALUES (1, 'BTC', 'E', 2, 1, 1)")
        c.execute("INSERT INTO snapshots (ts, base, expiry, expiry_ms, dte, captured_ms) VALUES (2, 'BTC', 'E', 2, 1, 2)")
        c.execute("INSERT INTO tracked_symbols (symbol, added_ms, until_ms, reason) VALUES ('S', 1, 9, 'r')")
        c.execute("INSERT INTO dvol_h1 (base, ts, close) VALUES ('BTC', 0, 0.5)")
        c.execute("INSERT INTO bot_state (key, value) VALUES ('running', '0')")
        c.execute("INSERT INTO bot_state (key, value) VALUES (?, '{\"rate\": 0.04, \"date_ms\": 1}')",
                  (rates.STATE_KEY,))
        c.execute("INSERT INTO bot_state (key, value) VALUES ('perp_h1_complete2:BTC', '1')")
        c.execute("INSERT INTO books (id, horizon, strategy, base, created_ms) VALUES ('b1', 'short', 's', 'BTC', 1)")
    c.close()


def test_roles_cover_legacy_schema_once():
    c = sqlite3.connect(":memory:")
    for m in db.MIGRATIONS:
        for st in m:
            c.execute(st)
    have = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}
    assert set(db.MARKET_TABLES) & set(db.ACCOUNT_TABLES) == set()
    assert LEGACY_TABLES == have                        # новая таблица общей схемы — без роли не останется


def test_fresh_files_hold_only_own_tables(cfg):
    db.account(cfg).close()
    assert tables(cfg.db_file) == set(db.ACCOUNT_TABLES)
    assert tables(cfg.market_db_file) == set(db.MARKET_TABLES) | {"market_state"}


def test_account_sees_market_tables_through_attach(cfg):
    track_symbols(cfg, ["BTC-X"], until_ms=10, reason="r")         # пишет через БД счёта — в рыночную
    conn = db.market(cfg)
    assert conn.execute("SELECT symbol FROM tracked_symbols").fetchone()[0] == "BTC-X"
    conn.close()
    conn = db.account(cfg)
    try:
        with conn:
            conn.execute("INSERT INTO bot_state (key, value) VALUES ('k', 'v')")
        assert conn.execute("SELECT COUNT(*) FROM mkt.tracked_symbols").fetchone()[0] == 1
        # JOIN между БД работает
        assert conn.execute("SELECT b.value, t.symbol FROM bot_state b, tracked_symbols t").fetchone()[:] == ("v", "BTC-X")
    finally:
        conn.close()
    assert "bot_state" not in tables(cfg.market_db_file)


def test_role_mismatch_refused(cfg):
    db.account(cfg).close()
    with pytest.raises(RuntimeError, match="открывается как"):
        db.connect(cfg.market_db_file, role=db.ACCOUNT)
    with pytest.raises(RuntimeError, match="открывается как"):
        db.connect(cfg.db_file, role=db.MARKET)
    with pytest.raises(ValueError):
        db.connect(cfg.market_db_file, role=db.MARKET, market=cfg.market_db_file)


@pytest.mark.parametrize("role", [db.ACCOUNT, db.MARKET])
def test_legacy_with_data_needs_split(tmp_path, role):
    p = str(tmp_path / "old" / "market-demo.db")
    legacy_db(p)
    with pytest.raises(db.SplitRequired, match="--split"):
        db.connect(p, role=role)
    c = sqlite3.connect(p)                                 # ничего не удалено, роль не поставлена
    assert c.execute("PRAGMA application_id").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 2
    assert c.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 1
    c.close()


def test_default_account_refused_while_legacy_present(cfg, tmp_path, monkeypatch):
    legacy = str(tmp_path / "data" / "market-demo.db")
    os.makedirs(os.path.dirname(legacy))
    legacy_db(legacy)
    monkeypatch.setattr(type(cfg), "legacy_db_file", property(lambda self: legacy))
    dflt = dataclasses.replace(cfg, db_path="")
    monkeypatch.setattr(type(cfg), "db_file", property(lambda self: str(tmp_path / "data" / "account-demo.db")
                                                       if not self.db_path else self.db_path))
    with pytest.raises(db.SplitRequired, match="optbot.market --split"):
        db.account(dflt)
    assert not os.path.exists(dflt.db_file)                # пустую БД счёта не создал
    assert check_db(dflt)[0] == 3 and "--split" in check_db(dflt)[1]   # служба не перезапускает по кругу
    assert check_db(cfg)[0] == 3                           # DB_PATH задан — общая БД всё равно не пропущена
    market.split(dflt)
    assert check_db(dflt) is None


def test_split_moves_data_and_state(cfg, tmp_path):
    legacy = str(tmp_path / "old" / "market-demo.db")
    legacy_db(legacy)
    res = market.split(cfg, legacy)
    assert not os.path.exists(legacy) and os.path.exists(legacy + ".pre-split")
    assert res.rows["snapshots"] == 2 and res.rows["books"] == 1
    assert sorted(res.state_keys) == ["perp_h1_complete2:BTC", rates.STATE_KEY]
    assert tables(cfg.db_file) == set(db.ACCOUNT_TABLES)
    assert tables(cfg.market_db_file) == set(db.MARKET_TABLES) | {"market_state"}
    conn = db.account(cfg)
    try:
        assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 2
        assert conn.execute("SELECT reason FROM tracked_symbols").fetchone()[0] == "r"
        assert conn.execute("SELECT id FROM books").fetchone()[0] == "b1"
        assert [r[0] for r in conn.execute("SELECT key FROM bot_state")] == ["running"]
        assert conn.execute("SELECT value FROM market_state WHERE key = ?", (rates.STATE_KEY,)).fetchone()
    finally:
        conn.close()
    with pytest.raises(FileExistsError):                   # второй раз — отказ, ничего не трогает
        market.split(cfg, legacy + ".pre-split", any_name=True)


def test_split_refuses_busy_or_wrong_source(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(market, "LOCK_WAIT_S", 0)
    legacy = str(tmp_path / "old" / "market-demo.db")
    legacy_db(legacy)
    w = sqlite3.connect(legacy)
    w.execute("BEGIN IMMEDIATE")                           # бот пишет
    with pytest.raises(RuntimeError, match="остановите бота"):
        market.split(cfg, legacy)
    w.rollback()
    w.close()
    assert os.path.exists(legacy) and not os.path.exists(cfg.db_file) and not os.path.exists(cfg.market_db_file)
    assert not [f for f in os.listdir(tmp_path) if "split-part" in f]
    db.account(cfg).close()                                # уже разделённая (с ролью) — не общая
    with pytest.raises(RuntimeError, match="не общая БД"):
        market.split(dataclasses.replace(cfg, db_path=str(tmp_path / "a2.db"),
                                         market_db_path=str(tmp_path / "m2.db")), cfg.db_file, any_name=True)
    with pytest.raises(FileNotFoundError):
        market.split(cfg, str(tmp_path / "nope" / "market-demo.db"))


def test_split_counts_mismatch_leaves_source(cfg, tmp_path, monkeypatch):
    legacy = str(tmp_path / "old" / "market-demo.db")
    legacy_db(legacy)
    real = market._counts
    calls = []

    def bad(conn, tbls):
        calls.append(1)
        got = real(conn, tbls)
        if len(calls) > 1 and "snapshots" in got:
            got["snapshots"] -= 1
        return got
    monkeypatch.setattr(market, "_counts", bad)
    with pytest.raises(RuntimeError, match="не сошлось"):
        market.split(cfg, legacy)
    assert os.path.exists(legacy) and not os.path.exists(cfg.db_file) and not os.path.exists(cfg.market_db_file)
    assert not [f for f in os.listdir(tmp_path) if "split-part" in f]


def test_split_cli(cfg, tmp_path, monkeypatch, capsys):
    legacy = str(tmp_path / "old" / "market-demo.db")
    legacy_db(legacy)
    monkeypatch.setattr(market, "settings", cfg)
    assert market.main(["--split", "--from", legacy]) == 0
    assert "рынок:" in capsys.readouterr().out
    assert market.main(["--split", "--from", legacy]) == 1
    assert "не выполнено" in capsys.readouterr().err


# ---------- по код-ревью ----------
def _default(cfg, tmp_path, monkeypatch, legacy):
    """Настройки с путями по умолчанию в tmp_path: data/market-demo.db (общая), market.db, account-demo.db."""
    data = tmp_path / "data"
    monkeypatch.setattr(type(cfg), "legacy_db_file", property(lambda self: legacy))
    monkeypatch.setattr(type(cfg), "db_file", property(
        lambda self: self.db_path or str(data / "account-demo.db")))
    monkeypatch.setattr(type(cfg), "market_db_file", property(
        lambda self: self.market_db_path or str(data / "market.db")))
    return dataclasses.replace(cfg, db_path="", market_db_path="")


def test_market_command_before_split_refused_then_split_works(cfg, tmp_path, monkeypatch):
    legacy = str(tmp_path / "data" / "market-demo.db")
    os.makedirs(os.path.dirname(legacy))
    legacy_db(legacy)
    d = _default(cfg, tmp_path, monkeypatch, legacy)
    with pytest.raises(db.SplitRequired):
        db.market(d)                                       # collector --stats и т. п. до разделения
    assert not os.path.exists(d.market_db_file)
    db.connect(d.market_db_file, role=db.MARKET).close()   # пустая рыночная всё же появилась (старый путь)
    res = market.split(d)
    assert not res.merged and res.rows["snapshots"] == 2   # пустую заменил полной копией
    db.market(d).close()


def test_split_refuses_running_marker_unless_forced(cfg, tmp_path):
    legacy = str(tmp_path / "old" / "market-demo.db")
    legacy_db(legacy)
    c = sqlite3.connect(legacy)
    with c:
        c.execute("UPDATE bot_state SET value = '1764270000' WHERE key = 'running'")
    c.close()
    with pytest.raises(RuntimeError, match="бот работает"):
        market.split(cfg, legacy)
    assert os.path.exists(legacy) and not os.path.exists(cfg.db_file)
    assert market.split(cfg, legacy, force=True).rows["books"] == 1


def test_split_holds_write_lock_during_copy(cfg, tmp_path, monkeypatch):
    legacy = str(tmp_path / "old" / "market-demo.db")
    legacy_db(legacy)
    real = market._fill
    blocked = []

    def copy(path, role, src, tables):
        w = sqlite3.connect(legacy, timeout=0)             # бот пытается дописать во время переноса
        try:
            w.execute("INSERT INTO bot_state (key, value) VALUES ('late', '1')")
        except sqlite3.OperationalError as e:
            blocked.append(str(e))
        finally:
            w.close()
        return real(path, role, src, tables)
    monkeypatch.setattr(market, "_fill", copy)
    market.split(cfg, legacy)
    assert blocked and all("locked" in b for b in blocked)


def test_split_db_path_equal_to_legacy_explained(cfg, tmp_path):
    legacy = str(tmp_path / "old" / "market-demo.db")
    legacy_db(legacy)
    with pytest.raises(ValueError, match="уберите DB_PATH"):
        market.split(dataclasses.replace(cfg, db_path=legacy), legacy)


def test_split_creates_missing_dirs(cfg, tmp_path):
    legacy = str(tmp_path / "old" / "market-demo.db")
    legacy_db(legacy)
    c = dataclasses.replace(cfg, db_path=str(tmp_path / "a" / "acc.db"), market_db_path=str(tmp_path / "m" / "mk.db"))
    market.split(c, legacy)
    assert os.path.isfile(c.db_file) and os.path.isfile(c.market_db_file)


def test_second_env_split_merges_into_existing_market(cfg, tmp_path):
    first = str(tmp_path / "d" / "market-demo.db")
    legacy_db(first)
    market.split(cfg, first)
    second = str(tmp_path / "m" / "market-demo.db")
    legacy_db(second)
    c = sqlite3.connect(second)
    with c:
        c.execute("UPDATE tracked_symbols SET until_ms = 99, reason = 'main'")
        c.execute("INSERT INTO tracked_symbols (symbol, added_ms, until_ms, reason) VALUES ('M', 1, 5, 'm')")
        c.execute("INSERT INTO leg_marks (ts, symbol, mark, captured_ms) VALUES (3, 'M', 7.5, 3)")
    c.close()
    c2 = dataclasses.replace(cfg, db_path=str(tmp_path / "account-mainnet.db"))
    res = market.split(c2, second)
    assert res.merged and "уже был" in str(res) and os.path.exists(second + ".pre-split")
    conn = db.account(c2)
    try:
        assert conn.execute("SELECT id FROM books").fetchone()[0] == "b1"
        assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 2      # рынок не задвоен
        got = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT symbol, until_ms, reason FROM tracked_symbols")}
        assert got == {"S": (99, "main"), "M": (5, "m")}
        assert conn.execute("SELECT mark FROM leg_marks WHERE symbol = 'M'").fetchone()[0] == 7.5
        assert conn.execute("SELECT COUNT(*) FROM bot_state").fetchone()[0] == 1       # ключи рынка — не в счёт
    finally:
        conn.close()


def test_state_keys_glob_exact(cfg, tmp_path):
    legacy = str(tmp_path / "old" / "market-demo.db")
    legacy_db(legacy)
    c = sqlite3.connect(legacy)
    with c:
        c.execute("INSERT INTO bot_state (key, value) VALUES ('perpXh1_completeZ', '1')")   # «_» — не шаблон
        c.execute("INSERT INTO bot_state (key, value) VALUES ('RF:x', '1')")                 # регистр важен
    c.close()
    res = market.split(cfg, legacy)
    assert sorted(res.state_keys) == ["perp_h1_complete2:BTC", rates.STATE_KEY]
    conn = db.account(cfg)
    try:
        assert {r[0] for r in conn.execute("SELECT key FROM bot_state")} == {"running", "perpXh1_completeZ", "RF:x"}
    finally:
        conn.close()


def test_failed_account_move_rolls_market_back(cfg, tmp_path, monkeypatch):
    legacy = str(tmp_path / "old" / "market-demo.db")
    legacy_db(legacy)
    real = market._place

    def place(t, d):
        if d == os.path.abspath(cfg.db_file):
            raise PermissionError("ro")
        real(t, d)
    monkeypatch.setattr(market, "_place", place)
    with pytest.raises(PermissionError):
        market.split(cfg, legacy)
    assert not os.path.exists(cfg.market_db_file) and not os.path.exists(cfg.db_file) and os.path.exists(legacy)
    monkeypatch.setattr(market, "_place", real)
    assert market.split(cfg, legacy).rows["snapshots"] == 2          # повтор возможен


def test_account_only_write_does_not_lock_market(cfg):
    conn = db.account(cfg)
    conn.execute("INSERT INTO bot_state (key, value) VALUES ('k', 'v')")   # транзакция только по счёту открыта
    other = sqlite3.connect(cfg.market_db_file, timeout=0)
    try:
        other.execute("BEGIN IMMEDIATE")                   # сборщик пишет в рыночную — не ждёт
        other.rollback()
    finally:
        other.close()
        conn.rollback()
        conn.close()


def test_explicit_db_path_next_to_legacy_refused(cfg, tmp_path):
    legacy = str(tmp_path / "market-demo.db")             # общая БД в папке будущей БД счёта
    legacy_db(legacy)
    c = dataclasses.replace(cfg, db_path=str(tmp_path / "account-demo.db"))
    assert c.legacy_db_file == legacy
    with pytest.raises(db.SplitRequired):
        db.account(c)
    with pytest.raises(db.SplitRequired):
        db.market(dataclasses.replace(c, market_db_path=str(tmp_path / "market.db")))
    assert not os.path.exists(c.db_file) and not os.path.exists(tmp_path / "market.db")
    res = market.split(dataclasses.replace(c, market_db_path=str(tmp_path / "market.db")))
    assert res.legacy == legacy + ".pre-split"


# ---------- по код-ревью 3 ----------
def test_iv_history_read_does_not_create_market_db(cfg):
    from optbot.signals import iv7_history
    assert iv7_history(cfg.market_db_file, "BTC", 0, 7) == []
    assert not os.path.exists(cfg.market_db_file)


def test_split_refuses_market_with_data_not_from_split(cfg, tmp_path):
    legacy = str(tmp_path / "old" / "market-demo.db")
    legacy_db(legacy)
    m = db.connect(cfg.market_db_file, role=db.MARKET)
    with m:
        m.execute("INSERT INTO perp_h1 (base, ts, close) VALUES ('BTC', 0, 1)")   # снимков нет, свечи есть
    m.close()
    with pytest.raises(FileExistsError, match="не из разделения"):
        market.split(cfg, legacy)
    m = db.connect(cfg.market_db_file, role=db.MARKET)
    assert m.execute("SELECT COUNT(*) FROM perp_h1").fetchone()[0] == 1           # не удалена
    m.close()
    assert os.path.exists(legacy) and not os.path.exists(cfg.db_file)


def test_split_refuses_market_open_elsewhere(cfg, tmp_path):
    legacy = str(tmp_path / "old" / "market-demo.db")
    legacy_db(legacy)
    m = db.connect(cfg.market_db_file, role=db.MARKET)   # открыта другим процессом: есть -wal/-shm
    try:
        m.execute("SELECT 1 FROM snapshots").fetchall()
        with pytest.raises(FileExistsError, match="открыта"):
            market.split(cfg, legacy)
    finally:
        m.close()
    assert market.split(cfg, legacy).rows["snapshots"] == 2                       # пустую, закрытую — заменил


def test_split_marks_market_and_keeps_wal_mode(cfg, tmp_path):
    legacy = str(tmp_path / "old" / "market-demo.db")
    legacy_db(legacy)
    market.split(cfg, legacy)
    for p in (cfg.db_file, cfg.market_db_file):
        c = sqlite3.connect(p)
        assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        c.close()
    m = db.market(cfg)
    assert m.execute("SELECT value FROM market_state WHERE key = ?", (market.SPLIT_KEY,)).fetchone()[0] == "market-demo.db"
    m.close()


def test_begin_write_locks_account_only(cfg):
    conn = db.account(cfg)
    conn.isolation_level = None
    db.begin_write(conn)
    try:
        for path, locked in ((cfg.db_file, True), (cfg.market_db_file, False)):
            o = sqlite3.connect(path, timeout=0)
            try:
                if locked:
                    with pytest.raises(sqlite3.OperationalError, match="locked"):
                        o.execute("BEGIN IMMEDIATE")
                else:
                    o.execute("BEGIN IMMEDIATE")
                    o.rollback()
            finally:
                o.close()
    finally:
        conn.execute("ROLLBACK")
        conn.close()


def test_order_registration_not_blocked_by_market_writer(cfg):
    from optbot.journal import Journal
    j = Journal(cfg)
    db.account(cfg).close()
    w = sqlite3.connect(cfg.market_db_file, timeout=0)
    w.execute("BEGIN IMMEDIATE")                           # сборщик (другой экземпляр) пишет в рыночную
    try:
        link = j.register_order(10, "x")
        conn = db.account(cfg)
        assert conn.execute("SELECT COUNT(*) FROM order_links WHERE link_id = ?", (link,)).fetchone()[0] == 1
        conn.close()
    finally:
        w.rollback()
        w.close()


# ---------- по код-ревью 4 ----------
def test_retrack_restores_missing_and_never_shortens(cfg):
    from optbot.collector import retrack
    conn = db.account(cfg)
    try:
        with conn:
            conn.execute("INSERT INTO tracked_symbols (symbol, added_ms, until_ms, reason) VALUES ('A', 1, 500, 'x')")
        n = retrack(conn, [(["A", "B"], 300, "demo:book:1"), (["C"], 50, "demo:sig:2")], now_ms=100)
        assert n == 1                                      # B; A уже дольше; C — экспирация прошла
        got = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT symbol, until_ms, reason FROM tracked_symbols")}
        assert got == {"A": (500, "x"), "B": (300, "demo:book:1")}
        assert retrack(conn, [(["A", "B"], 300, "demo:book:1")], now_ms=100) == 0
    finally:
        conn.close()


def test_split_merges_while_other_instance_has_market_open(cfg, tmp_path):
    first = str(tmp_path / "d" / "market-demo.db")
    legacy_db(first)
    market.split(cfg, first)
    other = db.market(cfg)                                  # экземпляр demo работает с рыночной
    other.execute("SELECT COUNT(*) FROM snapshots").fetchone()
    try:
        second = str(tmp_path / "m" / "market-demo.db")
        legacy_db(second)
        res = market.split(dataclasses.replace(cfg, db_path=str(tmp_path / "account-mainnet.db")), second)
        assert res.merged
    finally:
        other.close()


def test_split_upgrades_older_legacy_schema(cfg, tmp_path):
    p = str(tmp_path / "o" / "market-demo.db")
    os.makedirs(os.path.dirname(p))
    c = sqlite3.connect(p)
    for m in db.MIGRATIONS[:-1]:                           # v12: без dvol_h1
        for st in m:
            c.execute(st)
    c.execute(f"PRAGMA user_version={db.LEGACY_VERSION - 1}")
    with c:
        c.execute("INSERT INTO books (id, horizon, strategy, base, created_ms) VALUES ('b1', 'short', 's', 'BTC', 1)")
    c.close()
    res = market.split(cfg, p)
    assert res.rows["books"] == 1 and res.rows["dvol_h1"] == 0


def test_market_backup_skipped_when_fresh_exists(cfg, tmp_path, monkeypatch):
    import asyncio
    from optbot import backup
    from optbot.bot import OptionsBot
    c = dataclasses.replace(cfg, backup_dir=str(tmp_path / "bk"), backup_compress=False)
    db.account(c).close()
    b = OptionsBot.__new__(OptionsBot)
    b.cfg = c

    async def alert(*a, **k):
        return True
    b.admin_alert = alert
    asyncio.run(b._backup_job(None))
    names = sorted(os.listdir(c.backup_dir_abs))
    assert [n.split("-")[0] for n in names] == ["account", "market"]
    made = []
    real = backup.backup
    monkeypatch.setattr(backup, "backup", lambda cfg, now=None, path=None: made.append(path) or real(cfg, now, path))
    asyncio.run(b._backup_job(None))                       # второй экземпляр в тот же день: рыночную не копирует
    assert made == [c.db_file]


# ---------- по код-ревью 5 ----------
def test_transient_db_error_exits_for_restart(cfg, monkeypatch):
    monkeypatch.setattr(db, "account", lambda c: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
    code, text = check_db(cfg)
    assert code == 1 and "locked" in text                  # служба перезапустит (не код 3)


def test_role_mismatch_needs_admin(cfg):
    db.account(cfg).close()
    bad = dataclasses.replace(cfg, db_path=cfg.market_db_file)
    assert check_db(bad)[0] == 3


def test_refused_split_leaves_old_schema_untouched(cfg, tmp_path):
    p = str(tmp_path / "o" / "market-demo.db")
    os.makedirs(os.path.dirname(p))
    c = sqlite3.connect(p)
    for m in db.MIGRATIONS[:-1]:
        for st in m:
            c.execute(st)
    c.execute(f"PRAGMA user_version={db.LEGACY_VERSION - 1}")
    with c:
        c.execute("INSERT INTO bot_state (key, value) VALUES ('running', '1764270000')")
    c.close()
    with pytest.raises(RuntimeError, match="бот работает"):
        market.split(cfg, p)
    c = sqlite3.connect(p)
    assert c.execute("PRAGMA user_version").fetchone()[0] == db.LEGACY_VERSION - 1   # старый бот не сломан
    c.close()


def test_legacy_with_only_own_role_data_needs_split(tmp_path):
    p = str(tmp_path / "old" / "market-demo.db")
    os.makedirs(os.path.dirname(p))
    c = sqlite3.connect(p)
    for m in db.MIGRATIONS:
        for st in m:
            c.execute(st)
    c.execute(f"PRAGMA user_version={db.LEGACY_VERSION}")
    with c:      # сборщик был выключен: рыночные пусты, но ставка FRED — в bot_state
        c.execute("INSERT INTO bot_state (key, value) VALUES (?, '{}')", (rates.STATE_KEY,))
    c.close()
    with pytest.raises(db.SplitRequired):
        db.connect(p, role=db.ACCOUNT)


def test_old_backup_restored_with_fresh_mtime_is_not_fresh(cfg, tmp_path):
    import time
    from optbot import backup
    c = dataclasses.replace(cfg, backup_dir=str(tmp_path / "bk"))
    os.makedirs(c.backup_dir_abs)
    old = os.path.join(c.backup_dir_abs, "market-20261101-0320.db.gz")
    open(old, "wb").close()                                # mtime — сейчас, имя — 1 ноября
    assert backup.recent(c, c.market_db_file, 20 * 3600) is None
    now = time.time()
    fresh = os.path.join(c.backup_dir_abs, f"market-{time.strftime('%Y%m%d-%H%M', time.gmtime(now - 3600))}.db.gz")
    open(fresh, "wb").close()
    got = backup.recent(c, c.market_db_file, 20 * 3600, now)
    assert got and got[0] == fresh and 3500 < got[1] < 3700


# ---------- по код-ревью 6 ----------
def test_split_files_private_regardless_of_shell_umask(cfg, tmp_path):
    import stat
    legacy = str(tmp_path / "old" / "market-demo.db")
    legacy_db(legacy)
    old = os.umask(0o022)
    try:
        market.split(cfg, legacy)
    finally:
        os.umask(old)
    for p in (cfg.db_file, cfg.market_db_file):
        assert stat.S_IMODE(os.stat(p).st_mode) & 0o077 == 0


def test_split_refuses_other_env_name_unless_any_name(cfg, tmp_path):
    legacy = str(tmp_path / "old" / "market-mainnet.db")
    legacy_db(legacy)
    with pytest.raises(ValueError, match="--any-name"):
        market.split(cfg, legacy)                          # BYBIT_ENV=demo: журнал mainnet не в account-demo
    assert os.path.exists(legacy) and not os.path.exists(cfg.db_file)
    assert market.split(cfg, legacy, any_name=True).rows["books"] == 1


def test_merge_flag_into_market_not_from_split(cfg, tmp_path):
    m = db.connect(cfg.market_db_file, role=db.MARKET)    # рыночную создал другой экземпляр, копит данные
    with m:
        m.execute("INSERT INTO perp_h1 (base, ts, close) VALUES ('BTC', 0, 1)")
    m.close()
    legacy = str(tmp_path / "old" / "market-demo.db")
    legacy_db(legacy)
    with pytest.raises(FileExistsError, match="--merge"):
        market.split(cfg, legacy)
    res = market.split(cfg, legacy, allow_merge=True)
    assert res.merged
    conn = db.account(cfg)
    try:
        assert conn.execute("SELECT COUNT(*) FROM perp_h1").fetchone()[0] == 1   # данные экземпляра целы
        assert conn.execute("SELECT reason FROM tracked_symbols").fetchone()[0] == "r"
    finally:
        conn.close()


def test_merge_copies_leg_marks_in_batches(cfg, tmp_path, monkeypatch):
    first = str(tmp_path / "d" / "market-demo.db")
    legacy_db(first)
    market.split(cfg, first)
    second = str(tmp_path / "m" / "market-demo.db")
    legacy_db(second)
    c = sqlite3.connect(second)
    with c:
        c.executemany("INSERT INTO leg_marks (ts, symbol, mark, captured_ms) VALUES (1, ?, 1, 1)",
                      [(f"S{i}",) for i in range(7)])
    c.close()
    monkeypatch.setattr(market, "MERGE_BATCH", 2)
    market.split(dataclasses.replace(cfg, db_path=str(tmp_path / "account-2.db")), second)
    m = db.market(cfg)
    assert m.execute("SELECT COUNT(*) FROM leg_marks").fetchone()[0] == 7
    m.close()


def test_account_without_market_file_refused(cfg):
    db.account(cfg).close()
    for s in ("", "-wal", "-shm"):
        if os.path.exists(cfg.market_db_file + s):
            os.remove(cfg.market_db_file + s)
    with pytest.raises(db.SchemaError, match="MARKET_DB_PATH"):
        db.account(cfg)
    assert not os.path.exists(cfg.market_db_file)          # пустую рыночную не создал
    assert check_db(cfg)[0] == 3


# ---------- по код-ревью 7 ----------
def test_market_commands_refuse_missing_market_when_account_exists(cfg, capsys, monkeypatch):
    db.account(cfg).close()
    for x in ("", "-wal", "-shm"):
        if os.path.exists(cfg.market_db_file + x):
            os.remove(cfg.market_db_file + x)
    with pytest.raises(db.SchemaError, match="--init"):
        db.market(cfg)                                     # collector --once и т. п.
    assert not os.path.exists(cfg.market_db_file)
    monkeypatch.setattr(market, "settings", cfg)
    assert market.main(["--init"]) == 0 and os.path.exists(cfg.market_db_file)
    assert market.main(["--init"]) == 1                    # уже есть — не трогает
    db.account(cfg).close()


def test_split_does_not_clobber_market_created_meanwhile(cfg, tmp_path, monkeypatch):
    legacy = str(tmp_path / "old" / "market-demo.db")
    legacy_db(legacy)
    real = market._fill

    def fill(path, role, src, tables):
        got = real(path, role, src, tables)
        if role == db.MARKET:                              # другой экземпляр создал рыночную во время переноса
            c = sqlite3.connect(cfg.market_db_file)
            c.execute("CREATE TABLE mine (x)")
            c.close()
        return got
    monkeypatch.setattr(market, "_fill", fill)
    with pytest.raises(FileExistsError, match="появилась"):
        market.split(cfg, legacy)
    c = sqlite3.connect(cfg.market_db_file)
    assert c.execute("SELECT name FROM sqlite_master WHERE name = 'mine'").fetchone()   # не затёрта
    c.close()
    assert os.path.exists(legacy) and not os.path.exists(cfg.db_file)


def test_merge_failure_removes_account(cfg, tmp_path, monkeypatch):
    first = str(tmp_path / "d" / "market-demo.db")
    legacy_db(first)
    market.split(cfg, first)
    second = str(tmp_path / "m" / "market-demo.db")
    legacy_db(second)
    c2 = dataclasses.replace(cfg, db_path=str(tmp_path / "account-2.db"))
    monkeypatch.setattr(market, "_merge_legs", lambda m, l, e: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        market.split(c2, second)
    assert not os.path.exists(c2.db_file) and os.path.exists(second)   # повтор возможен
    monkeypatch.undo()
    assert market.split(c2, second).merged


def test_config_rejects_same_db_names(cfg, tmp_path):
    from optbot.bot import check_config
    same = dataclasses.replace(cfg, db_path=str(tmp_path / "a" / "bot.db"), market_db_path=str(tmp_path / "b" / "bot.db"))
    assert any("одно имя файла" in e for e in check_config(same))
    one = dataclasses.replace(cfg, market_db_path=cfg.db_file)
    assert any("один файл" in e for e in check_config(one))
    assert not any("MARKET_DB_PATH" in e for e in check_config(cfg))


# ---------- ревью 8 ----------
def test_failed_split_restores_empty_market(cfg, tmp_path, monkeypatch):
    db.connect(cfg.market_db_file, role=db.MARKET).close()   # --init другого экземпляра
    legacy = str(tmp_path / "old" / "market-demo.db")
    legacy_db(legacy)
    real = market._place

    def place(t, d):
        if d == os.path.abspath(cfg.db_file):
            raise PermissionError("ro")
        real(t, d)
    monkeypatch.setattr(market, "_place", place)
    with pytest.raises(PermissionError):
        market.split(cfg, legacy)
    c = sqlite3.connect(cfg.market_db_file)                # пустая рыночная — на месте, роль та же
    assert c.execute("PRAGMA application_id").fetchone()[0] == db.APP_ID[db.MARKET]
    assert not c.execute("SELECT 1 FROM snapshots").fetchone()
    c.close()
    assert not os.path.exists(cfg.market_db_file + ".split-old")


def test_split_refuses_to_overwrite_pre_split(cfg, tmp_path):
    legacy = str(tmp_path / "old" / "market-demo.db")
    legacy_db(legacy)
    open(legacy + ".pre-split", "wb").close()
    with pytest.raises(FileExistsError, match="pre-split"):
        market.split(cfg, legacy)


def test_place_without_hard_links(tmp_path, monkeypatch):
    t, d = str(tmp_path / "t"), str(tmp_path / "d")
    open(t, "w").write("x")
    monkeypatch.setattr(market.os, "link", lambda a, b: (_ for _ in ()).throw(PermissionError("no links")))
    market._place(t, d)
    assert open(d).read() == "x" and not os.path.exists(t)
    open(t, "w").write("y")
    with pytest.raises(FileExistsError):
        market._place(t, d)
    assert open(d).read() == "x"


def test_db_path_at_legacy_file_says_split(cfg, tmp_path):
    legacy = str(tmp_path / "old" / "market-demo.db")
    legacy_db(legacy)
    c = dataclasses.replace(cfg, db_path=legacy)
    with pytest.raises(db.SplitRequired, match="уберите DB_PATH"):
        db.account(c)
    with pytest.raises(db.SplitRequired, match="уберите DB_PATH"):
        db.market(c)


def test_default_place_legacy_only_warns(cfg, tmp_path, monkeypatch):
    """Общая БД на старом месте (data/), DB_PATH — в другой папке: может быть чужой — не блокирует и сама не
    разделяется (по умолчанию --split берёт только рядом с БД счёта), но предупреждение при старте."""
    from optbot.bot import config_warnings
    from optbot.config import Settings
    old = str(tmp_path / "data" / "market-demo.db")
    legacy_db(old)
    monkeypatch.setattr(Settings, "default_legacy_file", property(lambda self: old))
    c = dataclasses.replace(cfg, db_path=str(tmp_path / "srv" / "account-demo.db"))
    assert any("--from " + old in w for w in config_warnings(c))
    with pytest.raises(FileNotFoundError):
        market.split(c)                                    # без --from чужую не берёт
    db.account(c).close()                                  # не блокирует
    assert os.path.exists(old)
    here = dataclasses.replace(cfg, db_path=str(tmp_path / "data" / "account-demo.db"))
    assert not any("старая общая БД" in w for w in config_warnings(here))   # рядом — блокирует guard, не предупреждение


def test_default_legacy_file_is_project_data(cfg, monkeypatch):
    from optbot.config import ROOT
    monkeypatch.undo()
    assert cfg.default_legacy_file == os.path.join(ROOT, "data", "market-demo.db")


def test_paths_with_uri_chars(cfg, tmp_path):
    d = tmp_path / "bot#1 %20?"
    legacy = str(d / "market-demo.db")
    legacy_db(legacy)
    c = dataclasses.replace(cfg, db_path=legacy)
    with pytest.raises(db.SplitRequired, match="уберите DB_PATH"):
        db.account(c)
    res = market.split(dataclasses.replace(cfg, db_path=str(d / "account-demo.db"),
                                           market_db_path=str(d / "market.db")), legacy)
    assert os.path.exists(res.account)


def test_merge_carries_state_keys_if_missing(cfg, tmp_path, monkeypatch):
    first = str(tmp_path / "d" / "market-demo.db")
    legacy_db(first)
    market.split(cfg, first)
    m = sqlite3.connect(cfg.market_db_file)
    m.execute("DELETE FROM market_state WHERE key GLOB 'rf:*'")
    m.commit()
    m.close()
    second = str(tmp_path / "m" / "market-demo.db")
    legacy_db(second)
    s = sqlite3.connect(second)
    s.execute("INSERT OR REPLACE INTO bot_state (key, value) VALUES ('rf:DTB3', 'x')")
    s.commit()
    s.close()
    res = market.split(dataclasses.replace(cfg, db_path=str(tmp_path / "account-2.db")), second)
    assert res.merged
    m = sqlite3.connect(cfg.market_db_file)
    assert m.execute("SELECT value FROM market_state WHERE key = 'rf:DTB3'").fetchone() == ("x",)
    m.close()


def test_recent_ignores_unreadable(cfg, tmp_path, monkeypatch):
    from optbot import backup
    db.market(cfg).close()
    backup.backup(cfg, path=cfg.market_db_file)
    assert backup.recent(cfg, cfg.market_db_file, 3600)                   # свежая копия есть
    monkeypatch.setattr(backup.os.path, "getmtime", lambda f: (_ for _ in ()).throw(PermissionError("x")))
    assert backup.recent(cfg, cfg.market_db_file, 3600) is None           # нечитаемая — не свежая, без исключения


# ---------- ревью 9 ----------
def test_split_refuses_when_legacy_open_by_other_process(cfg, tmp_path):
    import subprocess
    import sys
    legacy = str(tmp_path / "old" / "market-demo.db")
    legacy_db(legacy)
    p = subprocess.Popen([sys.executable, "-c", "import sys,time; f=open(sys.argv[1]); print('ok', flush=True); "
                          "time.sleep(30)", legacy], stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "ok"
        with pytest.raises(RuntimeError, match=f"pid {p.pid}"):
            market.split(cfg, legacy, force=True)
    finally:
        p.kill()
        p.wait()
    assert os.path.exists(legacy) and not os.path.exists(cfg.db_file)
    assert market.split(cfg, legacy).legacy.endswith(".pre-split")


def test_legacy_renamed_while_locked(cfg, tmp_path, monkeypatch):
    legacy = str(tmp_path / "old" / "market-demo.db")
    legacy_db(legacy)
    seen = {}
    real = os.replace

    def replace(a, b):
        if a == os.path.abspath(legacy):                  # в момент переименования общая ещё заблокирована
            c = sqlite3.connect(a, timeout=0)
            try:
                c.execute("BEGIN IMMEDIATE")
                seen["locked"] = False
            except sqlite3.OperationalError:
                seen["locked"] = True
            finally:
                c.close()
        real(a, b)
    monkeypatch.setattr(market.os, "replace", replace)
    market.split(cfg, legacy)
    assert seen == {"locked": True}
    assert not os.path.exists(legacy + "-wal") and os.path.exists(legacy + ".pre-split")


def test_split_tags_old_reasons_with_env(cfg, tmp_path):
    legacy = str(tmp_path / "old" / "market-demo.db")
    legacy_db(legacy)
    c = sqlite3.connect(legacy)
    c.executemany("INSERT INTO tracked_symbols (symbol, added_ms, until_ms, reason) VALUES (?, 0, 9, ?)",
                  [("X-1", "book:3"), ("X-2", "sig:4"), ("X-3", "manual")])
    c.commit()
    c.close()
    market.split(cfg, legacy)
    m = sqlite3.connect(cfg.market_db_file)
    got = dict(m.execute("SELECT symbol, reason FROM tracked_symbols WHERE symbol GLOB 'X-*'"))
    m.close()
    assert got == {"X-1": "demo:book:3", "X-2": "demo:sig:4", "X-3": "manual"}
    second = str(tmp_path / "m" / "market-demo.db")       # слияние второго окружения — тоже с окружением
    legacy_db(second)
    c = sqlite3.connect(second)
    c.execute("INSERT INTO tracked_symbols (symbol, added_ms, until_ms, reason) VALUES ('Y-1', 0, 9, 'book:3')")
    c.commit()
    c.close()
    c2 = dataclasses.replace(cfg, env="mainnet", db_path=str(tmp_path / "account-mainnet.db"))
    market.split(c2, second, any_name=True)
    m = sqlite3.connect(cfg.market_db_file)
    assert m.execute("SELECT reason FROM tracked_symbols WHERE symbol = 'Y-1'").fetchone() == ("mainnet:book:3",)
    m.close()


def test_split_warns_about_foreign_tables(cfg, tmp_path):
    legacy = str(tmp_path / "old" / "market-demo.db")
    legacy_db(legacy)
    c = sqlite3.connect(legacy)
    c.execute("CREATE TABLE research_x (a)")
    c.commit()
    c.close()
    res = market.split(cfg, legacy)
    assert res.extra == ["research_x"] and "research_x" in str(res)


def test_rollback_removes_wal_of_placed_market(cfg, tmp_path, monkeypatch):
    legacy = str(tmp_path / "old" / "market-demo.db")
    legacy_db(legacy)
    real = market._place

    def place(t, d):
        if d == os.path.abspath(cfg.db_file):
            open(cfg.market_db_file + "-wal", "wb").write(b"x")   # кто-то успел открыть новую рыночную
            raise PermissionError("ro")
        real(t, d)
    monkeypatch.setattr(market, "_place", place)
    with pytest.raises(PermissionError):
        market.split(cfg, legacy)
    assert not os.path.exists(cfg.market_db_file + "-wal")


def test_db_path_custom_legacy_name_hint(cfg, tmp_path):
    legacy = str(tmp_path / "old" / "bot.db")
    legacy_db(legacy)
    c = dataclasses.replace(cfg, db_path=legacy)
    with pytest.raises(db.SplitRequired, match=r"--from .*bot\.db --any-name"):
        db.account(c)


# ---------- ревью 11 ----------
def test_merge_does_not_mark_candles_complete(cfg, tmp_path):
    first = str(tmp_path / "d" / "market-demo.db")
    legacy_db(first)
    market.split(cfg, first)
    m = sqlite3.connect(cfg.market_db_file)
    m.execute("DELETE FROM market_state WHERE key GLOB 'perp_h1_complete*' OR key GLOB 'rf:*'")
    m.commit()
    m.close()
    second = str(tmp_path / "m" / "market-demo.db")
    legacy_db(second)
    s = sqlite3.connect(second)
    s.executemany("INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)",
                  [("perp_h1_complete2:SOL", "1"), ("rf:DTB3", "x")])
    s.commit()
    s.close()
    res = market.split(dataclasses.replace(cfg, db_path=str(tmp_path / "account-2.db")), second)
    m = sqlite3.connect(cfg.market_db_file)
    got = dict(m.execute("SELECT key, value FROM market_state WHERE key != 'split_from'"))
    m.close()
    assert got == {"rf:DTB3": "x"}                         # свечи не переносились — и признак их полноты тоже
    assert res.state_keys == ["rf:DTB3"] and "если не было" in str(res)
    a = sqlite3.connect(res.account)
    assert not a.execute("SELECT 1 FROM bot_state WHERE key GLOB 'perp_h1_complete*'").fetchone()
    a.close()


@pytest.mark.parametrize("merge", [False, True])
def test_failed_legacy_rename_rolls_everything_back(cfg, tmp_path, monkeypatch, merge):
    if merge:
        first = str(tmp_path / "d" / "market-demo.db")
        legacy_db(first)
        market.split(cfg, first)
        cfg = dataclasses.replace(cfg, db_path=str(tmp_path / "x" / "account-demo.db"))
    legacy = str(tmp_path / "old" / "market-demo.db")
    legacy_db(legacy)
    real = os.replace

    def replace(a, b):
        if b.endswith(".pre-split"):
            raise PermissionError("dir ro")
        real(a, b)
    monkeypatch.setattr(market.os, "replace", replace)
    with pytest.raises(PermissionError):
        market.split(cfg, legacy)
    assert os.path.exists(legacy) and not os.path.exists(cfg.db_file)
    assert os.path.exists(cfg.market_db_file) == merge     # не слияние — новая рыночная тоже убрана
    assert not [f for f in os.listdir(os.path.dirname(cfg.db_file)) if "split-part" in f]
    monkeypatch.undo()
    assert market.split(cfg, legacy).legacy.endswith(".pre-split")   # повтор проходит


def test_zero_byte_market_is_replaced(cfg, tmp_path):
    os.makedirs(os.path.dirname(cfg.market_db_file), exist_ok=True)
    open(cfg.market_db_file, "wb").close()
    legacy = str(tmp_path / "old" / "market-demo.db")
    legacy_db(legacy)
    res = market.split(cfg, legacy)
    c = sqlite3.connect(res.market)
    assert c.execute("PRAGMA application_id").fetchone()[0] == db.APP_ID[db.MARKET]
    c.close()


# ---------- ревью 12 ----------
def test_rollback_keeps_market_opened_by_other(cfg, tmp_path, monkeypatch):
    legacy = str(tmp_path / "old" / "market-demo.db")
    legacy_db(legacy)
    real = market._place

    def place(t, d):
        if d == os.path.abspath(cfg.db_file):
            raise PermissionError("ro")
        real(t, d)
    monkeypatch.setattr(market, "_place", place)
    monkeypatch.setattr(market, "_openers", lambda ps: [4242] if any(p.endswith("market.db") for p in ps) else [])
    with pytest.raises(PermissionError):
        market.split(cfg, legacy)
    c = sqlite3.connect(cfg.market_db_file)                # оставлена (полная, из общей) — не убрана из-под процесса
    assert c.execute("SELECT value FROM market_state WHERE key = 'split_from'").fetchone()
    c.close()
    monkeypatch.undo()
    assert market.split(cfg, legacy).merged                # повтор — слиянием


def test_restore_aside_does_not_clobber(tmp_path):
    aside, m = str(tmp_path / "a"), str(tmp_path / "m")
    open(aside, "w").write("old")
    open(m, "w").write("live")
    market._restore_aside(aside, m)
    assert open(m).read() == "live" and open(aside).read() == "old"
    os.remove(m)
    market._restore_aside(aside, m)
    assert open(m).read() == "old" and not os.path.exists(aside)


def test_finish_failure_still_reports_success(cfg, tmp_path, monkeypatch):
    legacy = str(tmp_path / "old" / "market-demo.db")
    legacy_db(legacy)
    monkeypatch.setattr(market, "_finish", lambda *a: 1 / 0)
    res = market.split(cfg, legacy)
    assert res.legacy.endswith(".pre-split") and os.path.exists(res.account) and os.path.exists(res.legacy)


def test_zero_byte_market_with_wal_explains(cfg, tmp_path):
    os.makedirs(os.path.dirname(cfg.market_db_file), exist_ok=True)
    open(cfg.market_db_file, "wb").close()
    open(cfg.market_db_file + "-wal", "wb").close()
    legacy = str(tmp_path / "old" / "market-demo.db")
    legacy_db(legacy)
    with pytest.raises(FileExistsError, match="-wal/-shm"):
        market.split(cfg, legacy)


def test_init_rejects_split_flags(monkeypatch, cfg):
    monkeypatch.setattr(market, "settings", cfg)
    with pytest.raises(SystemExit):
        market.main(["--init", "--from", "x.db"])
