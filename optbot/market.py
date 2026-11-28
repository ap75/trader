"""Рыночная БД отдельно от БД счёта (этап 1 «Несколько счетов», PLAN.md).

Разделение старой общей БД (`data/market-<env>.db`: рынок и счёт вместе) — один раз, при остановленном боте:

    python -m optbot.market --split [--from data/market-demo.db]

Получается рыночная БД (`MARKET_DB_PATH`, по умолчанию data/market.db) и БД счёта (`DB_PATH`, по умолчанию
data/account-<env>.db); старая общая переименовывается в `….pre-split` (не удаляется). Ставка FRED и признак
полной истории свечей переезжают из bot_state в market_state. Число строк каждой таблицы сверяется с исходной.
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import sqlite3
import sys
import dataclasses
import fnmatch
from dataclasses import dataclass, field

from . import db
from .config import Settings, settings

log = logging.getLogger("optbot.market")

SPACE_FACTOR = 1.5   # свободного места — не меньше исходной БД (с WAL) × столько (обе новые БД и запас)
LOCK_WAIT_S = 5      # БД занята дольше — бот не остановлен
MERGE_BATCH = 5000   # строк котировок ног за транзакцию при слиянии в общую рыночную (читаются вне блокировки)


@dataclass
class SplitResult:
    market: str
    account: str
    legacy: str
    rows: dict[str, int] = field(default_factory=dict)
    state_keys: list[str] = field(default_factory=list)
    merged: bool = False      # рыночная уже была: только БД счёта и ноги на слежении
    extra: list[str] = field(default_factory=list)   # таблицы не из схемы бота — остались только в .pre-split

    def __str__(self) -> str:
        m = sum(self.rows.get(t, 0) for t in db.MARKET_TABLES)
        a = sum(self.rows.get(t, 0) for t in db.ACCOUNT_TABLES)
        keys = (f"; в market_state{' (если не было)' if self.merged else ''}: {', '.join(self.state_keys)}"
                if self.state_keys else "")
        if self.extra:
            keys += f"; ВНИМАНИЕ: таблицы не из схемы бота остались только в старой БД: {', '.join(self.extra)}"
        if self.merged:
            return (f"рынок: {self.market} уже был — дописаны ноги на слежении; счёт: {self.account} ({a} строк); "
                    f"старая БД: {self.legacy}{keys}")
        return (f"рынок: {self.market} ({m} строк); счёт: {self.account} ({a} строк); "
                f"старая БД: {self.legacy}{keys}")


def _counts(conn: sqlite3.Connection, tables) -> dict[str, int]:
    have = {r[0] for r in conn.execute("SELECT name FROM main.sqlite_master WHERE type = 'table'")}
    return {t: conn.execute(f"SELECT COUNT(*) FROM main.{t}").fetchone()[0] for t in tables if t in have}


SPLIT_KEY = "split_from"       # market_state: рыночная получена разделением (из какой общей БД) — в неё можно дописывать
MERGE_STATE_KEYS = ("rf:*",)   # из bot_state общей БД при слиянии в рыночную market_state — только эти


def _rm(p: str) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        if os.path.exists(p + suffix):
            os.remove(p + suffix)


def _check_space(legacy: str, dests: list[str]) -> None:
    """Свободное место на файловых системах назначения: таблицы переносятся без полных копий, вместе ~ размер
    общей БД (с её WAL) с запасом."""
    size = os.path.getsize(legacy) + (os.path.getsize(legacy + "-wal") if os.path.exists(legacy + "-wal") else 0)
    need: dict[int, tuple[str, float]] = {}
    for p in dests:
        d = os.path.dirname(p)
        dev = os.stat(d).st_dev
        need[dev] = (d, need.get(dev, (d, 0))[1] + size * SPACE_FACTOR / 2)
    for d, n in need.values():
        free = shutil.disk_usage(d).free
        if free < n:
            raise OSError(f"мало места в {d}: свободно {free / 1e9:.1f} ГБ, нужно ~{n / 1e9:.1f} ГБ")


def _market_kind(path: str, allow_merge: bool = False) -> str:
    """Существующая рыночная БД: 'split' (получена разделением — дописывать; другие экземпляры могут работать с ней),
    'empty' (пустая и, судя по -wal/-shm, никем не открыта — заменить), иначе отказ с объяснением."""
    if os.path.getsize(path) == 0:   # файл нулевой длины (сбой создания, touch) — заменить, если не открыт
        if any(os.path.exists(path + x) for x in ("-wal", "-shm")):
            raise FileExistsError(f"пустая {path} открыта другим процессом (или остались -wal/-shm после сбоя) — "
                                  f"остановите его или удалите её вместе с -wal/-shm; ничего не менял")
        return "empty"
    c = sqlite3.connect(path)
    try:
        app = c.execute("PRAGMA application_id").fetchone()[0]
        if app != db.APP_ID[db.MARKET]:
            raise FileExistsError(f"{path} уже есть и это не рыночная БД (application_id {app}) — ничего не менял")
        if c.execute("SELECT 1 FROM market_state WHERE key = ?", (SPLIT_KEY,)).fetchone():
            return "split"
        if db.has_rows(c, db.MARKET_TABLES + ("market_state",)):
            if allow_merge:
                return "split"
            raise FileExistsError(f"{path} уже с данными, но не из разделения (создана командой до --split или "
                                  f"другим экземпляром) — дописать в неё только ноги на слежении: --merge (история "
                                  f"рынка общей БД останется в ….pre-split); ничего не менял")
    finally:
        c.close()
    if os.path.exists(path + "-shm") or os.path.exists(path + "-wal"):
        raise FileExistsError(f"пустая {path} открыта другим процессом (или остались -wal/-shm после сбоя) — "
                              f"остановите его или удалите пустую БД вместе с -wal/-shm; ничего не менял")
    return "empty"


def _fill(path: str, role: str, legacy: str, tables) -> dict[str, int]:
    """Новая БД роли и перенос в неё таблиц общей БД (по таблице за транзакцию, журнал DELETE на время переноса —
    без растущего WAL). -> число строк по таблицам."""
    conn = db.connect(path, role=role)
    try:
        conn.isolation_level = None
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("ATTACH DATABASE ? AS src", (legacy,))
        for t in tables:
            conn.execute("BEGIN")
            conn.execute(f"INSERT INTO main.{t} SELECT * FROM src.{t}")
            conn.execute("COMMIT")
        conn.execute("DETACH DATABASE src")
        got = _counts(conn, tables)
        conn.execute("PRAGMA journal_mode=WAL")
        return got
    finally:
        conn.close()


def split(cfg: Settings = settings, legacy: str | None = None, force: bool = False,
          allow_merge: bool = False, any_name: bool = False) -> SplitResult:
    old_umask = os.umask(0o077)   # как у службы (UMask=0077): журнал сделок не читается другими пользователями
    try:
        return _split(cfg, legacy, force, allow_merge, any_name)
    finally:
        os.umask(old_umask)


def _split(cfg: Settings, legacy: str | None, force: bool, allow_merge: bool, any_name: bool) -> SplitResult:
    """Разделить общую БД на рыночную и БД счёта: новые БД своих ролей, таблицы переносятся из общей. Любая ошибка
    — до переименования исходной: она остаётся как была, недоделанные файлы удаляются. Рыночная БД уже получена
    разделением общей БД другого окружения (demo и mainnet делят рынок) — только БД счёта, а ноги на слежении и их
    котировки дописываются в рыночную; остальная история рынка этой общей БД остаётся в ….pre-split. Пока идёт
    разделение, общая БД заблокирована на запись."""
    legacy = os.path.abspath(legacy or cfg.legacy_db_file)
    market, account = os.path.abspath(cfg.market_db_file), os.path.abspath(cfg.db_file)
    if legacy in (market, account):
        raise ValueError(f"общая БД {legacy} совпадает с рыночной или БД счёта — уберите DB_PATH / MARKET_DB_PATH "
                         f"из .env (или задайте другие пути)")
    if not os.path.isfile(legacy):
        raise FileNotFoundError(f"нет общей БД {legacy}")
    if os.path.basename(legacy) != f"market-{cfg.env}.db" and not any_name:
        raise ValueError(f"{os.path.basename(legacy)} — не общая БД окружения {cfg.env} (market-{cfg.env}.db): журнал "
                         f"другого счёта попал бы в {os.path.basename(account)}; точно эта — --any-name")
    if os.path.exists(account):
        raise FileExistsError(f"{account} уже есть — разделение уже сделано? (ничего не менял)")
    if os.path.exists(legacy + ".pre-split"):
        raise FileExistsError(f"{legacy}.pre-split уже есть (прошлое разделение) — перенесите её, чтобы не затереть; "
                              f"ничего не менял")
    kind = _market_kind(market, allow_merge) if os.path.exists(market) else "new"
    merge = kind == "split"
    for p in (market, account):
        os.makedirs(os.path.dirname(p), exist_ok=True)
    _check_space(legacy, [market, account])   # при слиянии в рыночную пишутся котировки ног — запас и для неё

    if users := _openers([legacy, legacy + "-wal"]):
        raise RuntimeError(f"{os.path.basename(legacy)} открыта другим процессом (pid {', '.join(map(str, users))}) — "
                           f"бот работает? systemctl --user stop options-bot (--force тут не поможет); ничего не менял")
    ro = sqlite3.connect(db.ro_uri(legacy), uri=True)
    try:
        extra = [r[0] for r in ro.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                          "AND name NOT LIKE 'sqlite_%'")
                 if r[0] not in db.MARKET_TABLES + db.ACCOUNT_TABLES]
    finally:
        ro.close()
    guard = sqlite3.connect(legacy, timeout=LOCK_WAIT_S)
    src = sqlite3.connect(legacy, timeout=30)
    done = legacy + ".pre-split"
    tmp = [market + ".split-part", account + ".split-part"]
    got_m: dict[str, int] = {}
    try:
        src.row_factory = sqlite3.Row
        app = src.execute("PRAGMA application_id").fetchone()[0]
        ver = src.execute("PRAGMA user_version").fetchone()[0]
        if app != 0 or not 0 < ver <= db.LEGACY_VERSION:
            raise RuntimeError(f"{legacy}: не общая БД (application_id {app}, v{ver}; нужна v{db.LEGACY_VERSION} "
                               f"или старше без роли)")
        has_state = src.execute("SELECT 1 FROM sqlite_master WHERE name = 'bot_state'").fetchone()
        run = src.execute("SELECT value FROM bot_state WHERE key = 'running'").fetchone() if has_state else None
        if run and run["value"] not in ("0", None) and not force:
            raise RuntimeError("по отметке в БД бот работает (или остановился аварийно): systemctl --user stop "
                               "options-bot; если он точно остановлен — --force")
        # блокировка записи на всё время: работающий бот не допишет в общую БД после переноса
        try:
            guard.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as e:
            raise RuntimeError(f"{legacy} занята ({e}) — остановите бота") from e
        if ver < db.LEGACY_VERSION:   # старее последней общей схемы — обновить (под той же блокировкой: отпустить,
            guard.rollback()          # обновить своим соединением, взять снова)
            db.migrate_legacy(guard)
            guard.execute("BEGIN IMMEDIATE")
        before = _counts(src, db.MARKET_TABLES + db.ACCOUNT_TABLES)
        keys = [r["key"] for pat in db.MARKET_STATE_KEYS
                for r in src.execute("SELECT key FROM bot_state WHERE key GLOB ?", (pat,))]
        for p in tmp:
            _rm(p)

        if not merge:
            got_m = _fill(tmp[0], db.MARKET, legacy, db.MARKET_TABLES)
            m = db.connect(tmp[0], role=db.MARKET)
            try:
                m.execute("ATTACH DATABASE ? AS src", (legacy,))
                with m:
                    for k in keys:
                        m.execute("INSERT OR REPLACE INTO market_state (key, value) "
                                  "SELECT key, value FROM src.bot_state WHERE key = ?", (k,))
                    n_state = m.execute("SELECT COUNT(*) FROM market_state").fetchone()[0]
                    m.execute("INSERT INTO market_state (key, value) VALUES (?, ?)",
                              (SPLIT_KEY, os.path.basename(legacy)))
                    m.execute(_TAG_REASONS, (cfg.env,))
                m.execute("DETACH DATABASE src")
                m.execute("PRAGMA wal_checkpoint(TRUNCATE)")   # всё — в основном файле: -wal пуст
            finally:
                m.close()
            if n_state != len(keys):
                raise RuntimeError(f"market_state: {n_state} ключей вместо {len(keys)}")
        got_a = _fill(tmp[1], db.ACCOUNT, legacy, db.ACCOUNT_TABLES)
        a = db.connect(tmp[1], role=db.ACCOUNT)
        try:
            with a:
                a.executemany("DELETE FROM bot_state WHERE key = ?", [(k,) for k in keys])
            got_a["bot_state"] = a.execute("SELECT COUNT(*) FROM bot_state").fetchone()[0]
            a.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            a.close()
        bad = [f"{t}: {before.get(t)} → {got.get(t)}" for got, tables in ((got_m, () if merge else db.MARKET_TABLES),
                                                                          (got_a, db.ACCOUNT_TABLES))
               for t in tables if t != "bot_state" and before.get(t) != got.get(t)]
        if got_a.get("bot_state") != before.get("bot_state", 0) - len(keys):
            bad.append(f"bot_state: {before.get('bot_state')} − {len(keys)} → {got_a.get('bot_state')}")
        if bad:
            raise RuntimeError(f"число строк не сошлось: {'; '.join(bad)}")
        for p in tmp:
            for suffix in ("-wal", "-shm"):   # после checkpoint(TRUNCATE) и закрытия — пустые
                if os.path.exists(p + suffix):
                    os.remove(p + suffix)
        aside = market + ".split-old"   # пустая рыночная — в сторону (сбой — вернуть), не удалять
        if not merge:
            if kind == "empty":
                os.replace(market, aside)
            try:
                _place(tmp[0], market)        # сначала рыночная: без неё БД счёта не откроется
            except BaseException:
                _restore_aside(aside, market)
                raise

        def back_market():                   # назад: повторный --split возможен
            if merge:
                return
            if users := _openers([market, market + "-wal"]):   # новую рыночную уже открыли — не трогать: она
                log.warning("откат: %s уже открыта (pid %s) — оставлена; повторный --split допишет в неё",
                            market, ", ".join(map(str, users)))   # полная (из общей), повтор — слиянием
                _rm(aside)
                return
            os.replace(market, tmp[0])
            for suffix in ("-wal", "-shm"):
                _rm(market + suffix)
            _restore_aside(aside, market)

        try:
            _place(tmp[1], account)
        except BaseException:
            back_market()
            raise
        try:
            if merge:   # ноги на слежении этого окружения и их котировки — в общую рыночную (после БД счёта: сбой
                _merge_legs(market, legacy, cfg.env)   # — всё назад, повтор безопасен: upsert и INSERT OR IGNORE)
            os.replace(legacy, done)   # под блокировкой: работающий процесс не допишет в общую после переноса
        except BaseException:
            os.replace(account, tmp[1])
            for suffix in ("-wal", "-shm"):
                _rm(account + suffix)
            back_market()
            raise
        if os.path.exists(aside):
            os.remove(aside)
    except BaseException:
        for p in tmp:
            _rm(p)
        raise
    finally:
        if guard.in_transaction:
            guard.rollback()
        guard.close()
        src.close()
        db._migrated.clear()   # файлы .split-part и их «схема проверена» — не переносить в повторную попытку
    try:
        res = _finish(legacy, done, market, account, got_m, got_a, keys, merge)
    except Exception as e:   # разделение уже сделано (новые БД на месте, общая переименована) — не «не выполнено»
        log.warning("разделение сделано, но %s не приведена к одному файлу (%s) — копируйте её вместе с -wal", done, e)
        res = SplitResult(market, account, done, {**got_m, **got_a}, _reported_keys(keys, merge), merge)
    if extra:
        log.warning("таблицы не из схемы бота остались только в %s: %s", done, ", ".join(extra))
    return dataclasses.replace(res, extra=extra)


# ноги на слежении из общей БД — с окружением в причине, как у track_reason (ид книг и сигналов — свои у каждой БД счёта)
_TAG_REASONS = ("UPDATE tracked_symbols SET reason = ? || ':' || reason "
                "WHERE reason GLOB 'book:*' OR reason GLOB 'sig:*'")


def _openers(paths: list[str]) -> list[int]:
    """Другие процессы, у которых открыт один из файлов (Linux, /proc; нет /proc — пусто)."""
    want = {os.path.realpath(p) for p in paths if os.path.exists(p)}
    out = []
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit() and int(p) != os.getpid()]
    except OSError:
        return []
    for pid in pids:
        try:
            fds = os.listdir(f"/proc/{pid}/fd")
        except OSError:
            continue
        for fd in fds:
            try:
                if os.readlink(f"/proc/{pid}/fd/{fd}") in want:
                    out.append(int(pid))
                    break
            except OSError:
                continue
    return out


def _place(tmp: str, dst: str) -> None:
    """tmp -> dst, только если dst нет (появился за время переноса — другой процесс создал: не затирать)."""
    try:
        os.link(tmp, dst)
    except FileExistsError:
        raise FileExistsError(f"{dst} появилась во время разделения (другой процесс?) — ничего не заменил") from None
    except OSError:   # нет жёстких ссылок (FUSE, CIFS): занять имя O_EXCL, затем переименовать поверх пустышки
        try:
            os.close(os.open(dst, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        except FileExistsError:
            raise FileExistsError(f"{dst} появилась во время разделения (другой процесс?) — ничего не заменил") from None
        os.replace(tmp, dst)
        return
    os.remove(tmp)


def _merge_legs(market: str, legacy: str, env: str) -> None:
    """Ноги на слежении общей БД и их котировки — в общую рыночную (котировки — порциями по символам)."""
    mk = db.connect(market, role=db.MARKET)
    try:
        mk.execute("ATTACH DATABASE ? AS src", (legacy,))
        with mk:
            mk.execute("INSERT INTO tracked_symbols (symbol, added_ms, until_ms, reason) "
                       "SELECT symbol, added_ms, until_ms, CASE WHEN reason GLOB 'book:*' OR reason GLOB 'sig:*' "
                       "THEN ? || ':' || reason ELSE reason END FROM src.tracked_symbols WHERE true "
                       "ON CONFLICT(symbol) DO UPDATE SET until_ms = MAX(until_ms, excluded.until_ms), "
                       "reason = CASE WHEN excluded.until_ms > until_ms THEN excluded.reason ELSE reason END",
                       (env,))
        # котировки ног этого окружения (MFE сигналов, отметки книг) — слоты, которых в общей нет; один проход
        # чтения вне блокировки, запись порциями: работающий экземпляр не ждёт одну длинную транзакцию
        cols = [r[1] for r in mk.execute("PRAGMA src.table_info(leg_marks)")]
        ins = f"INSERT OR IGNORE INTO leg_marks ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
        rd = sqlite3.connect(db.ro_uri(legacy), uri=True)
        try:
            cur = rd.execute(f"SELECT {', '.join(cols)} FROM leg_marks")
            while rows := cur.fetchmany(MERGE_BATCH):
                with mk:
                    mk.executemany(ins, rows)
        finally:
            rd.close()
        with mk:   # ставка FRED — если в рыночной её ещё нет (иначе — её свежее); признак полной истории свечей —
            for pat in MERGE_STATE_KEYS:   # нет: свечи не переносятся, история рыночной могла быть неполной
                mk.execute("INSERT OR IGNORE INTO market_state (key, value) "
                           "SELECT key, value FROM src.bot_state WHERE key GLOB ?", (pat,))
        mk.execute("DETACH DATABASE src")
    finally:
        mk.close()


def _finish(legacy, done, market, account, got_m, got_a, keys, merge) -> SplitResult:
    """Общая уже переименована (под блокировкой); журнал WAL под старым именем (последнее закрытие его не убрало) —
    к ней, затем всё — в основной файл: остаётся один файл."""
    for suffix in ("-wal", "-shm"):
        if os.path.exists(legacy + suffix):
            if suffix == "-wal" and os.path.getsize(legacy + suffix):
                os.replace(legacy + suffix, done + suffix)
            else:
                os.remove(legacy + suffix)
    c = sqlite3.connect(done)
    try:
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        c.close()
    return SplitResult(market, account, done, {**got_m, **got_a}, _reported_keys(keys, merge), merge)


def _reported_keys(keys: list[str], merge: bool) -> list[str]:
    return keys if not merge else [k for k in keys if any(fnmatch.fnmatchcase(k, p) for p in MERGE_STATE_KEYS)]


def _restore_aside(aside: str, market: str) -> None:
    """Вернуть отложенную пустую рыночную — только если на её месте ничего нет (не затирать чужую новую)."""
    if not os.path.exists(aside):
        return
    try:
        _place(aside, market)
    except FileExistsError:
        log.warning("откат: %s уже создана другим процессом — пустая прежняя оставлена в %s", market, aside)


def init(cfg: Settings) -> int:
    """Пустая рыночная БД явно (db.account/db.market её сами не создают при имеющейся БД счёта)."""
    if os.path.exists(cfg.market_db_file):
        print(f"{cfg.market_db_file} уже есть — ничего не менял", file=sys.stderr)
        return 1
    old = cfg.legacy_db_file
    if os.path.exists(old) and not os.path.exists(cfg.db_file):
        print(f"есть старая общая БД {old} — нужна не --init, а --split", file=sys.stderr)
        return 1
    db.connect(cfg.market_db_file, role=db.MARKET).close()
    print(f"[{cfg.env}] создана пустая рыночная БД {cfg.market_db_file}")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    ap = argparse.ArgumentParser(prog="python -m optbot.market",
                                 description="Рыночная БД: разделение общей БД на рыночную и БД счёта")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--split", action="store_true", help="разделить общую БД (один раз, бот остановлен)")
    g.add_argument("--init", action="store_true",
                   help="создать пустую рыночную БД при уже имеющейся БД счёта (история начнётся заново)")
    ap.add_argument("--force", action="store_true",
                    help="бот остановлен аварийно (отметка «работает» осталась)")
    ap.add_argument("--any-name", action="store_true",
                    help="--from — общая БД с именем не market-<BYBIT_ENV>.db (переименованная копия)")
    ap.add_argument("--merge", action="store_true",
                    help="рыночная БД уже есть с данными, но не из разделения: дописать в неё только ноги на слежении")
    ap.add_argument("--from", dest="src", help=f"общая БД (по умолчанию {os.path.relpath(settings.legacy_db_file)})")
    a = ap.parse_args(argv)
    if a.init:
        if a.force or a.merge or a.any_name or a.src:
            ap.error("--init — без --force/--merge/--any-name/--from (они для --split)")
        return init(settings)
    try:
        res = split(settings, a.src, a.force, a.merge, a.any_name)
    except Exception as e:
        print(f"Разделение не выполнено: {e}", file=sys.stderr)
        return 1
    print(f"[{settings.env}] {res}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
