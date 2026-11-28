"""Вспомогательные функции тестов."""
import dataclasses
from decimal import Decimal

from optbot import db
from optbot.collector import track
from optbot.config import Settings


def with_cfg(cfg: Settings, **kw) -> Settings:
    """Копия настроек с изменёнными полями (Settings заморожен). Для dict-лимитов — {'BTC': '0.2'}."""
    for k, v in list(kw.items()):
        if isinstance(v, dict):
            base = dict(getattr(cfg, k))
            base.update({c: Decimal(str(x)) for c, x in v.items()})
            kw[k] = base
        elif isinstance(v, (int, float, str)) and isinstance(getattr(cfg, k), Decimal):
            kw[k] = Decimal(str(v))
    return dataclasses.replace(cfg, **kw)


def track_symbols(cfg: Settings, symbols: list[str], until_ms: int, reason: str = "") -> None:
    """Поставить ноги на слежение отдельным соединением БД счёта (в коде — track() в транзакции журнала сигналов;
    таблица — в рыночной БД)."""
    conn = db.account(cfg)
    try:
        with conn:
            track(conn, symbols, until_ms, reason)
    finally:
        conn.close()
