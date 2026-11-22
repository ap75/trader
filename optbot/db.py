"""SQLite (по умолчанию data/market-<BYBIT_ENV>.db, см. Settings.db_file): подключение и миграции схемы.

Пишут бот и при ручном запуске CLI сборщика (WAL, timeout 30 с; миграции — под BEGIN IMMEDIATE).
Соединение не делится между потоками: каждый проход открывает своё.
Время везде — UTC, миллисекунды.
"""
from __future__ import annotations

import os
import sqlite3
import time

# Миграции: список операторов на версию; номер версии = индекс + 1. Только дописывать.
# ВАЖНО: каждый элемент — ОДИН оператор (без «;» внутри) — выполняются в одной транзакции.
MIGRATIONS: list[list[str]] = [
    # 1: сборщик рыночных данных
    [
        """
        CREATE TABLE snapshots (
            ts             INTEGER NOT NULL,   -- начало слота (COLLECTOR_INTERVAL_MIN)
            base           TEXT    NOT NULL,
            expiry         TEXT    NOT NULL,   -- 27NOV26
            expiry_ms      INTEGER NOT NULL,
            dte            REAL    NOT NULL,   -- дней до экспирации
            underlying     REAL,               -- форвард этой экспирации (underlyingPrice)
            index_price    REAL,
            atm_strike     REAL,
            atm_iv         REAL,               -- среднее markIv колла и пута ATM
            straddle_mark  REAL,
            straddle_mid   REAL,               -- по bid/ask, NULL без полной котировки
            rr25           REAL,               -- iv_c25 - iv_p25
            iv_c25         REAL,
            iv_p25         REAL,
            atm_spread_pct REAL,               -- средний спред ATM-ног, % от mid
            atm_oi         REAL,
            n_quotes       INTEGER,            -- опционов с котировкой в экспирации
            captured_ms    INTEGER,            -- фактическое время снятия (ts — начало слота)
            PRIMARY KEY (ts, base, expiry)
        )
        """,
        """
        CREATE INDEX snapshots_base_ts ON snapshots (base, ts)
        """,
        """
        CREATE TABLE spot (
            ts          INTEGER NOT NULL,
            base        TEXT    NOT NULL,
            index_price REAL,
            perp_price  REAL,                  -- markPrice перпетуала BASEUSDT
            captured_ms INTEGER,
            PRIMARY KEY (ts, base)
        )
        """,
        """
        CREATE TABLE tracked_symbols (         -- ноги сигналов, за которыми следит сборщик (этап 4)
            symbol   TEXT PRIMARY KEY,
            added_ms INTEGER NOT NULL,
            until_ms INTEGER NOT NULL,         -- после экспирации слежение прекращается
            reason   TEXT
        )
        """,
        """
        CREATE TABLE leg_marks (
            ts         INTEGER NOT NULL,
            symbol     TEXT    NOT NULL,
            mark       REAL,
            bid        REAL,
            ask        REAL,
            iv         REAL,
            underlying REAL,
            captured_ms INTEGER,
            PRIMARY KEY (ts, symbol)
        )
        """,
    ],
    # 2: журнал сделок (этап 2)
    [
        """
        CREATE TABLE order_links (             -- кто и откуда выставил ордер (пишется ДО отправки на биржу)
            link_id    TEXT PRIMARY KEY,       -- orderLinkId
            source     TEXT NOT NULL,          -- manual | signal | cli
            signal_id  INTEGER,
            leg        INTEGER,
            user_id    INTEGER,
            user_name  TEXT,
            user_hash  TEXT,                   -- HMAC(секрет, user_id)[:6] — то, что видит биржа
            chat_id    INTEGER,
            created_ms INTEGER NOT NULL
        )
        """,
        """
        CREATE TABLE orders (                  -- последнее известное состояние ордера (WS order + сверка)
            order_id    TEXT PRIMARY KEY,
            link_id     TEXT,
            symbol      TEXT NOT NULL,
            side        TEXT,
            qty         REAL,
            price       REAL,
            iv          REAL,
            type        TEXT,
            reduce_only INTEGER,
            status      TEXT,
            cum_qty     REAL,
            avg_price   REAL,
            created_ms  INTEGER,
            updated_ms  INTEGER,
            raw         TEXT
        )
        """,
        """
        CREATE INDEX orders_link ON orders (link_id)
        """,
        """
        CREATE TABLE executions (
            exec_id     TEXT PRIMARY KEY,
            order_id    TEXT,
            link_id     TEXT,
            symbol      TEXT NOT NULL,
            side        TEXT,
            qty         REAL,
            price       REAL,
            value       REAL,
            fee         REAL,                  -- > 0 — уплачено, < 0 — получено (ребейт)
            exec_type   TEXT,                  -- Trade, Delivery, ...
            exec_time   INTEGER NOT NULL,
            is_maker    INTEGER,
            mark_price  REAL,
            index_price REAL,
            trade_iv    REAL,
            source      TEXT,                  -- ws | sync — кто записал первым
            notified    INTEGER NOT NULL DEFAULT 1,  -- 0 — записано сверкой, уведомления ещё не было
            raw         TEXT
        )
        """,
        """
        CREATE INDEX executions_time ON executions (exec_time)
        """,
        """
        CREATE TABLE deliveries (              -- asset/delivery-record: поставки (экспирации позиций)
            symbol         TEXT    NOT NULL,
            delivery_time  INTEGER NOT NULL,
            side           TEXT,
            position       REAL,
            strike         REAL,
            entry_price    REAL,
            delivery_price REAL,
            fee            REAL,
            rpl            REAL,               -- deliveryRpl по данным биржи
            raw            TEXT,
            PRIMARY KEY (symbol, delivery_time)
        )
        """,
        """
        CREATE TABLE delivery_prices (         -- market/new-delivery-price: цена поставки по экспирациям
            base          TEXT    NOT NULL,
            delivery_time INTEGER NOT NULL,
            price         REAL    NOT NULL,
            PRIMARY KEY (base, delivery_time)
        )
        """,
        """
        CREATE TABLE sync_state (              -- до какого момента выгружено по REST
            stream  TEXT PRIMARY KEY,          -- executions | orders | deliveries
            last_ms INTEGER NOT NULL
        )
        """,
    ],
    # 3: журнал сигналов (этап 4)
    [
        """
        CREATE TABLE signals (                 -- выданные /signals рекомендации, та же сделка (ноги) — одна строка
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            ts             INTEGER NOT NULL,   -- первая выдача (цены входа — на этот момент)
            last_shown_ms  INTEGER NOT NULL,
            shown          INTEGER NOT NULL DEFAULT 1,
            base           TEXT    NOT NULL,
            strategy       TEXT    NOT NULL,
            regime         TEXT,               -- high | low — режим IV
            trend          TEXT,               -- up | down | flat
            expiry         TEXT    NOT NULL,
            expiry_ms      INTEGER NOT NULL,
            legs_key       TEXT    NOT NULL,   -- символы и стороны ног: признак «та же сделка»
            legs           TEXT    NOT NULL,   -- JSON: symbol, kind, strike, side, qty, bid, ask, mark, price, mid, delta, iv
            qty            REAL    NOT NULL,
            net            REAL,               -- премия на позицию по исполнимым ценам: > 0 кредит, < 0 дебет
            net_mid        REAL,
            max_profit     REAL,               -- NULL — без ограничения
            max_loss       REAL,
            breakevens     TEXT,               -- JSON
            forward        REAL,
            em             REAL,               -- ожидаемое движение до экспирации
            metrics        TEXT,               -- JSON: iv7, rv7, rv14, pctl, history_days, enough_history, trend
            reasons        TEXT,
            cfg_hash       TEXT,               -- версия порогов SIG_*
            cfg            TEXT,               -- сами пороги (JSON)
            source         TEXT,               -- bot | cli
            user_id        INTEGER,
            user_name      TEXT,
            chat_id        INTEGER,
            settled_ms     INTEGER,            -- когда посчитан итог (NULL — ждёт экспирации)
            delivery_price REAL,
            pnl            REAL,               -- итог на экспирации, USDT на позицию, без комиссий
            pnl_mid        REAL,               -- то же при входе по mid
            r_mult         REAL,               -- pnl / max_loss
            max_dd         REAL,               -- худший PnL до экспирации при закрытии по исполнимой стороне (≤ 0)
            max_dd_mark    REAL,               -- то же по mark
            half_ms        INTEGER,            -- когда впервые достигнута половина макс. прибыли (исполнимо)
            marks_n        INTEGER,             -- срезов котировок ног в расчёте пути
            announced_ms   INTEGER             -- когда сводка итога ушла в группы (NULL — ещё нет)
        )
        """,
        """
        CREATE INDEX signals_ts ON signals (ts)
        """,
        """
        CREATE INDEX signals_open ON signals (settled_ms, legs_key)
        """,
        """
        CREATE INDEX signals_unannounced ON signals (id) WHERE settled_ms IS NOT NULL AND announced_ms IS NULL
        """,
        """
        CREATE TABLE signal_views (           -- кто видел сигнал: показ тому же человеку чаще VIEW_TTL — не новый
            signal_id INTEGER NOT NULL,
            viewer    TEXT    NOT NULL,       -- user id или источник (cli)
            ts        INTEGER NOT NULL,       -- последний засчитанный показ
            PRIMARY KEY (signal_id, viewer)
        )
        """,
    ],
    # 4: исполнение сигналов кнопкой «Выставить» (этап 5) — расчётная и фактическая цена по ногам
    [
        """
        CREATE TABLE signal_executions (
            id          INTEGER PRIMARY KEY,
            signal_id   INTEGER NOT NULL,
            ts          INTEGER NOT NULL,
            user_id     INTEGER,
            user_name   TEXT,
            chat_id     INTEGER,
            status      TEXT NOT NULL,     -- complete | partial | none | unwound | stuck | unknown
            planned_qty REAL,
            qty         REAL,              -- исполнено стратегией
            planned_net REAL,              -- премия по ценам сигнала на исполненное кол-во (+ кредит)
            actual_net  REAL,              -- фактическая по средним ценам ног
            cash        REAL,              -- всё движение премии, с закрытием лишнего (cash − actual_net — откат)
            legs        TEXT,              -- JSON: нога, сторона, нужно, исполнено, средняя, попыток, ошибка
            unwind      TEXT,              -- JSON: закрытие лишнего
            note        TEXT
        )
        """,
        """
        CREATE INDEX signal_executions_signal ON signal_executions (signal_id)
        """,
    ],
    # 5: состояние бота между запусками (отметка «работает» — нештатная остановка видна при следующем старте)
    [
        """
        CREATE TABLE bot_state (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
        """,
    ],
    # 6: бумажные книги эксперимента E1 (optbot/paper.py)
    [
        """
        CREATE TABLE books (                   -- книга = горизонт × стратегия × актив
            id         TEXT PRIMARY KEY,       -- short:iron_condor:BTC
            horizon    TEXT NOT NULL,          -- short (2–7 дн.) | long (14–45 дн.)
            strategy   TEXT NOT NULL,
            base       TEXT NOT NULL,
            created_ms INTEGER NOT NULL
        )
        """,
        """
        CREATE TABLE paper_trades (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            book          TEXT    NOT NULL,
            horizon       TEXT    NOT NULL,
            strategy      TEXT    NOT NULL,
            base          TEXT    NOT NULL,
            opened_ms     INTEGER NOT NULL,
            expiry        TEXT    NOT NULL,    -- у календаря — ближняя
            expiry_ms     INTEGER NOT NULL,
            far_expiry    TEXT,                -- календарь: дальняя
            far_expiry_ms INTEGER,
            legs          TEXT    NOT NULL,    -- JSON: symbol, kind, strike, side, expiry_ms, bid, ask, mark, fill
            qty           REAL    NOT NULL,
            qty_budget    REAL,                -- кол-во по бюджету риска (до лимитов и шага лота)
            qty_cap       TEXT,                -- урезано лимитом: MAX_QTY_<A> / MAX_POSITION_<A>
            entry_net     REAL,                -- премия по ценам входа на позицию: > 0 кредит
            entry_fees    REAL,
            max_profit    REAL,                -- на позицию с комиссией входа, NULL — без потолка / нет оценки
            max_loss      REAL    NOT NULL,    -- риск сделки (R) с комиссией входа
            im_regular    REAL,                -- IM обычного режима на входе (расчёт)
            im_pm         REAL,                -- IM Portfolio Margin — для бумажных книг не известна (NULL)
            forward       REAL,
            em            REAL,                -- ожидаемое движение до (ближней) экспирации
            tags          TEXT,                -- event — событие высокой важности до (ближней) экспирации
            events        TEXT,                -- JSON: [{ts, name}]
            metrics       TEXT,                -- JSON: IV/RV, pctl (используется ли), тренд, режим, причины
            cfg           TEXT,                -- PAPER_* на входе (JSON)
            status        TEXT    NOT NULL DEFAULT 'open',  -- open | closed | void
            closed_ms     INTEGER,
            close_reason  TEXT,                -- take | stop | time | cal_time | cal_take | cal_move | expiry | void
            exit_net      REAL,                -- премия закрытия (+ получено) или выплата на поставке
            exit_fees     REAL,
            delivery_price REAL,
            pnl           REAL,                -- с издержками входа и выхода
            r_mult        REAL,                -- pnl / max_loss
            mark_ms       INTEGER,             -- последняя отметка по рынку
            mark_pnl      REAL                 -- PnL по mark на последней отметке
        )
        """,
        """
        CREATE INDEX paper_trades_book ON paper_trades (book, status)
        """,
        """
        CREATE TABLE book_equity (             -- кривая книги на каждый срез (просадка, Шарп, Сортино — E3)
            ts         INTEGER NOT NULL,
            book       TEXT    NOT NULL,
            realized   REAL    NOT NULL,       -- сумма PnL закрытых
            unrealized REAL    NOT NULL,       -- открытые по mark, с комиссией входа
            open_n     INTEGER NOT NULL,
            open_risk  REAL    NOT NULL,
            im_regular REAL,
            im_pm      REAL,
            PRIMARY KEY (ts, book)
        )
        """,
        """
        CREATE TABLE paper_skips (             -- сигнал был, сделки нет: одна причина на книгу и экспирацию
            book   TEXT    NOT NULL,
            expiry TEXT    NOT NULL,
            code   TEXT    NOT NULL,           -- cost | max_open | exit_window | data | qty | total_risk | traded (миграция 9)
            ts     INTEGER NOT NULL,
            reason TEXT,
            PRIMARY KEY (book, expiry, code)
        )
        """,
    ],
    # 7: книги с фильтром издержек (SIG_MAX_COST_PCT) рядом с книгами без него, издержки сделки
    [
        """
        ALTER TABLE books ADD COLUMN cost_filter INTEGER NOT NULL DEFAULT 0
        """,
        """
        ALTER TABLE paper_trades ADD COLUMN cost_pct REAL
        """,
    ],
    # 8: история для проверок задним числом — полная цепочка опционов (у Bybit истории цепочки нет), фондирование
    # и открытый интерес перпетуала по срезам, индекс страха и жадности по дням
    [
        """
        CREATE TABLE chain_snapshots (         -- раз в CHAIN_SNAPSHOT_MIN: все страйки экспираций до CHAIN_MAX_DTE
            ts         INTEGER NOT NULL,       -- начало слота сборщика
            symbol     TEXT    NOT NULL,
            base       TEXT    NOT NULL,
            expiry_ms  INTEGER NOT NULL,
            strike     REAL    NOT NULL,
            kind       TEXT    NOT NULL,       -- C | P
            bid        REAL,
            ask        REAL,
            bid_size   REAL,
            ask_size   REAL,
            mark       REAL,
            iv         REAL,                   -- markIv
            bid_iv     REAL,
            ask_iv     REAL,
            delta      REAL,
            oi         REAL,
            underlying REAL,
            PRIMARY KEY (ts, symbol)
        )
        """,
        "CREATE INDEX chain_snapshots_base_ts ON chain_snapshots (base, ts)",
        """
        CREATE TABLE perp_context (            -- каждый срез: фондирование и открытый интерес BASEUSDT
            ts          INTEGER NOT NULL,
            base        TEXT    NOT NULL,
            funding     REAL,                  -- текущая ставка за период (доля)
            funding_h   REAL,                  -- период фондирования, ч
            oi          REAL,                  -- открытый интерес, контрактов (= базового актива)
            oi_value    REAL,                  -- USDT
            captured_ms INTEGER,
            PRIMARY KEY (ts, base)
        )
        """,
        """
        CREATE TABLE fear_greed (              -- индекс страха и жадности alternative.me, значение дня (UTC)
            ts    INTEGER PRIMARY KEY,         -- начало дня у источника, мс
            value INTEGER NOT NULL,
            label TEXT
        )
        """,
    ],
    # 9: итог расчёта сигнала на каждом шаге книг — почему книги стоят без сделок (paper --skips)
    [
        "ALTER TABLE paper_skips ADD COLUMN last_ts INTEGER",  # последний повтор отказа (ts — первый)
        """
        CREATE TABLE paper_signals (
            ts       INTEGER NOT NULL,         -- шаг книг
            base     TEXT    NOT NULL,
            kind     TEXT    NOT NULL,         -- short | long | calendar
            outcome  TEXT    NOT NULL,         -- signal | not_built | none | error
            strategy TEXT,
            regime   TEXT,                     -- high | low | normal | NULL
            trend    TEXT,
            reason   TEXT,                     -- нет сигнала / не собрался — почему
            PRIMARY KEY (ts, base, kind)
        )
        """,
    ],
    # 10: прогноз RV7 по HAR (vol.py): история часовых свечей перпетуала и ежемесячные оценки коэффициентов
    [
        """
        CREATE TABLE perp_h1 (
            base  TEXT    NOT NULL,
            ts    INTEGER NOT NULL,            -- начало часовой свечи BASEUSDT (linear), мс
            close REAL    NOT NULL,
            PRIMARY KEY (base, ts)
        ) WITHOUT ROWID
        """,
        """
        CREATE TABLE har_models (
            base        TEXT    NOT NULL,
            horizon     INTEGER NOT NULL,      -- дней (7)
            fitted_ms   INTEGER NOT NULL,      -- когда оценена (действует с этого момента)
            b0 REAL NOT NULL, b1 REAL NOT NULL, b2 REAL NOT NULL, b3 REAL NOT NULL,
            s2          REAL    NOT NULL,      -- дисперсия остатков обучения
            n           INTEGER NOT NULL,      -- моментов обучения
            data_end_ms INTEGER,               -- последняя свеча обучения
            PRIMARY KEY (base, horizon, fitted_ms)
        )
        """,
    ],
    # 11: бумажная книга перпетуалов EMA 4h (perpbook.py)
    [
        """
        CREATE TABLE perp_books (
            id              TEXT PRIMARY KEY,  -- perp:<стратегия>:<актив>
            base            TEXT    NOT NULL,
            strategy        TEXT    NOT NULL,
            capital         REAL    NOT NULL,  -- условный начальный капитал
            cash            REAL    NOT NULL,  -- капитал с реализованным итогом (без переоценки позиции)
            qty             REAL    NOT NULL,  -- подписанное количество (лонг > 0)
            entry           REAL    NOT NULL,  -- цена входа (с проскальзыванием)
            last_bar_ms     INTEGER NOT NULL,  -- open последней пройденной свечи
            last_funding_ms INTEGER NOT NULL,  -- метка последнего учтённого фондирования
            created_ms      INTEGER NOT NULL,
            params          TEXT               -- JSON параметров (config/perp_ema.json)
        )
        """,
        """
        CREATE TABLE perp_trades (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            book      TEXT    NOT NULL,
            base      TEXT    NOT NULL,
            side      INTEGER NOT NULL,        -- 1 лонг, −1 шорт
            status    TEXT    NOT NULL,        -- open | closed
            opened_ms INTEGER NOT NULL,
            entry_px  REAL    NOT NULL,
            qty       REAL    NOT NULL,
            risk      REAL    NOT NULL,        -- qty × 3·ATR при входе (для R)
            fees      REAL    NOT NULL,
            funding   REAL    NOT NULL,        -- получено (+) / уплачено (−)
            pnl       REAL    NOT NULL,        -- цена − комиссии + фондирование (у открытой — без переоценки)
            closed_ms INTEGER,
            exit_px   REAL,
            r_mult    REAL,
            reason    TEXT
        )
        """,
        "CREATE INDEX perp_trades_book ON perp_trades (book, status)",
        """
        CREATE TABLE perp_equity (
            ts     INTEGER NOT NULL,
            book   TEXT    NOT NULL,
            equity REAL    NOT NULL,           -- cash + переоценка позиции по последней цене
            cash   REAL    NOT NULL,
            qty    REAL    NOT NULL,
            price  REAL    NOT NULL,
            PRIMARY KEY (ts, book)
        )
        """,
    ],
    # 12: бумажная книга базиса: спот в лонг + перпетуал в шорт (basisbook.py)
    [
        """
        CREATE TABLE basis_books (
            id              TEXT PRIMARY KEY,  -- basis:<стратегия>:<актив>
            base            TEXT    NOT NULL,
            capital         REAL    NOT NULL,  -- условный начальный капитал
            cash            REAL    NOT NULL,  -- капитал с реализованным итогом (без переоценки ног)
            qty             REAL    NOT NULL,  -- количество каждой ноги (спот лонг = перп шорт)
            s_in            REAL    NOT NULL,  -- цена спота, от которой считается переоценка
            p_in            REAL    NOT NULL,  -- цена перпа, от которой считается переоценка
            last_bar_ms     INTEGER NOT NULL,  -- open последней пройденной дневной свечи
            last_funding_ms INTEGER NOT NULL,
            fees            REAL    NOT NULL DEFAULT 0,   -- всего комиссий (с начала книги)
            funding         REAL    NOT NULL DEFAULT 0,   -- всего фондирования (получено +)
            rebalances      INTEGER NOT NULL DEFAULT 0,
            skipped         INTEGER NOT NULL DEFAULT 0,   -- перестроек отложено защитой (перп и спот разошлись)
            owed_ms         INTEGER,           -- open, исполненный без фондирования с этой меткой (не пришло вовремя):
            owed_qty        REAL,              -- его заплатит количество до этого open
            owed_px         REAL,              -- по open перпа этой свечи
            created_ms      INTEGER NOT NULL,
            params          TEXT
        )
        """,
        """
        CREATE TABLE basis_events (            -- вход, перестройка, отложенная перестройка, фондирование
            id     INTEGER PRIMARY KEY AUTOINCREMENT,
            book   TEXT    NOT NULL,
            ts     INTEGER NOT NULL,
            kind   TEXT    NOT NULL,           -- enter | rebalance | skip | funding
            qty    REAL,                       -- количество после события
            spot   REAL,
            perp   REAL,
            amount REAL                        -- фондирование (+ получено) или издержки (−)
        )
        """,
        "CREATE INDEX basis_events_book ON basis_events (book, ts)",
        """
        CREATE TABLE basis_equity (
            ts     INTEGER NOT NULL,
            book   TEXT    NOT NULL,
            equity REAL    NOT NULL,           -- cash + переоценка обеих ног по последним ценам
            cash   REAL    NOT NULL,
            qty    REAL    NOT NULL,
            spot   REAL    NOT NULL,
            perp   REAL    NOT NULL,
            PRIMARY KEY (ts, book)
        )
        """,
    ],
]

