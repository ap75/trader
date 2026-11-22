"""deploy/offsite/pull_backups.py — выгрузка копий на компьютер с ротацией 7/4/6 (транспорт подменён)."""
import datetime as dt
import gzip
import importlib.util
import os
import pathlib
import shutil

import pytest

_p = pathlib.Path(__file__).resolve().parent.parent / "deploy" / "offsite" / "pull_backups.py"
_spec = importlib.util.spec_from_file_location("pull_backups", _p)
pb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pb)

KEEP = {"daily": 7, "weekly": 4, "monthly": 6}


def name(d: dt.date, t="0320", prefix="market-demo"):
    return f"{prefix}-{d:%Y%m%d}-{t}.db.gz"


class DirRemote:
    """«Сервер» — локальная папка; fail — имена, на которых скачивание ломается."""
    def __init__(self, path, fail=(), corrupt=()):
        self.path, self.fail, self.corrupt, self.fetched = path, set(fail), set(corrupt), []

    def list(self):
        return {n for n in os.listdir(self.path) if pb.parse(n) is not None}

    def fetch(self, n, dst):
        if n in self.fail:
            raise RuntimeError("network")
        self.fetched.append(n)
        if n in self.corrupt:
            pathlib.Path(dst).write_bytes(b"not gzip")
        else:
            shutil.copy(os.path.join(self.path, n), dst)


def put(srv, d, t="0320", prefix="market-demo", body=None):
    n = name(d, t, prefix)
    with gzip.open(os.path.join(srv, n), "wb") as f:
        f.write(pb.SQLITE_MAGIC + n.encode() if body is None else body)
    return n


def run(remote, dest, today=dt.date(2026, 12, 31)):
    return pb.run(remote, str(dest), KEEP, log=lambda *_: None, today=today)


def days(end: dt.date, n: int):
    return [end - dt.timedelta(days=i) for i in range(n)]


def files(dest, b):
    return sorted(os.listdir(os.path.join(dest, b)))


def test_parse():
    assert pb.parse("market-demo-20261122-0320.db.gz")[1] == dt.date(2026, 11, 22)
    assert pb.parse("market-demo-20261122-0320.db") is None           # несжатые — не берём
    assert pb.parse("market-demo-20261122-0320.db.gz.123.part.gz") is None
    assert pb.parse("market-demo-20261340-0320.db.gz") is None          # кривая дата
    assert pb.parse("x; rm -rf ~-20261122-0320.db.gz") is None         # чужие символы — не имя копии


def test_rotation_over_months(tmp_path):
    """Каждый день новая копия (сервер хранит 14) — 7 дневных, 4 воскресных, 6 первых чисел."""
    srv, dest = tmp_path / "srv", tmp_path / "dest"
    srv.mkdir()
    start = dt.date(2026, 1, 1)
    for i in range(240):
        d = start + dt.timedelta(days=i)
        put(srv, d)
        old = srv / name(d - dt.timedelta(days=14))
        if old.exists():
            old.unlink()
        run(DirRemote(srv), dest)
    last = start + dt.timedelta(days=239)
    assert files(dest, "daily") == sorted(name(d) for d in days(last, 7))
    sundays = sorted(d for d in days(last, 40) if d.weekday() == 6)[-4:]
    assert files(dest, "weekly") == sorted(name(d) for d in sundays)
    firsts = sorted(d for d in days(last, 240) if d.day == 1)[-6:]
    assert files(dest, "monthly") == sorted(name(d) for d in firsts)
    w = dest / "weekly" / name(sundays[-1])
    if sundays[-1] in days(last, 7):                                    # одна загрузка — жёсткая ссылка
        assert os.stat(w).st_ino == os.stat(dest / "daily" / name(sundays[-1])).st_ino
    assert not (dest / ".incoming").exists()


def test_computer_off_catches_up_missed_sunday_and_first(tmp_path):
    srv, dest = tmp_path / "srv", tmp_path / "dest"
    srv.mkdir()
    end = dt.date(2026, 3, 10)                       # 01.03 — воскресенье и первое число; компьютер был выключен
    for d in days(end, 14):
        put(srv, d)
    r = DirRemote(srv)
    run(r, dest)
    assert name(dt.date(2026, 3, 1)) in files(dest, "weekly") and name(dt.date(2026, 3, 1)) in files(dest, "monthly")
    assert len(files(dest, "daily")) == 7 and len(r.fetched) == len(set(r.fetched))   # каждое — один раз
    r2 = DirRemote(srv)
    run(r2, dest)
    assert r2.fetched == []                         # всё уже есть — повторно не качает


def test_one_per_day_latest_and_per_database(tmp_path):
    srv, dest = tmp_path / "srv", tmp_path / "dest"
    srv.mkdir()
    d = dt.date(2026, 3, 10)
    put(srv, d, "0320")
    put(srv, d, "1500")                              # ручная копия днём — берётся она
    put(srv, d, "0320", prefix="market-mainnet")
    run(DirRemote(srv), dest)
    assert files(dest, "daily") == sorted([name(d, "1500"), name(d, "0320", "market-mainnet")])


def test_failure_keeps_old_and_corrupt_not_placed(tmp_path):
    srv, dest = tmp_path / "srv", tmp_path / "dest"
    srv.mkdir()
    end = dt.date(2026, 3, 20)
    for d in days(end, 8):
        put(srv, d)
    run(DirRemote(srv), dest)
    before = files(dest, "daily")
    newest = put(srv, end + dt.timedelta(days=1))
    with pytest.raises(RuntimeError):
        run(DirRemote(srv, fail={newest}), dest)
    assert files(dest, "daily") == before            # сбой — ничего не удалено
    with pytest.raises(Exception):
        run(DirRemote(srv, corrupt={newest}), dest)
    assert files(dest, "daily") == before            # битый файл не попал в папки, старые на месте
    assert not (dest / ".incoming").exists()
    run(DirRemote(srv), dest)
    assert newest in files(dest, "daily") and len(files(dest, "daily")) == 7


