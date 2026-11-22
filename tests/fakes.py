"""Подставной pybit HTTP: те же имена методов и формат ответов, что у Bybit V5 (сверено на demo)."""
from __future__ import annotations

import math
import time
import uuid
from decimal import Decimal
from datetime import datetime, timezone

from pybit.exceptions import InvalidRequestError

from optbot.fmt import DAY_MS  # noqa: E402

EXP = "27NOV26"
EXP_MS = int(datetime(2026, 11, 27, 8, tzinfo=timezone.utc).timestamp() * 1000)
NOW = EXP_MS - 4 * DAY_MS  # для сигналов: до 27NOV26 — 4 дня (в окне 2–7)
BTC_PUT = f"BTC-{EXP}-60000-P-USDT"
BTC_CALL = f"BTC-{EXP}-70000-C-USDT"
ETH_CALL = f"ETH-{EXP}-2000-C-USDT"

SPECS = {  # base: (страйки, tickSize, qtyStep, minQty, maxQty)
    "BTC": ([60000, 65000, 70000], "5", "0.01", "0.01", "100"),
    "ETH": ([1800, 2000, 2200], "0.1", "0.1", "0.1", "1000"),
    "SOL": ([60, 70, 80], "0.01", "1", "1", "10000"),
}


def kline_rows(closes: list[float], interval_min: int, now_ms: int) -> list[list[str]]:
    """Свечи как у Bybit: от новых к старым, первая — текущая незакрытая (её close — последний в closes)."""
    step = interval_min * 60_000
    cur = now_ms // step * step
    n = len(closes)
    return [[str(cur - (n - 1 - i) * step), str(c), str(c), str(c), str(c), "1", "1"]
            for i, c in reversed(list(enumerate(closes)))]


def ok(result: dict) -> dict:
    return {"retCode": 0, "retMsg": "OK", "result": result}


