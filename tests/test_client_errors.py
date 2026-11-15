"""Классификация ошибок биржи, выяснение статуса после сбоя связи, изменение и отмена ордеров."""
import time
from decimal import Decimal as D

import pytest
import requests
from pybit.exceptions import FailedRequestError, InvalidRequestError

from optbot.bybit_client import BybitError, BybitNetworkError, OrderRequest, RiskError
from tests.helpers import with_cfg
from tests.fakes import BTC_CALL, BTC_PUT


def bybit_reject(code):
    return InvalidRequestError(request="req", message="msg", status_code=code, time="0", resp_headers={})


# ---------- _call ----------
@pytest.mark.parametrize("exc,cls", [
    (bybit_reject(110007), BybitError),                       # недостаточно средств — однозначный отказ
    (bybit_reject(10001), BybitError),
    (bybit_reject(10016), BybitNetworkError),                 # внутренний сбой биржи — ордер мог встать
    (bybit_reject(10000), BybitNetworkError),
    (requests.exceptions.ReadTimeout("timeout"), BybitNetworkError),
    (requests.exceptions.ConnectionError("down"), BybitNetworkError),
    (FailedRequestError("req", "Retries exceeded", 500, "0", {}), BybitNetworkError),
    (PermissionError("no api key"), BybitError),              # локальная ошибка — не «сбой связи»
    (TypeError("bad kwarg"), BybitError),
])
def test_call_error_mapping(client, exc, cls):
    def boom(**kw):
        raise exc
    with pytest.raises(BybitError) as e:
        client._call(boom)
    assert type(e.value) is cls


def test_retcode_in_response_body(client):
    with pytest.raises(BybitNetworkError):
        client._call(lambda: {"retCode": 10016, "retMsg": "service error"})
    with pytest.raises(BybitError):
        client._call(lambda: {"retCode": 110007, "retMsg": "insufficient"})


# ---------- выставление и статус после сбоя ----------
def test_place_order_sends_link_id(client, fake):
    r = OrderRequest(symbol=BTC_PUT, side="Buy", qty=D("0.01"), price=D("20"))
    res = client.place_order(r)
    sent = fake.called("place_order")[0]
    assert sent["orderLinkId"] == r.link_id and r.link_id.startswith("ob-") and len(r.link_id) <= 36
    assert sent["price"] == "20" and sent["timeInForce"] == "GTC" and r.sent
    assert res["orderId"] == fake.orders[-1]["orderId"]


def test_place_order_iv(client, fake):
    client.place_order(OrderRequest(symbol=BTC_PUT, side="Buy", qty=D("0.01"), iv=D("0.45")))
    sent = fake.called("place_order")[0]
    assert sent["orderIv"] == "0.45" and "price" not in sent


def test_timeout_after_send_finds_order(client, fake):
    fake.after["place_order"] = requests.exceptions.ReadTimeout("timeout")
    r = OrderRequest(symbol=BTC_PUT, side="Buy", qty=D("0.01"), price=D("20"))
    with pytest.raises(BybitNetworkError):
        client.place_order(r)
    assert "ордер НА БИРЖЕ: New" in client.order_status_after_failure(r)


def test_status_never_claims_not_placed(client):
    r = OrderRequest(symbol=BTC_PUT, side="Buy", qty=D("0.01"), price=D("20"), sent=True, link_id="ob-missing")
    s = client.order_status_after_failure(r)
    assert "НЕИЗВЕСТЕН" in s and "не выставлен" not in s


def test_status_when_failed_before_send(client, fake):
    fake.fail["get_tickers"] = requests.exceptions.ReadTimeout("timeout")
    r = OrderRequest(symbol=BTC_PUT, side="Buy", qty=D("0.01"), price=D("20"))
    with pytest.raises(BybitNetworkError):
        client.place_order(r)
    assert not r.sent and "не отправлен" in client.order_status_after_failure(r)
    assert fake.called("place_order") == []


def test_status_lookup_retries_through_errors(client, fake):
    o = fake.add_order(BTC_PUT)
    o["orderLinkId"] = "ob-x"
    fake.fail["get_open_orders"] = requests.exceptions.ConnectionError("down")
    r = OrderRequest(symbol=BTC_PUT, side="Buy", qty=D("0.05"), price=D("20"), sent=True, link_id="ob-x")
    assert "НА БИРЖЕ" in client.order_status_after_failure(r)


# ---------- get_order ----------
def test_get_order_checks_symbol(client, fake):
    o = fake.add_order(BTC_PUT)
    assert client.get_order(BTC_PUT, o["orderId"])["orderId"] == o["orderId"]
    assert client.get_order(BTC_CALL, o["orderId"]) is None  # биржа игнорирует symbol при orderId


@pytest.mark.parametrize("status,active", [("Created", True), ("New", True), ("PartiallyFilled", True),
                                           ("Filled", False), ("Cancelled", False)])
def test_get_order_only_active(client, fake, status, active):
    o = fake.add_order(BTC_PUT, status=status)
    assert (client.get_order(BTC_PUT, o["orderId"]) is not None) is active


# ---------- amend ----------
def test_amend_price(client, fake):
    o = fake.add_order(BTC_PUT, qty="0.05", price="20")
    r, orig = client.amend_request(BTC_PUT, o["orderId"], price=D("25"))
    assert (r.qty, r.price, r.amend_of) == (D("0.05"), D("25"), o["orderId"])
    client.amend_order(o["orderId"], r)
    assert fake.called("amend_order")[0]["price"] == "25" and fake.orders[0]["price"] == "25"


def test_amend_wrong_symbol(client, fake):
    o = fake.add_order(BTC_PUT)
    with pytest.raises(RiskError, match="относится к"):
        client.amend_request(BTC_CALL, o["orderId"], price=D("25"))


