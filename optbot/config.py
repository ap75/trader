"""Настройки из переменных окружения / файла .env."""
from __future__ import annotations

import logging
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


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # корень проекта


def _minutes_divisor(name: str, default: int) -> int:
    """Минуты, делящие час (1, 5, 10, 15, 30, 60); неверное значение -> 0 (разбирается в __post_init__)."""
    v = (os.getenv(name) or "").strip()
    try:
        n = int(v) if v else default
    except ValueError:
        return 0
    return n if 1 <= n <= 60 and 60 % n == 0 else 0


def _num_in(name: str, default, lo, hi, cast=float):
    """Число в [lo, hi]; неверное — значение по умолчанию с предупреждением (настройка не критична)."""
    v = (os.getenv(name) or "").strip()
    try:
        x = cast(v.replace(",", ".")) if v else default
        if lo <= x <= hi:
            return x
    except ValueError:
        pass
    kind = "целое" if cast is int else "число"
    logging.getLogger("optbot.config").warning("%s=%r: нужно %s %s…%s — использую %s", name, v, kind, lo, hi, default)
    return default


def _int_in(name: str, default: int, lo: int, hi: int) -> int:
    return _num_in(name, default, lo, hi, int)


def _float_in(name: str, default: float, lo: float, hi: float) -> float:
    return _num_in(name, default, lo, hi, float)


def _parse_hhmm(x: str) -> tuple[int, int] | None:
    h, sep, m = x.partition(":")
    digits = all(part.isascii() and part.isdecimal() for part in (h, m))  # «²» — isdigit, но int() его не берёт
    ok = sep and digits and len(m) == 2 and int(h) < 24 and int(m) < 60
    return (int(h), int(m)) if ok else None


def _hhmm(name: str, default: str) -> tuple[int, int] | None:
    """Время суток UTC «ЧЧ:ММ»; off/no/false/0 — выключено (None); неверное — по умолчанию с предупреждением."""
    v = (os.getenv(name) or "").strip().lower()
    if v in ("off", "no", "false", "0", "-"):
        return None
    t = _parse_hhmm(v or default)
    if t is None:
        logging.getLogger("optbot.config").warning("%s=%r: нужно ЧЧ:ММ (UTC) или off — использую %s",
                                                   name, v, default)
        t = _parse_hhmm(default)
    return t


def _list(name: str, default: str = "") -> list[str]:
    return [x.strip().upper() for x in os.getenv(name, default).split(",") if x.strip()]


def _pair_bad(a, b, k, strict: bool) -> bool:
    """Пара несогласована: strict — нужно a·k < b, иначе a·k ≤ b."""
    return a * k >= b if strict else a * k > b


_SIG_PAIRS = {"dte_min": 2, "dte_max": 7, "ema_fast": 12, "ema_slow": 48, "pctl_low": 30, "pctl_high": 70,
              "debit_short_delta": 0.25, "debit_long_delta": 0.45, "min_history_days": 14, "lookback_days": 60}
# ↑ значения по умолчанию связанных порогов — единственный источник: поля ниже берут их отсюда


