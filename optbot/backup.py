"""Резервная копия базы SQLite: онлайн через sqlite3 backup API (не копирование файла — согласованный снимок
при работающем боте, вместе с WAL), проверка копии (PRAGMA quick_check), хранение BACKUP_KEEP_DAYS дней.

Бот делает копию раз в сутки в BACKUP_DAILY_UTC; вручную: python -m optbot.backup.
Копия лежит на том же сервере (BACKUP_DIR) — забирать на свой компьютер (rsync/scp по расписанию, deploy/README.md).
"""
from __future__ import annotations

import glob
import logging
import contextlib
import os
import pathlib
import sqlite3
import sys
import time

from .config import Settings, settings

log = logging.getLogger("optbot.backup")


def backup(cfg: Settings, now: float | None = None) -> str:
    """Копия БД -> путь к файлу. Ошибка проверки копии — исключение (испорченная копия удаляется)."""
    now = now if now is not None else time.time()
    os.makedirs(cfg.backup_dir_abs, exist_ok=True)
    name = os.path.splitext(os.path.basename(cfg.db_file))[0]
    dst = os.path.join(cfg.backup_dir_abs, f"{name}-{time.strftime('%Y%m%d-%H%M', time.gmtime(now))}.db")
    if not os.path.isfile(cfg.db_file):  # не плодить пустые «копии» (и не вытеснять ими настоящие)
        raise FileNotFoundError(f"нет базы {cfg.db_file}")
    tmp = f"{dst}.{os.getpid()}.part"  # своё имя: ручной запуск и задача бота в одну минуту не мешают друг другу
    try:
        src = sqlite3.connect(pathlib.Path(os.path.abspath(cfg.db_file)).as_uri() + "?mode=ro", uri=True,
                              timeout=30)
        try:
            out = sqlite3.connect(tmp)
            try:
                src.backup(out)  # за один проход (читатель WAL не блокирует писателей бота)
                ok = out.execute("PRAGMA quick_check").fetchone()[0]
            finally:
                out.close()
        finally:
            src.close()
        if ok != "ok":
            raise RuntimeError(f"копия не прошла проверку: {ok}")
        os.replace(tmp, dst)  # готовая копия появляется целиком
    finally:
        for f in (tmp, tmp + "-journal", tmp + "-wal", tmp + "-shm"):  # сбой — недописанную копию не оставлять
            with contextlib.suppress(FileNotFoundError):
                os.remove(f)
    try:  # копия уже готова: сбой чистки — не «копия не сделана»
        prune(cfg, now)
    except OSError as e:
        log.warning("Резервные копии: старые не удалены — %s", e)
    return dst


def prune(cfg: Settings, now: float | None = None) -> list[str]:
    """Удалить копии старше BACKUP_KEEP_DAYS (по времени изменения файла); последнюю — никогда."""
    now = now if now is not None else time.time()
    name = os.path.splitext(os.path.basename(cfg.db_file))[0]
    def mtime(f):  # параллельный запуск мог уже удалить или переименовать файл
        try:
            return os.path.getmtime(f)
        except FileNotFoundError:
            return None
    for f in glob.glob(os.path.join(cfg.backup_dir_abs, f"{name}-[0-9]*.part*")):  # остатки оборванных копий
        t = mtime(f)
        if t is not None and now - t > 86400:
            with contextlib.suppress(FileNotFoundError):
                os.remove(f)
    files = sorted((t, f) for f in glob.glob(os.path.join(cfg.backup_dir_abs, f"{name}-[0-9]*.db"))  # [0-9]: не задеть копии другой базы с тем же префиксом
                   if (t := mtime(f)) is not None)
    old = [f for t, f in files[:-1] if now - t > cfg.backup_keep_days * 86400]
    for f in old:
        with contextlib.suppress(FileNotFoundError):
            os.remove(f)
    return old


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    try:
        path = backup(settings)
    except Exception as e:
        print(f"Копия не сделана: {e}", file=sys.stderr)
        return 1
    print(f"{path} ({os.path.getsize(path) / 1e6:.1f} МБ)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