def test_amend_noop_rejected(client, fake):
    o = fake.add_order(BTC_PUT, qty="0.05", price="20")
    with pytest.raises(RiskError, match="Ничего не меняется"):
        client.amend_request(BTC_PUT, o["orderId"], price=D("20"))


@pytest.mark.parametrize("kw,msg", [(dict(), "Укажите"), (dict(price=D(1), iv=D("0.5")), "Либо цена")])
def test_amend_bad_args(client, fake, kw, msg):
    o = fake.add_order(BTC_PUT)
    with pytest.raises(RiskError, match=msg):
        client.amend_request(BTC_PUT, o["orderId"], **kw)


def test_amend_market_order_rejected(client, fake):
    o = fake.add_order(BTC_PUT)
    o["orderType"] = "Market"
    with pytest.raises(RiskError, match="лимитные"):
        client.amend_request(BTC_PUT, o["orderId"], price=D("25"))


def test_amend_qty_must_exceed_filled_after_rounding(client, fake):
    o = fake.add_order(BTC_PUT, qty="0.05", filled="0.02", value="0.4")
    with pytest.raises(RiskError, match="больше уже исполненного"):
        client.amend_request(BTC_PUT, o["orderId"], qty=D("0.025"))  # округлится до 0.02


def test_amend_partial_fill_counts_only_remainder(make_client, cfg, fake):
    c = make_client(with_cfg(cfg, max_position={"BTC": "0.05"}))
    fake.positions.append({"symbol": BTC_PUT, "side": "Buy", "size": "0.03"})
    o = fake.add_order(BTC_PUT, qty="0.05", filled="0.03", value="0.6")
    r, _ = c.amend_request(BTC_PUT, o["orderId"], price=D("25"))
    assert any("позиция после: +0.05" in i for i in r.info)  # 0.03 позиция + 0.02 остаток, не 0.08
    with pytest.raises(RiskError, match="MAX_POSITION"):
        c.amend_request(BTC_PUT, o["orderId"], qty=D("0.06"), price=D("25"))


def test_amend_rechecks_fills_at_confirm(make_client, cfg, fake):
    c = make_client(with_cfg(cfg, max_position={"BTC": "0.05"}))
    o = fake.add_order(BTC_PUT, qty="0.05")
    r, _ = c.amend_request(BTC_PUT, o["orderId"], price=D("25"))
    o.update(cumExecQty="0.05", leavesQty="0", cumExecValue="1")  # между превью и подтверждением всё исполнилось
    with pytest.raises(RiskError, match="больше уже исполненного"):
        c.amend_order(o["orderId"], r)


def test_amend_inactive_at_confirm(client, fake):
    o = fake.add_order(BTC_PUT)
    r, _ = client.amend_request(BTC_PUT, o["orderId"], price=D("25"))
    fake.orders.clear()
    with pytest.raises(RiskError, match="больше не активен"):
        client.amend_order(o["orderId"], r)


def test_amend_yesterday_fill_not_in_today_premium(make_client, cfg, fake):
    c = make_client(with_cfg(cfg, max_daily_premium_usdt="2000"))
    o = fake.add_order(BTC_PUT, qty="0.05", filled="0.03", value="400",
                       created_ms=int(time.time() * 1000) - 2 * 86400_000)
    r, _ = c.amend_request(BTC_PUT, o["orderId"], price=D("25"))
    assert any("премия за сутки: 0.50" in i for i in r.info)  # только остаток 0.02 × 25


def test_amend_status_applied(client, fake):
    o = fake.add_order(BTC_PUT, qty="0.05", price="20")
    r, _ = client.amend_request(BTC_PUT, o["orderId"], price=D("25"))
    fake.after["amend_order"] = requests.exceptions.ReadTimeout("timeout")
    with pytest.raises(BybitNetworkError):
        client.amend_order(o["orderId"], r)
    assert "ПРИМЕНЕНО" in client.amend_status_after_failure(o["orderId"], r)


def test_amend_status_not_applied_never_suggests_retry(client, fake):
    o = fake.add_order(BTC_PUT, qty="0.05", price="20")
    r = OrderRequest(symbol=BTC_PUT, side="Buy", qty=D("0.05"), price=D("25"), sent=True)
    s = client.amend_status_after_failure(o["orderId"], r)
    assert "НЕИЗВЕСТЕН" in s and "повтор" not in s


def test_amend_status_iv_tolerance(client, fake):
    o = fake.add_order(BTC_PUT, qty="0.05", order_iv="0.5551")
    r = OrderRequest(symbol=BTC_PUT, side="Buy", qty=D("0.05"), iv=D("0.555"), sent=True)
    assert "ПРИМЕНЕНО" in client.amend_status_after_failure(o["orderId"], r)


def test_amend_status_order_gone(client):
    r = OrderRequest(symbol=BTC_PUT, side="Buy", qty=D("0.05"), price=D("25"), sent=True)
    assert "больше не активен" in client.amend_status_after_failure("nope", r)


def test_amend_status_not_sent(client):
    r = OrderRequest(symbol=BTC_PUT, side="Buy", qty=D("0.05"), price=D("25"))
    assert "не отправлено" in client.amend_status_after_failure("x", r)


# ---------- отмена ----------
def test_cancel_all_by_base(client, fake):
    fake.add_order(BTC_PUT)
    fake.add_order("ETH-27NOV26-2000-C-USDT")
    assert len(client.cancel_all("BTC")["list"]) == 1
    assert [o["symbol"] for o in fake.orders] == ["ETH-27NOV26-2000-C-USDT"]
