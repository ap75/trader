"""Журнал сделок (этап 2): ордера, исполнения, поставки в SQLite.

Онлайн — из приватного WebSocket (топики order и execution), плюс сверка по REST при старте и каждый час
(execution/list, order/history, asset/delivery-record) с перекрытием и дедупликацией по execId / orderId.
Кто выставил ордер — по orderLinkId (см. linkid.py) и таблице order_links, которая пишется до отправки ордера.

Реализованный PnL — по средней цене входа на каждый символ, в хронологии исполнений и поставок.
Поставка закрывает позицию по внутренней стоимости опциона при цене поставки.

Ручной запуск:
    python -m optbot.journal --sync            сверка с биржей
    python -m optbot.journal --report 7d       отчёт (сегодня | вчера | Nd | ДАТА | ДАТА..ДАТА)
    python -m optbot.journal --report 30d --csv out.csv
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import io
import json
import logging
import re
import sqlite3
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Iterator

from . import db
from .bybit_client import ACTIVE_STATUSES, parse_option_symbol, option_intrinsic
from .config import Settings, settings
from .fmt import DAY_MS, HOUR_MS, csv_safe, short_symbol, utc
from .linkid import new_link_id, parse_link_id, user_hash

if TYPE_CHECKING:
    from .bybit_client import OptionsClient

log = logging.getLogger("optbot.journal")

OVERLAP_MS = HOUR_MS              # сверка начинается на час раньше выгруженного — запоздавшие записи биржи
MAX_DEPTH_MS = 2 * 365 * DAY_MS   # глубже биржа не отдаёт
# окна запросов (минута запаса к лимиту биржи)
WINDOWS = {"executions": 7 * DAY_MS - 60_000, "orders": 7 * DAY_MS - 60_000, "deliveries": 30 * DAY_MS - 60_000}
EPS = 1e-9
DELIVERY_TYPES = {"Delivery"}     # execType поставки; остальные (Trade, BustTrade, ...) — сделки
GRACE_MS = 120_000                # исполнение моложе — ещё может прийти по WebSocket, сводкой не объявляем
MAX_REFRESH = 100                 # активных ордеров, дочитываемых по id за одну сверку
MAX_TG = 3800                     # запас до лимита Telegram 4096


def _num(x) -> float | None:
    """Число или None; 0 остаётся 0 (нулевая комиссия — значение, а не «нет данных»)."""
    try:
        return float(x) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _i(x) -> int | None:
    try:
        return int(x) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _bool(x) -> int:
    return int(x is True or str(x).lower() == "true")


def intrinsic(symbol: str, delivery_price: float, strike: float | None = None) -> float | None:
    """Внутренняя стоимость опциона при поставке (за 1 контракт)."""
    parsed = parse_option_symbol(symbol)
    if parsed is None:
        return None
    return option_intrinsic(parsed[3], strike if strike is not None else float(parsed[2]), delivery_price)


# ---------- период ----------
def parse_period(args: list[str], now_ms: int | None = None) -> tuple[int, int, str]:
    """[сегодня|вчера|Nd|ГГГГ-ММ-ДД|ГГГГ-ММ-ДД..ГГГГ-ММ-ДД] -> (start_ms, end_ms, подпись). По умолчанию 7d. UTC."""
    now = datetime.fromtimestamp((now_ms if now_ms is not None else time.time() * 1000) / 1000, timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    ms = lambda d: int(d.timestamp() * 1000)  # noqa: E731
    a = (args[0] if args else "7d").strip().lower()

    def day(s: str) -> datetime:
        try:
            return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            raise ValueError(f"Период {s!r}: сегодня | вчера | 7d | 2026-11-01 | 2026-11-01..2026-11-15") from None

    if a in ("сегодня", "today"):
        return ms(today), ms(now), "сегодня"
    if a in ("вчера", "yesterday"):
        return ms(today - timedelta(days=1)), ms(today), "вчера"
    if m := re.fullmatch(r"(\d{1,3})d", a):
        n = int(m[1])
        if not 1 <= n <= 730:
            raise ValueError("Период: от 1d до 730d")
        return ms(now - timedelta(days=n)), ms(now), f"{n} дн."
    if ".." in a:
        s, e = a.split("..", 1)
        d0, d1 = day(s), day(e)
        if d1 < d0:
            raise ValueError("Конец периода раньше начала")
        return ms(d0), ms(d1 + timedelta(days=1)), f"{s}..{e}"
    d = day(a)
    return ms(d), ms(d + timedelta(days=1)), a


# ---------- отчёт ----------
@dataclass
class SymbolStats:
    trades: int = 0
    bought: float = 0.0
    sold: float = 0.0
    realized: float = 0.0
    fees: float = 0.0
    delivered: bool = False


@dataclass
class Report:
    start_ms: int
    end_ms: int
    label: str
    trades: int = 0
    buy_value: float = 0.0      # уплачено премии
    sell_value: float = 0.0     # получено премии
    fees: float = 0.0           # комиссии сделок и поставок (> 0 — уплачено)
    deliveries: int = 0
    realized: float = 0.0       # до комиссий
    symbols: dict[str, SymbolStats] = field(default_factory=dict)
    open_positions: dict[str, tuple[float, float]] = field(default_factory=dict)  # symbol -> (qty, avg)
    unknown_deliveries: list[str] = field(default_factory=list)  # нет цены поставки — не учтены в PnL
    unknown_entries: list[str] = field(default_factory=list)  # вход раньше журнала — PnL поставки не учтён

    @property
    def net(self) -> float:
        return self.realized - self.fees


def _apply(state: list[float], signed_qty: float, price: float) -> float:
    """Средняя цена входа: изменяет state=[qty, avg], возвращает реализованный PnL."""
    q, avg = state
    if abs(q) < EPS or (q > 0) == (signed_qty > 0):
        n = abs(q) + abs(signed_qty)
        state[:] = [q + signed_qty, (avg * abs(q) + price * abs(signed_qty)) / n if n else 0.0]
        return 0.0
    closing = min(abs(signed_qty), abs(q))
    pnl = (price - avg) * closing * (1 if q > 0 else -1)
    rest = abs(signed_qty) - closing
    if rest > EPS:  # переворот позиции: остаток открывает новую по цене сделки
        state[:] = [rest * (1 if signed_qty > 0 else -1), price]
    else:
        q += signed_qty
        state[:] = [0.0, 0.0] if abs(q) < EPS else [q, avg]
    return pnl


# ---------- журнал ----------
@dataclass
class SyncResult:
    new_executions: list[dict] = field(default_factory=list)
    missed: list[dict] = field(default_factory=list)  # найдены только сверкой — для сводного уведомления
    orders: int = 0
    new_deliveries: list[dict] = field(default_factory=list)
    prices: int = 0
    first_run: bool = False     # первая выгрузка (история) — о «пропущенных» исполнениях не уведомлять
    errors: list[str] = field(default_factory=list)
    seconds: float = 0.0

    def __str__(self) -> str:
        err = f", ошибки: {'; '.join(self.errors)}" if self.errors else ""
        return (f"новых исполнений {len(self.new_executions)}, ордеров {self.orders}, "
                f"поставок {len(self.new_deliveries)}, цен поставки {self.prices} за {self.seconds:.1f} с{err}")


def load_delivery_prices(conn, base: str | None = None, lo_ms: int | None = None,
                         hi_ms: int | None = None) -> dict[str, list[tuple[int, float]]]:
    """delivery_prices -> {base: [(delivery_time, price)]}; base и окно [lo_ms, hi_ms] — сузить выборку."""
    q, a = "SELECT base, delivery_time, price FROM delivery_prices WHERE 1=1", []
    if base:
        q += " AND base = ?"
        a.append(base.upper())
    if lo_ms is not None:
        q += " AND delivery_time >= ?"
        a.append(lo_ms)
    if hi_ms is not None:
        q += " AND delivery_time <= ?"
        a.append(hi_ms)
    out: dict[str, list[tuple[int, float]]] = {}
    for r in conn.execute(q, a):
        out.setdefault(r["base"], []).append((r["delivery_time"], r["price"]))
    return out


def nearest_price(prices: list[tuple[int, float]], t: int) -> float | None:
    """Цена поставки ближайшая к t в пределах ±1 ч (одно правило для журнала сделок и журнала сигналов)."""
    near = [(abs(dt - t), px) for dt, px in prices if abs(dt - t) <= HOUR_MS]
    return min(near)[1] if near else None


class Journal:
    def __init__(self, cfg: Settings = settings):
        self.cfg = cfg
        self.enabled = cfg.journal_enabled
        # секрет для хеша пользователя в orderLinkId: JOURNAL_SECRET или производный от API-секрета
        self.secret = (cfg.journal_secret.encode() if cfg.journal_secret else
                       hmac.new(cfg.api_secret.encode(), b"optbot-link-id", hashlib.sha256).digest())

    @contextmanager
    def _db(self, timeout: float = 30) -> Iterator[sqlite3.Connection]:
        conn = db.connect(self.cfg.db_file, timeout=timeout)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # ---------- кто выставил ----------
    def tag(self, user_id: int | None) -> str:
        return user_hash(self.secret, user_id)

    def register_order(self, user_id: int | None, user_name: str = "", chat_id: int | None = None,
                       source: str = "manual") -> str:
        """Новый orderLinkId и запись «кто выставил» — ДО отправки ордера. Сбой записи не мешает ордеру:
        хеш пользователя всё равно есть в самом orderLinkId."""
        tag = self.tag(user_id)
        link = new_link_id(tag)
        if self.enabled:
            try:
                # короткий таймаут: занятая БД (сверка, CLI) не должна задерживать подтверждённый ордер
                with self._db(timeout=2) as c:
                    c.execute("INSERT INTO order_links (link_id, source, user_id, user_name, user_hash, chat_id, "
                              "created_ms) VALUES (?,?,?,?,?,?,?)",
                              (link, source, user_id, user_name, tag, chat_id, int(time.time() * 1000)))
            except Exception:
                log.exception("Журнал: не удалось записать ордер %s (ордер всё равно отправляется)", link)
        return link

    def describe_many(self, link_ids: list[str | None]) -> dict[str | None, str]:
        """describe() для нескольких ордеров одним запросом к БД (уведомления о пачке исполнений, сводка сверки)."""
        ids = sorted({x for x in link_ids if parse_link_id(x)["source"] in ("manual", "signal")})
        names: dict[str, str] = {}
        if self.enabled and ids:
            try:
                with self._db() as c:
                    for r in c.execute(f"SELECT link_id, user_name, source FROM order_links WHERE link_id IN "
                                       f"({','.join('?' * len(ids))})", ids):
                        names[r["link_id"]] = r["user_name"] or ("CLI" if r["source"] == "cli" else "")
            except Exception:
                log.warning("Журнал: источники ордеров не прочитаны", exc_info=True)
        return {x: self._describe(x, names.get(x, "")) for x in set(link_ids)}

    @staticmethod
    def _describe(link_id: str | None, name: str) -> str:
        p = parse_link_id(link_id)
        who = f" · {name}" if name else ""
        if p["source"] == "signal":
            return f"по сигналу #{p['signal_id']}{who}"
        if p["source"] in ("manual", "legacy"):
            return f"через бота{who}"
        return "вне бота"

    def describe(self, link_id: str | None) -> str:
        """Источник для уведомлений: «через бота · Имя», «по сигналу #N · Имя», «вне бота»."""
        return self.describe_many([link_id])[link_id]

    def forget_order(self, link_id: str) -> None:
        """Ордер точно не дошёл до биржи (отказ лимитов или биржи) — убрать запись «кто выставил»."""
        if not self.enabled:
            return
        try:
            with self._db(timeout=2) as c:
                c.execute("DELETE FROM order_links WHERE link_id = ?", (link_id,))
        except Exception:
            log.warning("Журнал: запись %s не удалена", link_id, exc_info=True)

    # ---------- запись ----------
    def record_orders(self, rows: list[dict]) -> int:
        """Состояние ордеров (WS order, order/history); более старое состояние не затирает новое."""
        if not self.enabled or not rows:
            return 0
        n = 0
        with self._db() as c:
            for o in rows:
                if not o.get("orderId") or not o.get("symbol"):
                    continue
                cur = c.execute(
                    "INSERT INTO orders (order_id, link_id, symbol, side, qty, price, iv, type, reduce_only, status, "
                    "cum_qty, avg_price, created_ms, updated_ms, raw) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(order_id) DO UPDATE SET status=excluded.status, qty=excluded.qty, "
                    "price=excluded.price, iv=excluded.iv, cum_qty=excluded.cum_qty, avg_price=excluded.avg_price, "
                    "updated_ms=excluded.updated_ms, raw=excluded.raw "
                    "WHERE orders.updated_ms IS NULL OR excluded.updated_ms >= orders.updated_ms",
                    (o["orderId"], o.get("orderLinkId"), o["symbol"], o.get("side"), _num(o.get("qty")),
                     _num(o.get("price")), _num(o.get("orderIv")), o.get("orderType"), _bool(o.get("reduceOnly")),
                     o.get("orderStatus"), _num(o.get("cumExecQty")), _num(o.get("avgPrice")),
                     _i(o.get("createdTime")), _i(o.get("updatedTime")), json.dumps(o, ensure_ascii=False)))
                n += cur.rowcount
        return n

    def record_executions(self, rows: list[dict], source: str, notified: bool = True) -> list[dict]:
        """Исполнения (дедупликация по execId). Возвращает впервые записанные, а при notified=True (WebSocket)
        ещё и записанные сверкой, но не объявленные — подробное уведомление не теряется в гонке со сверкой.
        notified=False — запись сверкой: объявит позже сводка (take_unnotified)."""
        if not self.enabled:
            return rows
        new = []
        with self._db() as c:
            for r in rows:
                if not r.get("execId") or not r.get("symbol") or _i(r.get("execTime")) is None:
                    log.warning("Журнал: исполнение без execId/symbol/execTime не записано: %s", r)
                    if notified:
                        new.append(r)  # записать нельзя, но уведомить (WebSocket) — можно
                    continue
                cur = c.execute(
                    "INSERT OR IGNORE INTO executions (exec_id, order_id, link_id, symbol, side, qty, price, value, "
                    "fee, exec_type, exec_time, is_maker, mark_price, index_price, trade_iv, source, notified, raw) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (r["execId"], r.get("orderId"), r.get("orderLinkId"), r["symbol"], r.get("side"),
                     _num(r.get("execQty")), _num(r.get("execPrice")), _num(r.get("execValue")), _num(r.get("execFee")),
                     r.get("execType"), _i(r.get("execTime")), _bool(r.get("isMaker")), _num(r.get("markPrice")),
                     _num(r.get("indexPrice")), _num(r.get("tradeIv")), source, int(notified),
                     json.dumps(r, ensure_ascii=False)))
                if cur.rowcount:
                    new.append(r)
                elif notified and c.execute("UPDATE executions SET notified = 1 WHERE exec_id = ? AND notified = 0",
                                            (r["execId"],)).rowcount:
                    new.append(r)  # сверка успела раньше WebSocket, но ещё не объявила
        return new

    def take_unnotified(self, before_ms: int) -> list[dict]:
        """Исполнения, записанные сверкой и не объявленные, старше before_ms; помечаются объявленными.
        UPDATE ... WHERE notified = 0 атомарен: объявит кто-то один — WebSocket или сводка."""
        out = []
        with self._db() as c:
            for r in c.execute("SELECT exec_id, raw FROM executions WHERE notified = 0 AND exec_time < ? "
                               "ORDER BY exec_time", (before_ms,)).fetchall():
                if c.execute("UPDATE executions SET notified = 1 WHERE exec_id = ? AND notified = 0",
                             (r["exec_id"],)).rowcount:
                    out.append(json.loads(r["raw"]))
        return out

    def unnotify(self, exec_ids: list[str]) -> None:
        """Сводка не дошла ни до кого — исполнения снова «не объявлены» (объявит следующая сверка)."""
        if not self.enabled or not exec_ids:
            return
        with self._db() as c:
            c.executemany("UPDATE executions SET notified = 0 WHERE exec_id = ?", [(x,) for x in exec_ids])

    def record_deliveries(self, rows: list[dict]) -> list[dict]:
        if not self.enabled:
            return []
        new = []
        with self._db() as c:
            for r in rows:
                t = _i(r.get("deliveryTime"))
                if not r.get("symbol") or t is None:
                    continue
                parsed = parse_option_symbol(r["symbol"])
                strike = _num(r.get("strike")) or (float(parsed[2]) if parsed else None)
                cur = c.execute(
                    "INSERT OR IGNORE INTO deliveries (symbol, delivery_time, side, position, strike, entry_price, "
                    "delivery_price, fee, rpl, raw) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (r["symbol"], t, r.get("side"), _num(r.get("position")), strike, _num(r.get("entryPrice")),
                     _num(r.get("deliveryPrice")), _num(r.get("fee")), _num(r.get("deliveryRpl")),
                     json.dumps(r, ensure_ascii=False)))
                if cur.rowcount:
                    new.append(r)
        return new

    def record_delivery_prices(self, base: str, rows: list[dict]) -> int:
        """Цены поставки из market/new-delivery-price в delivery_prices (журнал выключен — 0)."""
        if not self.enabled:
            return 0
        n = 0
        with self._db() as c:
            for r in rows:
                t, p = _i(r.get("deliveryTime")), _num(r.get("deliveryPrice"))
                if t is not None and p is not None:
                    n += c.execute("INSERT OR IGNORE INTO delivery_prices (base, delivery_time, price) VALUES (?,?,?)",
                                   (base.upper(), t, p)).rowcount
        return n

    # ---------- сверка ----------
    def _last(self, c: sqlite3.Connection, stream: str) -> int | None:
        r = c.execute("SELECT last_ms FROM sync_state WHERE stream = ?", (stream,)).fetchone()
        return r["last_ms"] if r else None

    def sync(self, client: "OptionsClient", now_ms: int | None = None, announce: bool = True) -> SyncResult:
        """announce=False (ручная сверка из CLI): пропущенные WebSocket исполнения не помечаются объявленными —
        сводку о них разошлёт бот.

        Догружает с биржи всё после последней сверки (с перекрытием) окнами по лимиту биржи.
        Прогресс сохраняется после каждого окна: сбой посередине не заставит качать всё заново."""
        t0 = time.monotonic()
        res = SyncResult()
        if not self.enabled:
            return res
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        with self._db() as c:
            done = self._last(c, "executions:backfilled")
            res.first_run = done is None
            # граница «истории»: исполнения раньше самой первой сверки — прошлое (не объявляем), позже —
            # их должен был принести WebSocket; не принёс — объявит сводка. Фиксируется один раз, поэтому
            # прерванная первая выгрузка не превращает последующие пропуски WebSocket в «историю»
            history_until = self._last(c, "executions:history_until")
            if history_until is None:
                history_until = (done if done is not None else now) - GRACE_MS
                c.execute("INSERT INTO sync_state (stream, last_ms) VALUES ('executions:history_until', ?)",
                          (history_until,))

        def rec_exec(rows: list[dict]) -> None:
            old = [r for r in rows if (_i(r.get("execTime")) or 0) < history_until]
            new = [r for r in rows if (_i(r.get("execTime")) or 0) >= history_until]
            res.new_executions.extend(self.record_executions(old, "sync", notified=True))
            res.new_executions.extend(self.record_executions(new, "sync", notified=False))

        streams = (
            ("executions", client.executions, rec_exec),
            ("orders", client.order_history, lambda rows: setattr(res, "orders", res.orders + self.record_orders(rows))),
            ("deliveries", client.delivery_records, lambda rows: res.new_deliveries.extend(
                self.record_deliveries(rows))),
        )
        for stream, fetch, record in streams:
            try:
                with self._db() as c:
                    last = self._last(c, stream)
                start = last - OVERLAP_MS if last is not None else now - self.cfg.journal_backfill_days * DAY_MS
                s = max(start, now - MAX_DEPTH_MS)
                while s < now:
                    e = min(s + WINDOWS[stream], now)
                    record(fetch(s, e))
                    with self._db() as c:
                        c.execute("INSERT INTO sync_state (stream, last_ms) VALUES (?, ?) "
                                  "ON CONFLICT(stream) DO UPDATE SET last_ms = excluded.last_ms", (stream, e))
                    s = e
                if stream == "executions" and res.first_run:
                    with self._db() as c:
                        c.execute("INSERT OR IGNORE INTO sync_state (stream, last_ms) VALUES ('executions:backfilled', ?)",
                                  (now,))
            except Exception as e:
                log.warning("Журнал: сверка %s — %s", stream, e, exc_info=not _is_exchange_error(e))
                res.errors.append(f"{stream}: {e}")
        try:
            # order/history фильтрует окно по ВРЕМЕНИ СОЗДАНИЯ (сверено на demo): ордер, созданный раньше окна
            # и исполненный/отменённый позже, окно не вернёт — дочитываем активные по журналу ордера по id
            with self._db() as c:
                active = [r["order_id"] for r in c.execute(
                    f"SELECT order_id FROM orders WHERE status IN ({','.join('?' * len(ACTIVE_STATUSES))}) "
                    "ORDER BY updated_ms LIMIT ?", (*sorted(ACTIVE_STATUSES), MAX_REFRESH))]
            for oid in active:
                if o := client.order_by_id(oid):
                    res.orders += self.record_orders([o])
        except Exception as e:
            log.warning("Журнал: обновление активных ордеров — %s", e, exc_info=not _is_exchange_error(e))
            res.errors.append(f"активные ордера: {e}")
        for base in self.cfg.base_coins:
            try:
                res.prices += self.record_delivery_prices(base, client.delivery_prices(base))
            except Exception as e:
                log.warning("Журнал: цены поставки %s — %s", base, e, exc_info=not _is_exchange_error(e))
                res.errors.append(f"цены поставки {base}: {e}")
        if announce:
            res.missed = self.take_unnotified(now - GRACE_MS)
        res.seconds = time.monotonic() - t0
        return res

    # ---------- отчёт ----------
    def report(self, start_ms: int, end_ms: int, label: str = "") -> Report:
        rep = Report(start_ms, end_ms, label)
        with self._db() as c:
            # вся история до конца периода (для средней цены входа), только нужные колонки — без raw
            execs = c.execute("SELECT symbol, side, qty, price, value, fee, exec_type, exec_time FROM executions "
                              "WHERE exec_time < ? ORDER BY exec_time, exec_id", (end_ms,)).fetchall()
            dels = c.execute("SELECT symbol, delivery_time, side, position, strike, entry_price, delivery_price, fee "
                             "FROM deliveries WHERE delivery_time < ? ORDER BY delivery_time", (end_ms,)).fetchall()
            prices = load_delivery_prices(c)
        record_px = {r["symbol"]: r["delivery_price"] for r in dels if r["delivery_price"] is not None}
        record_entry = {r["symbol"]: r["entry_price"] for r in dels if r["entry_price"]}

        def delivery_px(sym: str, t: int) -> float | None:
            """Цена поставки: из записи о поставке, иначе из market/new-delivery-price (±1 ч)."""
            if sym in record_px:
                return record_px[sym]
            return nearest_price(prices.get(sym.split("-")[0], []), t)

        delivered_by_exec = {r["symbol"] for r in execs if r["exec_type"] in DELIVERY_TYPES}
        events = [(r["exec_time"], 0, "exec", r) for r in execs]
        events += [(r["delivery_time"], 1, "delivery", r) for r in dels]
        events.sort(key=lambda e: (e[0], e[1]))
        state: dict[str, list[float]] = {}
        for t, _, kind, r in events:
            sym = r["symbol"]
            st = state.setdefault(sym, [0.0, 0.0])
            in_period = start_ms <= t < end_ms
            ss = rep.symbols.setdefault(sym, SymbolStats()) if in_period else SymbolStats()
            fee = r["fee"] or 0.0
            if kind == "exec" and r["exec_type"] not in DELIVERY_TYPES:
                qty, price = r["qty"] or 0.0, r["price"] or 0.0
                pnl = _apply(st, qty if r["side"] == "Buy" else -qty, price)
                value = r["value"] if r["value"] is not None else qty * price
                if in_period:
                    rep.trades += 1
                    ss.trades += 1
                    if r["side"] == "Buy":
                        rep.buy_value += value
                        ss.bought += qty
                    else:
                        rep.sell_value += value
                        ss.sold += qty
            else:
                no_entry = False
                if kind == "delivery" and sym in delivered_by_exec:
                    continue  # поставка уже пришла исполнением — не считать дважды
                # поставка закрывает позицию по внутренней стоимости (execPrice поставки не используем:
                # его смысл не сверен — это может быть цена базового актива)
                px = delivery_px(sym, t) if kind == "exec" else (r["delivery_price"] or delivery_px(sym, t))
                # q — закрываемая позиция со знаком: по журналу; side — только если позиции в истории нет.
                # У delivery-record side — сторона ЗАКРЫТИЯ (сверено на demo 17NOV26: длинная позиция → Sell)
                q = st[0]
                if kind == "exec" and abs(q) > EPS:
                    q = min(r["qty"] or 0.0, abs(q)) * (1 if q > 0 else -1)
                elif kind == "exec":  # позиции нет в нашей истории (открыта раньше выгрузки)
                    q = (r["qty"] or 0.0) * (-1 if r["side"] == "Buy" else 1)
                    st[:] = [q, record_entry.get(sym, 0.0)]
                    no_entry = sym not in record_entry
                elif abs(q) < EPS and r["position"]:  # позиции нет в нашей истории (старше выгрузки)
                    q = r["position"] * (-1 if r["side"] == "Buy" else 1)
                    st[:] = [q, r["entry_price"] or 0.0]
                    no_entry = not r["entry_price"]
                pay = intrinsic(sym, px, r["strike"] if kind == "delivery" else None) if px is not None else None
                if no_entry:
                    # цена входа неизвестна: выплата без уплаченной премии — не прибыль; PnL не считаем
                    _apply(st, -q, st[1])
                    pnl = 0.0
                    if in_period:
                        rep.unknown_entries.append(sym)
                elif pay is None:
                    # цена поставки неизвестна: позиция всё равно закрыта (экспирация), PnL — не считаем
                    if abs(q) > EPS:
                        _apply(st, -q, st[1])  # закрыть по средней цене входа: PnL 0, позиция обнулена
                    pnl = 0.0
                    if in_period:
                        rep.unknown_deliveries.append(sym)
                else:
                    pnl = _apply(st, -q, pay) if abs(q) > EPS else 0.0
                if in_period:
                    rep.deliveries += 1
                    ss.delivered = True
            if in_period:
                rep.fees += fee
                ss.fees += fee
                rep.realized += pnl
                ss.realized += pnl
        rep.open_positions = {s: (q, a) for s, (q, a) in state.items() if abs(q) > EPS}
        return rep

    def csv_bytes(self, start_ms: int, end_ms: int) -> bytes:
        """Исполнения и поставки за период — CSV (UTF-8 с BOM, чтобы Excel открыл кириллицу)."""
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(csv_safe(["time_utc", "kind", "symbol", "side", "qty", "price", "value", "fee", "exec_type", "source",
                    "user", "order_id", "exec_id", "link_id"]))
        with self._db() as c:
            for r in c.execute(
                    "SELECT e.*, l.user_name, l.source AS link_source FROM executions e "
                    "LEFT JOIN order_links l ON l.link_id = e.link_id "
                    "WHERE exec_time >= ? AND exec_time < ? ORDER BY exec_time", (start_ms, end_ms)):
                w.writerow(csv_safe([utc(r["exec_time"], seconds=True), "execution", r["symbol"], r["side"], r["qty"], r["price"],
                            r["value"], r["fee"], r["exec_type"], r["link_source"] or parse_link_id(r["link_id"])["source"],
                            r["user_name"] or "", r["order_id"], r["exec_id"], r["link_id"] or ""]))
            for r in c.execute("SELECT * FROM deliveries WHERE delivery_time >= ? AND delivery_time < ? "
                               "ORDER BY delivery_time", (start_ms, end_ms)):
                w.writerow(csv_safe([utc(r["delivery_time"], seconds=True), "delivery", r["symbol"], r["side"], r["position"],
                            r["delivery_price"], "", r["fee"], "", "", "", "", "", ""]))
        return ("﻿" + buf.getvalue()).encode()


