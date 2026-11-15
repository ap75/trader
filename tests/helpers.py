"""Вспомогательные функции тестов."""
import dataclasses
from decimal import Decimal

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