class FakeHTTP:
    def __init__(self):
        self.instruments = []
        self.tickers: dict[str, dict] = {}
        for base, (strikes, tick, step, mn, mx) in SPECS.items():
            for k in strikes:
                for cp in ("C", "P"):
                    sym = f"{base}-{EXP}-{k}-{cp}-USDT"
                    self.instruments.append({
                        "symbol": sym, "status": "Trading", "settleCoin": "USDT", "baseCoin": base,
                        "optionsType": "Call" if cp == "C" else "Put", "deliveryTime": str(EXP_MS),
                        "priceFilter": {"tickSize": tick},
                        "lotSizeFilter": {"qtyStep": step, "minOrderQty": mn, "maxOrderQty": mx},
                    })
                    self.tickers[sym] = {
                        "symbol": sym, "markPrice": "100", "bid1Price": "95", "ask1Price": "105",
                        "bid1Size": "10", "ask1Size": "10", "markIv": "0.5", "delta": "0.1" if cp == "C" else "-0.1",
                        "underlyingPrice": "65000" if base == "BTC" else "2000" if base == "ETH" else "70",
                        "indexPrice": "64990" if base == "BTC" else "1999" if base == "ETH" else "69.9",
                        "openInterest": "5",
                    }
        self.perp: dict[str, str] = {}         # symbol перпетуала -> markPrice
        self.funding_hist: dict[str, list[tuple[int, float]]] = {}  # symbol -> [(метка, ставка)]
        self.klines: dict[tuple[str, str], list] = {}  # (symbol, interval) -> строки kline, от новых к старым
        self.books: dict[str, dict] = {}       # symbol -> {"a": [[px, sz]], "b": [...]}; иначе из тикера
        self.positions: list[dict] = []
        self.orders: list[dict] = []           # активные
        self.history: list[dict] = []          # закрытые
        self.greeks: list[dict] = []
        self.executions: list[dict] = []       # execution/list
        self.deliveries: list[dict] = []       # asset/delivery-record
        self.delivery_px: dict[str, list[dict]] = {}  # base -> market/new-delivery-price
        self.page_size: int | None = None      # принудительная пагинация (проверка cursor)
        self.calls: list[tuple[str, dict]] = []
        self.fail: dict[str, Exception] = {}   # метод -> исключение (один раз)
        self.after: dict[str, Exception] = {}  # метод выполняется, затем бросает (обрыв после отправки)
        # IOC исполняется сразу против тикера (покупка ≥ ask, продажа ≤ bid); ioc_max — сколько максимум за раз
        self.ioc_max: dict[str, Decimal] = {}
        self.margin_mode = "REGULAR_MARGIN"

    # ---------- служебное ----------
    def _rec(self, name, kw):
        kw = {k: v for k, v in kw.items() if k != "self"}
        self.calls.append((name, kw))
        if name in self.fail:
            raise self.fail.pop(name)

    def called(self, name) -> list[dict]:
        return [kw for n, kw in self.calls if n == name]

    @staticmethod
    def _window(startTime, endTime, max_days):
        if startTime is not None and endTime is not None and endTime - startTime > max_days * DAY_MS:
            raise InvalidRequestError("req", f"The time range cannot exceed {max_days} days.", 10001, "0", {})

    def _page(self, rows, limit, cursor):
        size = min(limit, self.page_size or limit)
        start = int(cursor or 0)
        nxt = str(start + size) if start + size < len(rows) else ""
        return ok({"list": rows[start:start + size], "nextPageCursor": nxt, "category": "option"})

    @staticmethod
    def _in(rows, key, startTime, endTime):
        return [r for r in rows if (startTime is None or int(r[key]) >= startTime)
                and (endTime is None or int(r[key]) <= endTime)]

    def add_exec(self, symbol, side="Buy", qty="0.01", price="100", fee="0.1", t=None, link="ob-test",
                 exec_type="Trade", order_id="o1", exec_id=None) -> dict:
        e = {"execId": exec_id or str(uuid.uuid4()), "orderId": order_id, "orderLinkId": link, "symbol": symbol,
             "side": side, "execQty": qty, "execPrice": price, "execValue": str(Decimal(qty) * Decimal(price)),
             "execFee": fee, "execType": exec_type, "execTime": str(t if t is not None else int(time.time() * 1000)),
             "isMaker": False, "markPrice": price, "indexPrice": "65000", "tradeIv": "0.5", "leavesQty": "0"}
        self.executions.append(e)
        return e

    # ---------- история ----------
    def get_executions(self, category, startTime=None, endTime=None, limit=50, cursor=None, **kw):
        self._rec("get_executions", locals())
        self._window(startTime, endTime, 7)
        return self._page(self._in(self.executions, "execTime", startTime, endTime), limit, cursor)

    def get_option_delivery_record(self, category, startTime=None, endTime=None, limit=50, cursor=None, **kw):
        self._rec("get_option_delivery_record", locals())
        self._window(startTime, endTime, 30)
        return self._page(self._in(self.deliveries, "deliveryTime", startTime, endTime), limit, cursor)

    def get_new_delivery_price(self, category, baseCoin):
        self._rec("get_new_delivery_price", locals())
        return ok({"category": "option", "list": self.delivery_px.get(baseCoin, [])})

    def add_order(self, symbol, side="Buy", qty="0.05", price="20", filled="0", value="0", status="New",
                  reduce_only=False, created_ms=None, order_iv="", oid=None) -> dict:
        o = {"orderId": oid or str(uuid.uuid4()), "orderLinkId": "ob-test", "symbol": symbol, "side": side,
             "orderType": "Limit", "qty": qty, "price": price, "orderIv": order_iv,
             "leavesQty": str(Decimal(qty) - Decimal(filled)), "cumExecQty": filled, "cumExecValue": value,
             "orderStatus": status, "reduceOnly": reduce_only,
             "createdTime": str(created_ms if created_ms is not None else int(time.time() * 1000))}
        self.orders.append(o)
        return o

    # ---------- рынок ----------
    def get_instruments_info(self, category, baseCoin, limit=1000, cursor=None):
        self._rec("get_instruments_info", locals())
        return ok({"list": [i for i in self.instruments if i["baseCoin"] == baseCoin], "nextPageCursor": ""})

    def get_tickers(self, category, baseCoin=None, symbol=None, expDate=None):
        self._rec("get_tickers", locals())
        if category == "linear":  # перпетуал BASEUSDT
            return ok({"list": [{"symbol": symbol, "markPrice": self.perp.get(symbol, "65010"),
                                 "fundingRate": "0.0001", "fundingIntervalHour": "8",
                                 "openInterestValue": "6500000000", "openInterest": "100000"}]})
        if symbol:
            return ok({"list": [self.tickers[symbol]] if symbol in self.tickers else []})
        return ok({"list": [t for s, t in self.tickers.items() if s.startswith(baseCoin + "-")]})

    def get_funding_rate_history(self, category, symbol, startTime=None, endTime=None, limit=200):
        self._rec("get_funding_rate_history", locals())
        rows = [r for r in self.funding_hist.get(symbol, [])
                if (startTime is None or r[0] >= startTime) and (endTime is None or r[0] <= endTime)]
        rows = sorted(rows, reverse=True)[:limit]
        return ok({"category": category, "list": [{"symbol": symbol, "fundingRate": str(r),
                                                   "fundingRateTimestamp": str(t)} for t, r in rows]})

    def get_open_interest(self, category, symbol, intervalTime, limit=50, **kw):
        self._rec("get_open_interest", locals())
        now = int(time.time() * 1000) // 3_600_000 * 3_600_000
        rows = getattr(self, "oi_hist", None) or [100000 * (1 + 0.001 * i) for i in range(25)]  # старые -> новые
        n = len(rows)
        return ok({"symbol": symbol, "list": [{"openInterest": str(v), "timestamp": str(now - (n - 1 - i) * 3_600_000)}
                                              for i, v in reversed(list(enumerate(rows)))][:limit]})

    def get_kline(self, category, symbol, interval, limit=200, **kw):
        self._rec("get_kline", locals())
        rows = self.klines.get((symbol, str(interval)))
        if rows is None:  # по умолчанию — без тренда: колебания ±0.5% вокруг 65000
            step = int(interval) * 60_000
            cur = int(time.time() * 1000) // step * step
            rows = kline_rows([65000 * (1.005 if i % 2 else 1) for i in range(1000)], int(interval), cur + 1)
        if kw.get("end") is not None:   # листание истории назад (vol.update_candles)
            rows = [r for r in rows if int(r[0]) <= int(kw["end"])]
        return ok({"symbol": symbol, "category": category, "list": rows[:limit]})

    def get_orderbook(self, category, symbol, limit=25):
        self._rec("get_orderbook", locals())
        if symbol in self.books:
            return ok(self.books[symbol])
        t = self.tickers[symbol]
        a = [[t["ask1Price"], t["ask1Size"]]] if float(t["ask1Price"]) else []
        b = [[t["bid1Price"], t["bid1Size"]]] if float(t["bid1Price"]) else []
        return ok({"a": a, "b": b})

    # ---------- счёт ----------
    def get_account_info(self):
        self._rec("get_account_info", locals())
        return ok({"marginMode": self.margin_mode, "unifiedMarginStatus": 3})

    def get_wallet_balance(self, accountType):
        self._rec("get_wallet_balance", locals())
        return ok({"list": [{"totalEquity": "1000", "totalAvailableBalance": "900", "coin": []}]})

    def get_positions(self, category, baseCoin, limit=200, cursor=None):
        self._rec("get_positions", locals())
        return self._page([p for p in self.positions if p["symbol"].startswith(baseCoin + "-")], limit, cursor)

    def get_coin_greeks(self, baseCoin=None):
        self._rec("get_coin_greeks", locals())
        return ok({"list": [g for g in self.greeks if baseCoin in (None, g["baseCoin"])]})

    def get_open_orders(self, category, baseCoin=None, symbol=None, orderId=None, orderLinkId=None, limit=50,
                        cursor=None):
        self._rec("get_open_orders", locals())
        rows = self.orders
        if orderId:  # как на demo: при orderId символ игнорируется
            rows = [o for o in rows if o["orderId"] == orderId]
        else:
            if baseCoin:
                rows = [o for o in rows if o["symbol"].startswith(baseCoin + "-")]
            if symbol:
                rows = [o for o in rows if o["symbol"] == symbol]
            if orderLinkId:
                rows = [o for o in rows if o["orderLinkId"] == orderLinkId]
        return self._page(rows, limit, cursor)

    def get_order_history(self, category, symbol=None, orderLinkId=None, startTime=None, endTime=None, limit=50,
                          cursor=None, orderId=None):
        self._rec("get_order_history", locals())
        self._window(startTime, endTime, 7)
        rows = self.history + self.orders
        if orderId:  # как у Bybit: по id — без окна времени
            return ok({"list": [o for o in rows if o["orderId"] == orderId], "nextPageCursor": ""})
        if symbol:
            rows = [o for o in rows if o["symbol"] == symbol]
        if orderLinkId:
            rows = [o for o in rows if o["orderLinkId"] == orderLinkId]
        rows = self._in(rows, "createdTime", startTime, endTime)
        return self._page(rows, limit, cursor)

    # ---------- торговля ----------
    def place_order(self, **kw):
        self._rec("place_order", kw)
        o = {"orderId": str(uuid.uuid4()), "orderLinkId": kw["orderLinkId"], "symbol": kw["symbol"],
             "side": kw["side"], "orderType": kw["orderType"], "qty": kw["qty"], "price": kw.get("price", "0"),
             "orderIv": kw.get("orderIv", ""), "leavesQty": kw["qty"], "cumExecQty": "0", "cumExecValue": "0",
             "orderStatus": "New", "reduceOnly": kw["reduceOnly"], "createdTime": str(int(time.time() * 1000))}
        if any(x["orderLinkId"] == o["orderLinkId"] for x in self.orders + self.history):
            raise InvalidRequestError("req", "OrderLinkedID is duplicate", 110072, "0", {})
        if kw.get("timeInForce") == "IOC":
            self._ioc(o)
        elif kw.get("timeInForce") == "PostOnly" and self._crosses(o):  # как Bybit: взял бы ликвидность — снят
            o.update(leavesQty="0", orderStatus="Cancelled")
            self.history.append(o)
        else:
            self.orders.append(o)
        if "place_order" in self.after:
            raise self.after.pop("place_order")
        return ok({"orderId": o["orderId"], "orderLinkId": o["orderLinkId"]})

    def _ioc(self, o: dict) -> None:
        """Исполнить против тикера сразу, остаток снять — как IOC на бирже. Ордер — в историю."""
        t = self.tickers[o["symbol"]]
        qty = Decimal(o["qty"])
        touch = Decimal(t["ask1Price"] if o["side"] == "Buy" else t["bid1Price"])
        crosses = self._crosses(o)
        filled = min(qty, self.ioc_max.get(o["symbol"], qty)) if crosses else Decimal(0)
        o.update(cumExecQty=str(filled), cumExecValue=str(filled * touch), leavesQty="0",
                 orderStatus="Filled" if filled == qty else "PartiallyFilledCanceled" if filled else "Cancelled")
        self.history.append(o)

    def _crosses(self, o: dict) -> bool:
        t = self.tickers[o["symbol"]]
        price = Decimal(o["price"])
        touch = Decimal(t["ask1Price"] if o["side"] == "Buy" else t["bid1Price"])
        return touch > 0 and (price >= touch if o["side"] == "Buy" else price <= touch)

    def fill_resting(self, link_id: str, qty) -> None:
        """Исполнить стоящий ордер (частично или целиком) по его цене — «рынок пришёл к нам»."""
        o = next(o for o in self.orders if o["orderLinkId"] == link_id)
        q = Decimal(o["cumExecQty"]) + Decimal(str(qty))
        o.update(cumExecQty=str(q), cumExecValue=str(q * Decimal(o["price"])),
                 leavesQty=str(Decimal(o["qty"]) - q))
        if q >= Decimal(o["qty"]):
            o["orderStatus"] = "Filled"
            self.orders.remove(o)
            self.history.append(o)
        else:
            o["orderStatus"] = "PartiallyFilled"

    def amend_order(self, **kw):
        self._rec("amend_order", kw)
        o = next(o for o in self.orders if o["orderId"] == kw["orderId"])
        o["qty"] = kw["qty"]
        o["leavesQty"] = str(Decimal(kw["qty"]) - Decimal(o["cumExecQty"]))
        if "price" in kw:
            o["price"] = kw["price"]
        if "orderIv" in kw:
            o["orderIv"] = kw["orderIv"]
        if "amend_order" in self.after:
            raise self.after.pop("amend_order")
        return ok({"orderId": o["orderId"]})

    def cancel_order(self, category, symbol, orderId=None, orderLinkId=None):
        self._rec("cancel_order", locals())
        hit = [o for o in self.orders if (orderId and o["orderId"] == orderId) or
               (orderLinkId and o["orderLinkId"] == orderLinkId)]
        if not hit:  # как Bybit: 110001 order not exists or too late to cancel
            raise InvalidRequestError("req", "order not exists or too late to cancel", 110001, "0", {})
        for o in hit:  # снятый — в историю, как на бирже
            o.update(leavesQty="0", orderStatus="PartiallyFilledCanceled" if Decimal(o["cumExecQty"]) else "Cancelled")
            self.history.append(o)
        self.orders = [o for o in self.orders if o not in hit]
        return ok({"orderId": hit[0]["orderId"]})

    def cancel_all_orders(self, category, baseCoin=None):
        self._rec("cancel_all_orders", locals())
        gone = [o for o in self.orders if not baseCoin or o["symbol"].startswith(baseCoin + "-")]
        self.orders = [o for o in self.orders if o not in gone]
        return ok({"list": [{"orderId": o["orderId"]} for o in gone]})


