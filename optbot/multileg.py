"""Многоногая сделка (этап 5): ноги по очереди так, чтобы ни в какой момент не было непокрытой короткой позиции.

У Bybit нет атомарного комбо-ордера для опционов (batch не гарантирует исполнения всех ног), поэтому:
- сначала покупки, потом продажи (кондор — крылья, затем короткие ноги; кредитный спред — защитная, затем короткая);
- каждая нога — лимитный IOC: первая попытка по лучшей встречной цене (ask/bid), но не дальше
  mid ± MULTILEG_SLIPPAGE_PCT, дальше шагами к этому пределу (MULTILEG_STEPS попыток с паузой
  MULTILEG_STEP_DELAY_S); свежая котировка перед каждой попыткой. Встречная цена хоть одной ноги за пределом
  уже перед стартом — стратегия не открывается (precheck);
- PASSIVE_FIRST_LEG_SECONDS > 0: первая нога (покупка) сначала стоит PostOnly по mid столько секунд,
  неисполненное — снимается и добирается IOC, как обычно (по умолчанию выключено);
- частичное исполнение — следующие ноги ровно на исполненное количество (итог стратегии — минимум по ногам);
- лишнее у уже исполненных ног (нога исполнилась не вся или вовсе нет) закрывается reduce-only IOC тем же
  способом: сначала откуп коротких, потом продажа длинных. Не закрылось — остаётся, вызывающий уведомляет.
Каждая попытка — свой orderLinkId (`linkid.signal_link_id`), он же в журнале — источник «по сигналу #N».
Синхронный код: вызывать в потоке (asyncio.to_thread).
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Callable

from .bybit_client import (SIDE_SIGN, BybitError, BybitLocalError, BybitNetworkError, OptionsClient, OrderRequest,
                           RiskError, _d, parse_option_symbol)
from .config import Settings
from .fmt import px

log = logging.getLogger("optbot.multileg")

FINAL_STATUSES = {"Filled", "Cancelled", "PartiallyFilledCanceled", "Rejected", "Deactivated"}
_now = time.time  # часы журнала попыток: тесты подменяют модуль time (паузы), не часы
FILL_POLL_S = (0, 0.2, 0.5, 1.0, 2.0, 3.0)   # IOC завершается сразу: первый опрос — без паузы


class SymbolGuard:
    """Кто сейчас торгует символом: сделка по сигналу (одна за раз, на свои символы) или ручные ордера.
    Сделка не начнётся, пока по её символу летит ручной ордер, и ручной ордер — пока идёт сделка по его символу.
    Всё состояние — под одной короткой блокировкой (исполнение и отправка — в потоках to_thread);
    сама отправка ордеров — вне её."""

    def __init__(self):
        self._lock = threading.Lock()
        self._exec: set[str] | None = None   # символы идущей сделки по сигналу (None — сделки нет)
        self._manual: dict[str, int] = {}     # символ -> ручных ордеров в отправке

    def claim_exec(self, symbols: set[str]) -> None:
        with self._lock:
            if self._exec is not None:
                raise ValueError("Сейчас выполняется другая сделка по сигналу — дождитесь итога и повторите")
            busy = sorted(s for s in symbols if self._manual.get(s))
            if busy:
                raise ValueError(f"По {', '.join(busy)} сейчас отправляется ручной ордер — повторите через минуту")
            self._exec = set(symbols)

    def release_exec(self) -> None:
        with self._lock:
            self._exec = None

    def claim_manual(self, symbol: str) -> None:
        with self._lock:
            if self._exec is not None and symbol in self._exec:
                raise ValueError(f"По {symbol} сейчас исполняется сделка по сигналу — повторите после итога")
            self._manual[symbol] = self._manual.get(symbol, 0) + 1

    def release_manual(self, symbol: str) -> None:
        with self._lock:
            n = self._manual.pop(symbol, 1) - 1
            if n > 0:
                self._manual[symbol] = n

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._exec is not None or bool(self._manual)


class UnknownState(Exception):
    """Ордер ушёл, но его исход не выяснен (сбой связи и ордер не найден): дальше ноги не ставим.
    order — последнее увиденное состояние ордера (не завершён): уже исполненное в нём учитывается в итоге."""

    def __init__(self, msg: str, order: dict | None = None):
        super().__init__(msg)
        self.order = order


@dataclass
class LegFill:
    leg: int                 # номер ноги в сигнале (для orderLinkId)
    symbol: str
    side: str                # Buy | Sell
    qty: Decimal             # сколько нужно было
    plan_price: float = 0.0  # цена ноги в сигнале (исполнимая сторона на момент показа)
    filled: Decimal = Decimal(0)
    value: Decimal = Decimal(0)
    attempts: int = 0
    error: str | None = None
    tries: list[dict] = field(default_factory=list)  # журнал попыток: n, kind, ts, mid/bid/ask, price, filled, avg
    gap_ms: int | None = None  # пауза от последнего ответа предыдущей ноги до первой отправки этой
    started_ms: int | None = None  # первая отправка
    ended_ms: int | None = None    # последний ответ (итог попытки)

    @property
    def avg(self) -> Decimal:
        return self.value / self.filled if self.filled else Decimal(0)

    def mark_start(self, ts_ms: int) -> None:
        if self.started_ms is None:
            self.started_ms = ts_ms

    def log_try(self, kind: str, t: dict, price: Decimal, o: dict | None, error: str | None = None) -> None:
        bid, ask, mid = quote_mid(t)
        q, v = (_d(o.get("cumExecQty")), _d(o.get("cumExecValue"))) if o else (Decimal(0), Decimal(0))
        self.ended_ms = int(_now() * 1000)
        self.tries.append(dict(n=len(self.tries) + 1, kind=kind, ts=self.ended_ms, mid=float(mid),
                               bid=float(bid), ask=float(ask), price=float(price), filled=float(q),
                               avg=float(v / q) if q else None, **({"error": error} if error else {})))


@dataclass
class ExecResult:
    status: str                       # complete | partial | none | unwound | stuck | unknown
    qty: Decimal                      # исполнено стратегией (одинаково по всем ногам)
    planned_qty: Decimal
    legs: list[LegFill]
    unwind: list[LegFill] = field(default_factory=list)  # закрытие лишнего (reduce-only)
    left: list[str] = field(default_factory=list)        # что осталось несбалансированным (не закрылось)
    planned_net: float = 0.0          # премия по ценам сигнала на исполненное кол-во (+ кредит, − дебет)
    actual_net: float = 0.0           # фактическая премия на исполненное кол-во по средним ценам ног
    # всё движение премии: ноги целиком (и сверх итога) и закрытие лишнего; минус actual_net — цена отката
    cash: float = 0.0
    note: str = ""

    @property
    def alert(self) -> bool:
        """Исполнено не полностью, осталась несбалансированная позиция или исход неизвестен — срочно админу."""
        # unwound: сделка не собралась, исполненное закрыто — с потерей на спреде, админу тоже сразу
        return self.status in ("partial", "unwound", "stuck", "unknown") or bool(self.left)


def safe_order(legs: list[tuple]) -> list[int]:
    """Порядок ног: сначала покупки, потом продажи; внутри — как в сигнале. legs: (symbol, side, …)."""
    return sorted(range(len(legs)), key=lambda i: (legs[i][1] != "Buy", i))


def leg_side(side: int | float) -> str:
    """Сторона ноги сигнала (+1 покупка, −1 продажа) -> Buy | Sell; одна функция для карточки и превью."""
    return "Buy" if side > 0 else "Sell"


def quote_mid(t: dict) -> tuple[Decimal, Decimal, Decimal]:
    """-> (bid, ask, mid); mid — середина bid/ask, без одной из сторон — mark (0 — котировки нет)."""
    bid, ask, mark = _d(t.get("bid1Price")), _d(t.get("ask1Price")), _d(t.get("markPrice"))
    return bid, ask, ((bid + ask) / 2 if bid > 0 and ask > 0 else mark)


def _round(price: Decimal, buy: bool, tick: Decimal) -> Decimal:
    """К шагу цены в пассивную сторону — как prepare_order: сравнение попыток — по цене, ушедшей на биржу."""
    return (price / tick).to_integral_value(ROUND_DOWN if buy else ROUND_UP) * tick if tick > 0 else price


def step_price(t: dict, side: str, frac: Decimal, cfg: Settings, reduce_only: bool = False,
               tick: Decimal = Decimal(0)) -> Decimal:
    """Цена попытки: от лучшей встречной цены (frac=0; ask для покупки, bid для продажи; её нет — mid) к пределу
    mid ± MULTILEG_SLIPPAGE_PCT (frac=1); встречная за пределом — сам предел. И не дальше, чем пропустит
    проверка опечатки prepare_order (MAX_SLIPPAGE_PCT: открывающий — от mark, закрывающий — от встречной
    котировки). Иначе последние попытки упирались бы в отказ посреди сделки (mid бывает заметно выше mark)."""
    bid, ask, mid = quote_mid(t)
    buy = side == "Buy"
    slip = Decimal(str(cfg.multileg_slippage_pct)) / 100
    cap = mid * (1 + slip) if buy else mid * (1 - slip)
    touch = ask if buy else bid
    if reduce_only and touch > 0:  # закрытие лишнего должно уметь пересечь и широкий спред: не ближе встречной
        cap = max(cap, touch) if buy else min(cap, touch)
    ref = ((ask if buy else bid) if reduce_only else _d(t.get("markPrice"))) or _d(t.get("markPrice"))
    if ref > 0:
        m = Decimal(str(cfg.max_slippage_pct)) / 100 * Decimal("0.99")  # запас на округление к шагу
        cap = min(cap, ref * (1 + m)) if buy else max(cap, ref * (1 - m))
    start = touch if touch > 0 else mid
    start = min(start, cap) if buy else max(start, cap)
    price = start + (cap - start) * frac
    # к шагу цены — в сторону рынка, если не выходит за предел (встречная вне сетки иначе не пересекалась бы),
    # иначе — в пассивную сторону
    up = _round(price, not buy, tick)
    return up if (up <= cap if buy else up >= cap) else _round(price, buy, tick)


def passive_price(t: dict, cfg: Settings, tick: Decimal = Decimal(0)) -> Decimal:
    """Пассивная покупка (PostOnly): mid, строго ниже ask и в пределах MAX_SLIPPAGE_PCT от mark; 0 — не ставить."""
    bid, ask, mid = quote_mid(t)
    price = _round(mid, True, tick)
    if ask > 0 and price >= ask:
        price = _round(ask - (tick if tick > 0 else ask * Decimal("0.001")), True, tick)
    mark = _d(t.get("markPrice"))
    if mark > 0:
        price = min(price, _round(mark * (1 + Decimal(str(cfg.max_slippage_pct)) / 100 * Decimal("0.99")), True, tick))
    return price if price > 0 else Decimal(0)


@dataclass
class LegQuote:
    req: OrderRequest        # после prepare_order по худшей цене попыток
    bid: Decimal
    ask: Decimal
    mid: Decimal


def precheck(client: OptionsClient, cfg: Settings, legs: list[tuple]) -> tuple[list[LegQuote], list[str]]:
    """Все проверки до отправки — в порядке исполнения: биржа и цена по каждой ноге (по худшей цене попыток,
    mid ± MULTILEG_SLIPPAGE_PCT), лимиты портфеля — по сделке целиком (check_strategy). RiskError — отказ.
    -> котировки ног (в порядке safe_order) и строки лимитов для превью."""
    if len({Decimal(str(x[2])) for x in legs}) > 1:  # исполнитель ведёт все ноги одним количеством
        raise RiskError("Ноги с разным количеством (ratio) по кнопке не выставляются — выставьте вручную")
    if len({x[0] for x in legs}) < len(legs):  # ноги учитываются по символу — две в одном слились бы
        raise RiskError("Две ноги в одном символе по кнопке не выставляются — выставьте вручную")
    out = []
    for i in safe_order(legs):
        sym, side, qty = legs[i][:3]
        t = client.ticker(sym)
        bid, ask, mid = quote_mid(t)
        if mid <= 0:
            raise RiskError(f"{sym}: нет котировки — выставить нельзя")
        tick = client.resolve_symbol(sym).tick_size
        worst = step_price(t, side, Decimal(1), cfg, tick=tick)
        touch = ask if side == "Buy" else bid
        # лучшая встречная цена уже за пределом попыток — IOC не исполнится ни разу (или откроет лишь часть
        # ног): стратегию не начинаем
        if touch <= 0:
            raise RiskError(f"{sym}: нет {'ask' if side == 'Buy' else 'bid'} — стратегия не открывается")
        if (touch > worst) if side == "Buy" else (touch < worst):
            raise RiskError(f"{sym}: {'ask' if side == 'Buy' else 'bid'} {px(touch, dash=False)} за пределом "
                            f"{px(worst, dash=False)} (mid {px(mid, dash=False)} ± MULTILEG_SLIPPAGE_PCT="
                            f"{cfg.multileg_slippage_pct:g}%, не дальше MAX_SLIPPAGE_PCT от mark) — "
                            "стратегия не открывается")
        req = client.prepare_order(OrderRequest(symbol=sym, side=side, qty=Decimal(str(qty)), price=worst,
                                                time_in_force="IOC", portfolio_checked=True, quote=t))
        if side == "Sell":  # для лимитов премии — наибольшая премия попыток: продажа идёт от bid вниз
            req.premium = req.qty * max(step_price(t, side, Decimal(0), cfg, tick=tick), req.price)
            if req.premium > cfg.max_order_premium_usdt:  # prepare_order проверил по худшей (низкой) цене
                raise RiskError(f"{sym}: премия ~{req.premium:.2f} по bid > лимита MAX_ORDER_PREMIUM_USDT="
                                f"{cfg.max_order_premium_usdt}")
        out.append(LegQuote(req, bid, ask, mid))
    return out, client.check_strategy([q.req for q in out])


class MultiLegExecutor:
    def __init__(self, client: OptionsClient, cfg: Settings, link: Callable[[int], str],
                 sleep: Callable[[float], None] | None = None, forget: Callable[[str], None] | None = None):
        """link(leg) -> новый уникальный orderLinkId (и запись «кто выставил» в журнал до отправки);
        forget(link_id) — ордер точно не дошёл до биржи (отказ проверки): убрать запись из журнала."""
        self.c, self.cfg, self.link, self.forget = client, cfg, link, forget
        self.sleep = sleep or (lambda s: time.sleep(s))  # time — по имени модуля: тесты подменяют

    # ---------- одна нога ----------
    def _send(self, req: OrderRequest) -> dict:
        """IOC-ордер -> его итоговое состояние (cumExecQty, cumExecValue)."""
        try:
            self.c.place_order(req)
        except (BybitNetworkError, BybitLocalError):
            # сбой связи или неразобранный ответ после отправки — ордер мог исполниться: исход по orderLinkId
            if not req.sent:
                raise
            o = self._final(req)  # запрос мог дойти: исход — по orderLinkId
            if o is None:
                raise UnknownState(f"{req.symbol}: ордер {req.link_id} ушёл, исход неизвестен")
            return o
        o = self._final(req)
        if o is None:
            raise UnknownState(f"{req.symbol}: ордер {req.link_id} принят, но не найден")
        return o

    def _final(self, req: OrderRequest) -> dict | None:
        last = None
        for d in FILL_POLL_S:
            if d:
                self.sleep(d)
            try:
                last = self.c.find_order_by_link(req.symbol, req.link_id) or last
            except BybitError:
                continue
            if last and last.get("orderStatus") in FINAL_STATUSES:
                return last
        if last is not None:  # IOC не завершён: может исполниться ещё — следующая попытка дала бы перебор
            raise UnknownState(f"{req.symbol}: ордер {req.link_id} не завершён ({last.get('orderStatus')}, "
                               f"исполнено {last.get('cumExecQty')})", last)
        return None

    def _passive(self, f: LegFill) -> None:
        """Первая покупка — PostOnly по mid на PASSIVE_FIRST_LEG_SECONDS; неисполненное снимается, остаток доберёт
        _fill. Отказ до отправки (котировка, лимиты, опечатка) — не ошибка ноги: сразу IOC. Исход снятия
        неизвестен (ордер ещё активен) — UnknownState: иначе IOC поверх живого ордера дал бы перебор."""
        req = None
        try:
            t = self.c.ticker(f.symbol)
            price = passive_price(t, self.cfg, self.c.resolve_symbol(f.symbol).tick_size)
            if price <= 0:
                return
            try:
                link = self.link(f.leg)
            except Exception as e:  # orderLinkId не построен — ордер точно не отправлен
                raise RiskError(f"orderLinkId: {e}") from e
            req = OrderRequest(symbol=f.symbol, side="Buy", qty=f.qty, price=price, time_in_force="PostOnly",
                               link_id=link, portfolio_checked=True, quote=t)
            f.attempts += 1
            ts0 = int(_now() * 1000)
            try:
                self.c.place_order(req)
            finally:
                if req.sent:
                    f.mark_start(ts0)
        except (BybitNetworkError, BybitLocalError) as e:
            if req is None or not req.sent:  # не ушёл — сразу IOC; ушёл — исход по orderLinkId (_rest)
                log.warning("Пассивная нога %s не выставлена: %s", f.symbol, e)
                self._forget(req)
                return
        except (RiskError, BybitError, ValueError) as e:
            if isinstance(e, ValueError) and req is not None and req.sent:
                raise UnknownState(f"{f.symbol}: ордер {req.link_id} ушёл, ответ не разобран — {e}") from e
            # отказ проверки, orderLinkId или самой биржи — ордера нет (как в _fill): обычный IOC
            log.warning("Пассивная нога %s не выставлена: %s", f.symbol, e)
            if req is not None and req.sent:  # отказ биржи — попытка ушла: в журнал попыток
                f.log_try("passive", t, price, None, str(e))
            self._forget(req)
            return
        try:
            o = self._rest(req, self.cfg.passive_first_leg_seconds)
        except UnknownState as e:
            self._seen(f, e.order)
            f.log_try("passive", t, price, e.order, str(e))
            raise
        f.filled += _d(o.get("cumExecQty"))
        f.value += _d(o.get("cumExecValue"))
        f.log_try("passive", t, price, o)

    @staticmethod
    def _seen(f: LegFill, o: dict | None) -> None:
        if o:
            f.filled += _d(o.get("cumExecQty"))
            f.value += _d(o.get("cumExecValue"))

    def _forget(self, req: OrderRequest | None) -> None:
        if self.forget and req is not None and req.link_id and not req.sent:
            self.forget(req.link_id)

    def _rest(self, req: OrderRequest, seconds: float) -> dict:
        """Ждать исполнения стоящего ордера seconds, снять неисполненное -> итоговое состояние ордера."""
        waited, o = 0.0, None
        while waited < seconds:
            d = min(1.0, seconds - waited)
            self.sleep(d)
            waited += d
            try:
                o = self.c.find_order_by_link(req.symbol, req.link_id) or o
            except BybitError:
                continue
            if o and o.get("orderStatus") in FINAL_STATUSES:
                return o  # исполнен целиком (или PostOnly отклонён биржей)
        # снимать по orderLinkId всегда — даже если ордер не нашли (сбой опросов): он может стоять в стакане
        for k in range(3):
            try:
                self.c.cancel_by_link(req.symbol, req.link_id)
                break
            except BybitNetworkError as e:  # сбой связи — повторить снятие
                log.warning("Снятие пассивного ордера %s: %s", req.link_id, e)
                if k < 2:
                    self.sleep(0.5)
            except BybitError as e:  # не найден / уже исполнен или снят — итог покажет _final
                log.warning("Снятие пассивного ордера %s: %s", req.link_id, e)
                break
        warn = "пассивный ордер может стоять в стакане — проверьте /orders"
        seen = o  # последнее состояние из ожидания: его исполненное не теряется, если дальше связи нет
        try:
            o = self._final(req)
        except UnknownState as e:  # после снятия всё ещё активен
            raise UnknownState(f"{e}; {warn}", e.order or seen) from None
        if o is None:
            raise UnknownState(f"{req.symbol}: ордер {req.link_id} ушёл, исход неизвестен; {warn}", seen)
        return o

    def _fill(self, f: LegFill, reduce_only: bool = False) -> LegFill:
        """Добрать f.qty шагами цены от ask/bid к mid ± проскальзывание. Ошибка лимитов/биржи — в f.error."""
        steps = self.cfg.multileg_steps
        last_price = None
        for k in range(steps):
            left = f.qty - f.filled
            if left <= 0:
                break
            req = None
            try:
                t = self.c.ticker(f.symbol)
                bid, ask, mid = quote_mid(t)
                if mid <= 0:
                    f.error = "нет котировки"
                    break
                frac = Decimal(k) / Decimal(steps - 1) if steps > 1 else Decimal(0)  # одна — по ask/bid
                price = step_price(t, f.side, frac, self.cfg, reduce_only, self.c.resolve_symbol(f.symbol).tick_size)
                crosses = (ask > 0 and price >= ask) if f.side == "Buy" else (bid > 0 and price <= bid)
                # та же цена (шаг цены, предел) — без повтора, если рынок до неё не дошёл (дошёл — повторить)
                if last_price is not None and price == last_price and not crosses:
                    if k == steps - 1 and f.filled == 0:  # частично исполненная нога — не «ошибка»
                        f.error = ("цена не сдвигается к рынку (предел MULTILEG_SLIPPAGE_PCT / MAX_SLIPPAGE_PCT от "
                                   "mark или шаг цены) — повтор бесполезен")
                    if k < steps - 1:  # пауза как между попытками: рынку — время дойти до цены
                        self.sleep(self.cfg.multileg_step_delay_s)
                    continue  # следующий шаг может дать другую цену
                last_price = price
                # портфель проверен для сделки целиком (precheck) — по ноге не проверяем, иначе покупка,
                # увеличивающая дельту, упрётся в лимит посреди сделки; биржа и цена — на каждой попытке
                try:
                    link = self.link(f.leg)
                except Exception as e:  # orderLinkId не построен — ордер точно не отправлен
                    raise RiskError(f"orderLinkId: {e}") from e
                req = OrderRequest(symbol=f.symbol, side=f.side, qty=left, price=price, time_in_force="IOC",
                                   reduce_only=reduce_only, link_id=link, portfolio_checked=True,
                                   quote=t)  # тот же тикер, по которому посчитана цена: без второго запроса
                f.attempts += 1
                ts0 = int(_now() * 1000)
                try:
                    o = self._send(req)
                except UnknownState as e:
                    self._seen(f, e.order)  # уже исполненное в незавершённом ордере — в итог и «осталось»
                    f.log_try("ioc", t, price, e.order, str(e))
                    raise
                finally:
                    if req.sent:  # отказ проверки до отправки — не начало ноги на бирже
                        f.mark_start(ts0)
                f.log_try("ioc", t, price, o)
            except (RiskError, BybitError, ValueError) as e:
                if isinstance(e, ValueError) and req is not None and req.sent:
                    raise UnknownState(f"{f.symbol}: ордер {req.link_id} ушёл, ответ не разобран — {e}") from e
                # ValueError до отправки — инструмент не найден (resolve_symbol): ордер точно не ушёл
                # отказ проверки до отправки — не оставлять «призрак» «по сигналу #N». Отказ самой биржи
                # запись сохраняет: номер попытки не переиспользуется (биржа может помнить orderLinkId)
                self._forget(req)
                # сюда — только то, что точно не на бирже (отказ лимитов/биржи, сбой связи на котировке или
                # до отправки): прошлые попытки уже завершены. Ушедший ордер с неизвестным исходом — UnknownState
                err = f"сбой связи: {e}" if isinstance(e, BybitNetworkError) else str(e)
                if req is not None and req.sent:  # отказ биржи — попытка ушла: в журнал попыток
                    f.log_try("ioc", t, price, None, err)
                elif isinstance(e, BybitNetworkError) and k < steps - 1:
                    # сбой связи до отправки (котировка, стакан) — ордера нет: следующая попытка, а не откат
                    # всей сделки из-за мгновенного сбоя
                    log.warning("Нога %s, попытка %d: %s — повтор", f.symbol, k + 1, err)
                    self.sleep(self.cfg.multileg_step_delay_s)
                    continue
                f.error = err
                break
            f.filled += _d(o.get("cumExecQty"))
            f.value += _d(o.get("cumExecValue"))
            if f.filled < f.qty and k < steps - 1:
                self.sleep(self.cfg.multileg_step_delay_s)
        return f

    # ---------- сделка ----------
    def execute(self, legs: list[tuple[str, str, Decimal, float]]) -> ExecResult:
        """legs: (symbol, side, qty, цена в сигнале) в порядке сигнала (номер ноги = индекс).
        Всегда возвращает итог: непредвиденная ошибка и в конце (подсчёт статуса, премии) — «исход неизвестен»,
        чтобы вызывающий записал исполнение и срочно сообщил администратору."""
        planned = min(Decimal(str(q)) for _, _, q, _ in legs)
        fills = [LegFill(i, s, side, Decimal(str(q)), float(p)) for i, (s, side, q, p) in enumerate(legs)]
        res = ExecResult("complete", Decimal(0), planned, fills)
        try:
            return self._execute(legs, res)
        except Exception as e:
            log.exception("Многоногая сделка: сбой итога")
            note = f"внутренняя ошибка итога {type(e).__name__}: {e}"
            try:
                return self._unknown(res, note)
            except Exception:  # и итог «неизвестно» не построился — хотя бы статус и исполненное по ногам
                log.exception("Многоногая сделка: сбой итога «неизвестно»")
                res.status, res.note = "unknown", note
                closed: dict[str, Decimal] = {}  # уже закрытое reduce-only — не «осталось»
                for u in res.unwind:
                    closed[u.symbol] = closed.get(u.symbol, Decimal(0)) + u.filled
                res.left = [f"{f.side} {f.filled - closed.get(f.symbol, Decimal(0))} {f.symbol}" for f in fills
                            if f.filled - closed.get(f.symbol, Decimal(0)) > 0]
                return res

    def _execute(self, legs: list[tuple[str, str, Decimal, float]], res: ExecResult) -> ExecResult:
        fills, planned = res.legs, res.planned_qty
        target = planned
        try:
            prev = None
            for n, i in enumerate(safe_order(legs)):
                f = fills[i]
                f.qty = target
                if n == 0 and f.side == "Buy" and self.cfg.passive_first_leg_seconds > 0:
                    self._passive(f)
                self._fill(f)
                if prev is not None and prev.ended_ms is not None and f.started_ms is not None:
                    f.gap_ms = f.started_ms - prev.ended_ms
                prev = f
                target = min(target, f.filled)  # следующие ноги — ровно на исполненное
                if target <= 0:
                    break
        except UnknownState as e:
            return self._unknown(res, str(e))
        except Exception as e:  # непредвиденное (кривой ответ биржи): исполненное — как при неизвестном исходе
            log.exception("Многоногая сделка: сбой")
            return self._unknown(res, f"внутренняя ошибка {type(e).__name__}: {e}")
        self._mark_untried(fills)
        res.qty = target
        # лишнее сверх итога — закрыть: сначала откуп коротких, потом продажа длинных. Короткая не откупилась —
        # длинные того же типа (колл/пут) на её остаток не продаём: иначе останется непокрытая короткая
        excess = [f for f in fills if f.filled > target]
        naked = {"C": Decimal(0), "P": Decimal(0)}
        if excess:
            for f in sorted(excess, key=lambda f: f.side != "Sell"):
                kind = _kind(f.symbol)
                need = f.filled - target
                keep = Decimal(0)
                if f.side == "Buy" and naked.get(kind):
                    keep = min(need, naked[kind])  # оставить покрытие
                    naked[kind] -= keep
                    res.left.append(f"{f.side} {keep} {f.symbol} (покрывает неоткупленную короткую)")
                    need -= keep
                    if need <= 0:
                        continue
                u = LegFill(f.leg, f.symbol, "Buy" if f.side == "Sell" else "Sell", need)
                try:
                    self._fill(u, reduce_only=True)
                except UnknownState as e:  # исход закрытия неизвестен — дальше не торгуем (как в сделке)
                    u.error = str(e)
                    res.unwind.append(u)
                    return self._unknown(res, f"закрытие лишнего: {e}")
                except Exception as e:  # непредвиденное — исход ушедшей попытки неизвестен: дальше не торгуем
                    log.exception("Многоногая сделка: сбой закрытия лишнего")
                    u.error = f"внутренняя ошибка {type(e).__name__}: {e}"
                    res.unwind.append(u)
                    return self._unknown(res, f"закрытие лишнего: {u.error}")
                res.unwind.append(u)
                if u.filled < u.qty:
                    res.left.append(f"{f.side} {u.qty - u.filled} {f.symbol}")
                    if f.side == "Sell" and kind in naked:
                        naked[kind] += u.qty - u.filled
        if target <= 0:
            nothing = all(f.filled == 0 for f in fills)
            res.status = "none" if nothing else ("stuck" if res.left else "unwound")
            first_err = next((f"{f.symbol}: {f.error}" for f in fills if f.error and f.qty > 0), "")
            res.note = first_err or "не исполнилось по цене в пределах проскальзывания"
        elif target < planned or res.left:
            res.status = "stuck" if res.left else "partial"
        self._nets(res)
        return res

    @staticmethod
    def _mark_untried(fills: list[LegFill]) -> None:
        for f in fills:
            if f.attempts == 0 and f.filled == 0 and not f.error:
                f.qty, f.error = Decimal(0), "не выставлялась"  # предыдущая нога не исполнилась или стоп

    def _unknown(self, res: ExecResult, note: str) -> ExecResult:
        """Исход неизвестен: дальше не торгуем и не закрываем (неизвестно что) — всё исполненное админу,
        с учётом уже закрытого (известные исполнения закрытия вычитаются)."""
        self._mark_untried(res.legs)
        res.status, res.note = "unknown", note
        res.qty = min((f.filled for f in res.legs), default=Decimal(0))  # невыставленная нога — 0: стратегии нет
        closed: dict[str, Decimal] = {}
        for u in res.unwind:
            closed[u.symbol] = closed.get(u.symbol, Decimal(0)) + u.filled
        res.left = []
        for f in res.legs:
            n = f.filled - closed.get(f.symbol, Decimal(0))
            if n > 0:
                res.left.append(f"{f.side} {n} {f.symbol}")
        self._nets(res)
        return res

    @staticmethod
    def _nets(res: ExecResult) -> None:
        q = float(res.qty)  # премия: продажа +, покупка −
        res.planned_net = sum(-float(SIDE_SIGN[f.side]) * f.plan_price * q for f in res.legs)
        res.actual_net = sum(-float(SIDE_SIGN[f.side]) * float(f.avg) * q for f in res.legs if f.filled > 0)
        res.cash = sum(-float(SIDE_SIGN[f.side]) * float(f.value) for f in res.legs + res.unwind)


def _kind(symbol: str) -> str:
    """BTC-27NOV26-60000-P-USDT -> 'P' (нестандартный символ — '')."""
    p = parse_option_symbol(symbol)
    return p[3] if p else ""


def fmt_result(res: ExecResult, sig_id: int) -> str:
    names = {"complete": "✅ Исполнено полностью", "partial": "⚠️ Исполнено частично",
             "none": "⛔ Не исполнено", "unwound": "↩️ Не собралось — исполненное закрыто",
             "stuck": "⚠️ Не собралось — часть позиции осталась", "unknown": "⚠️ Исход неизвестен (сбой связи)"}
    lines = [f"{names[res.status]} · сигнал #{sig_id}", f"Кол-во стратегии: {res.qty} из {res.planned_qty}"]
    for f in res.legs:
        avg = f" @ {px(f.avg, dash=False)}" if f.filled else ""
        err = f" — {f.error}" if f.error else ""
        mid = f" mid {px(f.tries[0]['mid'], dash=False)}," if f.tries else ""  # mid при первой отправке
        lines.append(f" {f.side.upper():4} {f.filled}/{f.qty} {f.symbol}{avg} (в сигнале {f.plan_price:g},{mid} "
                     f"попыток {f.attempts}){err}")
    gaps = [f"{f.gap_ms / 1000:.1f}" for f in sorted(res.legs, key=lambda f: f.started_ms or 0)
            if f.gap_ms is not None]
    if gaps:
        lines.append("Пауза между ногами, с: " + ", ".join(gaps))
    for u in res.unwind:
        avg = f" @ {px(u.avg, dash=False)}" if u.filled else ""
        lines.append(f" закрытие: {u.side.upper()} {u.filled}/{u.qty} {u.symbol}{avg}")
    if res.qty > 0:
        diff = res.actual_net - res.planned_net
        kind = "Кредит" if res.actual_net >= 0 else "Дебет"
        lines.append(f"{kind}: {abs(res.actual_net):.2f} USDT (по ценам сигнала {abs(res.planned_net):.2f}, "
                     f"разница {diff:+.2f})")
    extra = res.cash - res.actual_net
    if res.status == "unknown":
        if res.cash:
            lines.append(f"Премия по исполненному: {res.cash:+.2f} USDT (без комиссий)")
    elif abs(extra) >= 0.005:
        lines.append(f"Закрытие лишнего: {extra:+.2f} USDT премии (без комиссий)")
    if res.left:
        lines.append("Осталось несбалансированным: " + "; ".join(res.left) + " — проверьте /pos")
    if res.note:
        lines.append(res.note)
    return "\n".join(lines)
