#!/usr/bin/env python3
"""Выгрузка резервных копий базы бота (*.db.gz) с сервера на свой компьютер с ротацией.

Запускается на компьютере (таймер systemd --user, deploy/offsite/), не на сервере. Только стандартная библиотека.

Хранение в DEST: daily/ — 7 последних дней, weekly/ — 4 последних воскресенья, monthly/ — 6 последних первых чисел
месяца (дата — из имени файла, UTC). Одна копия на дату (несколько за день — самая поздняя). Сервер хранит 14 дней:
компьютер был выключен меньше двух недель — пропущенные воскресенья и первые числа догрузятся.
Один файл скачивается один раз, в остальные папки — жёсткая ссылка (не место на диске). Скачанный файл проверяется
(gzip CRC, читается целиком) до того, как попасть в папки; удаление старых — только после успешной загрузки.

Настройки — аргументы или переменные окружения (EnvironmentFile таймера):
  OPTBOT_BACKUP_SERVER   user@host (обязательно)
  OPTBOT_BACKUP_REMOTE   папка копий на сервере (trader/backups)
  OPTBOT_BACKUP_DEST     папка на компьютере (~/trader-backups)
  OPTBOT_BACKUP_SSH_KEY  ключ ssh (пусто — по умолчанию ssh)
  OPTBOT_BACKUP_HEALTHCHECK  адрес пинга healthchecks (https; пусто — без пинга): успех — пинг, сбой — /fail
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import gzip
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request

NAME_RE = re.compile(r"^(?P<prefix>[A-Za-z0-9_.-]+)-(?P<date>\d{8})-(?P<time>\d{4})\.db\.gz$")
BUCKETS = ("daily", "weekly", "monthly")
DIR_RE = re.compile(r"^[A-Za-z0-9_./-]+$")
SQLITE_MAGIC = b"SQLite format 3\x00"


def parse(name: str) -> tuple[str, dt.date, str] | None:
    """Имя копии -> (база, дата UTC, время); не копия или кривая дата -> None."""
    m = NAME_RE.match(name)
    if not m:
        return None
    try:
        d = dt.datetime.strptime(m["date"], "%Y%m%d").date()
    except ValueError:
        return None
    return m["prefix"], d, m["time"]


def wanted(names: set[str], keep: dict[str, int], today: dt.date | None = None) -> dict[str, set[str]]:
    """Какие файлы должны лежать в каждой папке: по одному на дату, самые свежие даты, отдельно по каждой базе.
    Даты позже завтрашней (сбой часов сервера) не учитываются — иначе такой файл навсегда занял бы место."""
    limit = (today or dt.datetime.now(dt.timezone.utc).date()) + dt.timedelta(days=1)
    by_key: dict[tuple[str, dt.date], str] = {}
    for n in names:
        p = parse(n)
        if p is None or p[1] > limit:
            continue
        k = (p[0], p[1])
        if k not in by_key or n > by_key[k]:      # в пределах даты имя растёт со временем
            by_key[k] = n
    rule = {"daily": lambda d: True, "weekly": lambda d: d.weekday() == 6, "monthly": lambda d: d.day == 1}
    out: dict[str, set[str]] = {}
    for b in BUCKETS:
        res: set[str] = set()
        for prefix in {k[0] for k in by_key}:
            dates = sorted((d for p, d in by_key if p == prefix and rule[b](d)), reverse=True)[:keep[b]]
            res |= {by_key[(prefix, d)] for d in dates}
        out[b] = res
    return out


def plan(remote: set[str], local: dict[str, set[str]], keep: dict[str, int], today: dt.date | None = None):
    """-> (нужно в папку: {папка: имена}, удалить: {папка: имена}). Учитываются и уже скачанные (свои копии
    сервер удаляет через 14 дней — на компьютере они продолжают считаться)."""
    allnames = set(remote).union(*local.values())
    want = wanted(allnames, keep, today)
    add = {b: want[b] - local[b] for b in BUCKETS}
    # будущие даты — не трогать (не наши решения), остальное лишнее — удалить
    limit = (today or dt.datetime.now(dt.timezone.utc).date()) + dt.timedelta(days=1)
    drop = {b: {n for n in local[b] if (p := parse(n)) is not None and p[1] <= limit} - want[b] for b in BUCKETS}
    return add, drop


class SshRemote:
    def __init__(self, server: str, remote_dir: str, key: str = "", timeout: int = 60):
        d = remote_dir.rstrip("/")
        d = d[2:] if d.startswith("~/") else d          # путь от домашней папки: ~ не раскрывается в кавычках
        if not d or not DIR_RE.match(d):
            raise ValueError(f"папка на сервере: только буквы, цифры, _ . / - ({remote_dir!r})")
        self.server, self.dir, self.timeout = server, d, timeout
        # обрыв связи — ssh сам замечает за ~1 мин (ServerAlive); общего таймаута у загрузки нет: база растёт
        self.opts = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
                     "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4"]
        if key:
            self.opts += ["-i", os.path.expanduser(key), "-o", "IdentitiesOnly=yes"]

    def list(self) -> set[str]:
        cmd = f"ls -1 -- {shlex.quote(self.dir)}"
        r = subprocess.run(["ssh", *self.opts, self.server, cmd], capture_output=True, text=True,
                           timeout=self.timeout)
        if r.returncode != 0:
            raise RuntimeError(f"ssh ls: {r.stderr.strip() or r.returncode}")
        return {n for n in r.stdout.split() if parse(n) is not None}   # только имена копий — без чужого ввода

    def fetch(self, name: str, dst: str) -> None:
        assert parse(name) is not None
        r = subprocess.run(["scp", "-q", *self.opts, f"{self.server}:{self.dir}/{name}", dst],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"scp {name}: {r.stderr.strip() or r.returncode}")


def check_gzip(path: str) -> None:
    """Прочитать целиком: битый, недокачанный, пустой или не база SQLite — исключение."""
    with gzip.open(path, "rb") as f:
        if f.read(len(SQLITE_MAGIC)) != SQLITE_MAGIC:
            raise ValueError(f"{os.path.basename(path)}: не база SQLite (пустой или чужой файл)")
        while f.read(1 << 20):
            pass


def fsync_path(path: str) -> None:
    """fsync файла или папки (папка — чтобы переименования и ссылки пережили сбой питания)."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def place(src: str, dst: str) -> None:
    """Жёсткая ссылка (не копия); другая ФС — копия. Через временное имя: файл появляется целиком."""
    tmp = dst + ".part"
    with contextlib.suppress(FileNotFoundError):
        os.remove(tmp)
    try:
        os.link(src, tmp)
    except OSError:
        shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def local_state(dest: str) -> dict[str, set[str]]:
    out = {}
    for b in BUCKETS:
        d = os.path.join(dest, b)
        os.makedirs(d, exist_ok=True)
        out[b] = {n for n in os.listdir(d) if parse(n) is not None}
    return out


