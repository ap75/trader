"""Подставной pybit HTTP: те же имена методов и формат ответов, что у Bybit V5 (сверено на demo)."""
from __future__ import annotations

import time
import uuid
from decimal import Decimal
from datetime import datetime, timezone

EXP = "27NOV26"
EXP_MS = int(datetime(2026, 11, 27, 8, tzinfo=timezone.utc).timestamp() * 1000)
BTC_PUT = f"BTC-{EXP}-60000-P-USDT"
BTC_CALL = f"BTC-{EXP}-70000-C-USDT"
ETH_CALL = f"ETH-{EXP}-2000-C-USDT"

SPECS = {  # base: (страйки, tickSize, qtyStep, minQty, maxQty)
    "BTC": ([60000, 65000, 70000], "5", "0.01", "0.01", "100"),
    "ETH": ([1800, 2000, 2200], "0.1", "0.1", "0.1", "1000"),
    "SOL": ([60, 70, 80], "0.01", "1", "1", "10000"),
}


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
        self.books: dict[str, dict] = {}       # symbol -> {"a": [[px, sz]], "b": [...]}; иначе из тикера
        self.positions: list[dict] = []
        self.orders: list[dict] = []           # активные
        self.history: list[dict] = []          # закрытые
        self.greeks: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self.fail: dict[str, Exception] = {}   # метод -> исключение (один раз)
        self.after: dict[str, Exception] = {}  # метод выполняется, затем бросает (обрыв после отправки)

    # ---------- служебное ----------
    def _rec(self, name, kw):
        kw = {k: v for k, v in kw.items() if k != "self"}
        self.calls.append((name, kw))
        if name in self.fail:
            raise self.fail.pop(name)

    def called(self, name) -> list[dict]:
        return [kw for n, kw in self.calls if n == name]

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
            return ok({"list": [{"symbol": symbol, "markPrice": self.perp.get(symbol, "65010")}]})
        if symbol:
            return ok({"list": [self.tickers[symbol]] if symbol in self.tickers else []})
        return ok({"list": [t for s, t in self.tickers.items() if s.startswith(baseCoin + "-")]})

    def get_orderbook(self, category, symbol, limit=25):
        self._rec("get_orderbook", locals())
        if symbol in self.books:
            return ok(self.books[symbol])
        t = self.tickers[symbol]
        a = [[t["ask1Price"], t["ask1Size"]]] if float(t["ask1Price"]) else []
        b = [[t["bid1Price"], t["bid1Size"]]] if float(t["bid1Price"]) else []
        return ok({"a": a, "b": b})

    # ---------- счёт ----------
    def get_wallet_balance(self, accountType):
        self._rec("get_wallet_balance", locals())
        return ok({"list": [{"totalEquity": "1000", "totalAvailableBalance": "900", "coin": []}]})

    def get_positions(self, category, baseCoin, limit=200):
        self._rec("get_positions", locals())
        return ok({"list": [p for p in self.positions if p["symbol"].startswith(baseCoin + "-")]})

    def get_coin_greeks(self, baseCoin=None):
        self._rec("get_coin_greeks", locals())
        return ok({"list": [g for g in self.greeks if baseCoin in (None, g["baseCoin"])]})

    def get_open_orders(self, category, baseCoin=None, symbol=None, orderId=None, orderLinkId=None, limit=50):
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
        return ok({"list": rows[:limit]})

    def get_order_history(self, category, symbol=None, orderLinkId=None, startTime=None, limit=50, cursor=None):
        self._rec("get_order_history", locals())
        rows = self.history + self.orders
        if symbol:
            rows = [o for o in rows if o["symbol"] == symbol]
        if orderLinkId:
            rows = [o for o in rows if o["orderLinkId"] == orderLinkId]
        if startTime:
            rows = [o for o in rows if int(o["createdTime"]) >= startTime]
        return ok({"list": rows, "nextPageCursor": ""})

    # ---------- торговля ----------
    def place_order(self, **kw):
        self._rec("place_order", kw)
        o = {"orderId": str(uuid.uuid4()), "orderLinkId": kw["orderLinkId"], "symbol": kw["symbol"],
             "side": kw["side"], "orderType": kw["orderType"], "qty": kw["qty"], "price": kw.get("price", "0"),
             "orderIv": kw.get("orderIv", ""), "leavesQty": kw["qty"], "cumExecQty": "0", "cumExecValue": "0",
             "orderStatus": "New", "reduceOnly": kw["reduceOnly"], "createdTime": str(int(time.time() * 1000))}
        self.orders.append(o)
        if "place_order" in self.after:
            raise self.after.pop("place_order")
        return ok({"orderId": o["orderId"], "orderLinkId": o["orderLinkId"]})

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

    def cancel_order(self, category, symbol, orderId):
        self._rec("cancel_order", locals())
        self.orders = [o for o in self.orders if o["orderId"] != orderId]
        return ok({"orderId": orderId})

    def cancel_all_orders(self, category, baseCoin=None):
        self._rec("cancel_all_orders", locals())
        gone = [o for o in self.orders if not baseCoin or o["symbol"].startswith(baseCoin + "-")]
        self.orders = [o for o in self.orders if o not in gone]
        return ok({"list": [{"orderId": o["orderId"]} for o in gone]})
