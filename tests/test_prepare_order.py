"""Проверки перед отправкой ордера: шаги, лимиты, опечатки, стакан, портфель."""
import time
from decimal import Decimal as D

import pytest

from optbot.bybit_client import OrderRequest, RiskError
from tests.helpers import with_cfg
from tests.fakes import BTC_CALL, BTC_PUT, ETH_CALL


def req(symbol=BTC_PUT, side="Buy", qty="0.01", price="100", **kw):
    return OrderRequest(symbol=symbol, side=side, qty=D(qty), price=D(price) if price is not None else None, **kw)


# ---------- нормализация ----------
def test_symbol_without_suffix_and_case(client):
    r = client.prepare_order(req(symbol="btc-27nov26-60000-p"))
    assert r.symbol == BTC_PUT


def test_unknown_symbol(client):
    with pytest.raises(ValueError, match="не найден"):
        client.prepare_order(req(symbol="BTC-27NOV26-61000-P"))


def test_base_not_allowed(client):
    with pytest.raises(ValueError, match="BASE_COINS"):
        client.prepare_order(req(symbol="DOGE-27NOV26-1-P"))


def test_qty_rounded_down_to_step(client):
    assert client.prepare_order(req(qty="0.019")).qty == D("0.01")


def test_qty_below_min(client):
    with pytest.raises(RiskError, match="минимального"):
        client.prepare_order(req(qty="0.009"))


@pytest.mark.parametrize("side,raw,expected", [("Buy", "101", "100"), ("Sell", "101", "105")])
def test_price_rounded_to_passive_side(client, side, raw, expected):
    # покупка вниз, продажа вверх: округление не делает ордер агрессивнее
    assert client.prepare_order(req(side=side, price=raw)).price == D(expected)


def test_side_and_type_normalized(client):
    r = client.prepare_order(OrderRequest(symbol=BTC_PUT, side="buy", qty=D("0.01"), order_type="limit",
                                          price=D("100")))
    assert (r.side, r.order_type) == ("Buy", "Limit")


@pytest.mark.parametrize("kw,msg", [
    (dict(side="hold"), "side"),
    (dict(order_type="Stop"), "order_type"),
    (dict(order_type="Market", iv=D("0.5"), price=None), "без цены и IV"),
    (dict(price=None), "нужна цена"),
])
def test_bad_requests(client, kw, msg):
    base = dict(symbol=BTC_PUT, side="Buy", qty=D("0.01"), price=D("100"))
    base.update(kw)
    with pytest.raises(RiskError, match=msg):
        client.prepare_order(OrderRequest(**base))


# ---------- лимиты ордера ----------
def test_max_qty(client):
    with pytest.raises(RiskError, match="MAX_QTY_BTC"):
        client.prepare_order(req(qty="2"))


def test_max_qty_ignored_for_reduce_only(client):
    assert client.prepare_order(req(qty="2", side="Sell", reduce_only=True)).qty == D("2")


def test_max_premium(client, fake):
    fake.tickers[BTC_PUT].update(markPrice="600", bid1Price="595", ask1Price="605")
    assert client.prepare_order(req(qty="0.8", price="600")).price == D("600")  # 480 < 500
    with pytest.raises(RiskError, match="MAX_ORDER_PREMIUM_USDT"):
        client.prepare_order(req(qty="1", price="600"))  # 600 > 500


def test_max_premium_ignored_for_reduce_only(make_client, cfg):
    c = make_client(with_cfg(cfg, max_order_premium_usdt="0.5"))
    c.prepare_order(req(side="Sell", qty="0.01", reduce_only=True))


# ---------- опечатки в лимитной цене ----------
def test_buy_far_above_mark_rejected(client):
    with pytest.raises(RiskError, match="выше mark"):
        client.prepare_order(req(price="1500"))


def test_sell_far_below_mark_rejected(client):
    with pytest.raises(RiskError, match="ниже mark"):
        client.prepare_order(req(side="Sell", price="5"))


def test_passive_limit_far_below_mark_is_fine(client):
    r = client.prepare_order(req(price="20"))
    assert "встанет в стакан" in " ".join(r.info)


def test_reduce_only_compared_to_best_quote_not_mark(client, fake):
    # неликвидный опцион: mark 100, bid 60, ask 90 — закрыться по mid 75 можно, по 40 нельзя
    fake.tickers[BTC_PUT].update(markPrice="100", bid1Price="60", ask1Price="90")
    assert client.prepare_order(req(side="Sell", price="75", reduce_only=True)).price == D("75")
    with pytest.raises(RiskError, match="ниже bid"):
        client.prepare_order(req(side="Sell", price="40", reduce_only=True))
    with pytest.raises(RiskError, match="ниже mark"):  # открывающая — по mark
        client.prepare_order(req(side="Sell", price="75"))


# ---------- стакан ----------
def test_crossing_limit_reports_fill(client):
    r = client.prepare_order(req(price="105"))
    assert any("исполнение сейчас: 0.01" in i for i in r.info)


def test_market_requires_opposite_quote(client, fake):
    fake.tickers[BTC_PUT].update(ask1Price="0")
    with pytest.raises(RiskError, match="Нет встречных"):
        client.prepare_order(req(order_type="Market", price=None))


def test_market_spread_limit(client, fake):
    fake.tickers[BTC_PUT].update(bid1Price="50", ask1Price="105")
    with pytest.raises(RiskError, match="Спред"):
        client.prepare_order(req(order_type="Market", price=None))


