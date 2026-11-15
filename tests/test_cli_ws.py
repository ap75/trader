"""CLI (через run() с подставным клиентом) и разбор сообщений WebSocket."""
from decimal import Decimal as D

import pytest
import requests

from optbot import cli
from optbot.ws import ExecutionWatcher
from tests.fakes import BTC_PUT


@pytest.fixture
def run_cli(client, monkeypatch, capsys):
    def go(*argv, answer="y"):
        monkeypatch.setattr("builtins.input", lambda prompt="": answer)
        cli.run(cli.build_parser().parse_args(list(argv)), client)
        return capsys.readouterr()
    return go


def test_cli_read_commands(run_cli, fake):
    fake.add_order(BTC_PUT)
    assert "27NOV26" in run_cli("expiries", "BTC").out
    assert "Strike" in run_cli("chain", "BTC", "27NOV26", "-n", "1").out
    assert "60000" in run_cli("ticker", "BTC-27NOV26-60000-P").out
    assert BTC_PUT.replace("-USDT", "") in run_cli("orders").out
    run_cli("balance")
    run_cli("positions")
    run_cli("greeks")


def test_cli_buy_confirm_and_decline(run_cli, fake):
    assert "Отменено пользователем" in run_cli("buy", BTC_PUT, "0.01", "--price", "20", answer="n").out
    assert fake.called("place_order") == []
    assert "Ордер принят" in run_cli("buy", BTC_PUT, "0.01", "--price", "20").out
    sent = fake.called("place_order")[0]
    assert (sent["side"], sent["qty"], sent["price"]) == ("Buy", "0.01", "20")


def test_cli_sell_iv_and_market(run_cli, fake):
    run_cli("sell", BTC_PUT, "0.01", "--iv", "55")
    assert fake.called("place_order")[0]["orderIv"] == "0.55"
    run_cli("buy", BTC_PUT, "0.01", "--market")
    assert fake.called("place_order")[1]["orderType"] == "Market"


def test_cli_network_failure_exit_2(run_cli, fake):
    fake.after["place_order"] = requests.exceptions.ReadTimeout("timeout")
    with pytest.raises(SystemExit) as e:
        run_cli("buy", BTC_PUT, "0.01", "--price", "20")
    assert e.value.code == 2


def test_cli_amend_cancel_close(run_cli, fake):
    o = fake.add_order(BTC_PUT, qty="0.05", price="20")
    run_cli("amend", BTC_PUT, o["orderId"], "--price", "25")
    assert o["price"] == "25"
    run_cli("cancel", BTC_PUT, o["orderId"])
    assert fake.orders == []
    fake.positions.append({"symbol": BTC_PUT, "side": "Buy", "size": "0.03"})
    run_cli("close", BTC_PUT, "--price", "100")
    assert fake.called("place_order")[-1]["reduceOnly"] is True


def test_cli_amend_network_failure_exit_2(run_cli, fake):
    o = fake.add_order(BTC_PUT, qty="0.05", price="20")
    fake.after["amend_order"] = requests.exceptions.ReadTimeout("timeout")
    with pytest.raises(SystemExit) as e:
        run_cli("amend", BTC_PUT, o["orderId"], "--price", "25")
    assert e.value.code == 2


def test_cli_parse_helpers():
    assert cli._dec("1,5") == D("1.5")
    with pytest.raises(Exception):
        cli._dec("abc")


def test_ws_filters_option_rows(cfg):
    got = []
    w = ExecutionWatcher(cfg, got.append)
    w._on_message({"topic": "execution", "data": [{"category": "linear"}, {"category": "option", "execId": "1"}]})
    w._on_message({"topic": "execution", "data": [{"category": "spot"}]})
    assert got == [[{"category": "option", "execId": "1"}]]


def test_ws_callback_errors_swallowed(cfg):
    called = []

    def boom(rows):
        called.append(rows)
        raise RuntimeError("x")
    ExecutionWatcher(cfg, boom)._on_message({"data": [{"category": "option"}]})  # не падает
    assert called == [[{"category": "option"}]]
