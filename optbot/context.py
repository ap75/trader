"""Рыночный фон к сигналу: фондирование и открытый интерес перпетуала BASEUSDT (Bybit), индекс страха и жадности
(alternative.me, общий для крипторынка, обновляется раз в сутки). Справочно: на выбор стратегии не влияет —
пишется в карточку, журнал сигналов и бумажные сделки, чтобы журнал показал, помогал ли фон.

Сбой любого источника — поле пустое (None), сигнал считается как обычно.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

log = logging.getLogger("optbot.context")

FNG_TTL_S = 3600          # индекс обновляется раз в сутки — чаще раза в час не спрашивать
FNG_RETRY_S = 300         # после сбоя — не чаще раза в 5 мин (не тормозить каждый расчёт сигнала)
FNG_TIMEOUT_S = 5
FNG_MAX_AGE_S = 2 * 86400  # старше — не показывать и не писать в журнал (источник давно недоступен)


@dataclass
class MarketContext:
    funding: float | None = None          # текущая ставка фондирования за период (доля: 0.0001 = 0.01%)
    funding_h: float | None = None        # период фондирования, ч
    oi_value: float | None = None         # открытый интерес, USDT
    oi_chg_24h: float | None = None       # изменение открытого интереса (в контрактах) за 24 ч, доля
    fng: int | None = None                # индекс страха и жадности 0..100
    fng_label: str | None = None          # классификация источника (Extreme Fear … Extreme Greed)

    def as_dict(self) -> dict:
        return dict(funding=self.funding, funding_h=self.funding_h, oi_value=self.oi_value,
                    oi_chg_24h=self.oi_chg_24h, fng=self.fng, fng_label=self.fng_label)


class FearGreed:
    """Индекс страха и жадности с кэшем в памяти (общий для потоков расчёта сигналов). Запрос — в фоновом потоке:
    расчёт сигнала его не ждёт (первый расчёт после старта — без индекса)."""

    def __init__(self, url: str):
        self.url = url
        self._lock = threading.Lock()
        self._val: tuple[int, str] | None = None
        self._at = -1e18
        self._fail_at = -1e18
        self._busy = False
        self._src_ts = 0.0               # метка времени значения у источника (с), для FNG_MAX_AGE_S
        self._done = threading.Event()   # первое обновление завершено (успех или сбой)

    def _load(self) -> tuple[int, str, float]:
        # без данных пользователя: общий User-Agent, адрес — из настроек
        req = urllib.request.Request(self.url, headers={"User-Agent": "options-bot"})
        with urllib.request.urlopen(req, timeout=FNG_TIMEOUT_S) as r:
            d = json.loads(r.read(65536))["data"][0]
        v = int(d["value"])
        if not 0 <= v <= 100:
            raise ValueError(f"индекс вне 0..100: {v}")
        return v, str(d.get("value_classification") or ""), float(d.get("timestamp") or time.time())

    def refresh(self) -> None:
        """Запрос к источнику (блокирующий; вне блокировки — get других потоков не ждёт)."""
        try:
            v, label, ts = self._load()
        except Exception as e:
            with self._lock:
                self._fail_at, self._busy = time.monotonic(), False
            log.warning("Индекс страха и жадности не получен — %s", type(e).__name__)
        else:
            with self._lock:
                self._val, self._src_ts, self._at, self._busy = (v, label), ts, time.monotonic(), False
        self._done.set()

    def get(self, first_wait: float = 0.0) -> tuple[int, str] | None:
        """Текущее значение без ожидания сети; устарело — обновление в фоне (одно на все потоки). first_wait —
        сколько ждать самый первый ответ в процессе (короткоживущие CLI/--step иначе завершатся без индекса)."""
        if not self.url:
            return None
        with self._lock:
            now = time.monotonic()
            start = (not self._busy and now - self._at >= FNG_TTL_S and now - self._fail_at >= FNG_RETRY_S)
            self._busy = self._busy or start
        if start:  # вне блокировки: refresh берёт её сам
            try:
                threading.Thread(target=self.refresh, name="fng", daemon=True).start()
            except Exception as e:  # лимит потоков и т.п.: без индекса, повтор — после FNG_RETRY_S
                with self._lock:
                    self._busy, self._fail_at = False, time.monotonic()
                log.warning("Индекс страха и жадности: поток не запущен — %s", e)
        if first_wait > 0 and not self._done.is_set():
            self._done.wait(first_wait)
        with self._lock:  # возраст — по метке источника: «залипший» источник не выдаёт старое за свежее
            return self._val if self._val and time.time() - self._src_ts < FNG_MAX_AGE_S else None


def fng_history(url: str, limit: int = 0) -> list[tuple[int, int, str]]:
    """История индекса: [(начало дня, мс; значение; классификация)]; limit 0 — вся (с 2018 г.). Адрес — FNG_URL
    с заменённым limit. Ошибка — исключение (вызывающий решает, что делать)."""
    u = urllib.parse.urlsplit(url)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(u.query) if k != "limit"] + [("limit", str(limit))]
    req = urllib.request.Request(urllib.parse.urlunsplit(u._replace(query=urllib.parse.urlencode(q))),
                                 headers={"User-Agent": "options-bot"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read(4 << 20))["data"]
    out = []
    for d in data:
        try:  # битая строка — пропустить её, а не всю историю
            v, ts = int(d["value"]), int(d["timestamp"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= v <= 100:
            out.append((ts * 1000, v, str(d.get("value_classification") or "")))
    return out


_FNG: dict[str, FearGreed] = {}
_FNG_LOCK = threading.Lock()


def fear_greed(url: str) -> FearGreed:
    """Один кэш на адрес: движки сигналов бота, книг и CLI не спрашивают источник каждый своим запросом."""
    with _FNG_LOCK:
        return _FNG.setdefault(url, FearGreed(url))


def _f(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def perp_funding(client, base: str, ctx: MarketContext | None = None) -> MarketContext:
    """Фондирование и открытый интерес из тикера перпетуала; сбой — пустые поля."""
    ctx = ctx if ctx is not None else MarketContext()
    try:
        t = client.perp_ticker(base) or {}
        ctx.funding = _f(t.get("fundingRate"))
        ctx.funding_h = _f(t.get("fundingIntervalHour")) or (8.0 if ctx.funding is not None else None)
        ctx.oi_value = _f(t.get("openInterestValue"))
    except Exception as e:
        log.warning("Фондирование %s не получено — %s", base, e)
    return ctx


def perp_oi_change(client, base: str) -> float | None:
    """Изменение открытого интереса ровно за 24 ч (по меткам времени часовой истории); нет точки — None."""
    try:
        hist = client.perp_open_interest(base, 26)   # часовые, от старых к новым
    except Exception as e:
        log.warning("История открытого интереса %s не получена — %s", base, e)
        return None
    if not hist:
        return None
    last_ts, last = hist[-1]
    target = last_ts - 24 * 3_600_000
    # ближайшая точка не позже суток назад, но не старше ещё на час (пропуск часа, не круглая метка)
    ref = [v for ts, v in hist if target - 3_600_000 < ts <= target]
    return last / ref[-1] - 1 if ref and ref[-1] > 0 else None


def perp_context(client, base: str) -> MarketContext:
    ctx = perp_funding(client, base)
    ctx.oi_chg_24h = perp_oi_change(client, base)
    return ctx


# ---------- корзины для статистики ----------
_FNG_LABELS = {"extreme fear": "крайний страх", "fear": "страх", "neutral": "нейтрально", "greed": "жадность",
               "extreme greed": "крайняя жадность"}


def fng_bucket(v: int | None, label: str | None = None) -> str:
    """По классификации источника (как в карточке); без неё — по числу: 0–24 крайний страх, 25–44 страх,
    45–55 нейтрально, 56–75 жадность, 76–100 крайняя жадность."""
    if v is None:
        return "нет данных"
    if label and label.strip().lower() in _FNG_LABELS:
        return _FNG_LABELS[label.strip().lower()]
    for hi, name in ((24, "крайний страх"), (44, "страх"), (55, "нейтрально"), (75, "жадность")):
        if v <= hi:
            return name
    return "крайняя жадность"


FNG_ORDER = ("крайний страх", "страх", "нейтрально", "жадность", "крайняя жадность", "нет данных")
FUNDING_BASE = 0.0001  # базовая ставка Bybit за 8 ч (0.01%) — при «спокойном» рынке


def funding_bucket(rate: float | None, hours: float | None = None) -> str:
    """По ставке, приведённой к 8 ч: < 0 — шорты платят; до базовой 0.01% — норма; выше — лонги переплачивают."""
    if rate is None:
        return "нет данных"
    r8 = rate * 8 / (hours or 8)
    if r8 < 0:
        return "отрицательное"
    return "обычное (≤ 0.01%/8ч)" if r8 <= FUNDING_BASE * 1.0001 else "высокое (> 0.01%/8ч)"


FUNDING_ORDER = ("отрицательное", "обычное (≤ 0.01%/8ч)", "высокое (> 0.01%/8ч)", "нет данных")


def fmt_context(ctx: MarketContext | None) -> str | None:
    """Строка для карточки: « фондирование +0.010%/8ч · OI 1.23 млрд $ (+3.1% за 24ч) · страх/жадность 72 (Greed)»."""
    if ctx is None:
        return None
    parts = []
    if ctx.funding is not None:
        parts.append(f"фондирование {ctx.funding * 100:+.4f}%/{ctx.funding_h or 8:g}ч")
    if ctx.oi_value is not None:
        v = ctx.oi_value
        s = f"{v / 1e9:.2f} млрд $" if v >= 1e9 else f"{v / 1e6:.1f} млн $"
        if ctx.oi_chg_24h is not None:
            s += f" ({ctx.oi_chg_24h * 100:+.1f}% за 24ч)"
        parts.append("OI " + s)
    if ctx.fng is not None:
        parts.append(f"страх/жадность {ctx.fng}" + (f" ({ctx.fng_label})" if ctx.fng_label else ""))
    return " " + " · ".join(parts) if parts else None
