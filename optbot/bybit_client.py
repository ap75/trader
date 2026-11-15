"""Тонкая обёртка над pybit для опционов Bybit (API V5, category=option).

Все методы синхронные; в Telegram-боте вызываются через asyncio.to_thread.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_DOWN, ROUND_UP, Decimal

from pybit.unified_trading import HTTP

from .config import Settings, settings as default_settings

CATEGORY = "option"


class BybitError(RuntimeError):
    pass


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
        if resp.get("retCode") != 0:
            raise BybitError(f"{resp.get('retCode')}: {resp.get('retMsg')}")
        return resp["result"]

    def _call(self, fn, **params) -> dict:
        try:
            return self._ok(fn(**params))
        except BybitError:
            raise
        except Exception as e:  # pybit бросает свои исключения при retCode != 0
            raise BybitError(str(e)) from e

    # ---------- справочник инструментов ----------
    def load_instruments(self, base: str, force: bool = False) -> list[Instrument]:
        with self._instr_lock:
            return self._load_instruments(base.upper(), force)

    def _load_instruments(self, base: str, force: bool) -> list[Instrument]:
        fresh = time.time() - self._instr_loaded_at.get(base, 0) < self.INSTRUMENTS_TTL
        if fresh and not force:
            return [i for i in self._instruments.values() if i.base == base]

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
                parts = it["symbol"].split("-")  # BTC-27MAR26-70000-P-USDT
                out.append(Instrument(
                    symbol=it["symbol"],
                    base=base,
                    option_type=it.get("optionsType") or ("Call" if parts[3] == "C" else "Put"),
                    strike=_d(parts[2]),
                    expiry_ms=int(it["deliveryTime"]),
                    tick_size=_d(pf.get("tickSize")),
                    qty_step=_d(lot.get("qtyStep")),
                    min_qty=_d(lot.get("minOrderQty")),
                    max_qty=_d(lot.get("maxOrderQty")),
                ))
            cursor = res.get("nextPageCursor")
            if not cursor:
                break

        for sym in [s for s, i in self._instruments.items() if i.base == base]:
            del self._instruments[sym]
        self._instruments.update({i.symbol: i for i in out})
        self._instr_loaded_at[base] = time.time()
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
        with self._instr_lock:
            self.load_instruments(base)
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

    def chain(self, base: str, expiry_code: str, strikes_around_atm: int = 6) -> dict:
        """Цепочка опционов: строки по страйкам вокруг ATM, с котировками и греками."""
        rows = self.tickers(base, expiry_code)
        if not rows:
            raise BybitError(f"Нет данных для {base} {expiry_code}")
        underlying = _d(rows[0].get("underlyingPrice"))
        by_strike: dict[Decimal, dict] = {}
        for r in rows:
            _, _, k, cp, *_ = r["symbol"].split("-")
            by_strike.setdefault(_d(k), {})["call" if cp == "C" else "put"] = r
        strikes = sorted(by_strike)
        atm_idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - underlying))
        lo, hi = max(0, atm_idx - strikes_around_atm), atm_idx + strikes_around_atm + 1
        return {
            "base": base.upper(),
            "expiry": expiry_code.upper(),
            "underlying": underlying,
            "atm_strike": strikes[atm_idx],
            "rows": [(k, by_strike[k].get("call"), by_strike[k].get("put")) for k in strikes[lo:hi]],
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
        req.info = []
        if req.side not in ("Buy", "Sell"):
            raise RiskError("side должен быть Buy или Sell")
        buy = req.side == "Buy"

        if inst.qty_step > 0:
            req.qty = (req.qty / inst.qty_step).to_integral_value(ROUND_DOWN) * inst.qty_step
        if req.qty <= 0 or (inst.min_qty and req.qty < inst.min_qty):
            raise RiskError(f"Количество меньше минимального ({inst.min_qty})")
        if inst.max_qty and req.qty > inst.max_qty:
            raise RiskError(f"Количество больше максимального ({inst.max_qty})")

        limit = self.cfg.max_order_qty.get(inst.base, Decimal(0))
        if limit > 0 and req.qty > limit and not req.reduce_only:
            raise RiskError(f"Превышен лимит MAX_QTY_{inst.base}={limit}")

        t = self.ticker(inst.symbol)
        mark = _d(t.get("markPrice"))
        bid, ask = _d(t.get("bid1Price")), _d(t.get("ask1Price"))

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
        else:
            # Market или ордер по IV — оцениваем премию по лучшей встречной цене
            premium = req.qty * ((ask if buy else bid) or mark)

        if not req.reduce_only and premium > self.cfg.max_order_premium_usdt:
            raise RiskError(
                f"Премия ~{premium:.2f} {self.cfg.settle_coin} > лимита MAX_ORDER_PREMIUM_USDT="
                f"{self.cfg.max_order_premium_usdt}"
            )

        if req.iv is None:
            self._check_book(req, inst, mark, bid, ask)
        else:
            req.info.append("по IV: цену считает биржа, проверка по стакану не выполняется")

        if not req.reduce_only:
            self._check_portfolio(req, inst, t, premium)
        return req

    def _check_book(self, req: OrderRequest, inst: Instrument, mark: Decimal, bid: Decimal, ask: Decimal):
        """Оценка немедленного исполнения по стакану и защита от опечаток/тонкого рынка."""
        buy = req.side == "Buy"
        market = req.order_type == "Market"
        if market:
            if not (ask if buy else bid):
                raise RiskError(f"Нет встречных заявок ({'ask' if buy else 'bid'}) — рыночный ордер невозможен")
            if bid > 0 and ask > 0:
                spread = (ask - bid) / ((ask + bid) / 2) * 100
                if spread > self.cfg.max_spread_pct:
                    raise RiskError(
                        f"Спред {spread:.1f}% > MAX_SPREAD_PCT={self.cfg.max_spread_pct}% — "
                        f"используйте лимитный ордер (bid {bid} / ask {ask})")
        else:
            crosses = (ask > 0 and req.price >= ask) if buy else (bid > 0 and req.price <= bid)
            if not crosses:
                req.info.append("не пересекает рынок — встанет в стакан")
                return

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
            return
        avg = cost / filled
        rest = f", остаток {left} встанет в стакан" if left > 0 else ""
        req.info.append(f"исполнение сейчас: {filled} по ~{avg:.4g} (худшая {worst}){rest}")

        if mark > 0:
            adverse = (worst - mark) if buy else (mark - worst)
            dev = adverse / mark * 100
            if dev > self.cfg.max_slippage_pct:
                raise RiskError(
                    f"Цена исполнения до {worst} хуже mark {mark:.4g} на {dev:.1f}% "
                    f"(> MAX_SLIPPAGE_PCT={self.cfg.max_slippage_pct}%). Опечатка? "
                    "Поставьте лимит ближе к mark или измените лимит в .env")

    def _check_portfolio(self, req: OrderRequest, inst: Instrument, t: dict, premium: Decimal):
        """Лимиты портфеля: позиция по символу, дельта по активу, премия за сутки. Только для открывающих ордеров."""
        sign = Decimal(1) if req.side == "Buy" else Decimal(-1)

        lim = self.cfg.max_position.get(inst.base, Decimal(0))
        if lim > 0:
            pos = next((p for p in self.positions(inst.base) if p["symbol"] == inst.symbol), None)
            cur = (_d(pos["size"]) if pos["side"] == "Buy" else -_d(pos["size"])) if pos else Decimal(0)
            pending = sum(
                (_d(o.get("leavesQty")) * (1 if o["side"] == "Buy" else -1)
                 for o in self.open_orders(inst.base)
                 if o["symbol"] == inst.symbol and not o.get("reduceOnly") and o["orderId"] != req.amend_of
                 and o["side"] == req.side),
                Decimal(0),
            )
            before = cur + pending
            after = before + sign * req.qty
            if abs(after) > lim and abs(after) > abs(before):
                raise RiskError(f"Позиция {inst.symbol} станет {after:+} (с учётом открытых ордеров {pending:+}) — "
                                f"больше MAX_POSITION_{inst.base}={lim}")
            req.info.append(f"позиция после: {after:+} (лимит {lim})")

        lim = self.cfg.max_net_delta.get(inst.base, Decimal(0))
        if lim > 0:
            g = next((x for x in self.greeks(inst.base) if x.get("baseCoin") == inst.base), {})
            before = _d(g.get("totalDelta"))
            after = before + sign * req.qty * _d(t.get("delta"))
            if abs(after) > lim and abs(after) > abs(before):
                raise RiskError(f"Дельта опционов {inst.base} станет {after:+.4f} — больше MAX_NET_DELTA_{inst.base}={lim}")
            req.info.append(f"Δ {inst.base} после: {after:+.4f} (лимит {lim})")

        lim = self.cfg.max_daily_premium_usdt
        if lim > 0:
            used = self.daily_premium_used(exclude_order_id=req.amend_of)
            if used + premium > lim:
                raise RiskError(f"Премия за сутки UTC: {used:.2f} + {premium:.2f} > MAX_DAILY_PREMIUM_USDT={lim}")
            req.info.append(f"премия за сутки: {used + premium:.2f} из {lim}")

    def daily_premium_used(self, exclude_order_id: str | None = None) -> Decimal:
        """Премия открывающих (не reduce-only) опционных ордеров с начала суток UTC:
        исполненная часть (cumExecValue) + неисполненный остаток активных ордеров (leavesQty * price)."""
        start_ms = int(time.time() // 86400 * 86400 * 1000)
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
        for o in self.open_orders():  # активные — свежие данные важнее истории
            if int(o.get("createdTime", 0)) >= start_ms:
                orders[o["orderId"]] = o
        total = Decimal(0)
        for oid, o in orders.items():
            if oid == exclude_order_id or o.get("reduceOnly"):
                continue
            total += _d(o.get("cumExecValue"))
            if o.get("orderStatus") in ("New", "PartiallyFilled", "Untriggered"):
                total += _d(o.get("leavesQty")) * _d(o.get("price"))
        return total

    def place_order(self, req: OrderRequest) -> dict:
        req = self.prepare_order(req)
        params = dict(
            category=CATEGORY,
            symbol=req.symbol,
            side=req.side,
            orderType=req.order_type,
            qty=str(req.qty),
            orderLinkId=f"ob-{uuid.uuid4().hex[:24]}",  # для опционов обязателен
            reduceOnly=req.reduce_only,
        )
        if req.order_type == "Limit":
            params["timeInForce"] = req.time_in_force
            if req.iv is not None:
                params["orderIv"] = str(req.iv)
            else:
                params["price"] = str(req.price)
        return self._call(self.http.place_order, **params)

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
        orig = next((o for o in self.open_orders(inst.base) if o["orderId"] == order_id), None)
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
        )
        return self.prepare_order(req), orig

    def amend_order(self, order_id: str, req: OrderRequest) -> dict:
        """Отправляет изменение, подготовленное amend_request (лимиты проверяются повторно)."""
        req = self.prepare_order(req)
        params = dict(category=CATEGORY, symbol=req.symbol, orderId=order_id, qty=str(req.qty))
        if req.iv is not None:
            params["orderIv"] = str(req.iv)
        else:
            params["price"] = str(req.price)
        return self._call(self.http.amend_order, **params)

    def cancel_all(self, base: str | None = None) -> dict:
        params = dict(category=CATEGORY)
        if base:
            params["baseCoin"] = base.upper()
        return self._call(self.http.cancel_all_orders, **params)

    def close_position_request(self, symbol: str, price: Decimal | None = None) -> OrderRequest:
        """Готовит reduce-only ордер на закрытие позиции целиком."""
        inst = self.resolve_symbol(symbol)
        pos = next((p for p in self.positions(inst.base) if p["symbol"] == inst.symbol), None)
        if not pos:
            raise RiskError(f"Нет открытой позиции по {inst.symbol}")
        side = "Sell" if pos["side"] == "Buy" else "Buy"
        return OrderRequest(
            symbol=inst.symbol, side=side, qty=_d(pos["size"]),
            order_type="Limit" if price else "Market", price=price, reduce_only=True,
            time_in_force="GTC",
        )