def test_market_depth(client, fake):
    fake.books[BTC_PUT] = {"a": [["105", "0.01"]], "b": [["95", "1"]]}
    with pytest.raises(RiskError, match="можно исполнить только"):
        client.prepare_order(req(order_type="Market", price=None, qty="0.05"))


def test_market_slippage(client, fake):
    fake.books[BTC_PUT] = {"a": [["105", "0.01"], ["200", "1"]], "b": [["95", "1"]]}
    with pytest.raises(RiskError, match="хуже mark"):
        client.prepare_order(req(order_type="Market", price=None, qty="0.05"))


def test_market_premium_uses_book_walk(make_client, cfg, fake):
    # лучший ask × qty = 0.05*105 = 5.25 прошёл бы лимит 5.3; проход по стакану = 0.02*105 + 0.03*110 = 5.40
    fake.books[BTC_PUT] = {"a": [["105", "0.02"], ["110", "1"]], "b": [["95", "1"]]}
    c = make_client(with_cfg(cfg, max_order_premium_usdt="5.3"))
    with pytest.raises(RiskError, match="5.40"):
        c.prepare_order(req(order_type="Market", price=None, qty="0.05"))


def test_iv_order_skips_book(client):
    r = client.prepare_order(req(price=None, iv=D("0.5")))
    assert any("по IV" in i for i in r.info)
    assert client.http.called("get_orderbook") == []


def test_ticker_cached_on_request(client):
    r = client.prepare_order(req())
    assert r.ticker["symbol"] == BTC_PUT


# ---------- портфель ----------
def test_position_limit_counts_pending_orders(make_client, cfg, fake):
    c = make_client(with_cfg(cfg, max_position={"BTC": "0.05"}))
    fake.positions.append({"symbol": BTC_PUT, "side": "Buy", "size": "0.03"})
    fake.add_order(BTC_PUT, qty="0.01")
    with pytest.raises(RiskError, match="MAX_POSITION_BTC"):
        c.prepare_order(req(qty="0.02"))  # 0.03 + 0.01 + 0.02 = 0.06
    assert any("позиция после: +0.05" in i for i in c.prepare_order(req(qty="0.01")).info)


def test_position_reduction_always_allowed(make_client, cfg, fake):
    c = make_client(with_cfg(cfg, max_position={"BTC": "0.01"}))
    fake.positions.append({"symbol": BTC_PUT, "side": "Buy", "size": "0.5"})
    c.prepare_order(req(side="Sell", price="100", qty="0.1"))  # уменьшает |позицию|


def test_delta_limit(make_client, cfg, fake):
    c = make_client(with_cfg(cfg, max_net_delta={"BTC": "0.01"}))
    fake.greeks.append({"baseCoin": "BTC", "totalDelta": "-0.009"})
    with pytest.raises(RiskError, match="MAX_NET_DELTA_BTC"):
        c.prepare_order(req(qty="0.05"))  # дельта пута -0.1 × 0.05 = -0.005
    c.prepare_order(req(symbol=BTC_CALL, qty="0.05"))  # колл уменьшает |дельту|


def test_daily_premium_limit(make_client, cfg, fake):
    c = make_client(with_cfg(cfg, max_daily_premium_usdt="10"))
    fake.add_order(BTC_PUT, qty="0.05", price="100", filled="0.05", value="5", status="Filled")
    fake.orders[-1]["leavesQty"] = "0"
    fake.add_order(ETH_CALL, qty="1", price="4")  # активный: 4 в резерве
    with pytest.raises(RiskError, match="MAX_DAILY_PREMIUM_USDT"):
        c.prepare_order(req(qty="0.02"))  # 5 + 4 + 2 = 11
    assert any("10.00 из 10" in i for i in c.prepare_order(req(qty="0.01")).info)


def test_daily_premium_ignores_old_and_reduce_only(make_client, cfg, fake):
    c = make_client(with_cfg(cfg, max_daily_premium_usdt="10"))
    fake.add_order(BTC_PUT, qty="1", price="100", created_ms=int(time.time() * 1000) - 2 * 86400_000)
    fake.add_order(BTC_PUT, qty="1", price="100", reduce_only=True)
    assert c.daily_premium_used() == 0


def test_portfolio_skipped_for_reduce_only(make_client, cfg, fake):
    c = make_client(with_cfg(cfg, max_position={"BTC": "0.01"}, max_daily_premium_usdt="0.01"))
    c.prepare_order(req(side="Sell", qty="0.5", reduce_only=True))
    assert fake.called("get_positions") == []


def test_open_orders_fetched_once_per_check(make_client, cfg, fake):
    c = make_client(with_cfg(cfg, max_position={"BTC": "1"}, max_daily_premium_usdt="100"))
    c.prepare_order(req())
    assert len(fake.called("get_open_orders")) == 3  # один полный список (3 актива), не дважды


def test_close_position_request(client, fake):
    fake.positions.append({"symbol": BTC_PUT, "side": "Buy", "size": "0.03"})
    r = client.close_position_request(BTC_PUT)
    assert (r.side, r.qty, r.order_type, r.reduce_only) == ("Sell", D("0.03"), "Market", True)
    assert client.close_position_request(BTC_PUT, D("90")).order_type == "Limit"


def test_close_price_zero_is_not_market(client, fake):
    fake.positions.append({"symbol": BTC_PUT, "side": "Buy", "size": "0.03"})
    with pytest.raises(RiskError, match="> 0"):
        client.close_position_request(BTC_PUT, D("0"))


def test_close_without_position(client):
    with pytest.raises(RiskError, match="Нет открытой позиции"):
        client.close_position_request(BTC_PUT)
