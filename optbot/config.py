"""Настройки из переменных окружения / файла .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal

from dotenv import load_dotenv

load_dotenv()


def _ids(name: str) -> set[int]:
    return {int(x) for x in os.getenv(name, "").replace(" ", "").split(",") if x}


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v is None or v.strip() == "" else v.strip().lower() in ("1", "true", "yes", "on")


def _list(name: str, default: str = "") -> list[str]:
    return [x.strip().upper() for x in os.getenv(name, default).split(",") if x.strip()]


@dataclass(frozen=True)
class Settings:
    # testnet | demo | mainnet
    env: str = os.getenv("BYBIT_ENV", "testnet").lower()
    api_key: str = os.getenv("BYBIT_API_KEY", "")
    api_secret: str = os.getenv("BYBIT_API_SECRET", "")
    settle_coin: str = os.getenv("SETTLE_COIN", "USDT").upper()
    base_coins: list[str] = field(default_factory=lambda: _list("BASE_COINS", "BTC,ETH,SOL"))

    # Ограничения безопасности
    max_order_qty: dict[str, Decimal] = field(
        default_factory=lambda: {
            c: Decimal(os.getenv(f"MAX_QTY_{c}", "0")) for c in _list("BASE_COINS", "BTC,ETH,SOL")
        }
    )
    # Максимальная премия по одному ордеру (qty * price), в USDT
    max_order_premium_usdt: Decimal = Decimal(os.getenv("MAX_ORDER_PREMIUM_USDT", "500"))

    # Защита от опечаток: худшая ожидаемая цена исполнения (по стакану) хуже mark больше чем на N% -> отказ
    max_slippage_pct: Decimal = Decimal(os.getenv("MAX_SLIPPAGE_PCT", "15"))
    # Рыночный ордер запрещён, если спред (ask-bid)/mid больше N%
    max_spread_pct: Decimal = Decimal(os.getenv("MAX_SPREAD_PCT", "20"))

    # Лимиты портфеля (0 = выключено). Уменьшать позицию/дельту можно всегда.
    # |позиция по символу| после ордера (с учётом открытых ордеров в ту же сторону)
    max_position: dict[str, Decimal] = field(
        default_factory=lambda: {
            c: Decimal(os.getenv(f"MAX_POSITION_{c}", "0")) for c in _list("BASE_COINS", "BTC,ETH,SOL")
        }
    )
    # |суммарная дельта опционов по активу| после ордера, в единицах актива
    max_net_delta: dict[str, Decimal] = field(
        default_factory=lambda: {
            c: Decimal(os.getenv(f"MAX_NET_DELTA_{c}", "0")) for c in _list("BASE_COINS", "BTC,ETH,SOL")
        }
    )
    # Суммарная премия открывающих (не reduce-only) опционных ордеров за сутки UTC, USDT
    max_daily_premium_usdt: Decimal = Decimal(os.getenv("MAX_DAILY_PREMIUM_USDT", "0"))

    # Telegram
    tg_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    # Группы, где бот работает (id групп отрицательные). Из остальных групп бот выходит.
    tg_allowed_chat_ids: set[int] = field(default_factory=lambda: _ids("TELEGRAM_ALLOWED_CHAT_IDS"))
    # Права — по пользователю (from_user.id), в личке и в разрешённых группах
    tg_trader_ids: set[int] = field(default_factory=lambda: _ids("TELEGRAM_TRADER_IDS"))  # торговля + просмотр
    tg_viewer_ids: set[int] = field(default_factory=lambda: _ids("TELEGRAM_VIEWER_IDS"))  # только просмотр
    # Просмотр (не торговля) для всех участников разрешённых групп
    allow_all_group_members_view: bool = _bool("ALLOW_ALL_GROUP_MEMBERS_VIEW", False)
    # Баланс/позиции/ордера/греки — только в личке
    sensitive_commands_dm_only: bool = _bool("SENSITIVE_COMMANDS_DM_ONLY", True)

    def __post_init__(self):
        # клиент идёт в mainnet при любом значении, кроме testnet/demo — опечатка не должна туда вести
        if self.env not in ("testnet", "demo", "mainnet"):
            raise ValueError(f"BYBIT_ENV={self.env!r}: допустимо testnet, demo или mainnet")

    @property
    def is_mainnet(self) -> bool:
        return self.env == "mainnet"  # значение проверено в __post_init__


settings = Settings()
