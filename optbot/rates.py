"""Безрисковая ставка для «сверх ставки» книги базиса: векселя США 3 мес. (FRED DTB3, CSV без ключа).

Бот берёт ставку раз в сутки (`RF_URL`, пусто — не брать) и хранит последнее значение в `market_state` (рыночная БД). Не удалось —
остаётся последнее известное; значение старше 7 дней (по дате наблюдения) — предупреждение администратору. Действует
более свежая (по дате наблюдения) из FRED и `rf_annual`/`rf_date` в config/basis.json — запасное значение можно
обновить вручную.
"""
from __future__ import annotations

import calendar
import json
import logging
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import Settings, settings
from .fmt import DAY_MS

log = logging.getLogger("optbot.rates")

STATE_KEY = "rf:DTB3"
STALE_DAYS = 7
TIMEOUT_S = 30
WINDOW_DAYS = 30            # запрашиваем только последний месяц (cosd), не всю историю с 1954 г.


@dataclass
class Rate:
    rate: float              # годовая, доля (0.0416)
    date_ms: int             # дата наблюдения (00:00 UTC)
    source: str              # fred | config

    def age_days(self, now_ms: int) -> float:
        return (now_ms - self.date_ms) / DAY_MS

    def label(self) -> str:
        d = datetime.fromtimestamp(self.date_ms / 1000, timezone.utc).strftime("%Y-%m-%d")
        return f"{self.rate:.2%} ({'FRED DTB3' if self.source == 'fred' else 'config/basis.json'}, {d})"


def parse_csv(text: str) -> list[tuple[int, float]]:
    """CSV FRED «observation_date,DTB3» -> [(дата 00:00 UTC, ставка долей)]; пропуски («.», пусто) — без них."""
    out = []
    for line in text.strip().splitlines()[1:]:
        d, _, v = line.strip().partition(",")
        v = v.strip()
        if not v or v == ".":
            continue
        try:
            day = datetime.strptime(d.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
            out.append((calendar.timegm(day.timetuple()) * 1000, float(v) / 100))
        except ValueError:
            continue
    return sorted(out)


def fetch(url: str, now_ms: int) -> tuple[int, float]:
    """Последнее наблюдение с FRED -> (дата, ставка); нет данных — ValueError."""
    since = datetime.fromtimestamp((now_ms - WINDOW_DAYS * DAY_MS) / 1000, timezone.utc).strftime("%Y-%m-%d")
    full = url + ("&" if "?" in url else "?") + f"cosd={since}"
    req = urllib.request.Request(full, headers={"User-Agent": "options-bot"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        rows = parse_csv(r.read().decode("utf-8", "replace"))
    if not rows:
        raise ValueError("в ответе FRED нет значений")
    d, v = rows[-1]
    if not 0 <= v < 0.5:
        raise ValueError(f"неправдоподобная ставка {v:.2%}")
    return d, v


def _config_rate() -> Rate:
    from .basisbook import load_params
    p = load_params()
    return Rate(p.rf_annual, p.rf_date_ms, "config")


def current(conn) -> Rate:
    """Последняя известная ставка — более свежая по дате наблюдения из FRED (market_state) и config/basis.json
    (администратор вписал новую ставку при недоступном FRED — она и действует)."""
    try:
        cfg_rate = _config_rate()
    except Exception as e:      # config/basis.json испорчен — без запасного значения
        log.warning("Ставка: config/basis.json не прочитан (%s)", e)
        cfg_rate = None
    row = conn.execute("SELECT value FROM market_state WHERE key = ?", (STATE_KEY,)).fetchone()
    if row:
        try:
            v = json.loads(row["value"])
            fred = Rate(float(v["rate"]), int(v["date_ms"]), "fred")
            return fred if cfg_rate is None or fred.date_ms >= cfg_rate.date_ms else cfg_rate
        except (TypeError, ValueError, KeyError):
            log.warning("Ставка: запись %s испорчена — беру config/basis.json", STATE_KEY)
    if cfg_rate is None:
        raise ValueError("ставки нет: ни FRED, ни config/basis.json")
    return cfg_rate


@dataclass
class UpdateResult:
    rate: Rate | None
    error: str | None = None
    stale: bool = False

    def __str__(self) -> str:
        r = self.rate.label() if self.rate else "нет"
        return f"ставка {r}" + (f", ошибка: {self.error}" if self.error else "") + (", устарела" if self.stale else "")


def update(conn, cfg: Settings = settings, now_ms: int | None = None, fetcher=fetch) -> UpdateResult:
    """Взять ставку с FRED и сохранить; ошибка — последнее известное значение. stale — старше STALE_DAYS."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    err = None
    if cfg.rf_url:
        try:
            d, v = fetcher(cfg.rf_url, now_ms)
            with conn:
                conn.execute("INSERT OR REPLACE INTO market_state (key, value) VALUES (?, ?)",
                             (STATE_KEY, json.dumps({"rate": v, "date_ms": d, "fetched_ms": now_ms})))
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            log.warning("Ставка FRED не получена: %s — остаётся последнее значение", err)
    try:
        rate = current(conn)
    except Exception as e:
        return UpdateResult(None, err or str(e), True)
    return UpdateResult(rate, err, rate.age_days(now_ms) > STALE_DAYS)