def _is_exchange_error(e: Exception) -> bool:
    from .bybit_client import BybitError
    return isinstance(e, BybitError)


def fmt_report(rep: Report, max_symbols: int = 15) -> str:
    def m(x: float) -> str:
        return f"{x:+,.2f}".replace(",", " ")

    def n(x: float, d: int = 2) -> str:  # разделитель тысяч — пробел (запятые в тексте не трогаем)
        return f"{x:,.{d}f}".replace(",", " ")

    lines = [f"Журнал за {rep.label} ({utc(rep.start_ms)} … {utc(rep.end_ms)} UTC)"]
    if not rep.symbols:
        lines.append("сделок и поставок нет")
    else:
        lines += [
            f"сделок {rep.trades}, поставок {rep.deliveries}",
            f"премия: уплачено {n(rep.buy_value)}, получено {n(rep.sell_value)}",
            f"комиссии {n(rep.fees, 4)}",
            f"реализованный PnL {m(rep.realized)}, за вычетом комиссий {m(rep.net)} USDT",
            "",
        ]
        items = sorted(rep.symbols.items(), key=lambda kv: -abs(kv[1].realized))
        for sym, s in items[:max_symbols]:
            mark = " (поставка)" if s.delivered else ""
            lines.append(f"{short_symbol(sym)}: +{s.bought:g}/−{s.sold:g}, PnL {m(s.realized)}{mark}")
        if len(items) > max_symbols:
            lines.append(f"… ещё {len(items) - max_symbols} (полностью — в CSV)")
    if rep.open_positions:
        lines += ["", "Открыто на конец периода (по журналу):"]
        opened = sorted(rep.open_positions.items())
        for sym, (q, avg) in opened[:max_symbols]:
            lines.append(f"{short_symbol(sym)}: {q:+g} @ {avg:g}")
        if len(opened) > max_symbols:
            lines.append(f"… ещё {len(opened) - max_symbols}")
    if rep.unknown_deliveries:
        unk = sorted(set(rep.unknown_deliveries))
        more = f" и ещё {len(unk) - 5}" if len(unk) > 5 else ""
        lines.append(f"⚠️ нет цены поставки (не в PnL): {', '.join(map(short_symbol, unk[:5]))}{more}")
    if rep.unknown_entries:
        unk = sorted(set(rep.unknown_entries))
        more = f" и ещё {len(unk) - 5}" if len(unk) > 5 else ""
        lines.append(f"⚠️ вход раньше журнала (поставка не в PnL): {', '.join(map(short_symbol, unk[:5]))}{more}")
    text = "\n".join(lines)
    return text if len(text) <= MAX_TG else text[:MAX_TG] + "\n… (сокращено — полностью в CSV)"


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    ap = argparse.ArgumentParser(prog="python -m optbot.journal", description="Журнал сделок")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--sync", action="store_true", help="сверка с биржей")
    g.add_argument("--report", metavar="ПЕРИОД", help="сегодня | вчера | 7d | 2026-11-01 | 2026-11-01..2026-11-15")
    ap.add_argument("--csv", metavar="ФАЙЛ", help="с --report: записать исполнения и поставки в CSV")
    a = ap.parse_args(argv)
    j = Journal(settings)
    if a.sync:
        from .bybit_client import OptionsClient
        res = j.sync(OptionsClient(settings), announce=False)  # сводку о пропущенном разошлёт бот
        print(f"[{settings.env}] {settings.db_file}: {res}")
        return 1 if res.errors else 0
    start, end, label = parse_period([a.report])
    print(fmt_report(j.report(start, end, label)))
    if a.csv:
        with open(a.csv, "wb") as f:
            f.write(j.csv_bytes(start, end))
        print(f"CSV: {a.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