def test_foreign_files_untouched(tmp_path):
    srv, dest = tmp_path / "srv", tmp_path / "dest"
    srv.mkdir()
    (dest / "daily").mkdir(parents=True)
    (dest / "daily" / "notes.txt").write_text("x")
    put(srv, dt.date(2026, 3, 10))
    run(DirRemote(srv), dest)
    assert "notes.txt" in files(dest, "daily")


def test_main_requires_server(monkeypatch, capsys):
    monkeypatch.delenv("OPTBOT_BACKUP_SERVER", raising=False)
    assert pb.main([]) == 2
    assert pb.main(["--server", "u@h", "--daily", "0"]) == 2


def test_main_retries_then_fails(monkeypatch, tmp_path):
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise RuntimeError("down")
    monkeypatch.setattr(pb, "run", boom)
    monkeypatch.setattr(pb.time, "sleep", lambda s: None)
    assert pb.main(["--server", "u@h", "--dest", str(tmp_path), "--retries", "3"]) == 1
    assert len(calls) == 3


def test_empty_or_foreign_gzip_rejected(tmp_path):
    srv, dest = tmp_path / "srv", tmp_path / "dest"
    srv.mkdir()
    good = put(srv, dt.date(2026, 3, 9))
    bad = put(srv, dt.date(2026, 3, 10), body=b"")                      # пустая, но корректный gzip
    with pytest.raises(ValueError):
        run(DirRemote(srv), dest)
    assert bad not in files(dest, "daily")
    (srv / bad).unlink()
    run(DirRemote(srv), dest)
    assert files(dest, "daily") == [good]


def test_future_dates_ignored(tmp_path):
    srv, dest = tmp_path / "srv", tmp_path / "dest"
    srv.mkdir()
    today = dt.date(2026, 3, 10)
    for d in days(today, 8):
        put(srv, d)
    future = put(srv, dt.date(2036, 12, 1))                             # сбой часов сервера
    msgs = []
    pb.run(DirRemote(srv), str(dest), KEEP, log=msgs.append, today=today)
    assert any("в будущем" in m and future in m for m in msgs)
    assert future not in files(dest, "daily") and future not in files(dest, "monthly")
    assert len(files(dest, "daily")) == 7 and name(today) in files(dest, "daily")


def test_remote_dir_validation():
    assert pb.SshRemote("u@h", "~/trader/backups/").dir == "trader/backups"
    for bad in ("trader/my backups", "x;rm -rf ~", "", "~/"):
        with pytest.raises(ValueError):
            pb.SshRemote("u@h", bad)
    assert pb.main(["--server", "u@h", "--remote-dir", "a b"]) == 2
    assert pb.main(["--server", "u@h", "--healthcheck", "http://x"]) == 2


def test_healthcheck_ping_ok_and_fail(monkeypatch, tmp_path):
    urls = []

    class R:
        def close(self):
            pass
    monkeypatch.setattr(pb.urllib.request, "urlopen", lambda req, timeout: urls.append(req.full_url) or R())
    monkeypatch.setattr(pb.time, "sleep", lambda s: None)
    monkeypatch.setattr(pb, "run", lambda *a, **k: 0)
    assert pb.main(["--server", "u@h", "--dest", str(tmp_path), "--healthcheck", "https://hc/abc"]) == 0

    def boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(pb, "run", boom)
    assert pb.main(["--server", "u@h", "--dest", str(tmp_path), "--retries", "2",
                    "--healthcheck", "https://hc/abc?x=1"]) == 1
    assert urls == ["https://hc/abc", "https://hc/abc/fail?x=1"]


def test_lock_serialises_runs(tmp_path, monkeypatch):
    """Второй запуск в ту же папку ждёт блокировку (flock), а не чистит .incoming первого."""
    import fcntl
    srv, dest = tmp_path / "srv", tmp_path / "dest"
    srv.mkdir()
    dest.mkdir()
    put(srv, dt.date(2026, 3, 10))
    with open(dest / ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        calls = []
        monkeypatch.setattr(pb.fcntl, "flock", lambda f, op: calls.append(op))
        run(DirRemote(srv), dest)
        assert calls == [fcntl.LOCK_EX]


def test_fsync_before_prune(tmp_path, monkeypatch):
    srv, dest = tmp_path / "srv", tmp_path / "dest"
    srv.mkdir()
    end = dt.date(2026, 3, 20)
    for d in days(end, 7):
        put(srv, d)
    run(DirRemote(srv), dest)
    put(srv, end + dt.timedelta(days=1))                               # новый день — самая старая уходит
    order = []
    real_fsync, real_remove = pb.fsync_path, pb.os.remove
    monkeypatch.setattr(pb, "fsync_path", lambda p: order.append(("fsync", os.path.basename(p))) or real_fsync(p))
    monkeypatch.setattr(pb.os, "remove", lambda p: order.append(("rm", os.path.basename(p))) or real_remove(p))
    run(DirRemote(srv), dest)
    first_rm = next(i for i, x in enumerate(order) if x[0] == "rm" and x[1].endswith(".db.gz"))
    synced = {x[1] for x in order[:first_rm] if x[0] == "fsync"}
    assert {"daily", "weekly", "monthly"} <= synced
