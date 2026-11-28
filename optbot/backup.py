"""Резервная копия базы SQLite: онлайн через sqlite3 backup API (не копирование файла — согласованный снимок
при работающем боте, вместе с WAL), проверка копии (PRAGMA quick_check), сжатие gzip (BACKUP_COMPRESS, файл
.db.gz — восстановить: gunzip), хранение BACKUP_KEEP_DAYS дней.

Бот делает копии БД счёта и рыночной раз в сутки в BACKUP_DAILY_UTC; вручную: python -m optbot.backup (обе).
Копия лежит на том же сервере (BACKUP_DIR) — забирать на свой компьютер (rsync/scp по расписанию, deploy/README.md).
"""
from __future__ import annotations

import calendar
import contextlib
import glob
import gzip
import logging
import shutil
import os
import pathlib
import sqlite3
import sys
import time

from .config import Settings, settings

log = logging.getLogger("optbot.backup")


def backup(cfg: Settings, now: float | None = None, path: str | None = None) -> str:
    """Копия БД (path; по умолчанию — БД счёта) -> путь к файлу. Ошибка проверки копии — исключение (испорченная
    копия удаляется). Имя копии — по имени файла БД: account-demo-…, market-…"""
    now = now if now is not None else time.time()
    path = path or cfg.db_file
    os.makedirs(cfg.backup_dir_abs, exist_ok=True)
    name = os.path.splitext(os.path.basename(path))[0]
    dst = os.path.join(cfg.backup_dir_abs, f"{name}-{time.strftime('%Y%m%d-%H%M', time.gmtime(now))}.db")
    if not os.path.isfile(path):  # не плодить пустые «копии» (и не вытеснять ими настоящие)
        raise FileNotFoundError(f"нет базы {path}")
    tmp = f"{dst}.{os.getpid()}.part"  # своё имя: ручной запуск и задача бота в одну минуту не мешают друг другу
    try:
        src = sqlite3.connect(pathlib.Path(os.path.abspath(path)).as_uri() + "?mode=ro", uri=True,
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
        if cfg.backup_compress:  # проверена несжатая; сжатая — тоже через .part
            dst += ".gz"
            with open(tmp, "rb") as fi, open(tmp + ".gz", "wb") as raw:
                with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6) as fo:
                    shutil.copyfileobj(fi, fo, 1 << 20)
                raw.flush()
                os.fsync(raw.fileno())  # сбой питания сразу после копии — не пустой .gz
            with gzip.open(tmp + ".gz", "rb") as fc:  # прочитать целиком: проверка CRC сжатой копии
                while fc.read(1 << 20):
                    pass
            os.replace(tmp + ".gz", dst)
        else:
            os.replace(tmp, dst)  # готовая копия появляется целиком
    finally:
        for f in (tmp, tmp + ".gz", tmp + "-journal", tmp + "-wal", tmp + "-shm"):  # сбой — недописанное убрать
            with contextlib.suppress(FileNotFoundError):
                os.remove(f)
    try:  # копия уже готова: сбой чистки — не «копия не сделана»
        prune(cfg, now, path)
    except OSError as e:
        log.warning("Резервные копии: старые не удалены — %s", e)
    return dst


def recent(cfg: Settings, path: str, max_age_s: float, now: float | None = None) -> tuple[str, float] | None:
    """Копия БД path моложе max_age_s и по времени в имени, и по времени файла -> (файл, возраст, с) | None
    (рыночную БД делят экземпляры — копирует первый за сутки; старая копия, возвращённая на место, — не свежая)."""
    now = now if now is not None else time.time()
    name = os.path.splitext(os.path.basename(path))[0]
    best = None
    for pat in ("db", "db.gz"):
        for f in glob.glob(os.path.join(cfg.backup_dir_abs, f"{name}-[0-9]*.{pat}")):
            stamp = os.path.basename(f)[len(name) + 1:].split(".")[0]
            try:
                made = calendar.timegm(time.strptime(stamp, "%Y%m%d-%H%M"))
                age = now - max(made, 0)
                if now - os.path.getmtime(f) < max_age_s and 0 <= age < max_age_s and (best is None or age < best[1]):
                    best = (f, age)
            except (OSError, ValueError):   # нечитаемый файл копии — не свежая копия: лучше сделать новую
                pass
    return best


def prune(cfg: Settings, now: float | None = None, path: str | None = None) -> list[str]:
    """Удалить копии БД path (по умолчанию — счёта) старше BACKUP_KEEP_DAYS (по времени изменения файла); последнюю
    — никогда."""
    now = now if now is not None else time.time()
    name = os.path.splitext(os.path.basename(path or cfg.db_file))[0]
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
    # [0-9]: не задеть копии другой базы с тем же префиксом; .db и .db.gz — общий счёт (переход на сжатие)
    found = [f for pat in ("db", "db.gz") for f in glob.glob(os.path.join(cfg.backup_dir_abs, f"{name}-[0-9]*.{pat}"))]
    files = sorted((t, f) for f in found if (t := mtime(f)) is not None)
    old = [f for t, f in files[:-1] if now - t > cfg.backup_keep_days * 86400]
    for f in old:
        with contextlib.suppress(FileNotFoundError):
            os.remove(f)
    return old


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    rc = 0
    for src in (settings.db_file, settings.market_db_file):
        try:
            path = backup(settings, path=src)
        except Exception as e:
            print(f"Копия {src} не сделана: {e}", file=sys.stderr)
            rc = 1
            continue
        print(f"{path} ({os.path.getsize(path) / 1e6:.1f} МБ)")
    return rc


if __name__ == "__main__":
    sys.exit(main())
