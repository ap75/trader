"""Календарь событий высокой важности (FOMC, CPI): тег `event` у сигналов и бумажных сделок (E1).

Файл EVENTS_FILE (по умолчанию events.csv в корне проекта): строки `ГГГГ-ММ-ДД,ЧЧ:ММ,важность,название`
(время UTC; `#` — комментарий). Ведётся вручную по официальным календарям — источники в шапке файла.
Перечитывается при изменении файла (по времени изменения) — без перезапуска бота.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

log = logging.getLogger("optbot.events")


@dataclass(frozen=True)
class Event:
    ts_ms: int
    importance: str     # high | medium | low
    name: str


def parse(text: str) -> list[Event]:
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            d, t, imp, name = (x.strip() for x in line.split(",", 3))
            dt = datetime.strptime(f"{d} {t}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except ValueError:
            log.warning("Календарь событий: строка %d не разобрана: %r", n, line)
            continue
        out.append(Event(int(dt.timestamp() * 1000), imp.lower(), name))
    return sorted(out, key=lambda e: e.ts_ms)


class Calendar:
    """Кэш файла: перечитывается, когда меняется mtime. Нет файла — событий нет (с предупреждением один раз)."""

    def __init__(self, path: str):
        self.path = path
        self._mtime: float | None = None
        self._events: list[Event] = []
        self._warned = False
        self._lock = threading.Lock()

    def events(self) -> list[Event]:
        with self._lock:
            try:
                mtime = os.path.getmtime(self.path)
            except OSError:
                if not self._warned:
                    log.warning("Календарь событий %s не найден — тег event не ставится", self.path)
                    self._warned = True
                self._mtime, self._events = None, []
                return []
            if mtime != self._mtime:
                with open(self.path, encoding="utf-8") as f:
                    self._events = parse(f.read())
                self._mtime, self._warned = mtime, False
            return list(self._events)

    def between(self, lo_ms: int, hi_ms: int, importance: str = "high") -> list[Event]:
        """События важности importance в [lo_ms, hi_ms]."""
        return [e for e in self.events() if lo_ms <= e.ts_ms <= hi_ms and e.importance == importance]

    def covered_until(self) -> int | None:
        """Последнее событие в файле: дальше календарь пуст (тег event там не поставится)."""
        ev = self.events()
        return ev[-1].ts_ms if ev else None


_cache: dict[str, Calendar] = {}


def calendar(path: str) -> Calendar:
    if path not in _cache:
        _cache[path] = Calendar(path)
    return _cache[path]