# ---------- подставная цепочка по Блэку–Шоулзу ----------
def _n(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs(F, K, t, iv, kind):
    d1 = (math.log(F / K) + iv * iv * t / 2) / (iv * math.sqrt(t))
    d2 = d1 - iv * math.sqrt(t)
    if kind == "C":
        return F * _n(d1) - K * _n(d2), _n(d1)
    return K * _n(-d2) - F * _n(-d1), _n(d1) - 1


def set_chain(fake, base="BTC", F=65000.0, iv=0.5, step=500, n=20, spread=0.04, oi="50", exp=EXP,
              exp_ms=EXP_MS, now=NOW, min_qty="0.01", keep=False):
    """Заменяет опционы актива цепочкой с ценами и дельтами по Блэку–Шоулзу (keep — добавить серию к прежним)."""
    if not keep:
        fake.instruments = [i for i in fake.instruments if i["baseCoin"] != base]
        fake.tickers = {s: t for s, t in fake.tickers.items() if not s.startswith(base + "-")}
    t = (exp_ms - now) / DAY_MS / 365
    atm = round(F / step) * step
    for k in range(atm - n * step, atm + n * step + 1, step):
        for cp in ("C", "P"):
            sym = f"{base}-{exp}-{k}-{cp}-USDT"
            price, delta = bs(F, k, t, iv, cp)
            price = max(price, step * 0.002)
            fake.instruments.append({
                "symbol": sym, "status": "Trading", "settleCoin": "USDT", "baseCoin": base,
                "optionsType": "Call" if cp == "C" else "Put", "deliveryTime": str(exp_ms),
                "priceFilter": {"tickSize": "5"},
                "lotSizeFilter": {"qtyStep": min_qty, "minOrderQty": min_qty, "maxOrderQty": "100"}})
            fake.tickers[sym] = {
                "symbol": sym, "markPrice": f"{price:.2f}", "bid1Price": f"{price * (1 - spread / 2):.2f}",
                "ask1Price": f"{price * (1 + spread / 2):.2f}", "bid1Size": "10", "ask1Size": "10", "markIv": str(iv), "delta": f"{delta:.4f}",
                "gamma": "0.00001", "vega": "10", "theta": "-20", "underlyingPrice": str(F),
                "indexPrice": str(F - 10), "openInterest": oi}


def set_klines(fake, base="BTC", rv=0.4, trend_per_bar=0.0, now=NOW):
    """1h: колебания с годовой RV ≈ rv; 4h: рост/падение trend_per_bar за свечу (0 — колебания без тренда)."""
    a = rv / math.sqrt(365 * 24)
    h1 = [65000 * (1 + a if i % 2 else 1) for i in range(800)]  # RV30 — 720 ч
    if trend_per_bar:
        h4 = [65000 * (1 + trend_per_bar) ** i for i in range(200)]
    else:
        h4 = [65000 * (1.002 if i % 2 else 1) for i in range(200)]
    fake.klines[(f"{base}USDT", "60")] = kline_rows(h1, 60, now)
    fake.klines[(f"{base}USDT", "240")] = kline_rows(h4, 240, now)
