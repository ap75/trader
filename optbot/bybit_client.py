"""Тонкая обёртка над pybit для опционов Bybit (API V5, category=option).

Все методы синхронные; в Telegram-боте вызываются через asyncio.to_thread.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_DOWN, ROUND_UP, Decimal

import requests
from pybit.exceptions import FailedRequestError, InvalidRequestError
from pybit.unified_trading import HTTP

from .config import Settings, settings as default_settings

log = logging.getLogger("optbot.client")

CATEGORY = "option"


class BybitError(RuntimeError):
    """Биржа однозначно отклонила запрос (retCode != 0)."""


# retCode, при которых биржа могла принять запрос (сбой/таймаут на её стороне)
UNCERTAIN_RET_CODES = {10000, 10016, 10019}
# паузы при выяснении статуса после сбоя связи, с
LOOKUP_DELAYS: tuple[float, ...] = (1, 2, 4, 8)
ACTIVE_STATUSES = {"Created", "New", "PartiallyFilled", "Untriggered"}  # Created — принят, ещё не в стакане


class BybitNetworkError(BybitError):
    """Сеть/таймаут/HTTP-ошибка: неизвестно, дошёл ли запрос до биржи (ордер мог быть выставлен)."""


class RiskError(ValueError):
    """Ордер отклонён локальными ограничениями безопасности."""


@dataclass
class Instrument:
    symbol: str
    base: str
    option_type: str  # Call / Put
    strike: Decimal
    expiry_ms: int
    tick_size: Decimal
    qty_step: Decimal
    min_qty: Decimal
    max_qty: Decimal

    @property
    def expiry_code(self) -> str:
        """'27MAR26' — как в названии символа."""
        return self.symbol.split("-")[1]

    @property
    def expiry_dt(self) -> datetime:
        return datetime.fromtimestamp(self.expiry_ms / 1000, tz=timezone.utc)


@dataclass
class OrderRequest:
    symbol: str
    side: str  # Buy / Sell
    qty: Decimal
    order_type: str = "Limit"  # Limit / Market
    price: Decimal | None = None
    iv: Decimal | None = None  # ордер по IV, напр. 0.55 = 55%
    reduce_only: bool = False
    time_in_force: str = "GTC"  # GTC / IOC / FOK / PostOnly
    amend_of: str | None = None  # orderId изменяемого ордера (не учитывается в лимитах портфеля)
    amend_filled: Decimal = Decimal(0)        # уже исполнено у изменяемого ордера (cumExecQty)
    amend_filled_value: Decimal = Decimal(0)  # и на какую сумму (cumExecValue)
    amend_created_ms: int = 0                 # createdTime изменяемого ордера
    sent: bool = False           # запрос на биржу ушёл (до этого сбой сети = ничего не изменилось)
    link_id: str | None = None   # orderLinkId — задаётся при отправке; по нему ищем ордер после сбоя сети
    ticker: dict | None = None   # тикер, полученный в prepare_order (для превью без повторного запроса)
    # Заполняет prepare_order: строки для превью (оценка исполнения, состояние лимитов)
    info: list[str] = field(default_factory=list)

    def describe(self) -> str:
        what = (
            "MARKET" if self.order_type == "Market"
            else f"IV {self.iv * 100:.1f}%" if self.iv is not None
            else f"@ {self.price}"
        )
        ro = " (reduce-only)" if self.reduce_only else ""
        return f"{self.side.upper()} {self.qty} {self.symbol} {what}{ro}"


def parse_option_symbol(sym: str) -> tuple[str, str, Decimal, str] | None:
    """'BTC-27NOV26-60000-P-USDT' -> ('BTC', '27NOV26', Decimal('60000'), 'P'); нестандартный символ -> None."""
    parts = sym.split("-")
    if len(parts) < 4 or parts[3] not in ("C", "P"):
        return None
    try:
        strike = Decimal(parts[2])
    except Exception:
        return None
    if not strike.is_finite() or strike <= 0:  # 'NaN', 'Infinity', '-1' — не страйк
        return None
    return parts[0], parts[1], strike, parts[3]


def group_by_strike(rows: list[dict]) -> dict[Decimal, dict[str, dict]]:
    """Тикеры одной экспирации -> {страйк: {'C': тикер колла, 'P': тикер пута}}. Нестандартные символы пропускаются."""
    out: dict[Decimal, dict[str, dict]] = {}
    for r in rows:
        p = parse_option_symbol(r.get("symbol", ""))
        if p:
            out.setdefault(p[2], {})[p[3]] = r
    return out


def spread_pct(bid, ask):
    """Спред в % от mid; None, если нет одной из сторон. Одна формула для риск-проверки и сборщика."""
    if not bid or not ask or bid <= 0 or ask <= 0:
        return None
    return (ask - bid) / ((ask + bid) / 2) * 100


def _d(x) -> Decimal:
    try:
        return Decimal(str(x)) if x not in (None, "") else Decimal(0)
    except Exception:
        return Decimal(0)


class OptionsClient:
    INSTRUMENTS_TTL = 300  # сек

    def __init__(self, cfg: Settings = default_settings):
        self.cfg = cfg
        self.http = HTTP(
            testnet=cfg.env == "testnet",
            demo=cfg.env == "demo",
            api_key=cfg.api_key or None,
            api_secret=cfg.api_secret or None,
            recv_window=10000,
        )
        self._instruments: dict[str, Instrument] = {}
        self._instr_loaded_at: dict[str, float] = {}
        self._instr_lock = threading.RLock()  # бот вызывает методы из разных потоков

    # ---------- низкий уровень ----------
    @staticmethod
    def _ok(resp: dict) -> dict:
        code = resp.get("retCode")
        if code != 0:
            cls = BybitNetworkError if code in UNCERTAIN_RET_CODES else BybitError
            raise cls(f"{code}: {resp.get('retMsg')}")
        return resp["result"]

    def _call(self, fn, **params) -> dict:
        try:
            return self._ok(fn(**params))
        except BybitError:
            raise
        except InvalidRequestError as e:  # retCode != 0: отказ биржи, кроме её внутренних сбоев
            cls = BybitNetworkError if e.status_code in UNCERTAIN_RET_CODES else BybitError
            raise cls(f"{e.status_code}: {e.message}") from e
        except (requests.exceptions.RequestException, FailedRequestError) as e:
            # таймаут/обрыв, HTTP 5xx, нечитаемый ответ (pybit оборачивает в FailedRequestError):
            # запрос мог дойти до биржи
            raise BybitNetworkError(f"{type(e).__name__}: {e}") from e
        except Exception as e:  # локальная ошибка до отправки (нет ключей, неверные параметры)
            raise BybitError(f"Ошибка запроса/конфигурации: {type(e).__name__}: {e}") from e

    # ---------- справочник инструментов ----------
    def load_instruments(self, base: str, force: bool = False) -> list[Instrument]:
        """Справочник с кэшем на INSTRUMENTS_TTL. Блокировка — только на чтение/замену кэша, не на сетевые
        запросы: долгая загрузка (сборщик, плохая связь) не задерживает торговые команды."""
        base = base.upper()
        with self._instr_lock:
            fresh = time.time() - self._instr_loaded_at.get(base, 0) < self.INSTRUMENTS_TTL
            if fresh and not force:
                return [i for i in self._instruments.values() if i.base == base]
        out = self._fetch_instruments(base)
        with self._instr_lock:
            for sym in [s for s, i in self._instruments.items() if i.base == base]:
                del self._instruments[sym]
            self._instruments.update({i.symbol: i for i in out})
            self._instr_loaded_at[base] = time.time()
        return out

    def _fetch_instruments(self, base: str) -> list[Instrument]:
        cursor, out = None, []
        while True:
            params = dict(category=CATEGORY, baseCoin=base, limit=1000)
            if cursor:
                params["cursor"] = cursor
            res = self._call(self.http.get_instruments_info, **params)
            for it in res.get("list", []):
                if it.get("settleCoin", self.cfg.settle_coin) != self.cfg.settle_coin:
                    continue
                if it.get("status") != "Trading":
                    continue
                lot, pf = it.get("lotSizeFilter", {}), it.get("priceFilter", {})
                parsed = parse_option_symbol(it.get("symbol", ""))  # BTC-27MAR26-70000-P-USDT
                if not parsed:
                    log.warning("Нестандартный символ в справочнике пропущен: %s", it.get("symbol"))
                    continue
                out.append(Instrument(
                    symbol=it["symbol"],
                    base=base,
                    option_type=it.get("optionsType") or ("Call" if parsed[3] == "C" else "Put"),
                    strike=parsed[2],
                    expiry_ms=int(it["deliveryTime"]),
                    tick_size=_d(pf.get("tickSize")),
                    qty_step=_d(lot.get("qtyStep")),
                    min_qty=_d(lot.get("minOrderQty")),
                    max_qty=_d(lot.get("maxOrderQty")),
                ))
            cursor = res.get("nextPageCursor")
            if not cursor:
                break
        return out

    def expiries(self, base: str) -> list[tuple[str, datetime]]:
        seen: dict[str, datetime] = {}
        for i in self.load_instruments(base):
            seen.setdefault(i.expiry_code, i.expiry_dt)
        return sorted(seen.items(), key=lambda kv: kv[1])

    def resolve_symbol(self, raw: str) -> Instrument:
        """Принимает 'BTC-27MAR26-70000-P-USDT', 'BTC-27MAR26-70000-P' или 'btc 27mar26 70000 p'."""
        s = "-".join(raw.upper().replace("_", " ").split())
        base = s.split("-")[0]
        if base not in self.cfg.base_coins:
            raise ValueError(f"Базовый актив {base} не в списке BASE_COINS")
        self.load_instruments(base)
        with self._instr_lock:
            for cand in (s, f"{s}-{self.cfg.settle_coin}"):
                if cand in self._instruments:
                    return self._instruments[cand]
        raise ValueError(f"Инструмент {raw} не найден (или не торгуется)")

    # ---------- рыночные данные ----------
    def tickers(self, base: str, expiry_code: str | None = None) -> list[dict]:
        params = dict(category=CATEGORY, baseCoin=base.upper())
        if expiry_code:
            params["expDate"] = expiry_code
        rows = self._call(self.http.get_tickers, **params).get("list", [])
        suffix = f"-{self.cfg.settle_coin}"
        if self.cfg.settle_coin != "USDC":
            rows = [r for r in rows if r["symbol"].endswith(suffix)]
        if expiry_code:
            rows = [r for r in rows if r["symbol"].split("-")[1] == expiry_code.upper()]
        return rows

    def ticker(self, symbol: str) -> dict:
        rows = self._call(self.http.get_tickers, category=CATEGORY, symbol=symbol).get("list", [])
        if not rows:
            raise BybitError(f"Нет тикера для {symbol}")
        return rows[0]

    def perp_ticker(self, base: str) -> dict | None:
        """Тикер линейного перпетуала BASEUSDT (markPrice, indexPrice, lastPrice)."""
        rows = self._call(self.http.get_tickers, category="linear", symbol=f"{base.upper()}USDT").get("list", [])
        return rows[0] if rows else None

    def chain(self, base: str, expiry_code: str, strikes_around_atm: int = 6) -> dict:
        """Цепочка опционов: строки по страйкам вокруг ATM, с котировками и греками."""
        rows = self.tickers(base, expiry_code)
        if not rows:
            raise BybitError(f"Нет данных для {base} {expiry_code}")
        underlying = _d(rows[0].get("underlyingPrice"))
        by_strike = group_by_strike(rows)
        if not by_strike:
            raise BybitError(f"Нет данных для {base} {expiry_code}")
        strikes = sorted(by_strike)
        atm_idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - underlying))
        lo, hi = max(0, atm_idx - strikes_around_atm), atm_idx + strikes_around_atm + 1
        return {
            "base": base.upper(),
            "expiry": expiry_code.upper(),
            "underlying": underlying,
            "atm_strike": strikes[atm_idx],
            "rows": [(k, by_strike[k].get("C"), by_strike[k].get("P")) for k in strikes[lo:hi]],
        }

    # ---------- аккаунт ----------
    def balance(self) -> dict:
        res = self._call(self.http.get_wallet_balance, accountType="UNIFIED")
        return res["list"][0] if res.get("list") else {}

    def positions(self, base: str | None = None) -> list[dict]:
        bases = [base.upper()] if base else self.cfg.base_coins
        out = []
        for b in bases:
            res = self._call(self.http.get_positions, category=CATEGORY, baseCoin=b, limit=200)
            out += [p for p in res.get("list", []) if _d(p.get("size")) != 0]
        return out

    def open_orders(self, base: str | None = None) -> list[dict]:
        bases = [base.upper()] if base else self.cfg.base_coins
        out = []
        for b in bases:
            res = self._call(self.http.get_open_orders, category=CATEGORY, baseCoin=b, limit=50)
            out += res.get("list", [])
        return out

    def _active_order(self, symbol: str, order_id: str) -> dict | None:
        """Активный ордер по orderId. ВНИМАНИЕ: при заданном orderId биржа игнорирует symbol
        (проверено на demo) — символ результата нужно сверять отдельно."""
        rows = self._call(self.http.get_open_orders, category=CATEGORY, symbol=symbol, orderId=order_id).get("list", [])
        # по orderId realtime-эндпоинт может вернуть и недавно закрытый ордер
        return next((o for o in rows if o.get("orderStatus") in ACTIVE_STATUSES), None)

    def get_order(self, symbol: str, order_id: str) -> dict | None:
        """Активный ордер по orderId и символу (без пагинации по всему списку)."""
        o = self._active_order(symbol, order_id)
        return o if o and o.get("symbol") == symbol else None

    def greeks(self, base: str | None = None) -> list[dict]:
        params = {"baseCoin": base.upper()} if base else {}
        return self._call(self.http.get_coin_greeks, **params).get("list", [])

    # ---------- торговля ----------
    def orderbook(self, symbol: str, depth: int = 25) -> dict:
        return self._call(self.http.get_orderbook, category=CATEGORY, symbol=symbol, limit=depth)

    def prepare_order(self, req: OrderRequest) -> OrderRequest:
        """Нормализует qty/price по шагам инструмента и проверяет все лимиты. Ничего не отправляет.

        Проверки: шаги и мин/макс биржи; MAX_QTY_*; MAX_ORDER_PREMIUM_USDT; по стакану — глубина
        для market, спред для market, проскальзывание худшей цены исполнения против mark;
        лимиты портфеля (позиция, дельта, премия за день) — кроме reduce-only.
        """
        inst = self.resolve_symbol(req.symbol)
        req.symbol = inst.symbol
        req.side = req.side.capitalize()
        req.order_type = req.order_type.capitalize()
        req.info = []
        if req.side not in ("Buy", "Sell"):
            raise RiskError("side должен быть Buy или Sell")
        if req.order_type not in ("Limit", "Market"):
            raise RiskError("order_type должен быть Limit или Market")
        if req.order_type == "Market" and (req.iv is not None or req.price is not None):
            raise RiskError("Рыночный ордер — без цены и IV")
        buy = req.side == "Buy"

        if inst.qty_step > 0:
            req.qty = (req.qty / inst.qty_step).to_integral_value(ROUND_DOWN) * inst.qty_step
        if req.qty <= 0 or (inst.min_qty and req.qty < inst.min_qty):
            raise RiskError(f"Количество меньше минимального ({inst.min_qty})")
        if req.amend_of and req.qty <= req.amend_filled:
            raise RiskError(f"Новое количество {req.qty} (после округления к шагу {inst.qty_step}) должно быть "
                            f"больше уже исполненного ({req.amend_filled})")
        if inst.max_qty and req.qty > inst.max_qty:
            raise RiskError(f"Количество больше максимального ({inst.max_qty})")

        limit = self.cfg.max_order_qty.get(inst.base, Decimal(0))
        if limit > 0 and req.qty > limit and not req.reduce_only:
            raise RiskError(f"Превышен лимит MAX_QTY_{inst.base}={limit}")

        t = self.ticker(inst.symbol)
        req.ticker = t
        mark = _d(t.get("markPrice"))
        bid, ask = _d(t.get("bid1Price")), _d(t.get("ask1Price"))
        orders_cache: dict = {}  # open_orders на время одной проверки

        if req.order_type == "Limit" and req.iv is None:
            if req.price is None:
                raise RiskError("Для лимитного ордера нужна цена или IV")
            if inst.tick_size > 0:
                # к шагу цены — в пассивную сторону: покупка вниз, продажа вверх (не делаем ордер агрессивнее)
                rounding = ROUND_DOWN if buy else ROUND_UP
                req.price = (req.price / inst.tick_size).to_integral_value(rounding) * inst.tick_size
            if req.price <= 0:
                raise RiskError("Цена должна быть > 0")
            premium = req.qty * req.price
            self._check_limit_price(req, mark, bid, ask)

        if req.iv is None:
            walk_cost = self._check_book(req, inst, mark, bid, ask)
        else:
            walk_cost = None
            req.info.append("по IV: цену считает биржа, проверка по стакану не выполняется")
        if req.order_type == "Market":
            premium = walk_cost  # _check_book для market всегда возвращает стоимость прохода или отказывает
        elif req.iv is not None:
            premium = req.qty * ((ask if buy else bid) or mark)  # оценка: цену по IV считает биржа

        if not req.reduce_only and premium > self.cfg.max_order_premium_usdt:
            raise RiskError(
                f"Премия ~{premium:.2f} {self.cfg.settle_coin} > лимита MAX_ORDER_PREMIUM_USDT="
                f"{self.cfg.max_order_premium_usdt}"
            )

        if not req.reduce_only:
            self._check_portfolio(req, inst, t, premium, orders_cache)
        return req

    def _check_limit_price(self, req: OrderRequest, mark: Decimal, bid: Decimal, ask: Decimal):
        """Опечатка в лимитной цене. Проверяется сама цена — неисполненный остаток будет стоять по ней.

        Открывающий ордер: покупка дороже mark (продажа дешевле) больше чем на MAX_SLIPPAGE_PCT — отказ.
        Закрывающий (reduce-only): сравнение с лучшей встречной котировкой (продажа — с bid, покупка — с ask),
        чтобы на широком спреде можно было закрыться по mid, но не «подарить» цену глубоко за рынком.
        """
        buy = req.side == "Buy"
        if req.reduce_only:
            ref, ref_name = ((ask, "ask") if buy else (bid, "bid"))
            if ref <= 0:
                ref, ref_name = mark, "mark"
        else:
            ref, ref_name = mark, "mark"
        if ref <= 0:
            return
        dev = ((req.price - ref) if buy else (ref - req.price)) / ref * 100
        if dev > self.cfg.max_slippage_pct:
            raise RiskError(
                f"Цена {req.price} {'выше' if buy else 'ниже'} {ref_name} {ref:.4g} на {dev:.1f}% "
                f"(> MAX_SLIPPAGE_PCT={self.cfg.max_slippage_pct}%). Опечатка? "
                "Поставьте цену ближе к рынку или измените лимит в .env")

    def _check_book(self, req: OrderRequest, inst: Instrument, mark: Decimal, bid: Decimal,
                    ask: Decimal) -> Decimal | None:
        """Оценка немедленного исполнения по стакану и защита от тонкого рынка.
        Возвращает стоимость немедленно исполняемой части (для market — всего ордера)."""
        buy = req.side == "Buy"
        market = req.order_type == "Market"
        if market:
            if not (ask if buy else bid):
                raise RiskError(f"Нет встречных заявок ({'ask' if buy else 'bid'}) — рыночный ордер невозможен")
            spread = spread_pct(bid, ask)
            if spread is not None:
                if spread > self.cfg.max_spread_pct:
                    raise RiskError(
                        f"Спред {spread:.1f}% > MAX_SPREAD_PCT={self.cfg.max_spread_pct}% — "
                        f"используйте лимитный ордер (bid {bid} / ask {ask})")
        else:
            crosses = (ask > 0 and req.price >= ask) if buy else (bid > 0 and req.price <= bid)
            if not crosses:
                req.info.append("не пересекает рынок — встанет в стакан")
                return None

        levels = self.orderbook(inst.symbol)["a" if buy else "b"]
        left, cost, worst = req.qty, Decimal(0), Decimal(0)
        for p_raw, size_raw in levels:
            p, size = _d(p_raw), _d(size_raw)
            if not market and (p > req.price if buy else p < req.price):
                break
            take = min(left, size)
            cost += take * p
            worst = p
            left -= take
            if left <= 0:
                break
        filled = req.qty - left
        if market and left > 0:
            raise RiskError(f"В стакане ({len(levels)} уровней) можно исполнить только {filled} из {req.qty} — "
                            "уменьшите количество или используйте лимит")
        if filled <= 0:
            req.info.append("не пересекает рынок — встанет в стакан")
            return None
        avg = cost / filled
        rest = f", остаток {left} встанет в стакан" if left > 0 else ""
        req.info.append(f"исполнение сейчас: {filled} по ~{avg:.4g} (худшая {worst}){rest}")

        if market and mark > 0:  # лимитки: worst не хуже их цены, а цену проверяет _check_limit_price
            adverse = (worst - mark) if buy else (mark - worst)
            dev = adverse / mark * 100
            if dev > self.cfg.max_slippage_pct:
                raise RiskError(
                    f"Цена исполнения до {worst} хуже mark {mark:.4g} на {dev:.1f}% "
                    f"(> MAX_SLIPPAGE_PCT={self.cfg.max_slippage_pct}%). Опечатка? "
                    "Поставьте лимит ближе к mark или измените лимит в .env")
        return cost

    def _check_portfolio(self, req: OrderRequest, inst: Instrument, t: dict, premium: Decimal,
                         orders_cache: dict):
        """Лимиты портфеля: позиция по символу, дельта по активу, премия за сутки. Только для открывающих ордеров.

        При amend исполненная часть изменяемого ордера уже сидит в позиции/дельте/премии,
        поэтому добавляется только неисполненный остаток (qty - cumExecQty).
        """
        sign = Decimal(1) if req.side == "Buy" else Decimal(-1)
        new_qty = req.qty - req.amend_filled

        def all_orders() -> list[dict]:
            if "all" not in orders_cache:
                orders_cache["all"] = self.open_orders()
            return orders_cache["all"]

        def base_orders() -> list[dict]:
            if self.cfg.max_daily_premium_usdt > 0 or "all" in orders_cache:  # всё равно понадобится полный список
                return [o for o in all_orders() if o["symbol"].startswith(inst.base + "-")]
            if inst.base not in orders_cache:
                orders_cache[inst.base] = self.open_orders(inst.base)
            return orders_cache[inst.base]

        lim = self.cfg.max_position.get(inst.base, Decimal(0))
        if lim > 0:
            pos = next((p for p in self.positions(inst.base) if p["symbol"] == inst.symbol), None)
            cur = (_d(pos["size"]) if pos["side"] == "Buy" else -_d(pos["size"])) if pos else Decimal(0)
            pending = sum(
                (_d(o.get("leavesQty")) * (1 if o["side"] == "Buy" else -1)
                 for o in base_orders()
                 if o["symbol"] == inst.symbol and not o.get("reduceOnly") and o["orderId"] != req.amend_of
                 and o["side"] == req.side),
                Decimal(0),
            )
            before = cur + pending
            after = before + sign * new_qty
            if abs(after) > lim and abs(after) > abs(before):
                raise RiskError(f"Позиция {inst.symbol} станет {after:+} (с учётом открытых ордеров {pending:+}) — "
                                f"больше MAX_POSITION_{inst.base}={lim}")
            req.info.append(f"позиция после: {after:+} (лимит {lim})")

        lim = self.cfg.max_net_delta.get(inst.base, Decimal(0))
        if lim > 0:
            g = next((x for x in self.greeks(inst.base) if x.get("baseCoin") == inst.base), {})
            before = _d(g.get("totalDelta"))
            after = before + sign * new_qty * _d(t.get("delta"))
            if abs(after) > lim and abs(after) > abs(before):
                raise RiskError(f"Дельта опционов {inst.base} станет {after:+.4f} — больше MAX_NET_DELTA_{inst.base}={lim}")
            req.info.append(f"Δ {inst.base} после: {after:+.4f} (лимит {lim})")

        lim = self.cfg.max_daily_premium_usdt
        if lim > 0:
            used = self.daily_premium_used(exclude_order_id=req.amend_of, open_orders=all_orders())
            # для amend: остаток — по новой цене; исполненная часть — по факту, если ордер создан сегодня
            # (вчерашние ордера в сегодняшний лимит не входят — как и в daily_premium_used)
            filled_today = req.amend_filled_value if req.amend_created_ms >= self._day_start_ms() else Decimal(0)
            add = (premium * new_qty / req.qty if req.qty else premium) + filled_today
            if used + add > lim:
                raise RiskError(f"Премия за сутки UTC: {used:.2f} + {add:.2f} > MAX_DAILY_PREMIUM_USDT={lim}")
            req.info.append(f"премия за сутки: {used + add:.2f} из {lim}")

    @staticmethod
    def _day_start_ms() -> int:
        return int(time.time() // 86400 * 86400 * 1000)

    def daily_premium_used(self, exclude_order_id: str | None = None,
                           open_orders: list[dict] | None = None) -> Decimal:
        """Премия открывающих (не reduce-only) опционных ордеров с начала суток UTC:
        исполненная часть (cumExecValue) + неисполненный остаток активных ордеров (leavesQty * price)."""
        start_ms = self._day_start_ms()
        orders: dict[str, dict] = {}
        cursor = None
        while True:
            params = dict(category=CATEGORY, startTime=start_ms, limit=50)
            if cursor:
                params["cursor"] = cursor
            res = self._call(self.http.get_order_history, **params)
            for o in res.get("list", []):
                orders[o["orderId"]] = o
            cursor = res.get("nextPageCursor")
            if not cursor or not res.get("list"):
                break
        for o in (open_orders if open_orders is not None else self.open_orders()):  # активные — свежее истории
            if int(o.get("createdTime", 0)) >= start_ms:
                orders[o["orderId"]] = o
        total = Decimal(0)
        for oid, o in orders.items():
            if oid == exclude_order_id or o.get("reduceOnly"):
                continue
            total += _d(o.get("cumExecValue"))
            if o.get("orderStatus") in ACTIVE_STATUSES:
                total += _d(o.get("leavesQty")) * _d(o.get("price"))
        return total

    def place_order(self, req: OrderRequest) -> dict:
        req = self.prepare_order(req)
        req.link_id = f"ob-{uuid.uuid4().hex[:24]}"
        params = dict(
            category=CATEGORY,
            symbol=req.symbol,
            side=req.side,
            orderType=req.order_type,
            qty=str(req.qty),
            orderLinkId=req.link_id,  # для опционов обязателен; по нему ищем ордер после сбоя сети
            reduceOnly=req.reduce_only,
        )
        if req.order_type == "Limit":
            params["timeInForce"] = req.time_in_force
            if req.iv is not None:
                params["orderIv"] = str(req.iv)
            else:
                params["price"] = str(req.price)
        req.sent = True
        return self._call(self.http.place_order, **params)

    def find_order_by_link(self, symbol: str, link_id: str) -> dict | None:
        """Ордер по orderLinkId среди активных и в истории (для выяснения статуса после сбоя сети)."""
        for fn in (self.http.get_open_orders, self.http.get_order_history):
            rows = self._call(fn, category=CATEGORY, symbol=symbol, orderLinkId=link_id).get("list", [])
            if rows:
                return rows[0]
        return None

    @staticmethod
    def _poll(check, delays: tuple[float, ...]):
        """Опрос с паузами: check() -> (готово, результат). Возвращает (результат | None, последняя ошибка)."""
        last_err = None
        for d in delays:
            time.sleep(d)
            try:
                done, res = check()
            except BybitError as e:
                last_err = e
                continue
            if done:
                return res, last_err
        return None, last_err

    def order_status_after_failure(self, req: OrderRequest, delays: tuple[float, ...] = LOOKUP_DELAYS) -> str:
        """Статус ордера после сбоя связи. Ищет по orderLinkId с паузами: запрос мог ещё обрабатываться.
        Никогда не утверждает «не выставлен» — только «найден» или «пока не найден»."""
        if not req.sent or not req.link_id:
            return "ордер не отправлен (сбой при проверке перед отправкой)"

        def check():
            o = self.find_order_by_link(req.symbol, req.link_id)
            return bool(o), o
        o, err = self._poll(check, delays)
        if o:
            return (f"ордер НА БИРЖЕ: {o.get('orderStatus')}, исполнено {o.get('cumExecQty')} из {o.get('qty')}, "
                    f"orderId {o.get('orderId')}")
        tail = f" (последняя ошибка поиска: {err})" if err else ""
        return f"ордер {req.link_id} пока не найден — статус НЕИЗВЕСТЕН{tail}. Проверьте /orders через минуту"

    def amend_status_after_failure(self, order_id: str, req: OrderRequest,
                                   delays: tuple[float, ...] = LOOKUP_DELAYS) -> str:
        """Статус изменения после сбоя связи: ждёт, пока ордер примет новые qty/цену.
        Если так и не видно — «статус НЕИЗВЕСТЕН», не предлагая повторить."""
        if not req.sent:
            return "изменение не отправлено (сбой при проверке перед отправкой) — ордер прежний"
        state: dict = {}

        def matches(o: dict) -> bool:
            if _d(o.get("qty")) != req.qty:
                return False
            if req.iv is not None:  # биржа может хранить IV с другой точностью
                return abs(_d(o.get("orderIv")) - req.iv) <= Decimal("0.0005")
            return _d(o.get("price")) == req.price

        def check():
            o = self.get_order(req.symbol, order_id)
            state["last"] = o
            return (o is None or matches(o)), o

        o, err = self._poll(check, delays)
        if "last" in state and state["last"] is None:
            return "ордер больше не активен (исполнен или отменён) — проверьте позиции"
        if o:
            return (f"изменение ПРИМЕНЕНО: {o.get('side')} {o.get('qty')} @ {o.get('price')}, "
                    f"исполнено {o.get('cumExecQty')}")
        last = state.get("last")
        if last:
            return (f"изменение не видно, ордер сейчас: {last.get('qty')} @ {last.get('price')}, "
                    f"исполнено {last.get('cumExecQty')} — статус НЕИЗВЕСТЕН, проверьте ордера через минуту")
        tail = f" (ошибка: {err})" if err else ""
        return f"статус НЕИЗВЕСТЕН{tail} — проверьте ордера через минуту"

    def cancel_order(self, symbol: str, order_id: str) -> dict:
        return self._call(self.http.cancel_order, category=CATEGORY, symbol=symbol, orderId=order_id)

    def amend_request(self, symbol: str, order_id: str, qty: Decimal | None = None,
                      price: Decimal | None = None, iv: Decimal | None = None) -> tuple[OrderRequest, dict]:
        """Готовит изменение открытого ордера: новый запрос (проверенный лимитами) + исходный ордер.

        Не заданные параметры берутся из текущего ордера. Ничего не отправляет.
        """
        if qty is None and price is None and iv is None:
            raise RiskError("Укажите новую цену, IV или количество")
        if price is not None and iv is not None:
            raise RiskError("Либо цена, либо IV")
        inst = self.resolve_symbol(symbol)
        orig = self._active_order(inst.symbol, order_id)
        if not orig:
            raise RiskError(f"Открытый ордер {order_id} не найден")
        if orig.get("symbol") != inst.symbol:
            raise RiskError(f"Ордер {order_id} относится к {orig.get('symbol')}, а не к {inst.symbol}")
        if orig.get("orderType") != "Limit":
            raise RiskError("Изменять можно только лимитные ордера")
        req = OrderRequest(
            symbol=inst.symbol, side=orig["side"],
            qty=qty if qty is not None else _d(orig["qty"]),
            price=None if iv is not None else (price if price is not None else _d(orig["price"])),
            iv=iv, reduce_only=bool(orig.get("reduceOnly")), amend_of=order_id,
            amend_filled=_d(orig.get("cumExecQty")), amend_filled_value=_d(orig.get("cumExecValue")),
            amend_created_ms=int(orig.get("createdTime") or 0),
        )
        req = self.prepare_order(req)
        same_qty = req.qty == _d(orig["qty"])
        same_px = (abs(req.iv - _d(orig.get("orderIv"))) <= Decimal("0.0005")) if req.iv is not None \
            else req.price == _d(orig.get("price"))
        if same_qty and same_px:  # иначе после сбоя связи «как было» неотличимо от «применено»
            raise RiskError("Ничего не меняется: цена (IV) и количество совпадают с текущими")
        return req, orig

    def amend_order(self, order_id: str, req: OrderRequest) -> dict:
        """Отправляет изменение, подготовленное amend_request (лимиты проверяются повторно
        по свежему состоянию ордера: между превью и подтверждением он мог частично исполниться)."""
        cur = self.get_order(req.symbol, order_id)
        if not cur:
            raise RiskError(f"Ордер {order_id} больше не активен (исполнен или отменён)")
        req.amend_filled = _d(cur.get("cumExecQty"))
        req.amend_filled_value = _d(cur.get("cumExecValue"))
        req = self.prepare_order(req)
        params = dict(category=CATEGORY, symbol=req.symbol, orderId=order_id, qty=str(req.qty))
        if req.iv is not None:
            params["orderIv"] = str(req.iv)
        else:
            params["price"] = str(req.price)
        req.sent = True
        return self._call(self.http.amend_order, **params)

    def cancel_all(self, base: str | None = None) -> dict:
        params = dict(category=CATEGORY)
        if base:
            params["baseCoin"] = base.upper()
        return self._call(self.http.cancel_all_orders, **params)

    def close_position_request(self, symbol: str, price: Decimal | None = None) -> OrderRequest:
        """Готовит reduce-only ордер на закрытие позиции целиком. price=None — по рынку."""
        if price is not None and price <= 0:
            raise RiskError("Цена закрытия должна быть > 0 (без цены — по рынку)")
        inst = self.resolve_symbol(symbol)
        pos = next((p for p in self.positions(inst.base) if p["symbol"] == inst.symbol), None)
        if not pos:
            raise RiskError(f"Нет открытой позиции по {inst.symbol}")
        side = "Sell" if pos["side"] == "Buy" else "Buy"
        return OrderRequest(
            symbol=inst.symbol, side=side, qty=_d(pos["size"]),
            order_type="Market" if price is None else "Limit", price=price, reduce_only=True,
            time_in_force="GTC",
        )