@dataclass(frozen=True)
class SignalSettings:
    """Пороги сигналов (этап 3, optbot.signals). Проценты — в процентах (70 = 70%), доли — долями."""
    target_days: float = _float_in("SIG_TARGET_DAYS", 7, 1, 30)        # срок IV постоянной дюрации
    dte_min: float = _float_in("SIG_DTE_MIN", _SIG_PAIRS["dte_min"], 0.5, 30)              # окно экспирации, дней
    dte_max: float = _float_in("SIG_DTE_MAX", _SIG_PAIRS["dte_max"], 1, 60)
    min_history_days: float = _float_in("SIG_MIN_HISTORY_DAYS", _SIG_PAIRS["min_history_days"], 1, 365)  # меньше — «мало истории»
    lookback_days: int = _int_in("SIG_LOOKBACK_DAYS", _SIG_PAIRS["lookback_days"], 7, 730)      # окно истории для IV percentile
    pctl_high: float = _float_in("SIG_PCTL_HIGH", _SIG_PAIRS["pctl_high"], 50, 100)         # IV высокая: pctl ≥
    pctl_low: float = _float_in("SIG_PCTL_LOW", _SIG_PAIRS["pctl_low"], 0, 50)             # IV низкая: pctl ≤
    iv_rv_high: float = _float_in("SIG_IV_RV_HIGH", 1.15, 1, 3)        # IV высокая: IV7 > RV7 × k
    ema_fast: int = _int_in("SIG_EMA_FAST", _SIG_PAIRS["ema_fast"], 2, 299)                # EMA на 4h-свечах
    ema_slow: int = _int_in("SIG_EMA_SLOW", _SIG_PAIRS["ema_slow"], 3, 300)   # ×3 свечей ≤ лимита kline 1000
    trend_pct: float = _float_in("SIG_TREND_PCT", 1.0, 0, 20)          # тренд: |EMAfast/EMAslow − 1| ≥ N%
    short_delta: float = _float_in("SIG_SHORT_DELTA", 0.20, 0.05, 0.45)  # короткие ноги кондора / кредит-спреда
    debit_long_delta: float = _float_in("SIG_DEBIT_LONG_DELTA", _SIG_PAIRS["debit_long_delta"], 0.2, 0.7)
    debit_short_delta: float = _float_in("SIG_DEBIT_SHORT_DELTA", _SIG_PAIRS["debit_short_delta"], 0.05, 0.45)
    wing_em: float = _float_in("SIG_WING_EM", 0.5, 0.1, 3)             # ширина крыла, доля ожидаемого движения
    max_leg_spread_pct: float = _float_in("SIG_MAX_LEG_SPREAD_PCT", 20, 1, 100)  # спред ноги, % от mid
    min_oi_usd: float = _float_in("SIG_MIN_OI_USD", 20000, 0, 1e9)     # OI ноги × форвард, USDT
    min_credit_ratio: float = _float_in("SIG_MIN_CREDIT_RATIO", 0.10, 0, 0.9)  # кредит ≥ доли ширины спреда
    max_debit_ratio: float = _float_in("SIG_MAX_DEBIT_RATIO", 0.60, 0.1, 1)    # дебет ≤ доли ширины спреда

    def __post_init__(self):
        # перепутанные пары — обе по умолчанию (сигналы не должны молча работать на несогласованных порогах)
        # (меньшее, большее, во сколько раз большее должно превышать меньшее, как сказать в логе)
        for lo, hi, k, strict, how in (("dte_min", "dte_max", 1, False, "не больше"),
                                       ("ema_fast", "ema_slow", 1, True, "меньше"),
                                       ("pctl_low", "pctl_high", 1, True, "меньше"),
                                       ("debit_short_delta", "debit_long_delta", 1, True, "меньше"),
                               # история считается по часам со срезами: окно с запасом на пропуски сбора
                                       ("min_history_days", "lookback_days", 1.25, False, "не больше 80% от")):
            a, b = getattr(self, lo), getattr(self, hi)
            bad = _pair_bad(a, b, k, strict)
            if bad:
                # сбросить только то, что задано в .env (другое — уже значение по умолчанию); оба/ни одного — оба
                env_lo, env_hi = (bool((os.getenv(f"SIG_{x.upper()}") or "").strip()) for x in (lo, hi))
                reset = [lo] if env_lo and not env_hi else [hi] if env_hi and not env_lo else [lo, hi]
                if len(reset) == 1:  # сброс одного должен давать согласованную пару, иначе — оба
                    na = _SIG_PAIRS[lo] if reset == [lo] else a
                    nb = _SIG_PAIRS[hi] if reset == [hi] else b
                    if _pair_bad(na, nb, k, strict):
                        reset = [lo, hi]
                logging.getLogger("optbot.config").warning(
                    "SIG_%s=%s должно быть %s SIG_%s=%s — использую %s", lo.upper(), a, how, hi.upper(), b,
                    ", ".join(f"SIG_{x.upper()}={_SIG_PAIRS[x]}" for x in reset))
                for x in reset:
                    object.__setattr__(self, x, _SIG_PAIRS[x])
        if not self.dte_min <= self.target_days <= self.dte_max:  # не сброс: может быть намеренно
            logging.getLogger("optbot.config").warning(
                "SIG_TARGET_DAYS=%g вне окна экспираций SIG_DTE_MIN…SIG_DTE_MAX (%g…%g): IV считается на другой срок, "
                "чем у сделок", self.target_days, self.dte_min, self.dte_max)


