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
