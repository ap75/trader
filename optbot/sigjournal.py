"""Журнал сигналов (этап 4): запись выданных /signals, итог после экспирации, статистика /signals_stats.

Запись: каждая выдача сигнала с ногами. Та же сделка (те же ноги и стороны), ещё не дошедшая до экспирации, —
одна строка: повторный показ увеличивает счётчик shown, цены входа остаются от первой выдачи. Ноги ставятся
на слежение в сборщике (leg_marks каждые COLLECTOR_INTERVAL_MIN) до экспирации.

Итог (задача бота с периодом сверки журнала JOURNAL_SYNC_MIN, по умолчанию час, и `--evaluate`): после экспирации по цене поставки (delivery_prices журнала сделок,
±1 ч; нет — запрос market/new-delivery-price) — PnL выплаты на позицию по исполнимым ценам входа и по mid,
в долях риска R = PnL / макс. риск. По котировкам ног из leg_marks — худший промежуточный PnL при закрытии
по исполнимой стороне (длинные — по bid, короткие — по ask) и по mark, и первый момент, когда исполнимый PnL
достиг половины макс. прибыли (у стрэддла без потолка — половины риска). Комиссии не учитываются.

Запуск:
    python -m optbot.sigjournal --evaluate                   посчитать итоги
    python -m optbot.sigjournal --stats 30d [--base BTC] [--csv f]
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import logging
import math
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field

from . import db
from .collector import track
from .config import SIG_LONG_FIELDS, Settings, settings
from .fmt import DAY_MS, HOUR_MS, csv_safe, utc
from .journal import Journal, load_delivery_prices, nearest_price, parse_period
from .signals import STRATEGIES, Leg, Signal, payoff

log = logging.getLogger("optbot.sigjournal")

VIEW_TTL_MS = DAY_MS                # та же сделка тому же человеку снова — новый показ (shown + 1) не раньше
SHOW_DB_TIMEOUT_S = 5               # запись показа ждёт занятую БД не дольше — карточка не должна висеть
SETTLE_GRACE_MS = 30 * 60_000       # цена поставки публикуется после 08:00 — раньше не ищем
PRICE_GIVEUP_MS = 14 * DAY_MS        # ответ биржи пуст или запрос не удаётся столько — закрыть без итога всё равно
DATA_ERRORS = (KeyError, TypeError, ValueError, IndexError)  # ошибки данных строки (не временные)
PRICE_TIMEOUT_MS = 3 * DAY_MS        # цены нет столько после экспирации — сигнал закрывается без итога
REGIMES = {"high": "IV высокая", "low": "IV низкая"}


def cfg_version(cfg: Settings) -> tuple[str, str]:
    """(хеш, JSON) порогов SIG_*: статистику можно разбить по версии настроек."""
    raw = json.dumps({k: v for k, v in asdict(cfg.sig).items() if k not in SIG_LONG_FIELDS}, sort_keys=True)
    return hashlib.sha1(raw.encode()).hexdigest()[:8], raw


def legs_key(legs: list[Leg]) -> str:
    return "|".join(sorted(f"{'+' if l.side > 0 else '-'}{l.symbol}" for l in legs))


def _leg_json(l: Leg) -> dict:
    return dict(symbol=l.symbol, kind=l.kind, strike=l.strike, side=l.side, qty=l.qty, bid=l.bid, ask=l.ask,
                mark=l.mark, price=l.price, mid=l.mid, delta=l.delta, iv=l.iv)


def _legs_from_json(raw: str) -> list[Leg]:
    return [Leg(symbol=d["symbol"], kind=d["kind"], strike=d["strike"], side=d["side"], qty=d["qty"],
                bid=d["bid"], ask=d["ask"], mark=d.get("mark", 0.0)) for d in json.loads(raw)]


def _inf_none(x: float | None) -> float | None:
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else x


def _json(v) -> str:
    """JSON без нестандартных Infinity/NaN (их не читают Excel Power Query, JSON.parse) — вместо них null."""
    if isinstance(v, dict):
        v = {k: _inf_none(x) for k, x in v.items()}
    elif isinstance(v, list):
        v = [_inf_none(x) for x in v]
    return json.dumps(v, allow_nan=False)


@dataclass
class PathStats:
    max_dd: float | None = None
    max_dd_mark: float | None = None
    half_ms: int | None = None
    n: int = 0


def path_stats(legs: list[Leg], marks: list[dict], target: float | None) -> PathStats:
    """Путь до экспирации по срезам leg_marks: [{ts, symbol, mark, bid, ask}]. Берутся только срезы, где есть
    все ноги; target — PnL «половины макс. прибыли»."""
    by_ts: dict[int, dict[str, dict]] = {}
    for r in marks:
        by_ts.setdefault(r["ts"], {})[r["symbol"]] = r
    out = PathStats()
    entry = {l.symbol: l.price for l in legs}
    entry_mid = {l.symbol: l.mid for l in legs}  # путь по mark — от mid входа, иначе спред входа считался бы дважды
    for ts in sorted(by_ts):
        row = by_ts[ts]
        if any(l.symbol not in row for l in legs):
            continue
        out.n += 1
        # строка ноги в срезе есть, а mark пуст — это ноль (сборщик пишет 0 как NULL): дальнее крыло обесценилось
        # как раз в худшие моменты — пропуск занизил бы просадку
        mark_pnl = sum(l.side * l.qty * ((row[l.symbol]["mark"] or 0.0) - entry_mid[l.symbol]) for l in legs)
        out.max_dd_mark = min(out.max_dd_mark if out.max_dd_mark is not None else 0.0, mark_pnl)
        exits = [(row[l.symbol]["bid"] if l.side > 0 else row[l.symbol]["ask"]) for l in legs]
        # длинная нога без bid (дальнее крыло к экспирации; сборщик пишет нулевой bid как NULL) — продаётся
        # по 0, это настоящая цена выхода; короткую без ask выкупить нельзя — закрыть сделку было нельзя
        if any(l.side < 0 and not (x and x > 0) for l, x in zip(legs, exits)):
            continue
        exits = [max(x or 0.0, 0.0) for x in exits]
        exec_pnl = sum(l.side * l.qty * (x - entry[l.symbol]) for l, x in zip(legs, exits))
        out.max_dd = min(out.max_dd if out.max_dd is not None else 0.0, exec_pnl)
        if target is not None and out.half_ms is None and exec_pnl >= target:
            out.half_ms = ts
    return out


@dataclass
class EvalResult:
    settled: int = 0
    waiting: int = 0           # экспирация прошла, цены поставки ещё нет
    void: int = 0              # цены поставки так и не было (PRICE_TIMEOUT_MS) — закрыты без итога
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        err = f", ошибки: {'; '.join(self.errors)}" if self.errors else ""
        void = f", без итога (нет цены) {self.void}" if self.void else ""
        return f"итогов {self.settled}, ждут цену поставки {self.waiting}{void}{err}"


class SignalJournal:
    def __init__(self, cfg: Settings = settings):
        self.cfg = cfg
        self.cfg_hash, self.cfg_json = cfg_version(cfg)  # пороги неизменны на время жизни объекта

    def _db(self, timeout: float = 30):
        return db.connect(self.cfg.db_file, timeout)

    # ---------- запись ----------
    def record(self, sig: Signal, source: str = "bot", user_id: int | None = None, user_name: str = "",
               chat_id: int | None = None, now_ms: int | None = None) -> int | None:
        """Записать показ сигнала с ногами -> id, он же в sig.id. Та же открытая сделка — её id и
        sig.journal_first_ms; shown + 1, только если этот человек (или источник) не видел её VIEW_TTL_MS —
        в БД, а не в памяти: переживает перезапуск и общий для бота и CLI. Без ног — None."""
        if not self._journaled(sig):
            return None
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        key = legs_key(sig.legs)
        h, raw = self.cfg_hash, self.cfg_json
        conn = self._db(SHOW_DB_TIMEOUT_S)
        viewer = str(user_id) if user_id is not None else source
        try:
            # сначала без блокировки: тот же человек снова в пределах VIEW_TTL — писать нечего, не ждём занятую БД
            row, fresh = self._open_and_seen(conn, sig, viewer, now_ms)
            if row and fresh:
                return self._attach(sig, row)
            with conn:
                # блокировка записи ДО поиска: бот и `cli signals` одновременно не вставят одну сделку дважды
                conn.execute("BEGIN IMMEDIATE")
                row, fresh = self._open_and_seen(conn, sig, viewer, now_ms)
                if row:
                    if not fresh:
                        conn.execute("UPDATE signals SET shown = shown + 1, last_shown_ms = ? WHERE id = ?",
                                     (now_ms, row["id"]))
                        self._viewed(conn, row["id"], viewer, now_ms)
                    return self._attach(sig, row)
                m, p = sig.metrics, sig.profile
                metrics = dict(iv7=m.iv7, rv7=m.rv7, rv14=m.rv14, pctl=m.pctl, history_days=m.history_days,
                               enough_history=m.enough_history, history_stale=m.history_stale,
                               trend=m.trend.direction, ema_gap_pct=m.trend.gap_pct, exp_iv=sig.exp_iv,
                               dte=sig.dte, straddle=sig.straddle)
                cur = conn.execute(
                    "INSERT INTO signals (ts, last_shown_ms, base, strategy, regime, trend, expiry, expiry_ms, legs_key, "
                    "legs, qty, net, net_mid, max_profit, max_loss, breakevens, forward, em, metrics, reasons, "
                    "cfg_hash, cfg, source, user_id, user_name, chat_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (now_ms, now_ms, m.base, sig.strategy, sig.regime, m.trend.direction, sig.expiry, sig.expiry_ms,
                     key, json.dumps([_leg_json(l) for l in sig.legs]), sig.qty, sig.net, sig.net_mid,
                     _inf_none(p.max_profit), _inf_none(p.max_loss), _json(p.breakevens), sig.forward, sig.em,
                     _json(metrics), "; ".join(sig.reasons), h, raw, source, user_id, user_name, chat_id))
                sid = cur.lastrowid
                track(conn, [l.symbol for l in sig.legs], sig.expiry_ms, f"sig:{sid}", now_ms)
                self._viewed(conn, sid, viewer, now_ms)
                sig.id, sig.journal_first_ms, sig.journal_net = sid, None, None
                return sid
        finally:
            conn.close()

    def _open_and_seen(self, conn, sig: Signal, viewer: str, now_ms: int):
        """-> (открытая запись той же сделки или None, видел ли её viewer за VIEW_TTL_MS)."""
        row = self._find_open(conn, sig, now_ms)
        if not row:
            return None, False
        seen = conn.execute("SELECT ts FROM signal_views WHERE signal_id = ? AND viewer = ?",
                            (row["id"], viewer)).fetchone()
        return row, bool(seen and now_ms - seen["ts"] < VIEW_TTL_MS)

    @staticmethod
    def _attach(sig: Signal, row) -> int:
        sig.id, sig.journal_first_ms, sig.journal_net = row["id"], row["ts"], row["net"]
        return row["id"]

    @staticmethod
    def _viewed(conn, sid: int, viewer: str, now_ms: int) -> None:
        conn.execute("INSERT INTO signal_views (signal_id, viewer, ts) VALUES (?,?,?) "
                     "ON CONFLICT(signal_id, viewer) DO UPDATE SET ts = excluded.ts", (sid, viewer, now_ms))

    def _journaled(self, sig: Signal) -> bool:
        """Попадает ли сигнал в журнал. JOURNAL_ENABLED=0 — журналы не пишутся."""
        return self.cfg.journal_enabled and bool(sig.legs) and sig.strategy is not None and sig.expiry_ms is not None

    def _find_open(self, conn, sig: Signal, now_ms: int):
        """Открытая запись той же сделки: те же ноги, размер и пороги, экспирация впереди."""
        return conn.execute("SELECT id, ts, net FROM signals WHERE settled_ms IS NULL AND legs_key = ? AND qty = ? "
                            "AND cfg_hash = ? AND expiry_ms > ? ORDER BY id LIMIT 1",
                            (legs_key(sig.legs), sig.qty, self.cfg_hash, now_ms)).fetchone()

    # ---------- итог ----------
    def evaluate(self, client=None, now_ms: int | None = None) -> EvalResult:
        """Итоги сигналов, у которых прошла экспирация. client — дозапросить цену поставки, если её нет в БД."""
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        res = EvalResult()
        if not self.cfg.journal_enabled:
            return res
        conn = self._db()
        try:
            due = conn.execute("SELECT * FROM signals WHERE settled_ms IS NULL AND expiry_ms <= ? ORDER BY id",
                               (now_ms - SETTLE_GRACE_MS,)).fetchall()
            if not due:
                return res
            # только нужное окно цен, а не вся таблица
            lo = min(x["expiry_ms"] for x in due) - HOUR_MS
            hi = max(x["expiry_ms"] for x in due) + HOUR_MS
            prices = load_delivery_prices(conn, lo_ms=lo, hi_ms=hi)
            fetched: dict[str, bool] = {}   # актив -> запрос к бирже прошёл и вернул цены
            jr = Journal(self.cfg)
            for s in due:
                px = nearest_price(prices.get(s["base"], []), s["expiry_ms"])
                if px is None and client is not None and s["base"] not in fetched:
                    fetched[s["base"]] = False
                    try:
                        rows = client.delivery_prices(s["base"])
                        jr.record_delivery_prices(s["base"], rows)  # пригодится и журналу сделок
                        # перечитать из БД: разбор строк — один (журнала, кривые строки пропускает)
                        prices[s["base"]] = load_delivery_prices(conn, s["base"], lo, hi).get(s["base"], [])
                        fetched[s["base"]] = bool(rows)
                    except Exception as e:
                        log.warning("Сигналы: цена поставки %s — %s", s["base"], e)
                        res.errors.append(f"цена поставки {s['base']}: {e}")
                    px = nearest_price(prices.get(s["base"], []), s["expiry_ms"])
                try:  # одна сломанная строка не останавливает остальные
                    self._evaluate_one(conn, s, px, now_ms, fetched.get(s["base"], False), res)
                except Exception as e:
                    # закрыть без итога — только ошибку данных строки (кривой JSON, нет поля), не временную
                    if isinstance(e, DATA_ERRORS) and now_ms - s["expiry_ms"] > PRICE_TIMEOUT_MS:  # не считается столько — закрыть, не мучить лог
                        with conn:
                            conn.execute("UPDATE signals SET settled_ms = ? WHERE id = ? AND settled_ms IS NULL",
                                         (now_ms, s["id"]))
                        log.error("Сигналы: #%s — итог не считается (%s), закрыт без итога", s["id"], e)
                        res.void += 1
                        continue
                    log.exception("Сигналы: #%s — итог не посчитан", s["id"])
                    res.errors.append(f"#{s['id']}: {e}")
        finally:
            conn.close()
        return res

    @staticmethod
    def _evaluate_one(conn, s, px: float | None, now_ms: int, fetched_ok: bool, res: EvalResult) -> None:
        if px is None and now_ms - s["expiry_ms"] > PRICE_TIMEOUT_MS:
            # биржа ответила непустым списком, а цены нет (new-delivery-price — последние 50): уже не будет —
            # закрыть без итога, иначе запрос и предупреждение каждый час бесконечно. Сбой запроса, пустой
            # ответ или запроса не было (без client) — ждём.
            # пустой ответ / сбой / без client — ждём, но не вечно: через PRICE_GIVEUP_MS закрыть в любом случае
            if not fetched_ok and now_ms - s["expiry_ms"] <= PRICE_GIVEUP_MS:
                res.waiting += 1
                return
            with conn:
                n = conn.execute("UPDATE signals SET settled_ms = ? WHERE id = ? AND settled_ms IS NULL",
                                 (now_ms, s["id"])).rowcount
            if n:  # иначе параллельный --evaluate уже закрыл
                log.warning("Сигналы: #%s — нет цены поставки %s %s за %d дн., закрыт без итога", s["id"],
                            s["base"], s["expiry"], PRICE_TIMEOUT_MS // DAY_MS)
                res.void += 1
            return
        if px is None:
            res.waiting += 1
            return
        with conn:
            done = SignalJournal._settle(conn, s, px, now_ms)
        if done:  # параллельный --evaluate мог посчитать раньше — тогда не объявляем повторно
            res.settled += 1

    @staticmethod
    def _settle(conn, s, delivery: float, now_ms: int) -> bool:
        legs = _legs_from_json(s["legs"])
        mids = [d["mid"] for d in json.loads(s["legs"])]
        pnl = payoff(legs, delivery)
        pnl_mid = payoff(legs, delivery, mids)
        max_loss, max_profit = s["max_loss"], s["max_profit"]
        # прибыль без потолка (стрэддл, NULL) — цель «половина риска»; прибыли нет (≤ 0) — цели нет
        if max_profit is None:
            target = 0.5 * max_loss if max_loss else None
        else:
            target = 0.5 * max_profit if max_profit > 0 else None
        syms = [l.symbol for l in legs]
        marks = [dict(r) for r in conn.execute(
            f"SELECT ts, symbol, mark, bid, ask FROM leg_marks WHERE symbol IN ({','.join('?' * len(syms))}) "
            "AND COALESCE(captured_ms, ts) >= ? AND ts <= ? ORDER BY ts", (*syms, s["ts"], s["expiry_ms"]))]
        ps = path_stats(legs, marks, target)
        return conn.execute(
            "UPDATE signals SET settled_ms = ?, delivery_price = ?, pnl = ?, pnl_mid = ?, r_mult = ?, max_dd = ?, "
            "max_dd_mark = ?, half_ms = ?, marks_n = ? WHERE id = ? AND settled_ms IS NULL",
            (now_ms, delivery, pnl, pnl_mid, pnl / max_loss if max_loss else None, ps.max_dd, ps.max_dd_mark,
             ps.half_ms, ps.n, s["id"])).rowcount == 1

    # ---------- статистика ----------
    def get(self, signal_id: int) -> dict | None:
        """Запись сигнала по номеру (кнопка «Выставить»): ноги — списком словарей."""
        conn = self._db(SHOW_DB_TIMEOUT_S)
        try:
            r = conn.execute("SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()
        finally:
            conn.close()
        if r is None:
            return None
        d = dict(r)
        d["legs"] = json.loads(d["legs"])
        return d

    def executions(self, signal_id: int) -> list[dict]:
        """Прошлые исполнения сигнала кнопкой «Выставить» (превью предупреждает о повторе)."""
        conn = self._db(SHOW_DB_TIMEOUT_S)
        try:
            return [dict(r) for r in conn.execute(
                "SELECT ts, user_name, status, qty FROM signal_executions WHERE signal_id = ? ORDER BY ts",
                (signal_id,))]
        finally:
            conn.close()

    def record_execution(self, signal_id: int, res, user_id: int | None, user_name: str, chat_id: int | None,
                         now_ms: int | None = None) -> int:
        """Исполнение кнопкой «Выставить»: статус, расчётная и фактическая премия, ноги (multileg.ExecResult)."""
        def clean(d: dict) -> dict:  # NaN/Infinity -> null на любом уровне (allow_nan=False не уронит запись)
            return {k: _clean(x) for k, x in d.items()}

        def _clean(x):
            if isinstance(x, dict):
                return clean(x)
            if isinstance(x, list):
                return [_clean(t) for t in x]
            return _inf_none(x)

        def legs(fs):
            return _json([clean(dict(leg=f.leg, symbol=f.symbol, side=f.side, qty=float(f.qty), filled=float(f.filled),
                                     avg=float(f.avg), plan=f.plan_price, attempts=f.attempts, error=f.error,
                                     gap_ms=f.gap_ms, tries=f.tries)) for f in fs])
        conn = self._db()
        try:
            with conn:
                cur = conn.execute(
                    "INSERT INTO signal_executions (signal_id, ts, user_id, user_name, chat_id, status, planned_qty, "
                    "qty, planned_net, actual_net, cash, legs, unwind, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (signal_id, now_ms if now_ms is not None else int(time.time() * 1000), user_id, user_name,
                     chat_id, res.status, float(res.planned_qty), float(res.qty), res.planned_net, res.actual_net,
                     res.cash, legs(res.legs), legs(res.unwind), res.note))
                return cur.lastrowid
        finally:
            conn.close()

    def rows(self, start_ms: int, end_ms: int, base: str | None = None) -> list[dict]:
        conn = self._db()
        try:
            q = "SELECT * FROM signals WHERE ts >= ? AND ts < ?"
            a: list = [start_ms, end_ms]
            if base:
                q += " AND base = ?"
                a.append(base.upper())
            return [dict(r) for r in conn.execute(q + " ORDER BY ts", a)]
        finally:
            conn.close()

    def unannounced(self) -> list[dict]:
        """Итоги, о которых группы ещё не знают — кем бы ни посчитаны (бот или `--evaluate`)."""
        conn = self._db()
        try:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM signals WHERE settled_ms IS NOT NULL AND pnl IS NOT NULL AND announced_ms IS NULL "
                "ORDER BY id")]
        finally:
            conn.close()

    def mark_announced(self, ids: list[int], now_ms: int | None = None) -> None:
        if not ids:
            return
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        conn = self._db()
        try:
            with conn:
                conn.execute(f"UPDATE signals SET announced_ms = ? WHERE id IN ({','.join('?' * len(ids))})",
                             (now_ms, *ids))
        finally:
            conn.close()

    def csv_bytes(self, start_ms: int, end_ms: int, base: str | None = None, names: bool = True,
                  rows: list[dict] | None = None) -> bytes:
        """names=False — без имени запросившего (файл для зрителей в группе). rows — уже прочитанные строки."""
        cols = ["id", "ts", "base", "strategy", "regime", "trend", "expiry", "qty", "net", "net_mid", "max_profit",
                "max_loss", "breakevens", "forward", "em", "shown", "source", *(["user_name"] if names else []),
                "cfg_hash",
                "delivery_price", "pnl", "pnl_mid", "r_mult", "max_dd", "max_dd_mark", "half_ms", "marks_n", "legs",
                "metrics", "reasons"]
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(cols)
        for r in rows if rows is not None else self.rows(start_ms, end_ms, base):
            r = dict(r, ts=utc(r["ts"], seconds=True), half_ms=utc(r["half_ms"]) if r["half_ms"] else "",
                     strategy=STRATEGIES.get(r["strategy"], r["strategy"]))
            w.writerow(csv_safe([r[c] if r[c] is not None else "" for c in cols]))
        return ("﻿" + buf.getvalue()).encode()


@dataclass
class Group:
    n: int = 0
    settled: int = 0
    void: int = 0              # экспирация прошла, итога нет (цена поставки не найдена)
    pending_price: int = 0     # экспирация прошла, итог ещё не посчитан (ждёт цену поставки)
    n_r: int = 0               # итогов с R (R нет, только если max_loss 0 или NULL — в средний R не входят)
    wins: int = 0
    sum_r: float = 0.0
    sum_pnl: float = 0.0
    half: int = 0
    dd_r: list[float] = field(default_factory=list)

    def add(self, r: dict, now_ms: int) -> None:
        self.n += 1
        if r["settled_ms"] is None:
            self.pending_price += r["expiry_ms"] <= now_ms
            return
        if r["pnl"] is None:
            self.void += 1
            return
        self.settled += 1
        self.wins += r["pnl"] > 0
        if r["r_mult"] is not None:
            self.sum_r += r["r_mult"]
            self.n_r += 1
        self.sum_pnl += r["pnl"]
        self.half += r["half_ms"] is not None
        if r["max_dd"] is not None and r["max_loss"]:
            self.dd_r.append(r["max_dd"] / r["max_loss"])


@dataclass
class Stats:
    label: str
    base: str | None
    total: Group = field(default_factory=Group)
    by_strategy: dict[str, Group] = field(default_factory=dict)
    by_regime: dict[str, Group] = field(default_factory=dict)


def compute_stats(rows: list[dict], label: str, base: str | None = None, now_ms: int | None = None) -> Stats:
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    st = Stats(label, base)
    for r in rows:
        st.total.add(r, now_ms)
        st.by_strategy.setdefault(r["strategy"], Group()).add(r, now_ms)
        st.by_regime.setdefault(r["regime"] or "?", Group()).add(r, now_ms)
    return st


def _pct(a: int, b: int) -> str:
    return f"{a} из {b} ({a / b:.0%})" if b else "-"


def fmt_stats(st: Stats) -> str:
    t = st.total
    head = f"Сигналы{' ' + st.base.upper() if st.base else ''} за {st.label}: {t.n}"
    if not t.n:
        return head + "\nСигналов с ногами за период нет."
    extra = (f", экспирация прошла, итог ещё не посчитан {t.pending_price}" if t.pending_price else "") + \
        (f", без цены поставки {t.void}" if t.void else "")
    lines = [head + f" (итог есть у {t.settled}, ждут экспирации {t.n - t.settled - t.void - t.pending_price}"
             f"{extra})"]
    if t.settled:
        lines += [
            f"Прибыльных: {_pct(t.wins, t.settled)}",
            f"Итог: Σ {t.sum_r:+.2f}R ({t.sum_pnl:+,.2f} USDT) · средний "
            + (f"{t.sum_r / t.n_r:+.2f}R" if t.n_r else "-"),
            f"Дошли до 50% макс. прибыли: {_pct(t.half, t.settled)}",
            "Медианная просадка: " + (f"{statistics.median(t.dd_r):+.2f}R" if t.dd_r else "-"),
        ]

    def table(title: str, groups: dict[str, Group], names: dict[str, str]) -> None:
        lines.append(f"\n{title}:")
        lines.append(f"{'':<22} {'n':>3} {'итог':>4} {'приб':>4} {'ΣR':>6} {'ср.R':>6}")
        for k, g in sorted(groups.items(), key=lambda kv: -kv[1].n):
            avg = f"{g.sum_r / g.n_r:+.2f}" if g.n_r else "-"
            lines.append(f"{names.get(k, k)[:22]:<22} {g.n:>3} {g.settled:>4} {g.wins:>4} {g.sum_r:>+6.2f} {avg:>6}")

    table("По стратегиям", st.by_strategy, STRATEGIES)
    table("По режиму IV", st.by_regime, REGIMES)
    lines.append("\nR — доля макс. риска; итог на экспирации по ценам входа на исполнимой стороне, без комиссий. "
                 "Просадка — худший промежуточный итог при закрытии по bid/ask.")
    return "\n".join(lines)


SETTLED_PER_MESSAGE = 20  # строк сводки в одном сообщении (лимит Telegram 4096 символов)


def fmt_settled(rows: list[dict]) -> str:
    """Сводка итогов для группы — одно сообщение (бот режет на части по SETTLED_PER_MESSAGE строк)."""
    lines = ["🧾 Итоги сигналов (экспирация прошла):"]
    for r in rows:
        r_s = f"{r['r_mult']:+.2f}R" if r["r_mult"] is not None else "-"
        lines.append(f"#{r['id']} {r['base']} {STRATEGIES.get(r['strategy'], r['strategy'])} {r['expiry']}: "
                     f"{r['pnl']:+,.2f} USDT ({r_s}), поставка {r['delivery_price']:,.2f}")
    lines.append("Подробно: /signals_stats")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    ap = argparse.ArgumentParser(prog="python -m optbot.sigjournal", description="Журнал сигналов")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--evaluate", action="store_true", help="посчитать итоги сигналов после экспирации")
    g.add_argument("--stats", metavar="ПЕРИОД", help="сегодня | вчера | 30d | 2026-11-01..2026-11-30")
    ap.add_argument("--base", help="только этот актив")
    ap.add_argument("--csv", metavar="ФАЙЛ", help="с --stats: сигналы в CSV")
    a = ap.parse_args(argv)
    sj = SignalJournal(settings)
    if a.evaluate:
        from .bybit_client import OptionsClient
        res = sj.evaluate(OptionsClient(settings))
        print(f"[{settings.env}] {settings.db_file}: {res}")
        return 1 if res.errors else 0
    start, end, label = parse_period([a.stats])
    print(fmt_stats(compute_stats(sj.rows(start, end, a.base), label, a.base)))
    if a.csv:
        with open(a.csv, "wb") as f:
            f.write(sj.csv_bytes(start, end, a.base))
        print(f"CSV: {a.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
