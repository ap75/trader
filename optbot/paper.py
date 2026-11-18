"""Бумажный движок (эксперимент E1): виртуальные книги «горизонт × стратегия × актив».

Книги: горизонты short (2–7 дн., SIG_DTE_*) и long (14–45 дн., SIG_LONG_DTE_*); стратегии сигналов (кондор,
кредитные и дебетовые спреды, стрэддл), у long — ещё календарный спред. Активы — PAPER_BASES (по умолчанию
BASE_COINS).

Шаг (`PaperEngine.step`, после каждого среза сборщика и `--step`):
1. Отметка открытых сделок по `leg_marks` (ноги на слежении у сборщика, reason `book:<id>`) и правила выхода:
   - прибыль ≥ PAPER_TAKE_PCT % макс. прибыли (у стрэддла — % риска), стоп: убыток ≥ PAPER_STOP_PCT % риска;
   - за PAPER_EXIT_DAYS_SHORT / _LONG дн. до экспирации (0 — держать до экспирации);
   - календарь: за PAPER_CALENDAR_EXIT_H ч до ближней экспирации, прибыль ≥ PAPER_CALENDAR_TAKE_PCT % дебета,
     цена ушла от страйка дальше ожидаемого движения (на входе, до ближней).
   Выход — по исполнимой стороне (продажа по bid, откуп по ask) хуже на PAPER_SLIPPAGE_PCT, плюс комиссия.
   Короткую ногу без ask не откупить — выход ждёт следующего среза; купленная без bid — по 0, только если по mark
   стоит ≤ NO_BID_WING_PCT % риска. Стоп — по mark (нет mark — ждём), цели — по исполнимой стороне.
2. После экспирации — итог по цене поставки (`delivery_prices`, нет — запрос к бирже), комиссия поставки.
   Календарь, не закрытый до ближней экспирации: ближняя — по цене поставки, дальняя — продажа по bid.
3. Вход: сигнал стратегии книги есть; в книге меньше PAPER_MAX_OPEN_PER_BOOK открытых и на этой (ближней)
   экспирации книга ещё не входила (одна сделка на экспирацию); суммарный риск открытых по всем книгам с новой —
   не больше PAPER_MAX_TOTAL_RISK_USDT. Покупка по ask, продажа по bid хуже на PAPER_SLIPPAGE_PCT; комиссия
   Bybit min(PAPER_FEE_RATE·индекс, PAPER_FEE_CAP_PCT % премии) × кол-во (индекс ≈ форвард экспирации).
   Кол-во = min(PAPER_RISK_USDT / риск на 1 лот, MAX_QTY_<A>, MAX_POSITION_<A>), вниз до шага лота; урезано
   лимитом — пометка qty_cap; меньше минимального лота — пропуск (paper_skips, с причиной).
4. Кривая книг (`book_equity`): реализованный PnL + открытые по mark, риск и IM обычного режима
   (короткая нога: mark + max(PAPER_IM_SHORT_PCT %·S − OTM, PAPER_IM_MIN_PCT %·S)); IM PM — н/д (NULL).

Тег event — событие высокой важности (EVENTS_FILE) до (ближней) экспирации: статистика — отдельно.

Запуск:
    python -m optbot.paper --step                 шаг вручную (на сервере; бот делает его после среза)
    python -m optbot.paper --status               открытые сделки по книгам
    python -m optbot.paper --stats 30d [--csv f]  итоги закрытых: по книгам, с тегом event и без
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import math
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from decimal import ROUND_FLOOR, Decimal

from . import db
from .bybit_client import OptionsClient, option_intrinsic, parse_option_symbol
from .collector import track
from .config import PaperSettings, Settings, settings
from .fmt import DAY_MS, HOUR_MS, csv_safe, utc
from .journal import Journal, load_delivery_prices, nearest_price, parse_period
from .signals import STRATEGIES, Leg, Signal, SignalEngine, option_fee, profile

log = logging.getLogger("optbot.paper")

SIGNAL_STRATEGIES = ("iron_condor", "bull_put", "bear_call", "bull_call", "bear_put", "long_straddle")
BOOK_STRATEGIES = {"short": SIGNAL_STRATEGIES, "long": SIGNAL_STRATEGIES + ("calendar",)}
SETTLE_GRACE_MS = 30 * 60_000      # цена поставки публикуется после 08:00
PRICE_GIVEUP_MS = 3 * DAY_MS       # цены поставки нет столько — сделка закрывается без итога (void)
MARK_MAX_AGE_MS = 40 * 60_000      # котировка ноги старше — для выхода не годится
NO_BID_WING_PCT = 5                # купленная нога без bid продаётся по 0, если по mark стоит ≤ N% риска сделки
EXIT_MARGIN_MS = 12 * HOUR_MS      # вход — не ближе полусуток к окну раннего выхода
REASONS = {"take": "цель по прибыли", "stop": "стоп", "time": "срок до экспирации", "cal_time": "до ближней экспирации",
           "cal_take": "цель календаря", "cal_move": "цена ушла от страйка", "expiry": "экспирация",
           "void": "без итога (нет цены поставки)"}


COST_SUFFIX = ":cost"  # книга с фильтром издержек SIG_MAX_COST_PCT (рядом — та же без фильтра)


def book_id(horizon: str, strategy: str, base: str, cost: bool = False) -> str:
    return f"{horizon}:{strategy}:{base}" + (COST_SUFFIX if cost else "")


def is_cost_book(book: str) -> bool:
    return book.endswith(COST_SUFFIX)


def all_books(cfg: Settings) -> list[tuple[str, str, str, str, bool]]:
    """[(id, горизонт, стратегия, актив, с фильтром издержек)] — активы PAPER_BASES, которые есть в BASE_COINS.
    Фильтр выключен (SIG_MAX_COST_PCT=0) — книг «:cost» нет."""
    bases = [b for b in cfg.paper.bases if b in cfg.base_coins]
    variants = (False, True) if cfg.sig.max_cost_pct else (False,)
    return [(book_id(h, s, b, v), h, s, b, v) for b in bases for h, ss in BOOK_STRATEGIES.items() for s in ss
            for v in variants]


# ---------- издержки ----------
def fill_price(pc: PaperSettings, side: int, bid: float, ask: float) -> float | None:
    """Исполнение по рынку: покупка по ask, продажа по bid — хуже на PAPER_SLIPPAGE_PCT. Нет котировки — None
    (продажа длинной без bid — по 0: дальнее крыло обесценилось, это настоящая цена выхода)."""
    k = pc.slippage_pct / 100
    if side > 0:
        return ask * (1 + k) if ask and ask > 0 else None
    return max(bid or 0.0, 0.0) * (1 - k)


def trade_fee(pc: PaperSettings, index: float, price: float, qty: float) -> float:
    """Комиссия Bybit за сделку с опционом (signals.option_fee с настройками PAPER_FEE_*)."""
    return option_fee(index, price, qty, pc.fee_rate, pc.fee_cap_pct)


def delivery_fee(pc: PaperSettings, base: str, kind: str, strike: float, s: float, qty: float) -> float:
    """Комиссия поставки: min(ставка·индекс, потолок·внутренняя стоимость) × кол-во; вне денег — 0."""
    intr = option_intrinsic(kind, strike, s)
    if intr <= 0:
        return 0.0
    return min(pc.delivery_fee_rate.get(base, 0.00015) * s, pc.delivery_fee_cap_pct / 100 * intr) * qty


def im_regular(pc: PaperSettings, legs: list[dict], marks: dict[str, float], s: float, qty: float) -> float:
    """IM обычного режима (REGULAR_MARGIN, сверено на demo): короткая нога mark + max(N%·S − OTM, M%·S),
    купленная — 0 (покрытие не учитывается)."""
    out = 0.0
    for l in legs:
        if l["side"] > 0:
            continue
        otm = max(l["strike"] - s, 0.0) if l["kind"] == "C" else max(s - l["strike"], 0.0)
        out += (marks.get(l["symbol"], 0.0) + max(pc.im_short_pct / 100 * s - otm, pc.im_min_pct / 100 * s)) * qty
    return out


def _mark(m: dict) -> float | None:
    """mark ноги: сборщик пишет ноль как NULL — пусто и без bid — это 0 (обесценилась), пусто при bid — нет данных."""
    if m.get("mark") is not None:
        return m["mark"]
    return 0.0 if not m.get("bid") else None


def _floor_step(x: float, step: Decimal) -> Decimal:
    if step <= 0:
        return Decimal(str(x))
    return (Decimal(str(x)) / step).to_integral_value(ROUND_FLOOR) * step


# ---------- результат шага ----------
@dataclass
class StepResult:
    ts: int
    opened: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    marked: int = 0
    waiting: int = 0              # экспирация прошла, цены поставки ещё нет
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        parts = [f"отмечено {self.marked}", f"открыто {len(self.opened)}", f"закрыто {len(self.closed)}"]
        if self.skipped:
            parts.append(f"пропущено {len(self.skipped)}")
        if self.waiting:
            parts.append(f"ждут цену поставки {self.waiting}")
        if self.errors:
            parts.append("ошибки: " + "; ".join(self.errors))
        return ", ".join(parts)


class PaperEngine:
    def __init__(self, client: OptionsClient, cfg: Settings = settings, engine: SignalEngine | None = None):
        self.c = client
        self.cfg = cfg
        self.pc = cfg.paper
        self.engine = engine if engine is not None else SignalEngine(client, cfg)
        self._lock = threading.Lock()  # бот и ручной шаг в одном процессе не идут одновременно

    def _db(self):
        return db.connect(self.cfg.db_file)

    # ---------- шаг ----------
    def step(self, now_ms: int | None = None, signals: dict[str, dict[str, Signal]] | None = None) -> StepResult:
        """Один шаг: выходы и итоги, затем входы, затем кривая. signals — {актив: {книга-стратегия: Signal}}
        (тесты); без него — расчёт по бирже."""
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        res = StepResult(ts=now_ms)
        if not self.pc.enabled:
            return res
        with self._lock:
            conn = self._db()
            self._px_cache: dict[str, list[tuple[int, float]] | None] = {}  # актив -> ответ биржи за этот шаг
            try:
                self._ensure_books(conn, now_ms)
                self._exits(conn, now_ms, res)
                self._settle(conn, now_ms, res)
                if signals is None:
                    signals = self._signals(now_ms, res)
                for base, by_book in signals.items():
                    for bid, sig in by_book.items():
                        try:
                            self._enter(conn, bid, sig, now_ms, res)
                        except Exception as e:
                            log.exception("Бумажные книги: вход %s не выполнен", bid)
                            res.errors.append(f"{bid}: {e}")
                self._equity(conn, now_ms)
            finally:
                conn.close()
        return res

    def _ensure_books(self, conn, now_ms: int) -> None:
        with conn:
            conn.executemany("INSERT OR IGNORE INTO books (id, horizon, strategy, base, created_ms, cost_filter) "
                             "VALUES (?,?,?,?,?,?)", [(i, h, s, b, now_ms, int(v)) for i, h, s, b, v in all_books(self.cfg)])

    def _signals(self, now_ms: int, res: StepResult) -> dict[str, dict[str, Signal]]:
        """Сигналы по активам: короткий и длинный горизонты и календарь — по одному запросу к бирже на актив."""
        out: dict[str, dict[str, Signal]] = {}
        books = all_books(self.cfg)
        for base in sorted({x[3] for x in books}):
            try:
                mk = self.engine.market(base, now_ms)
            except Exception as e:
                log.warning("Бумажные книги: данные %s — %s", base, e)
                res.errors.append(f"данные {base}: {e}")
                continue
            by_book, got = {}, {}
            pad = EXIT_MARGIN_MS / DAY_MS  # экспирации ближе окна раннего выхода (с запасом) — не предлагать
            ex_s = self.pc.exit_days_short + pad if self.pc.exit_days_short else 0
            ex_l = self.pc.exit_days_long + pad if self.pc.exit_days_long else 0
            ex_c = self.pc.calendar_exit_h / 24 + pad
            for key, fn in (("short", lambda: self.engine.compute(base, horizon="short", market=mk, min_dte=ex_s)),
                            ("long", lambda: self.engine.compute(base, horizon="long", market=mk, min_dte=ex_l)),
                            ("calendar", lambda: self.engine.calendar_signal(
                                base, market=mk, metrics=got["long"].metrics if "long" in got else None,
                                min_near_dte=ex_c))):
                try:  # сбой одного расчёта не теряет остальные
                    sig = got[key] = fn()
                except Exception as e:
                    log.exception("Бумажные книги: сигнал %s %s не посчитан", key, base)
                    res.errors.append(f"сигнал {key} {base}: {e}")
                    continue
                if sig.strategy and sig.legs:  # обе версии книги: без фильтра и с фильтром издержек
                    for x in books:
                        if x[1:4] == (sig.horizon, sig.strategy, base):
                            by_book[x[0]] = sig
            out[base] = by_book
        return out

    # ---------- вход ----------
    def _skip(self, conn, bid: str, expiry: str, code: str, reason: str, now_ms: int, res: StepResult) -> None:
        with conn:
            n = conn.execute("INSERT OR IGNORE INTO paper_skips (book, expiry, code, ts, reason) VALUES (?,?,?,?,?)",
                             (bid, expiry, code, now_ms, reason)).rowcount
        if n:  # одна причина на книгу и экспирацию — лог не засоряется каждые 15 минут
            log.info("Бумажные книги: %s %s — пропуск: %s", bid, expiry, reason)
            res.skipped.append(f"{bid} {expiry}: {reason}")

    def _size(self, sig: Signal, fills: list[float]) -> tuple[float, float, float | None, float, str | None, str | None]:
        """-> (кол-во, по бюджету, макс. прибыль на 1, риск на 1, урезано чем, почему нельзя)."""
        base = sig.metrics.base
        unit = [Leg(**{**asdict(l), "qty": 1.0}) for l in sig.legs]
        fee1 = sum(trade_fee(self.pc, sig.forward, f, 1.0) for f in fills)
        debit1 = -sum(-l.side * f for l, f in zip(unit, fills))
        if sig.strategy == "calendar":
            risk1, prof1 = debit1 + fee1, None  # один страйк: риск = дебет
        else:
            p = profile(unit, fills)
            if math.isinf(p.max_loss):
                return 0, 0, None, 0, None, "риск без ограничения"
            risk1 = p.max_loss + fee1
            prof1 = None if math.isinf(p.max_profit) else p.max_profit - fee1
        if risk1 <= 0:
            return 0, 0, prof1, risk1, None, "риск ≤ 0 по ценам входа (данные?)"
        raw = self.pc.risk_usdt / risk1
        q, cap = raw, None
        for name, lim in ((f"MAX_QTY_{base}", self.cfg.max_order_qty.get(base)),
                          (f"MAX_POSITION_{base}", self.cfg.max_position.get(base))):
            if lim and lim > 0 and q > float(lim):
                q, cap = float(lim), f"{name}={lim}"
        insts = self.c.load_instruments(base)  # кэш клиента
        by_sym = {i.symbol: i for i in insts}
        step = max((by_sym[l.symbol].qty_step for l in sig.legs if l.symbol in by_sym), default=Decimal("0"))
        min_q = max((by_sym[l.symbol].min_qty for l in sig.legs if l.symbol in by_sym), default=Decimal(str(sig.qty)))
        qd = _floor_step(q, step)
        if qd < min_q:
            why = (f"кол-во {raw:.4g} по бюджету {self.pc.risk_usdt:g} USDT (риск на 1 — {risk1:.2f})"
                   + (f", лимит {cap}" if cap else "") + f" < мин. лота {min_q}")
            return 0, raw, prof1, risk1, cap, why
        return float(qd), raw, prof1, risk1, cap, None

    def _enter(self, conn, bid: str, sig: Signal, now_ms: int, res: StepResult) -> None:
        pc = self.pc
        expiry = sig.expiry
        row = conn.execute("SELECT COUNT(*) n FROM paper_trades WHERE book = ? AND status = 'open'", (bid,)).fetchone()
        if conn.execute("SELECT 1 FROM paper_trades WHERE book = ? AND expiry = ? LIMIT 1", (bid, expiry)).fetchone():
            return  # одна сделка на экспирацию: после выхода по цели та же серия заново не открывается
        lim = self.cfg.sig.max_cost_pct
        if is_cost_book(bid) and sig.is_costly(lim):
            have = "премии нет" if sig.cost_pct is None else f"издержки {sig.cost_pct:.0f}% премии"
            self._skip(conn, bid, expiry, "cost", f"{have} > SIG_MAX_COST_PCT={lim:g}", now_ms, res)
            return
        if row["n"] >= pc.max_open_per_book:
            self._skip(conn, bid, expiry, "max_open", f"открыто {row['n']} = PAPER_MAX_OPEN_PER_BOOK", now_ms, res)
            return
        exit_ms = (sig.expiry_ms - pc.calendar_exit_h * HOUR_MS if sig.strategy == "calendar" else
                   sig.expiry_ms - (pc.exit_days_long if sig.horizon == "long" else pc.exit_days_short) * DAY_MS)
        if now_ms >= exit_ms - EXIT_MARGIN_MS:  # вход сразу (или почти сразу) попал бы в окно раннего выхода
            self._skip(conn, bid, expiry, "exit_window", "до экспирации меньше срока раннего выхода", now_ms, res)
            return
        fills = [fill_price(pc, l.side, l.bid, l.ask) for l in sig.legs]
        if any(f is None or not math.isfinite(f) for f in fills):
            self._skip(conn, bid, expiry, "data", "нет ask у покупаемой ноги", now_ms, res)
            return
        qty, raw, prof1, risk1, cap, why = self._size(sig, fills)
        if why:
            self._skip(conn, bid, expiry, "qty", why, now_ms, res)
            return
        risk = risk1 * qty
        fees = sum(trade_fee(pc, sig.forward, f, qty) for f in fills)
        net = sum(-l.side * f * qty for l, f in zip(sig.legs, fills))
        legs = [dict(symbol=l.symbol, kind=l.kind, strike=l.strike, side=l.side,
                     expiry_ms=sig.far_expiry_ms if (sig.strategy == "calendar" and l.side > 0) else sig.expiry_ms,
                     bid=l.bid, ask=l.ask, mark=l.mark, fill=f) for l, f in zip(sig.legs, fills)]
        im = im_regular(pc, legs, {l.symbol: l.mark for l in sig.legs}, sig.forward, qty)
        m = sig.metrics
        metrics = dict(horizon=m.horizon, iv=m.iv, rv=m.rv, rv14=m.rv14, iv7=m.iv7, rv7=m.rv7, iv30=m.iv30,
                       rv30=m.rv30, pctl=m.pctl, pctl_used=m.enough_history and m.pctl is not None,
                       history_days=m.history_days, history_stale=m.history_stale, regime=sig.regime,
                       trend=m.trend.direction, ema_gap_pct=m.trend.gap_pct, exp_iv=sig.exp_iv, dte=sig.dte,
                       reasons=sig.reasons)
        events = [dict(ts=e.ts_ms, name=e.name) for e in sig.events]
        over, tid = None, None
        with conn:
            # ручной --step одновременно с ботом: та же сделка и суммарный риск — проверка под блокировкой записи
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM paper_trades WHERE book = ? AND expiry = ? LIMIT 1",
                            (bid, expiry)).fetchone():
                return
            if conn.execute("SELECT COUNT(*) FROM paper_trades WHERE book = ? AND status = 'open'",
                            (bid,)).fetchone()[0] >= pc.max_open_per_book:
                return  # параллельный шаг успел открыть — лимит книги
            # лимит — на каждую версию книг отдельно (с фильтром и без — два параллельных эксперимента)
            used = conn.execute("SELECT COALESCE(SUM(max_loss), 0) FROM paper_trades WHERE status = 'open' AND "
                                "(book LIKE ?) = ?", ("%" + COST_SUFFIX, int(is_cost_book(bid)))).fetchone()[0]
            if pc.max_total_risk_usdt > 0 and used + risk > pc.max_total_risk_usdt + 1e-9:
                over = (f"риск открытых {used:.2f} + {risk:.2f} > PAPER_MAX_TOTAL_RISK_USDT="
                        f"{pc.max_total_risk_usdt:g}")
            else:
                cur = conn.execute(
                    "INSERT INTO paper_trades (book, horizon, strategy, base, opened_ms, expiry, expiry_ms, far_expiry, "
                    "far_expiry_ms, legs, qty, qty_budget, qty_cap, entry_net, entry_fees, max_profit, max_loss, "
                    "im_regular, forward, em, tags, events, metrics, cfg, cost_pct) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (bid, sig.horizon, sig.strategy, m.base, now_ms, expiry, sig.expiry_ms, sig.far_expiry,
                     sig.far_expiry_ms, json.dumps(legs), qty, raw, cap, net, fees,
                     prof1 * qty if prof1 is not None else None, risk, im, sig.forward, sig.em,
                     ",".join(sig.tags) or None, json.dumps(events), _json(metrics),
                     _json({**asdict(pc), "sig_max_cost_pct": self.cfg.sig.max_cost_pct}),
                     sig.cost_pct))
                tid = cur.lastrowid
                for exp_ms in {l["expiry_ms"] for l in legs}:
                    track(conn, [l["symbol"] for l in legs if l["expiry_ms"] == exp_ms], exp_ms, f"book:{tid}", now_ms)
        if over:
            self._skip(conn, bid, expiry, "total_risk", over, now_ms, res)
            return
        log.info("Бумажные книги: #%s %s %s кол-во %s, риск %.2f%s", tid, bid, expiry, qty, risk,
                 f" (урезано {cap})" if cap else "")
        res.opened.append(f"#{tid} {bid} {expiry}")

    # ---------- отметка и выход ----------
    def _marks(self, conn, symbols: list[str], now_ms: int) -> dict[str, dict]:
        """Котировки ног из одного среза — последнего, где есть все ноги, не старше MARK_MAX_AGE_MS (ноги из
        разных срезов дали бы цены, которых вместе не было). Нет такого — {}."""
        if not symbols:
            return {}
        rows = conn.execute(
            f"SELECT symbol, ts, mark, bid, ask, underlying FROM leg_marks WHERE symbol IN "
            f"({','.join('?' * len(symbols))}) AND ts >= ? AND ts <= ? ORDER BY ts",
            (*symbols, now_ms - MARK_MAX_AGE_MS, now_ms)).fetchall()
        by_ts: dict[int, dict[str, dict]] = {}
        for r in rows:
            by_ts.setdefault(r["ts"], {})[r["symbol"]] = dict(r)
        need = set(symbols)
        for ts in sorted(by_ts, reverse=True):
            if need <= set(by_ts[ts]):
                return by_ts[ts]
        return {}

    def _exit_value(self, t, legs: list[dict], marks: dict[str, dict], live: list[dict]) -> tuple[float, float] | None:
        """(премия закрытия live-ног: + получено, комиссия) по исполнимой стороне; короткую не откупить — None."""
        net = fees = 0.0
        for l in live:
            mk = marks.get(l["symbol"])
            if mk is None:
                return None
            if (l["side"] > 0 and not (mk["bid"] and mk["bid"] > 0)
                    and (_mark(mk) or 0) * t["qty"] > NO_BID_WING_PCT / 100 * t["max_loss"]):
                return None  # нет bid, а нога стоит заметную долю риска — по 0 не продаём, ждём котировку
            f = fill_price(self.pc, -l["side"], mk["bid"], mk["ask"])
            if f is None:
                return None
            s = mk["underlying"] or t["forward"]
            net += l["side"] * f * t["qty"]   # длинная продаётся (+), короткая откупается (−)
            fees += trade_fee(self.pc, s, f, t["qty"])
        return net, fees

    def _exits(self, conn, now_ms: int, res: StepResult) -> None:
        pc = self.pc
        for t in conn.execute("SELECT * FROM paper_trades WHERE status = 'open' ORDER BY id").fetchall():
            try:
                legs = json.loads(t["legs"])
                live = [l for l in legs if l["expiry_ms"] > now_ms]
                if t["strategy"] != "calendar" and not live:
                    continue  # все ноги истекли — итог по цене поставки (_settle)
                marks = self._marks(conn, [l["symbol"] for l in live], now_ms)
                if t["strategy"] == "calendar" and len(live) < len(legs):
                    self._calendar_after_near(conn, t, legs, live, marks, now_ms, res)
                    continue
                if not live:
                    continue
                # отметка по mark (кривая книги; по ней же — стоп: спред bid/ask сам по себе стоп не включает)
                mpnl = None
                mk_ = {s_: _mark(m_) for s_, m_ in marks.items()}
                if all(mk_.get(l["symbol"]) is not None for l in legs):
                    mpnl = sum(l["side"] * t["qty"] * (mk_[l["symbol"]] - l["fill"]) for l in legs) - t["entry_fees"]
                    with conn:
                        conn.execute("UPDATE paper_trades SET mark_ms = ?, mark_pnl = ? WHERE id = ?",
                                     (now_ms, mpnl, t["id"]))
                    res.marked += 1
                ev = self._exit_value(t, legs, marks, legs)
                if ev is None:
                    continue
                exit_net, exit_fees = ev
                pnl = t["entry_net"] + exit_net - t["entry_fees"] - exit_fees
                reason = self._exit_reason(t, legs, marks, pnl, now_ms, mpnl)
                if reason:
                    self._close(conn, t, reason, now_ms, exit_net, exit_fees, pnl, res)
            except Exception as e:
                log.exception("Бумажные книги: #%s — отметка не выполнена", t["id"])
                res.errors.append(f"#{t['id']}: {e}")

    def _exit_reason(self, t, legs, marks, pnl: float, now_ms: int, mark_pnl: float | None = None) -> str | None:
        """pnl — по исполнимой стороне (цели); стоп — по mark (нет mark — по исполнимой)."""
        pc = self.pc
        risk = t["max_loss"]
        stop_pnl = mark_pnl  # нет mark — стоп не проверяется (по bid/ask его включил бы один спред): ждём срез
        if t["strategy"] == "calendar":
            if now_ms >= t["expiry_ms"] - pc.calendar_exit_h * HOUR_MS:
                return "cal_time"
            if pnl >= pc.calendar_take_pct / 100 * risk:
                return "cal_take"
            if stop_pnl is not None and stop_pnl <= -pc.stop_pct / 100 * risk:
                return "stop"
            s = next((marks[l["symbol"]]["underlying"] for l in legs if l["side"] < 0
                      and marks.get(l["symbol"], {}).get("underlying")), None)
            if s and t["em"] and abs(s - legs[0]["strike"]) > t["em"]:
                return "cal_move"
            return None
        target = t["max_profit"] if t["max_profit"] is not None else risk  # стрэддл — % риска
        if target > 0 and pnl >= pc.take_pct / 100 * target:
            return "take"
        if stop_pnl is not None and stop_pnl <= -pc.stop_pct / 100 * risk:
            return "stop"
        days = pc.exit_days_long if t["horizon"] == "long" else pc.exit_days_short
        if days > 0 and now_ms >= t["expiry_ms"] - days * DAY_MS:
            return "time"
        return None

    def _close(self, conn, t, reason: str, now_ms: int, exit_net: float | None, exit_fees: float | None,
               pnl: float | None, res: StepResult, delivery: float | None = None) -> None:
        status = "void" if reason == "void" else "closed"
        with conn:
            n = conn.execute(
                "UPDATE paper_trades SET status = ?, closed_ms = ?, close_reason = ?, exit_net = ?, exit_fees = ?, "
                "delivery_price = ?, pnl = ?, r_mult = ? WHERE id = ? AND status = 'open'",
                (status, now_ms, reason, exit_net, exit_fees, delivery, pnl,
                 pnl / t["max_loss"] if pnl is not None and t["max_loss"] else None, t["id"])).rowcount
        if n:
            log.info("Бумажные книги: #%s %s закрыта — %s, PnL %s", t["id"], t["book"], REASONS.get(reason, reason),
                     "н/д" if pnl is None else f"{pnl:+.2f}")
            res.closed.append(f"#{t['id']} {t['book']}: {reason}")

    def _delivery_price(self, conn, base: str, expiry_ms: int) -> float | None:
        """Цена поставки: из БД, нет — из ответа биржи (один запрос на актив за шаг, ответ — для всех экспираций)."""
        prices = load_delivery_prices(conn, base, expiry_ms - HOUR_MS, expiry_ms + HOUR_MS)
        px_ = nearest_price(prices.get(base, []), expiry_ms)
        if px_ is not None:
            return px_
        cache = getattr(self, "_px_cache", {})
        if base not in cache:
            cache[base] = None
            try:
                rows = self.c.delivery_prices(base)
                got = []
                for r in rows:  # из ответа напрямую: при JOURNAL_ENABLED=0 в БД цены не попадут
                    try:
                        got.append((int(r["deliveryTime"]), float(r["deliveryPrice"])))
                    except (KeyError, TypeError, ValueError):
                        continue
                cache[base] = got
            except Exception as e:
                log.warning("Бумажные книги: цена поставки %s — %s", base, e)
                return None
            try:  # пригодится журналам (выключен — не пишет); сбой записи не теряет полученные цены
                Journal(self.cfg).record_delivery_prices(base, rows)
            except Exception as e:
                log.warning("Бумажные книги: цены поставки %s не записаны в БД — %s", base, e)
        return nearest_price(cache[base] or [], expiry_ms)

    def _settle(self, conn, now_ms: int, res: StepResult) -> None:
        """Итог сделок, у которых все ноги истекли (не календарь), — по цене поставки."""
        for t in conn.execute("SELECT * FROM paper_trades WHERE status = 'open' AND strategy != 'calendar' "
                              "AND expiry_ms <= ? ORDER BY id", (now_ms - SETTLE_GRACE_MS,)).fetchall():
            try:
                s = self._delivery_price(conn, t["base"], t["expiry_ms"])
                if s is None:
                    if now_ms - t["expiry_ms"] > PRICE_GIVEUP_MS:
                        self._close(conn, t, "void", now_ms, None, None, None, res)
                    else:
                        res.waiting += 1
                    continue
                legs = json.loads(t["legs"])
                payout = sum(l["side"] * option_intrinsic(l["kind"], l["strike"], s) * t["qty"] for l in legs)
                dfee = sum(delivery_fee(self.pc, t["base"], l["kind"], l["strike"], s, t["qty"]) for l in legs)
                pnl = t["entry_net"] + payout - t["entry_fees"] - dfee
                self._close(conn, t, "expiry", now_ms, payout, dfee, pnl, res, delivery=s)
            except Exception as e:
                log.exception("Бумажные книги: #%s — итог не посчитан", t["id"])
                res.errors.append(f"#{t['id']}: {e}")

    def _calendar_after_near(self, conn, t, legs, live, marks, now_ms: int, res: StepResult) -> None:
        """Ближняя истекла, а календарь открыт (бот не работал): ближняя — по цене поставки, дальняя — продажа."""
        dead = [l for l in legs if l not in live]
        if any(now_ms < l["expiry_ms"] + SETTLE_GRACE_MS for l in dead):
            return
        px_ = {}
        for l in dead:  # каждая истёкшая нога — по цене поставки своей экспирации
            if l["expiry_ms"] not in px_:
                px_[l["expiry_ms"]] = self._delivery_price(conn, t["base"], l["expiry_ms"])
        if any(v is None for v in px_.values()):
            if now_ms - max(px_) > PRICE_GIVEUP_MS:
                self._close(conn, t, "void", now_ms, None, None, None, res)
            else:
                res.waiting += 1
            return
        s = px_[t["expiry_ms"]] if t["expiry_ms"] in px_ else next(iter(px_.values()))
        payout = sum(l["side"] * option_intrinsic(l["kind"], l["strike"], px_[l["expiry_ms"]]) * t["qty"] for l in dead)
        dfee = sum(delivery_fee(self.pc, t["base"], l["kind"], l["strike"], px_[l["expiry_ms"]], t["qty"])
                   for l in dead)
        ev = self._exit_value(t, legs, marks, live) if live else (0.0, 0.0)
        if ev is None:
            return  # дальнюю продать не по чему — следующий срез
        pnl = t["entry_net"] + payout + ev[0] - t["entry_fees"] - dfee - ev[1]
        self._close(conn, t, "expiry", now_ms, payout + ev[0], dfee + ev[1], pnl, res, delivery=s)

    # ---------- кривая ----------
    def _equity(self, conn, now_ms: int) -> None:
        rows = []
        for b in conn.execute("SELECT DISTINCT book FROM paper_trades").fetchall():
            book = b["book"]
            realized = conn.execute("SELECT COALESCE(SUM(pnl), 0) FROM paper_trades WHERE book = ? AND "
                                    "status = 'closed'", (book,)).fetchone()[0]
            opened = conn.execute("SELECT * FROM paper_trades WHERE book = ? AND status = 'open'", (book,)).fetchall()
            unreal = risk = im = 0.0
            for t in opened:
                legs = json.loads(t["legs"])
                marks = self._marks(conn, [l["symbol"] for l in legs if l["expiry_ms"] > now_ms], now_ms)
                unreal += t["mark_pnl"] if t["mark_pnl"] is not None else -t["entry_fees"]
                risk += t["max_loss"]
                s = next((m["underlying"] for m in marks.values() if m.get("underlying")), t["forward"])
                im += im_regular(self.pc, [l for l in legs if l["expiry_ms"] > now_ms],
                                 {k: v_ for k, v in marks.items() if (v_ := _mark(v)) is not None}, s, t["qty"])
            rows.append((now_ms, book, realized, unreal, len(opened), risk, im))
        with conn:
            conn.executemany("INSERT OR REPLACE INTO book_equity (ts, book, realized, unrealized, open_n, open_risk, "
                             "im_regular) VALUES (?,?,?,?,?,?,?)", rows)

    # ---------- отчёты ----------
    def status(self) -> str:
        conn = self._db()
        try:
            rows = conn.execute("SELECT * FROM paper_trades WHERE status = 'open' ORDER BY book, id").fetchall()
            total = sum(r["max_loss"] for r in rows)
        finally:
            conn.close()
        if not rows:
            return "Открытых бумажных сделок нет"
        lim = f" из {self.pc.max_total_risk_usdt:g}" if self.pc.max_total_risk_usdt else ""
        parts = []
        for name, flt in (("без фильтра", False), ("с фильтром издержек", True)):
            part = [r for r in rows if is_cost_book(r["book"]) == flt]
            if part:  # лимит суммарного риска — на каждую версию книг отдельно
                parts.append(f"{name} {len(part)}, риск {sum(r['max_loss'] for r in part):.2f}{lim}")
        lines = [f"Открытые бумажные сделки: {len(rows)} — " + "; ".join(parts) + " USDT"]
        for r in rows:
            mark = "" if r["mark_pnl"] is None else f", по mark {r['mark_pnl']:+.2f}"
            tag = f" [{r['tags']}]" if r["tags"] else ""
            cap = f" (урезано {r['qty_cap']})" if r["qty_cap"] else ""
            lines.append(f"#{r['id']} {r['book']} {r['expiry']}{'/' + r['far_expiry'] if r['far_expiry'] else ''} "
                         f"кол-во {r['qty']:g}{cap}, риск {r['max_loss']:.2f}{mark}{tag}")
        return "\n".join(lines)

    def closed(self, start_ms: int, end_ms: int) -> list[dict]:
        conn = self._db()
        try:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM paper_trades WHERE status IN ('closed', 'void') AND closed_ms >= ? AND closed_ms < ? "
                "ORDER BY id",
                (start_ms, end_ms))]
        finally:
            conn.close()


DELIVERY_FEE_TOL_PCT = 5   # расхождение комиссии поставки с формулой больше — сообщить
DELIVERY_FEE_MAX_BAD = 3   # о расхождениях — не больше стольких сообщений (дальше — лог и --check-delivery-fees)


def check_delivery_fees(cfg: Settings, conn, mark: bool = True) -> list[dict]:
    """Сверка формулы комиссии поставки с фактическими поставками в деньгах (deliveries: внутренняя стоимость > 0,
    в том числе с нулевой комиссией биржи).
    mark — запомнить проверенные (bot_state): сообщать о каждой новой с расхождением и о первом совпадении.
    -> [{symbol, qty, price, fee, expected, rate_part, cap_part, ok, report}]"""
    pc = cfg.paper
    st = conn.execute("SELECT value FROM bot_state WHERE key = 'delivery_fee_checked'").fetchone()
    seen = json.loads(st["value"]) if st and st["value"] else {}
    if not mark:
        seen = {}
    since, had_ok = int(seen.get("since", -1)), bool(seen.get("ok"))
    keys = set(seen.get("keys", []))  # проверенные в окне [since, …): поставки одной экспирации приходят не разом
    out, last, n_bad = [], since, int(seen.get("bad", 0))
    for r in conn.execute("SELECT symbol, delivery_time, position, strike, delivery_price, fee FROM deliveries "
                          "WHERE delivery_time >= ? ORDER BY delivery_time", (since,)):
        key = f"{r['symbol']}@{r['delivery_time']}"
        last = max(last, r["delivery_time"])
        p = parse_option_symbol(r["symbol"])
        if key in keys or not p or not r["delivery_price"] or not r["position"] or r["fee"] is None:
            continue  # комиссии в записи нет — сверять не с чем
        base, kind, strike = p[0], p[3], float(r["strike"] or p[2])
        qty, s_ = abs(r["position"]), r["delivery_price"]
        intr = option_intrinsic(kind, strike, s_)
        keys.add(key)
        if intr <= 0:
            continue  # вне денег комиссии нет и по формуле — сверять нечего
        rate_part = pc.delivery_fee_rate.get(base, 0.00015) * s_ * qty
        cap_part = pc.delivery_fee_cap_pct / 100 * intr * qty
        expected = min(rate_part, cap_part)  # = delivery_fee(...)
        fee = abs(r["fee"])  # знак у биржи — не сверен: сравниваем величину
        ok = abs(fee - expected) <= max(DELIVERY_FEE_TOL_PCT / 100 * expected, 1e-6)
        # о расхождениях — первые DELIVERY_FEE_MAX_BAD (систематическое — категория без комиссии — не засыпает
        # личку); о совпадении — первое
        report = (not ok and n_bad < DELIVERY_FEE_MAX_BAD) or (ok and not had_ok)
        n_bad += (not ok) and report
        out.append(dict(key=key, symbol=r["symbol"], qty=qty, price=s_, fee=fee, expected=expected,
                        rate_part=rate_part, cap_part=cap_part, ok=ok, report=report))
        had_ok = had_ok or ok
    if mark and keys != set(seen.get("keys", [])):
        # окно — 2 сут. до последней поставки: опоздавшие записи той же экспирации ещё проверятся; ключи — только
        # в окне (список не растёт)
        cut = last - 2 * DAY_MS
        old = {k: int(k.rsplit("@", 1)[1]) for k in keys}
        state = {"since": cut, "ok": had_ok, "bad": n_bad, "keys": sorted(k for k, t in old.items() if t >= cut)}
        with conn:
            conn.execute("INSERT INTO bot_state (key, value) VALUES ('delivery_fee_checked', ?) ON CONFLICT(key) "
                         "DO UPDATE SET value = excluded.value", (json.dumps(state),))
    return out


def fmt_delivery_check(x: dict) -> str:
    verdict = "совпала" if x["ok"] else f"РАСХОЖДЕНИЕ больше {DELIVERY_FEE_TOL_PCT}%"
    return (f"Комиссия поставки {x['symbol']} × {x['qty']:g} при цене {x['price']:g}: биржа {x['fee']:.6g}, "
            f"формула {x['expected']:.6g} = min(ставка·S = {x['rate_part']:.6g}, потолок·внутренняя = "
            f"{x['cap_part']:.6g}) — формула {verdict}" + ("" if x["ok"] else
            ". Поправить PAPER_DELIVERY_FEE_RATE / PAPER_DELIVERY_FEE_CAP_PCT (или формулу в paper.py)."))


def _json(v) -> str:
    def clean(x):
        if isinstance(x, float) and not math.isfinite(x):
            return None
        if isinstance(x, dict):
            return {k: clean(y) for k, y in x.items()}
        if isinstance(x, (list, tuple)):
            return [clean(y) for y in x]
        return x
    return json.dumps(clean(v), allow_nan=False)


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    pnl = [r["pnl"] for r in rows if r["pnl"] is not None]
    rs = [r["r_mult"] for r in rows if r["r_mult"] is not None]
    return dict(n=n, wins=sum(1 for x in pnl if x > 0), pnl=sum(pnl), sum_r=sum(rs),
                avg_r=sum(rs) / len(rs) if rs else None, worst=min(pnl) if pnl else None)


def fmt_stats(rows: list[dict], label: str) -> str:
    if not rows:
        return f"Бумажные книги, {label}: закрытых сделок нет"
    rows = [{"status": "closed", **r} for r in rows]
    void = [r for r in rows if r["status"] == "void"]
    rows = [r for r in rows if r["status"] != "void"]
    lines = [f"Бумажные книги, {label}: закрытых {len(rows)}"
             + (f", без итога (нет цены поставки) {len(void)}" if void else "")]

    def line(name: str, part: list[dict]) -> str:
        s = summarize(part)
        avg = "-" if s["avg_r"] is None else f"{s['avg_r']:+.2f}"
        worst = "-" if s["worst"] is None else f"{s['worst']:+.2f}"
        return (f" {name}: {s['n']} сд., прибыльных {s['wins']}, PnL {s['pnl']:+.2f}, ΣR {s['sum_r']:+.2f}, "
                f"ср. R {avg}, худшая {worst}")

    for tag, part in (("без event", [r for r in rows if not r["tags"]]),
                      ("с event", [r for r in rows if r["tags"]]),
                      ("книги без фильтра издержек", [r for r in rows if not is_cost_book(r["book"])]),
                      ("книги с фильтром издержек", [r for r in rows if is_cost_book(r["book"])])):
        if part:
            lines.append(line(tag, part))
    lines.append("По книгам:")
    for book in sorted({r["book"] for r in rows}):
        part = [r for r in rows if r["book"] == book]
        lines.append(line(book, [r for r in part if not r["tags"]]) if any(not r["tags"] for r in part) else
                     f" {book}: только с event")
        if any(r["tags"] for r in part):
            lines.append(line(f"{book} [event]", [r for r in part if r["tags"]]))
    return "\n".join(lines)


CSV_FIELDS = ("id", "book", "cost_pct", "horizon", "strategy", "base", "opened_ms", "closed_ms", "expiry", "far_expiry", "qty",
              "qty_cap", "entry_net", "entry_fees", "exit_net", "exit_fees", "max_profit", "max_loss", "im_regular",
              "pnl", "r_mult", "close_reason", "delivery_price", "tags")


def to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_FIELDS + ("opened_utc", "closed_utc"))
    for r in rows:
        w.writerow(csv_safe([r.get(k) for k in CSV_FIELDS]) + [utc(r["opened_ms"]), utc(r["closed_ms"])])
    return buf.getvalue()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    ap = argparse.ArgumentParser(prog="python -m optbot.paper", description="Бумажные книги (эксперимент E1)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--step", action="store_true", help="шаг: выходы, итоги, входы по текущим сигналам")
    g.add_argument("--status", action="store_true", help="открытые сделки")
    g.add_argument("--stats", nargs="?", const="30d", metavar="ПЕРИОД", help="итоги закрытых (по умолчанию 30d)")
    g.add_argument("--check-delivery-fees", action="store_true",
                   help="сверить формулу комиссии поставки с поставками в деньгах из журнала сделок")
    ap.add_argument("--csv", metavar="ФАЙЛ", help="со --stats: сделки в CSV")
    a = ap.parse_args(argv)
    eng = PaperEngine(OptionsClient(), settings)
    if a.status:
        print(eng.status())
        return 0
    if a.check_delivery_fees:
        conn = db.connect(settings.db_file)
        try:
            got = check_delivery_fees(settings, conn, mark=False)
        finally:
            conn.close()
        print("\n".join(fmt_delivery_check(x) for x in got) or "Поставок в деньгах в журнале нет")
        return 0 if all(x["ok"] for x in got) else 1
    if a.stats:
        start, end, label = parse_period([a.stats])
        rows = eng.closed(start, end)
        print(fmt_stats(rows, label))
        if a.csv:
            with open(a.csv, "w", encoding="utf-8", newline="") as f:
                f.write(to_csv(rows))
        return 0
    if not settings.paper.enabled:
        print("PAPER_ENABLED=0 — бумажные книги выключены")
        return 1
    res = eng.step()
    print(f"[{settings.env}] {res}")
    for x in res.opened + res.closed + res.skipped:
        print(" ", x)
    return 1 if res.errors else 0


if __name__ == "__main__":
    sys.exit(main())