def run(remote, dest: str, keep: dict[str, int], log=print, today: dt.date | None = None) -> int:
    """Один проход -> число скачанных файлов. Сбой — исключение; удаление — только если всё нужное получено.
    Два запуска в одну папку (вручную и таймер) — второй ждёт первого."""
    dest = os.path.expanduser(dest)
    os.makedirs(dest, exist_ok=True)
    with open(os.path.join(dest, ".lock"), "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        return _run(remote, dest, keep, log, today)


def _run(remote, dest: str, keep: dict[str, int], log, today) -> int:
    local = local_state(dest)
    names = remote.list()
    add, drop = plan(names, local, keep, today)
    incoming = os.path.join(dest, ".incoming")
    os.makedirs(incoming, exist_ok=True)
    fetched = 0
    try:
        for name in sorted(set().union(*add.values())):
            src = next((os.path.join(dest, b, name) for b in BUCKETS if name in local[b]), None)
            if src is None:            # нигде на компьютере нет — скачать и проверить
                if name not in names:
                    continue           # уже нет и на сервере (не должно случиться: wanted берёт из имеющихся)
                src = os.path.join(incoming, name)
                remote.fetch(name, src + ".part")
                check_gzip(src + ".part")
                fsync_path(src + ".part")      # сбой питания после удаления старых — новые не пустые
                os.replace(src + ".part", src)
                fetched += 1
                log(f"скачано {name} ({os.path.getsize(src) / 1e6:.1f} МБ)")
            for b in BUCKETS:
                if name in add[b]:
                    place(src, os.path.join(dest, b, name))
    finally:
        shutil.rmtree(incoming, ignore_errors=True)
    for b in BUCKETS:                          # ссылки в папках — на диске до удаления старых
        fsync_path(os.path.join(dest, b))
    future = sorted(n for n in names if (p := parse(n)) and p[1] > (today or dt.datetime.now(dt.timezone.utc).date())
                    + dt.timedelta(days=1))
    if future:
        log(f"ВНИМАНИЕ: копии с датой в будущем пропущены (часы сервера или компьютера?): {', '.join(future)}")
    for b in BUCKETS:
        for name in sorted(drop[b]):
            with contextlib.suppress(FileNotFoundError):
                os.remove(os.path.join(dest, b, name))
            log(f"удалено {b}/{name}")
    return fetched


def main(argv: list[str] | None = None) -> int:
    env = os.environ.get
    ap = argparse.ArgumentParser(description="Выгрузка резервных копий бота с сервера (ротация 7/4/6)")
    ap.add_argument("--server", default=env("OPTBOT_BACKUP_SERVER", ""), help="user@host")
    ap.add_argument("--remote-dir", default=env("OPTBOT_BACKUP_REMOTE", "trader/backups"))
    ap.add_argument("--dest", default=env("OPTBOT_BACKUP_DEST", "~/trader-backups"))
    ap.add_argument("--ssh-key", default=env("OPTBOT_BACKUP_SSH_KEY", ""))
    ap.add_argument("--daily", type=int, default=7)
    ap.add_argument("--weekly", type=int, default=4)
    ap.add_argument("--monthly", type=int, default=6)
    ap.add_argument("--retries", type=int, default=3, help="попыток (сеть после включения поднимается не сразу)")
    ap.add_argument("--retry-wait", type=int, default=60, help="секунд между попытками")
    ap.add_argument("--healthcheck", default=env("OPTBOT_BACKUP_HEALTHCHECK", ""),
                    help="адрес пинга healthchecks (https): успех — пинг, сбой — /fail")
    a = ap.parse_args(argv)
    if not a.server:
        print("не задан сервер: --server или OPTBOT_BACKUP_SERVER", file=sys.stderr)
        return 2
    keep = {"daily": a.daily, "weekly": a.weekly, "monthly": a.monthly}
    if min(keep.values()) < 1:
        print("--daily/--weekly/--monthly должны быть ≥ 1", file=sys.stderr)
        return 2
    if a.healthcheck and not a.healthcheck.startswith("https://"):
        print("--healthcheck: только https://", file=sys.stderr)
        return 2
    try:
        remote = SshRemote(a.server, a.remote_dir, a.ssh_key)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    for attempt in range(1, max(1, a.retries) + 1):
        try:
            n = run(remote, a.dest, keep)
            print(f"готово: скачано {n}")
            ping(a.healthcheck, ok=True)
            return 0
        except Exception as e:
            print(f"попытка {attempt}: {e}", file=sys.stderr)
            if attempt < a.retries:
                time.sleep(a.retry_wait)
    ping(a.healthcheck, ok=False)
    return 1


def ping(url: str, ok: bool) -> None:
    """Пинг healthchecks; сбой пинга — только сообщение (адрес — секрет, не печатается)."""
    if not url:
        return
    if not ok:
        u = urllib.parse.urlsplit(url)
        url = urllib.parse.urlunsplit(u._replace(path=u.path.rstrip("/") + "/fail"))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "optbot-pull-backups"})
        urllib.request.urlopen(req, timeout=15).close()
    except Exception as e:
        print(f"пинг healthchecks не прошёл: {type(e).__name__}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
