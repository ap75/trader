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


WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _weekly(name: str, default: str) -> tuple[int, int, int] | None:
    """«ДЕНЬ ЧЧ:ММ» UTC (mon … sun) -> (день недели 0–6, ч, мин); off — выключено; неверное — по умолчанию."""
    v = (os.getenv(name) or "").strip().lower()
    if v in ("off", "no", "false", "0", "-"):
        return None

    def parse(x: str):
        d, _, t = x.partition(" ")
        hm = _parse_hhmm(t.strip())
        return (WEEKDAYS.index(d), *hm) if d in WEEKDAYS and hm else None
    got = parse(v or default)
    if got is None:
        logging.getLogger("optbot.config").warning("%s=%r: нужно «ДЕНЬ ЧЧ:ММ» (mon…sun, UTC) или off — использую %s",
                                                   name, v, default)
        got = parse(default)
    return got


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
              "debit_short_delta": 0.25, "debit_long_delta": 0.45, "min_history_days": 14, "lookback_days": 60,
              "long_dte_min": 14, "long_dte_max": 45, "cal_near_dte_min": 5, "cal_near_dte_max": 14}
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
    # издержки (комиссии входа и выхода + спред bid/ask) ≤ N% премии по mid: в карточке — предупреждение,
    # бумажные книги с фильтром («:cost») не открывают; 0 — без фильтра
    max_cost_pct: float = _float_in("SIG_MAX_COST_PCT", 15, 0, 100)
    # Длинный горизонт (эксперимент E1, бумажные книги): окно экспираций, срок IV, RV за 30 дн.
    long_dte_min: float = _float_in("SIG_LONG_DTE_MIN", _SIG_PAIRS["long_dte_min"], 7, 120)
    long_dte_max: float = _float_in("SIG_LONG_DTE_MAX", _SIG_PAIRS["long_dte_max"], 7, 180)
    long_target_days: float = _float_in("SIG_LONG_TARGET_DAYS", 30, 7, 120)   # IV30
    # RV14 / RV30 ≥ k — волатильность растёт: премию не продавать (кредитные стратегии, календарь)
    rv14_rv30_max: float = _float_in("SIG_RV14_RV30_MAX", 1.25, 1, 5)
    # percentile IV30 — только при истории ≥ N дн. (до этого решение по IV30/RV30, с пометкой)
    iv_pct_min_days: float = _float_in("IV_PCT_MIN_DAYS", 30, 1, 365)
    # percentile IV30 BTC/ETH по DVOL Deribit (dvol.py) — в решения; выключен — DVOL докачивается и pctl по нему
    # только справочно (карточка, metrics). Включение меняет версию порогов журнала сигналов (cfg_version)
    dvol_pctl_enabled: bool = _bool("DVOL_PCTL_ENABLED", False)
    # Календарный спред: ATM IV ближней / дальней ≥ k (инверсия) и тренд боковой
    calendar_inversion: float = _float_in("SIG_CALENDAR_INVERSION", 1.10, 1, 3)
    cal_near_dte_min: float = _float_in("SIG_CAL_NEAR_DTE_MIN", _SIG_PAIRS["cal_near_dte_min"], 1, 60)  # ближняя нога
    cal_near_dte_max: float = _float_in("SIG_CAL_NEAR_DTE_MAX", _SIG_PAIRS["cal_near_dte_max"], 1, 60)

    def __post_init__(self):
        # перепутанные пары — обе по умолчанию (сигналы не должны молча работать на несогласованных порогах)
        # (меньшее, большее, во сколько раз большее должно превышать меньшее, как сказать в логе)
        for lo, hi, k, strict, how in (("dte_min", "dte_max", 1, False, "не больше"),
                                       ("ema_fast", "ema_slow", 1, True, "меньше"),
                                       ("pctl_low", "pctl_high", 1, True, "меньше"),
                                       ("debit_short_delta", "debit_long_delta", 1, True, "меньше"),
                                       ("long_dte_min", "long_dte_max", 1, False, "не больше"),
                                       ("cal_near_dte_min", "cal_near_dte_max", 1, False, "не больше"),
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
        if self.iv_pct_min_days > 0.8 * self.lookback_days:  # покрытие часами со срезами — с запасом на пропуски
            fixed = round(0.8 * self.lookback_days, 1)
            logging.getLogger("optbot.config").warning(
                "IV_PCT_MIN_DAYS=%g больше 80%% от SIG_LOOKBACK_DAYS=%g — история не наберётся, использую %g",
                self.iv_pct_min_days, self.lookback_days, fixed)
            object.__setattr__(self, "iv_pct_min_days", fixed)
        if not self.dte_min <= self.target_days <= self.dte_max:  # не сброс: может быть намеренно
            logging.getLogger("optbot.config").warning(
                "SIG_TARGET_DAYS=%g вне окна экспираций SIG_DTE_MIN…SIG_DTE_MAX (%g…%g): IV считается на другой срок, "
                "чем у сделок", self.target_days, self.dte_min, self.dte_max)


_SIG_DEFAULT = SignalSettings()
# поля длинного горизонта и календаря: в версию порогов журнала сигналов (/signals — короткий горизонт) не входят
SIG_LONG_FIELDS = frozenset({"long_dte_min", "long_dte_max", "long_target_days", "rv14_rv30_max", "iv_pct_min_days",
                             "calendar_inversion", "cal_near_dte_min", "cal_near_dte_max", "max_cost_pct"})


def _per_base(name: str, default: float, lo: float, hi: float, overrides: dict[str, float] | None = None
              ) -> dict[str, float]:
    """{актив: значение}: NAME_<АКТИВ> или NAME, иначе default (или overrides[актив])."""
    common = _float_in(name, default, lo, hi)
    out = {}
    for c in _list("BASE_COINS", "BTC,ETH,SOL"):
        dflt = common if (os.getenv(name) or "").strip() else (overrides or {}).get(c, default)
        out[c] = _float_in(f"{name}_{c}", dflt, lo, hi)
    return out


@dataclass(frozen=True)
class PaperSettings:
    """Бумажный движок (эксперимент E1, optbot.paper). Проценты — в процентах."""
    enabled: bool = _bool("PAPER_ENABLED", True)
    bases: tuple[str, ...] = tuple(_list("PAPER_BASES") or _list("BASE_COINS", "BTC,ETH,SOL"))
    risk_usdt: float = _float_in("PAPER_RISK_USDT", 100, 1, 1e6)               # бюджет риска на сделку
    max_open_per_book: int = _int_in("PAPER_MAX_OPEN_PER_BOOK", 5, 1, 100)
    max_total_risk_usdt: float = _float_in("PAPER_MAX_TOTAL_RISK_USDT", 3000, 0, 1e9)  # 0 — без лимита
    slippage_pct: float = _float_in("PAPER_SLIPPAGE_PCT", 1, 0, 50)            # хуже bid/ask на N% цены
    fee_rate: float = _float_in("PAPER_FEE_RATE", 0.0003, 0, 0.01)            # от индекса × кол-во (taker)
    fee_cap_pct: float = _float_in("PAPER_FEE_CAP_PCT", 7, 0, 100)             # но не больше N% премии
    # поставка: min(ставка × индекс, 12.5% внутренней стоимости) × кол-во; вне денег — 0 (Bybit, 2026-11)
    delivery_fee_rate: dict[str, float] = field(default_factory=lambda: _per_base(
        "PAPER_DELIVERY_FEE_RATE", 0.00015, 0, 0.01, {"SOL": 0.0002}))
    delivery_fee_cap_pct: float = _float_in("PAPER_DELIVERY_FEE_CAP_PCT", 12.5, 0, 100)
    take_pct: float = _float_in("PAPER_TAKE_PCT", 50, 1, 100)                  # выход: % макс. прибыли
    stop_pct: float = _float_in("PAPER_STOP_PCT", 50, 1, 100)                  # стоп: убыток ≥ % макс. убытка
    exit_days_short: float = _float_in("PAPER_EXIT_DAYS_SHORT", 0, 0, 30)      # за N дн. до экспирации; 0 — держать
    exit_days_long: float = _float_in("PAPER_EXIT_DAYS_LONG", 7, 0, 60)
    calendar_exit_h: float = _float_in("PAPER_CALENDAR_EXIT_H", 18, 1, 72)     # до ближней экспирации
    calendar_take_pct: float = _float_in("PAPER_CALENDAR_TAKE_PCT", 35, 1, 500)  # прибыль ≥ % дебета
    im_short_pct: float = _float_in("PAPER_IM_SHORT_PCT", 10, 0, 100)          # IM короткой: mark + max(N%·S − OTM,
    im_min_pct: float = _float_in("PAPER_IM_MIN_PCT", 5, 0, 100)               #                     M%·S)
    # условный капитал системы книг (RV, HAR, с фильтром и без — у каждой свой): % в /books и /stress
    capital_usdt: float = _float_in("PAPER_CAPITAL_USDT", 10000, 1, 1e9)
    # /stress, сценарии падения с ростом IV: у путов IV растёт во столько раз сильнее, чем у коллов (перекос)
    stress_put_iv_mult: float = _float_in("STRESS_PUT_IV_MULT", 1.5, 1, 5)


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
    sig: SignalSettings = _SIG_DEFAULT
    paper: PaperSettings = field(default_factory=PaperSettings)
    # резервная копия БД (sqlite backup API): раз в сутки в ЧЧ:ММ UTC (off — нет), куда, сколько дней хранить
    backup_daily_utc: tuple[int, int] | None = _hhmm("BACKUP_DAILY_UTC", "03:20")
    backup_dir: str = os.getenv("BACKUP_DIR", "backups").strip() or "backups"
    backup_keep_days: int = _int_in("BACKUP_KEEP_DAYS", 14, 1, 3650)
    backup_compress: bool = _bool("BACKUP_COMPRESS", True)  # gzip (.db.gz): SQLite сжимается в разы
    # внешний мониторинг (healthchecks.io и т.п.): GET после каждого среза сборщика, сбой среза — <url>/fail
    healthcheck_url: str = os.getenv("HEALTHCHECK_URL", "").strip()
    # полная цепочка опционов в БД (история для проверок задним числом): раз в N мин (первый срез периода;
    # 0 — нет), экспирации до N дней, страйки с |Δ| от N, хранить N дней (0 — всегда)
    chain_snapshot_min: int = _int_in("CHAIN_SNAPSHOT_MIN", 60, 0, 1440)
    chain_max_dte: float = _float_in("CHAIN_MAX_DTE", 60, 1, 400)
    chain_min_delta: float = _float_in("CHAIN_MIN_DELTA", 0.02, 0, 0.5)
    chain_keep_days: int = _int_in("CHAIN_KEEP_DAYS", 0, 0, 36500)
    # индекс страха и жадности (рыночный фон в карточке и журналах; пусто — не запрашивать)
    fng_url: str = os.getenv("FNG_URL", "https://api.alternative.me/fng/?limit=1").strip()
    # безрисковая ставка для книги базиса (векселя США 3 мес., CSV без ключа) — раз в сутки; пусто — не брать
    rf_url: str = os.getenv("RF_URL", "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DTB3").strip()
    # DVOL Deribit (публичный API без ключа) — история для перцентиля IV30 BTC/ETH, ежедневно; пусто — не качать
    dvol_url: str = os.getenv("DVOL_URL", "https://www.deribit.com/api/v2/public/get_volatility_index_data").strip()
    # календарь событий высокой важности (тег event): относительный путь — от корня проекта
    events_path: str = os.getenv("EVENTS_FILE", "events.csv").strip() or "events.csv"  # один экземпляр: SIG_* читаются при импорте, предупреждения — один раз

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
    # еженедельный отчёт книг (/books 7d и /stress) в группы и трейдерам: «ДЕНЬ ЧЧ:ММ» UTC; off — нет
    books_weekly_utc: tuple[int, int, int] | None = _weekly("BOOKS_WEEKLY_UTC", "mon 09:15")
    # сообщение о смене рекомендации по активу — не чаще раза в N часов; 0 — не присылать
    auto_signals_change_hours: float = _float_in("AUTO_SIGNALS_CHANGE_HOURS", 4, 0, 168)
    # срочные администратору (доступ к бирже, лимиты, сделки вне бота): одно и то же — не чаще раза в N минут
    admin_alert_quiet_min: float = _float_in("ADMIN_ALERT_QUIET_MIN", 30, 0, 1440)
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
    def backup_dir_abs(self) -> str:
        return self.backup_dir if os.path.isabs(self.backup_dir) else os.path.join(ROOT, self.backup_dir)

    @property
    def events_file(self) -> str:
        return self.events_path if os.path.isabs(self.events_path) else os.path.join(ROOT, self.events_path)

    @property
    def is_mainnet(self) -> bool:
        return self.env == "mainnet"  # значение проверено в __post_init__


settings = Settings()