_SIG_DEFAULT = SignalSettings()


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

    # Сборщик рыночных данных (этап 1): срез каждые N минут в SQLite
    collector_enabled: bool = _bool("COLLECTOR_ENABLED", True)
    collector_interval_min: int = _minutes_divisor("COLLECTOR_INTERVAL_MIN", 15)  # 0 — неверное значение
    # Пусто — своя БД на окружение (data/market-<env>.db): данные testnet не смешиваются с mainnet/demo.
    # Итоговый путь — свойство db_file (относительные пути — от корня проекта, а не от текущей папки).
    db_path: str = os.getenv("DB_PATH", "").strip()

    # Журнал сделок (этап 2): ордера, исполнения, поставки — в той же БД
    journal_enabled: bool = _bool("JOURNAL_ENABLED", True)
    journal_sync_min: int = _int_in("JOURNAL_SYNC_MIN", 60, 5, 1440)        # сверка по REST
    journal_backfill_days: int = _int_in("JOURNAL_BACKFILL_DAYS", 30, 1, 730)  # глубина первой выгрузки
    # секрет для хеша пользователя в orderLinkId; пусто — производный от BYBIT_API_SECRET
    journal_secret: str = os.getenv("JOURNAL_SECRET", "").strip()

    # Многоногая сделка (этап 5, кнопка «Выставить»): каждая нога — лимитный IOC, первая попытка по ask/bid,
    # дальше шагами к mid ± MULTILEG_SLIPPAGE_PCT; сначала покупки, потом продажи (без непокрытых коротких)
    multileg_steps: int = _int_in("MULTILEG_STEPS", 3, 1, 10)
    multileg_slippage_pct: float = _float_in("MULTILEG_SLIPPAGE_PCT", 10, 0, 50)
    multileg_step_delay_s: float = _float_in("MULTILEG_STEP_DELAY_S", 1, 0, 30)
    # первая нога (покупка) сначала PostOnly по mid столько секунд; 0 — выключено (сразу IOC по ask)
    passive_first_leg_seconds: float = _float_in("PASSIVE_FIRST_LEG_SECONDS", 0, 0, 120)

    # Сигналы (этап 3): пороги SIG_*
    sig: SignalSettings = _SIG_DEFAULT  # один экземпляр: SIG_* читаются при импорте, предупреждения — один раз

    # Telegram
    tg_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    # Группы, где бот работает (id групп отрицательные). Из остальных групп бот выходит.
    tg_allowed_chat_ids: set[int] = field(default_factory=lambda: _ids("TELEGRAM_ALLOWED_CHAT_IDS"))
    # Права — по пользователю (from_user.id), в личке и в разрешённых группах
    tg_trader_ids: set[int] = field(default_factory=lambda: _ids("TELEGRAM_TRADER_IDS"))  # торговля + просмотр
    tg_viewer_ids: set[int] = field(default_factory=lambda: _ids("TELEGRAM_VIEWER_IDS"))  # только просмотр
    # администраторы (владелец бота): всё, что трейдеру, и функции «только для администратора» — в личку
    tg_admin_ids: set[int] = field(default_factory=lambda: _ids("TELEGRAM_ADMIN_IDS"))
    # Автосигналы администраторам в личку (в группы не идут); без TELEGRAM_ADMIN_IDS — не работают
    auto_signals_daily_utc: tuple[int, int] | None = _hhmm("AUTO_SIGNALS_DAILY_UTC", "09:05")  # сводка; None — нет
    # сообщение о смене рекомендации по активу — не чаще раза в N часов; 0 — не присылать
    auto_signals_change_hours: float = _float_in("AUTO_SIGNALS_CHANGE_HOURS", 4, 0, 168)
    # Просмотр (не торговля) для всех участников разрешённых групп
    allow_all_group_members_view: bool = _bool("ALLOW_ALL_GROUP_MEMBERS_VIEW", False)
    # Баланс/позиции/ордера/греки — только в личке
    sensitive_commands_dm_only: bool = _bool("SENSITIVE_COMMANDS_DM_ONLY", True)

    def __post_init__(self):
        # клиент идёт в mainnet при любом значении, кроме testnet/demo — опечатка не должна туда вести
        if self.env not in ("testnet", "demo", "mainnet"):
            raise ValueError(f"BYBIT_ENV={self.env!r}: допустимо testnet, demo или mainnet")
        v = self.collector_interval_min
        if not (isinstance(v, int) and 1 <= v <= 60 and 60 % v == 0):
            # необязательный сбор данных не должен блокировать бота и CLI (в т.ч. экстренную отмену ордеров):
            # сборщик выключается, сетка слотов для ручного запуска — 15 минут
            if self.collector_enabled:
                logging.getLogger("optbot.config").error(
                    "COLLECTOR_INTERVAL_MIN=%r: должен делить 60 (1, 5, 10, 15, 30, 60) — сборщик ВЫКЛЮЧЕН",
                    os.getenv("COLLECTOR_INTERVAL_MIN") if v == 0 else v)
            object.__setattr__(self, "collector_enabled", False)
            object.__setattr__(self, "collector_interval_min", 15)

    @property
    def db_file(self) -> str:
        path = self.db_path or os.path.join("data", f"market-{self.env}.db")
        return path if os.path.isabs(path) else os.path.join(ROOT, path)

    @property
    def is_mainnet(self) -> bool:
        return self.env == "mainnet"  # значение проверено в __post_init__


settings = Settings()