_migrated: set[str] = set()  # файлы, схема которых уже проверена в этом процессе


def connect(path: str, timeout: float = 30) -> sqlite3.Connection:
    """Открывает БД (создаёт файл и папку при необходимости); схема проверяется один раз на файл за процесс."""
    key = os.path.abspath(path) if path != ":memory:" else None
    if key:
        os.makedirs(os.path.dirname(key), exist_ok=True)
    if key and not os.path.exists(key):
        _migrated.discard(key)  # файл удалили на ходу — схему создать заново
    conn = sqlite3.connect(path, timeout=timeout)
    try:
        conn.row_factory = sqlite3.Row
        if key not in _migrated:
            _set_wal(conn, timeout)  # хранится в файле — достаточно один раз; timeout — как у connect
            migrate(conn)
            if key:
                _migrated.add(key)
        conn.execute("PRAGMA synchronous=NORMAL")
    except BaseException:
        conn.close()  # иначе каждая неудачная попытка оставляет открытый файл
        raise
    return conn


def _set_wal(conn: sqlite3.Connection, timeout: float = 30) -> None:
    """Переключение в WAL не ждёт занятую БД (busy timeout на него не действует) — повторяем сами:
    иначе два процесса, впервые открывающие новый файл, получают «database is locked»."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) or time.monotonic() > deadline:
                raise
            time.sleep(0.05)


def migrate(conn: sqlite3.Connection) -> int:
    """Применяет недостающие миграции; возвращает итоговую версию схемы.

    BEGIN IMMEDIATE берёт блокировку записи ДО чтения версии: второй процесс, открывающий новую БД
    одновременно, дождётся и увидит уже применённую схему, а не будет создавать таблицы повторно.
    """
    conn.isolation_level = None  # транзакциями управляем сами
    try:
        conn.execute("BEGIN IMMEDIATE")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > len(MIGRATIONS):
            raise RuntimeError(f"Схема БД v{version} новее кода (v{len(MIGRATIONS)}) — обновите код")
        for i in range(version, len(MIGRATIONS)):
            for stmt in MIGRATIONS[i]:
                conn.execute(stmt)
            conn.execute(f"PRAGMA user_version={i + 1}")
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:  # BEGIN мог не выполниться (БД занята) — тогда откатывать нечего
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.isolation_level = ""  # обратно к обычному режиму модуля sqlite3
    return len(MIGRATIONS)
