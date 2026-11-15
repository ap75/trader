"""Журнал сделок: orderLinkId, запись, сверка по REST (окна, cursor, дедупликация), PnL, отчёт, бот и CLI."""
import asyncio
import time
from types import SimpleNamespace as NS

import pytest

from optbot import cli, db
from optbot.bot import OptionsBot
from optbot.journal import (
    DAY_MS, GRACE_MS, OVERLAP_MS, Journal, _apply, fmt_report, intrinsic, main, parse_period,
)
from optbot.linkid import NO_USER, new_link_id, parse_link_id, signal_link_id, user_hash
from optbot.ws import ExecutionWatcher
from tests.fakes import BTC_CALL, BTC_PUT, EXP_MS
from tests.helpers import with_cfg
from telegram.error import NetworkError
from tests.test_bot import TG, TRADER, exec_row, pid_from

NOW = EXP_MS - 5 * DAY_MS
H = 3_600_000


def q(cfg, sql, *a):
    conn = db.connect(cfg.db_file)  # со схемой: запрос может идти раньше первой записи журнала
    try:
        return [dict(r) for r in conn.execute(sql, a)]
    finally:
        conn.close()


@pytest.fixture
def j(cfg):
    return Journal(cfg)


def run(coro):
    return asyncio.run(coro)


# ---------- orderLinkId ----------
def test_link_id_formats():
    link = new_link_id("abc123")
    assert link.startswith("man-uabc123-") and len(link) <= 36
    assert parse_link_id(link) == {"source": "manual", "user_hash": "abc123"}
    s = signal_link_id(42, 3, "abc123", 2)
    assert parse_link_id(s) == {"source": "signal", "signal_id": 42, "leg": 3, "user_hash": "abc123"}
    assert parse_link_id("ob-0336c72e3af345a68436b370")["source"] == "legacy"
    for ext in ("", None, "web-123", "man-uXYZ-1"):
        assert parse_link_id(ext)["source"] == "external"
    with pytest.raises(ValueError):
        signal_link_id(10 ** 20, 1, "abc123", 1)


def test_user_hash_hides_user_id():
    h = user_hash(b"secret", 205102580)
    assert len(h) == 6 and h == user_hash(b"secret", 205102580) != user_hash(b"secret", 1470678444)
    assert h != user_hash(b"other", 205102580) and "2051" not in h
    assert user_hash(b"secret", None) == NO_USER


def test_secret_from_env_or_api_secret(cfg):
    assert Journal(cfg).secret != Journal(with_cfg(cfg, journal_secret="x")).secret == b"x"
    assert Journal(cfg).secret != cfg.api_secret.encode()  # не сам API-секрет


# ---------- запись ----------
def test_register_and_describe(j, cfg):
    link = j.register_order(TRADER, "Павел", -100111)
    row = q(cfg, "SELECT * FROM order_links")[0]
    assert (row["link_id"], row["user_id"], row["user_name"], row["chat_id"]) == (link, TRADER, "Павел", -100111)
    assert row["user_hash"] == j.tag(TRADER) and link.startswith(f"man-u{j.tag(TRADER)}-")
    assert j.describe(link) == "через бота · Павел"
    assert j.describe(j.register_order(None, "CLI", source="cli")) == "через бота · CLI"
    assert j.describe("ob-legacy") == "через бота"
    assert j.describe("") == "вне бота"
    assert j.describe(signal_link_id(7, 1, "abc123", 1)) == "по сигналу #7"


def test_register_survives_db_failure(cfg, tmp_path, caplog):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    jj = Journal(with_cfg(cfg, db_path=str(blocker / "sub" / "m.db")))  # папку создать нельзя
    link = jj.register_order(TRADER, "Павел")
    assert link.startswith("man-u") and "ордер всё равно отправляется" in caplog.text


def test_executions_dedup_and_malformed(j, cfg):
    r = exec_row(execId="e1", execTime=str(NOW))
    assert j.record_executions([r, dict(r)], "ws") == [r]
    assert j.record_executions([r], "sync") == []
    bad = [{"execId": "e2"}, exec_row(execId="e3")]                  # без symbol/времени
    assert j.record_executions(bad, "ws") == bad                      # не записаны, но уведомить можно
    assert j.record_executions(bad, "sync", notified=False) == []
    assert q(cfg, "SELECT COUNT(*) n FROM executions")[0]["n"] == 1
    assert q(cfg, "SELECT source FROM executions")[0]["source"] == "ws"


def test_orders_older_state_does_not_overwrite(j, cfg):
    base = dict(orderId="o1", symbol=BTC_PUT, side="Buy", qty="0.05", orderStatus="New", updatedTime="200")
    j.record_orders([base])
    j.record_orders([base | {"orderStatus": "Filled", "updatedTime": "300", "cumExecQty": "0.05"}])
    j.record_orders([base | {"orderStatus": "PartiallyFilled", "updatedTime": "250"}])  # запоздало
    row = q(cfg, "SELECT status, cum_qty FROM orders")[0]
    assert row == {"status": "Filled", "cum_qty": 0.05}
    assert j.record_orders([{"orderId": "x"}]) == 0


def test_deliveries_and_prices(j, cfg):
    d = {"symbol": BTC_CALL, "deliveryTime": str(EXP_MS), "side": "Buy", "position": "0.01",
         "deliveryPrice": "71000", "fee": "0.5", "deliveryRpl": "9"}
    assert len(j.record_deliveries([d, d])) == 1
    assert q(cfg, "SELECT strike, rpl FROM deliveries")[0] == {"strike": 70000, "rpl": 9}  # страйк из символа
    rows = [{"deliveryPrice": "64014.88", "deliveryTime": "1794700800000"}, {"deliveryPrice": ""}]
    assert j.record_delivery_prices("btc", rows) == 1 and j.record_delivery_prices("BTC", rows) == 0


def test_disabled_journal_is_noop(cfg):
    jj = Journal(with_cfg(cfg, journal_enabled=False))
    rows = [exec_row(execId="e1", execTime="1")]
    assert jj.register_order(TRADER).startswith("man-u")
    assert jj.record_executions(rows, "ws") == rows  # уведомлять обо всём
    assert jj.record_orders([{"orderId": "o", "symbol": BTC_PUT}]) == 0
    assert str(jj.sync(None)).startswith("новых исполнений 0")


# ---------- сверка ----------
def test_first_sync_backfills_in_exchange_windows(j, cfg, client, fake):
    fake.add_exec(BTC_PUT, t=NOW - 20 * DAY_MS, exec_id="old")
    fake.add_exec(BTC_PUT, t=NOW - H, exec_id="new")
    fake.deliveries.append({"symbol": BTC_PUT, "deliveryTime": str(NOW - 2 * DAY_MS), "deliveryPrice": "1"})
    fake.delivery_px["BTC"] = [{"deliveryPrice": "65000", "deliveryTime": str(NOW - DAY_MS)}]
    res = j.sync(client, NOW)
    assert not res.errors and res.first_run
    assert {r["execId"] for r in res.new_executions} == {"old", "new"} and len(res.new_deliveries) == 1
    assert res.prices == 1
    ex = fake.called("get_executions")
    assert len(ex) == 5 and all(c["endTime"] - c["startTime"] < 7 * DAY_MS for c in ex)  # 30 дн. окнами ≤ 7
    assert ex[0]["startTime"] == NOW - 30 * DAY_MS and ex[-1]["endTime"] == NOW
    assert len(fake.called("get_option_delivery_record")) == 2   # окна ≤ 30 дней
    state = {r["stream"]: r["last_ms"] for r in q(cfg, "SELECT * FROM sync_state")}
    assert state == {"executions": NOW, "orders": NOW, "deliveries": NOW, "executions:backfilled": NOW,
                     "executions:history_until": NOW - GRACE_MS}


def test_next_sync_from_last_with_overlap_no_duplicates(j, client, fake):
    fake.add_exec(BTC_PUT, t=NOW - H, exec_id="a")
    j.sync(client, NOW)
    fake.calls.clear()
    fake.add_exec(BTC_PUT, t=NOW + 10 * 60_000, exec_id="b")  # пропущено WebSocket
    res = j.sync(client, NOW + H)
    assert not res.first_run and [r["execId"] for r in res.new_executions] == ["b"]
    assert fake.called("get_executions")[0]["startTime"] == NOW - OVERLAP_MS


def test_sync_pagination(j, client, fake):
    fake.page_size = 2
    for i in range(5):
        fake.add_exec(BTC_PUT, t=NOW - H + i, exec_id=f"p{i}")
    res = j.sync(client, NOW)
    assert len(res.new_executions) == 5
    assert any(c.get("cursor") for c in fake.called("get_executions"))


def test_sync_progress_kept_on_failure(j, cfg, client, fake):
    real = fake.get_executions
    calls = []

    def flaky(**kw):
        calls.append(kw)
        if len(calls) == 3:
            raise ConnectionError("down")
        return real(**kw)
    fake.get_executions = flaky
    res = j.sync(client, NOW)
    assert res.errors and "executions" in res.errors[0]
    last = q(cfg, "SELECT last_ms FROM sync_state WHERE stream='executions'")[0]["last_ms"]
    assert last == calls[1]["endTime"]                      # два окна сохранены
    assert q(cfg, "SELECT COUNT(*) n FROM sync_state")[0]["n"] == 4  # остальные потоки + граница истории


# ---------- PnL ----------
def test_apply_average_cost():
    st = [0.0, 0.0]
    assert _apply(st, 0.02, 100) == 0 and st == [0.02, 100]
    assert _apply(st, 0.02, 200) == 0 and st == [0.04, 150]
    assert _apply(st, -0.01, 170) == pytest.approx(0.2) and st[0] == pytest.approx(0.03)
    assert _apply(st, -0.05, 120) == pytest.approx(0.03 * -30)   # закрытие и переворот в шорт 0.02
    assert st == [pytest.approx(-0.02), 120]
    assert _apply(st, 0.02, 100) == pytest.approx(0.4) and st == [0.0, 0.0]  # шорт закрыт дешевле


@pytest.mark.parametrize("sym,px,out", [(BTC_CALL, 71000, 1000), (BTC_CALL, 69000, 0), (BTC_PUT, 59000, 1000),
                                        (BTC_PUT, 61000, 0)])
def test_intrinsic(sym, px, out):
    assert intrinsic(sym, px) == out


def _ex(j, sym, side, qty, price, t, fee="0.1", etype="Trade", eid=None):
    j.record_executions([exec_row(execId=eid or f"{sym}{side}{t}", symbol=sym, side=side, execQty=str(qty),
                                  execPrice=str(price), execValue=str(qty * price), execFee=fee,
                                  execTime=str(t), execType=etype)], "ws")


def test_report_realized_and_period_cost_basis(j):
    _ex(j, BTC_PUT, "Buy", 0.02, 100, NOW - 10 * DAY_MS)    # до периода — только цена входа
    _ex(j, BTC_PUT, "Sell", 0.01, 150, NOW - DAY_MS)
    rep = j.report(NOW - 2 * DAY_MS, NOW, "2 дн.")
    assert (rep.trades, rep.sell_value, rep.buy_value) == (1, pytest.approx(1.5), 0)
    assert rep.realized == pytest.approx(0.5) and rep.fees == pytest.approx(0.1)
    assert rep.net == pytest.approx(0.4)
    assert rep.open_positions == {BTC_PUT: (pytest.approx(0.01), 100)}
    text = fmt_report(rep)
    assert "сделок 1" in text and "+0.50" in text and "BTC-27NOV26-60000-P: +0/−0.01" in text
    assert "Открыто на конец периода" in text and "уплачено 0.00, получено 1.50" in text


def test_report_delivery_closes_position(j):
    _ex(j, BTC_CALL, "Buy", 0.01, 300, NOW)
    j.record_deliveries([{"symbol": BTC_CALL, "deliveryTime": str(EXP_MS), "side": "Buy", "position": "0.01",
                          "deliveryPrice": "71000", "fee": "0.2"}])
    rep = j.report(NOW - DAY_MS, EXP_MS + DAY_MS, "x")
    assert rep.deliveries == 1 and rep.realized == pytest.approx((1000 - 300) * 0.01)
    assert rep.fees == pytest.approx(0.3) and rep.open_positions == {}
    assert "(поставка)" in fmt_report(rep)


def test_report_delivery_not_double_counted_with_delivery_exec(j):
    _ex(j, BTC_CALL, "Buy", 0.01, 300, NOW)
    _ex(j, BTC_CALL, "Sell", 0.01, 1000, EXP_MS, fee="0", etype="Delivery")
    j.record_deliveries([{"symbol": BTC_CALL, "deliveryTime": str(EXP_MS), "side": "Buy", "position": "0.01",
                          "deliveryPrice": "71000"}])
    rep = j.report(NOW - DAY_MS, EXP_MS + DAY_MS, "x")
    assert rep.realized == pytest.approx(7) and rep.trades == 1 and rep.deliveries == 1


def test_report_delivery_of_position_older_than_history(j):
    j.record_deliveries([{"symbol": BTC_PUT, "deliveryTime": str(EXP_MS), "side": "Sell", "position": "0.01",
                          "entryPrice": "200", "deliveryPrice": "65000"}])  # пут истёк пустым — шорт заработал
    assert j.report(NOW, EXP_MS + 1, "x").realized == pytest.approx(2)


def test_report_unknown_delivery_price(j):
    _ex(j, BTC_PUT, "Buy", 0.01, 100, NOW)
    j.record_deliveries([{"symbol": BTC_PUT, "deliveryTime": str(EXP_MS), "position": "0.01", "fee": "0.3"}])
    rep = j.report(NOW - 1, EXP_MS + 1, "x")
    assert rep.unknown_deliveries == [BTC_PUT] and "нет цены поставки" in fmt_report(rep)
    assert rep.open_positions == {} and rep.fees == pytest.approx(0.4)  # позиция закрыта, комиссия учтена


def test_report_empty(j):
    assert "сделок и поставок нет" in fmt_report(j.report(0, NOW, "7 дн."))


def test_csv(j):
    link = j.register_order(TRADER, "Павел")
    j.record_executions([exec_row(execId="c1", execTime=str(NOW), orderLinkId=link)], "ws")
    j.record_deliveries([{"symbol": BTC_PUT, "deliveryTime": str(NOW + 1), "deliveryPrice": "1"}])
    data = j.csv_bytes(NOW - 1, NOW + 2)
    text = data.decode("utf-8-sig").splitlines()
    assert data.startswith("﻿".encode()) and text[0].startswith("time_utc,kind")
    assert ",execution," in text[1] and ",manual,Павел," in text[1] and ",delivery," in text[2]


# ---------- период ----------
T = 1794753000000  # 2026-11-15 14:30 UTC


@pytest.mark.parametrize("args,start,end,label", [
    ([], T - 7 * DAY_MS, T, "7 дн."),
    (["30d"], T - 30 * DAY_MS, T, "30 дн."),
    (["сегодня"], 1794700800000, T, "сегодня"),
    (["вчера"], 1794700800000 - DAY_MS, 1794700800000, "вчера"),
    (["2026-11-15"], 1794700800000, 1794700800000 + DAY_MS, "2026-11-15"),
    (["2026-11-01..2026-11-15"], 1794700800000 - 14 * DAY_MS, 1794700800000 + DAY_MS, "2026-11-01..2026-11-15"),
])
def test_parse_period(args, start, end, label):
    assert parse_period(args, T) == (start, end, label)


@pytest.mark.parametrize("bad", ["0d", "999d", "abc", "2026-13-01", "2026-11-15..2026-11-01"])
def test_parse_period_errors(bad):
    with pytest.raises(ValueError):
        parse_period([bad], T)


# ---------- бот ----------
@pytest.fixture
def b(make_client, cfg):
    return OptionsBot(make_client(cfg), cfg)


def test_bot_order_registered_before_send(b, cfg):
    tg = TG()
    run(b.cmd_buy(tg.upd(TRADER), tg.ctx(BTC_PUT, "0.01", "20")))
    assert q(cfg, "SELECT COUNT(*) n FROM order_links")[0]["n"] == 0   # до подтверждения — ничего
    run(b.on_callback(tg.upd(TRADER, data=f"ok:{pid_from(tg)}"), tg.ctx()))
    sent = b.c.http.called("place_order")[0]
    row = q(cfg, "SELECT * FROM order_links")[0]
    assert sent["orderLinkId"] == row["link_id"] and row["user_id"] == TRADER and row["user_name"] == "user10"
    assert sent["orderLinkId"].startswith(f"man-u{b.journal.tag(TRADER)}-")


def test_bot_ws_executions_recorded_and_deduplicated(b, cfg, monkeypatch):
    notified = []

    async def on_exec(rows):
        notified.append(rows)
    monkeypatch.setattr(b, "on_executions", on_exec)
    r = exec_row(execId="w1", execTime=str(NOW))
    run(b._ws_executions([r]))
    run(b._ws_executions([r]))                     # повтор (переподключение) — без второго уведомления
    assert notified == [[r]] and q(cfg, "SELECT COUNT(*) n FROM executions")[0]["n"] == 1
    run(b._ws_orders([{"orderId": "o1", "symbol": BTC_PUT, "orderStatus": "Filled", "updatedTime": "1"}]))
    assert q(cfg, "SELECT status FROM orders")[0]["status"] == "Filled"


def test_ws_callback_does_not_touch_db_in_pybit_thread(b, monkeypatch):
    monkeypatch.setattr(b.journal, "record_executions", lambda *a, **k: pytest.fail("БД в потоке pybit"))
    monkeypatch.setattr(b.journal, "record_orders", lambda *a, **k: pytest.fail("БД в потоке pybit"))
    tasks = []
    b._app, b._loop = NS(create_task=lambda coro: (tasks.append(coro), coro.close())), \
        NS(call_soon_threadsafe=lambda f: f())
    b._on_ws_executions([exec_row()])
    b._on_ws_orders([{"orderId": "o1"}])
    assert len(tasks) == 2                          # только передано в asyncio


def test_bot_execution_names_trader(b):
    tg = TG()
    b._app = NS(bot=tg.bot)
    link = b.journal.register_order(TRADER, "Павел")
    run(b.on_executions([exec_row(orderLinkId=link)]))
    assert "через бота · Павел" in tg.last("send")


def test_sync_job_reports_missed_only_after_first_run(b, fake):
    tg = TG()
    b._app = NS(bot=tg.bot)
    fake.add_exec(BTC_PUT, exec_id="hist")
    run(b._sync_job(NS()))                          # первая выгрузка — история, без уведомлений
    assert not [t for k, t, _ in tg.out if k == "send"]
    with db.connect(b.journal.cfg.db_file) as c:  # первая сверка была час назад
        c.execute("UPDATE sync_state SET last_ms = last_ms - 3600000 WHERE stream = 'executions:history_until'")
    fake.add_exec(BTC_PUT, side="Sell", qty="0.02", price="120", exec_id="missed",
                  t=int(time.time() * 1000) - 5 * 60_000)
    fake.add_exec(BTC_PUT, exec_id="fresh")          # моложе GRACE — может ещё прийти по WS
    run(b._sync_job(NS()))
    texts = [t for k, t, _ in tg.out if k == "send"]
    assert len(texts) == 3 and "Сверка журнала: 1" in texts[0] and "SELL 0.02" in texts[0]


def test_sync_job_survives_errors(b, caplog, monkeypatch):
    monkeypatch.setattr(b.journal, "sync", lambda c: 1 / 0)
    run(b._sync_job(NS()))
    assert "сверка не выполнена" in caplog.text


def test_cmd_journal_and_csv(b):
    tg = TG()
    _ex(b.journal, BTC_PUT, "Buy", 0.01, 100, NOW)
    docs = []

    async def send_document(document, filename):
        docs.append((filename, document))
    u = tg.upd(TRADER)
    u.effective_chat.send_document = send_document
    run(b.cmd_journal(u, tg.ctx("2026-11-22", "csv")))  # день NOW
    assert "Журнал за 2026-11-22" in tg.last() and "сделок 1" in tg.last()
    assert docs and docs[0][0] == "journal-demo-2026-11-22.csv" and b"BTC-27NOV26-60000-P" in docs[0][1]


def test_cmd_journal_bad_args_and_dm_only(b):
    tg = TG()
    run(b.cmd_journal(tg.upd(TRADER), tg.ctx("7d", "30d")))
    assert "Формат: /journal" in tg.last()
    run(b.cmd_journal(tg.upd(TRADER, "g"), tg.ctx()))
    assert "только в личке" in tg.last()


def test_cmd_journal_disabled(make_client, cfg):
    off = with_cfg(cfg, journal_enabled=False)
    bb, tg = OptionsBot(make_client(off), off), TG()
    run(bb.cmd_journal(tg.upd(TRADER), tg.ctx()))
    assert "Журнал выключен" in tg.last()


# ---------- WebSocket, CLI ----------
def test_ws_order_topic_filtered(cfg):
    got = []
    w = ExecutionWatcher(cfg, lambda rows: None, got.append)
    w._on_order_message({"data": [{"category": "option", "orderId": "1"}, {"category": "linear"}]})
    w._on_order_message({"data": [{"category": "spot"}]})
    assert got == [[{"category": "option", "orderId": "1"}]]


def test_cli_order_registered(client, cfg, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    cli.run(cli.build_parser().parse_args(["buy", BTC_PUT, "0.01", "--price", "20"]), client)
    row = q(cfg, "SELECT * FROM order_links")[0]
    assert row["source"] == "cli" and row["user_hash"] == NO_USER
    assert client.http.called("place_order")[0]["orderLinkId"] == row["link_id"]


def test_journal_cli(monkeypatch, cfg, make_client, capsys, tmp_path, fake):
    monkeypatch.setattr("optbot.journal.settings", cfg)
    monkeypatch.setattr("optbot.bybit_client.OptionsClient", lambda c=None: make_client(cfg))
    fake.add_exec(BTC_PUT)
    assert main(["--sync"]) == 0 and "новых исполнений 1" in capsys.readouterr().out
    out = tmp_path / "j.csv"
    assert main(["--report", "сегодня", "--csv", str(out)]) == 0
    assert "сделок 1" in capsys.readouterr().out and out.read_bytes().startswith("﻿".encode())


# ---------- исправления по ревью ----------
def test_interrupted_backfill_stays_history(j, client, fake):
    fake.add_exec(BTC_PUT, t=NOW - 20 * DAY_MS, exec_id="old")
    real, n = fake.get_executions, []

    def flaky(**kw):
        n.append(1)
        if len(n) == 2:
            raise ConnectionError("down")
        return real(**kw)
    fake.get_executions = flaky
    assert j.sync(client, NOW).first_run
    fake.get_executions = real
    res = j.sync(client, NOW + H)                   # дозагрузка истории — всё ещё первая выгрузка
    assert res.first_run and [r["execId"] for r in res.new_executions] == ["old"] and res.missed == []
    assert not j.sync(client, NOW + 2 * H).first_run


def test_ws_after_sync_still_notifies_once(j, client, fake):
    j.sync(client, NOW)                             # первая выгрузка завершена
    e = fake.add_exec(BTC_PUT, t=NOW + H, exec_id="race")
    res = j.sync(client, NOW + H + 1000)            # сверка успела раньше WS, исполнение ещё «свежее»
    assert [r["execId"] for r in res.new_executions] == ["race"] and res.missed == []
    assert j.record_executions([e], "ws") == [e]    # WS приходит — подробное уведомление будет
    assert j.record_executions([e], "ws") == []
    assert j.take_unnotified(NOW + 10 * H) == []    # и сводка его не повторит


def test_unnotified_announced_after_grace(j, client, fake):
    j.sync(client, NOW)
    fake.add_exec(BTC_PUT, t=NOW + H, exec_id="gap")
    assert j.sync(client, NOW + H + 1000).missed == []           # моложе GRACE
    assert [r["execId"] for r in j.sync(client, NOW + 2 * H).missed] == ["gap"]
    assert j.sync(client, NOW + 3 * H).missed == []              # один раз


def test_delivery_exec_uses_intrinsic_not_exec_price(j):
    _ex(j, BTC_CALL, "Buy", 0.01, 300, NOW)
    _ex(j, BTC_CALL, "Sell", 0.01, 65000, EXP_MS, fee="0", etype="Delivery")  # execPrice — цена актива?
    j.record_delivery_prices("BTC", [{"deliveryPrice": "71000", "deliveryTime": str(EXP_MS)}])
    rep = j.report(NOW - DAY_MS, EXP_MS + DAY_MS, "x")
    assert rep.realized == pytest.approx((1000 - 300) * 0.01) and rep.open_positions == {}


def test_delivery_exec_without_price_is_unknown(j):
    _ex(j, BTC_CALL, "Buy", 0.01, 300, NOW)
    _ex(j, BTC_CALL, "Sell", 0.01, 65000, EXP_MS, etype="Delivery")
    rep = j.report(NOW - DAY_MS, EXP_MS + DAY_MS, "x")
    assert rep.unknown_deliveries == [BTC_CALL] and rep.realized == 0


def test_bust_trade_counts_as_trade(j):
    _ex(j, BTC_PUT, "Buy", 0.01, 100, NOW)
    _ex(j, BTC_PUT, "Sell", 0.01, 80, NOW + 1, etype="BustTrade")
    rep = j.report(NOW - 1, NOW + 2, "x")
    assert (rep.trades, rep.deliveries) == (2, 0) and rep.sell_value == pytest.approx(0.8)
    assert rep.realized == pytest.approx(-0.2)


def test_delivery_price_from_delivery_prices_table(j):
    _ex(j, BTC_PUT, "Buy", 0.01, 100, NOW)
    j.record_deliveries([{"symbol": BTC_PUT, "deliveryTime": str(EXP_MS), "side": "Buy", "position": "0.01"}])
    j.record_delivery_prices("BTC", [{"deliveryPrice": "59000", "deliveryTime": str(EXP_MS + 1000)}])
    rep = j.report(NOW - 1, EXP_MS + DAY_MS, "x")
    assert rep.unknown_deliveries == [] and rep.realized == pytest.approx((1000 - 100) * 0.01)


def test_report_text_fits_telegram(j):
    for i in range(300):
        sym = f"BTC-27NOV26-{60000 + i * 5}-C-USDT"
        _ex(j, sym, "Buy", 0.01, 10, NOW + i)
    text = fmt_report(j.report(NOW - 1, NOW + 1000, "x"))
    assert len(text) <= 4096 and "… ещё" in text


def test_cli_sync_does_not_swallow_missed(j, client, fake):
    j.sync(client, NOW)
    fake.add_exec(BTC_PUT, t=NOW + H, exec_id="gap")
    assert j.sync(client, NOW + 2 * H, announce=False).missed == []   # ручная сверка
    assert [r["execId"] for r in j.sync(client, NOW + 3 * H).missed] == ["gap"]  # бот объявит


def test_first_sync_leaves_fresh_executions_for_ws(j, client, fake):
    e = fake.add_exec(BTC_PUT, t=NOW - 30_000, exec_id="fresh")      # сделка в первые секунды после старта
    assert j.sync(client, NOW).first_run
    assert j.record_executions([e], "ws") == [e]                      # WebSocket ещё уведомит
    fake.add_exec(BTC_PUT, t=NOW - 40_000, exec_id="fresh2")
    j.sync(client, NOW + 1)                     # уже не первая выгрузка: свежее ждёт WS или сводку
    assert [r["execId"] for r in j.sync(client, NOW + H).missed] == ["fresh2"]


def test_delivery_exec_side_follows_position(j):
    _ex(j, BTC_CALL, "Buy", 0.01, 300, NOW)
    _ex(j, BTC_CALL, "Buy", 0.01, 0, EXP_MS, fee="0", etype="Delivery")  # side «как у позиции»
    j.record_delivery_prices("BTC", [{"deliveryPrice": "71000", "deliveryTime": str(EXP_MS)}])
    rep = j.report(NOW - DAY_MS, EXP_MS + DAY_MS, "x")
    assert rep.open_positions == {} and rep.realized == pytest.approx(7)


def test_csv_source_cli(j):
    link = j.register_order(None, "CLI", source="cli")
    j.record_executions([exec_row(execId="c1", execTime=str(NOW), orderLinkId=link)], "ws")
    assert ",cli,CLI," in j.csv_bytes(NOW - 1, NOW + 1).decode("utf-8-sig")


def test_register_uses_short_timeout(j, cfg, monkeypatch):
    seen = []
    real = db.connect
    monkeypatch.setattr(db, "connect", lambda path, timeout=30: seen.append(timeout) or real(path, timeout))
    j.register_order(TRADER, "x")
    assert seen == [2]


def test_delivery_exec_of_position_older_than_history_no_fake_profit(j):
    """Позиция куплена до начала журнала: выплата при поставке — не прибыль (премия неизвестна)."""
    _ex(j, BTC_CALL, "Sell", 0.01, 0, EXP_MS, fee="0", etype="Delivery")
    j.record_delivery_prices("BTC", [{"deliveryPrice": "71000", "deliveryTime": str(EXP_MS)}])
    rep = j.report(NOW, EXP_MS + 1, "x")
    assert rep.realized == 0 and rep.unknown_entries == [BTC_CALL] and rep.open_positions == {}
    assert "вход раньше журнала" in fmt_report(rep)


def test_delivery_exec_older_position_uses_record_entry_price(j):
    _ex(j, BTC_CALL, "Sell", 0.01, 0, EXP_MS, fee="0", etype="Delivery")
    j.record_deliveries([{"symbol": BTC_CALL, "deliveryTime": str(EXP_MS), "side": "Buy", "position": "0.01",
                          "entryPrice": "450", "deliveryPrice": "71000"}])
    rep = j.report(NOW, EXP_MS + 1, "x")
    assert rep.realized == pytest.approx((1000 - 450) * 0.01) and rep.unknown_entries == []


def test_csv_neutralizes_formulas(j):
    link = j.register_order(TRADER, '=HYPERLINK("http://x","click")')
    j.record_executions([exec_row(execId="c1", execTime=str(NOW), orderLinkId=link)], "ws")
    row = j.csv_bytes(NOW - 1, NOW + 1).decode("utf-8-sig").splitlines()[1]
    assert "'=HYPERLINK" in row and ",=" not in row


def test_connect_passes_timeout_to_wal_switch(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(db, "_set_wal", lambda conn, timeout=30: seen.append(timeout))
    db.connect(str(tmp_path / "n.db"), timeout=2).close()
    assert seen == [2]


def test_partial_first_sync_does_not_hide_later_ws_gaps(j, client, fake):
    """Первая выгрузка прервалась; исполнения после неё (пропуск WS) объявляются, а не уходят в историю."""
    fake.add_exec(BTC_PUT, t=NOW - 20 * DAY_MS, exec_id="old")
    real = fake.get_executions
    fake.get_executions = lambda **kw: (_ for _ in ()).throw(ConnectionError("down"))
    res = j.sync(client, NOW)
    assert res.errors and res.first_run
    fake.get_executions = real
    fake.add_exec(BTC_PUT, t=NOW + H, exec_id="gap")          # WebSocket в это время лежал
    res = j.sync(client, NOW + 2 * H)
    assert res.first_run and [r["execId"] for r in res.missed] == ["gap"]   # "old" — история, не объявлен


def test_delivery_record_without_entry_price_no_fake_profit(j):
    j.record_deliveries([{"symbol": BTC_CALL, "deliveryTime": str(EXP_MS), "side": "Buy", "position": "0.01",
                          "deliveryPrice": "71000"}])                  # entryPrice биржа не дала
    rep = j.report(NOW, EXP_MS + 1, "x")
    assert rep.realized == 0 and rep.unknown_entries == [BTC_CALL] and rep.open_positions == {}


def test_intrinsic_rejects_nonstandard_symbol():
    assert intrinsic("BTC-27NOV26-60000-X-USDT", 65000) is None


def test_describe_many_one_query(j, cfg, monkeypatch):
    a, b_ = j.register_order(TRADER, "Павел"), j.register_order(11, "Наталья")
    opened = []
    real = db.connect
    monkeypatch.setattr(db, "connect", lambda *x, **kw: opened.append(1) or real(*x, **kw))
    out = j.describe_many([a, b_, a, "web-123", None])
    assert len(opened) == 1
    assert out[a] == "через бота · Павел" and out[b_] == "через бота · Наталья"
    assert out["web-123"] == out[None] == "вне бота"


def test_forget_order(j, cfg):
    link = j.register_order(TRADER, "x")
    j.forget_order(link)
    assert q(cfg, "SELECT COUNT(*) n FROM order_links")[0]["n"] == 0


@pytest.mark.parametrize("exc,kept", [("risk", 0), ("exchange", 0), ("network", 1)])
def test_bot_rejected_order_leaves_no_link(b, cfg, monkeypatch, exc, kept):
    """Отказ лимитов или биржи — записи «кто выставил» нет; сбой связи — запись остаётся (ордер мог дойти)."""
    from optbot.bybit_client import BybitError, BybitNetworkError, RiskError
    err = {"risk": RiskError("лимит"), "exchange": BybitError("10001: bad"),
           "network": BybitNetworkError("timeout")}[exc]
    tg = TG()
    run(b.cmd_buy(tg.upd(TRADER), tg.ctx(BTC_PUT, "0.01", "20")))
    real = b.c.place_order

    def fail(req):
        if exc == "network":
            req.sent = True
        raise err
    monkeypatch.setattr(b.c, "place_order", fail)
    monkeypatch.setattr(b.c, "order_status_after_failure", lambda req: "неизвестно")
    run(b.on_callback(tg.upd(TRADER, data=f"ok:{pid_from(tg)}"), tg.ctx()))
    assert q(cfg, "SELECT COUNT(*) n FROM order_links")[0]["n"] == kept


def test_cli_rejected_order_leaves_no_link(client, cfg, monkeypatch):
    from optbot.bybit_client import BybitError
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    monkeypatch.setattr(client, "place_order", lambda req: (_ for _ in ()).throw(BybitError("10001: bad")))
    with pytest.raises(BybitError):
        cli.submit(client, cli.OrderRequest(symbol=BTC_PUT, side="Buy", qty=__import__("decimal").Decimal("0.01"),
                                            price=__import__("decimal").Decimal("20")))
    assert q(cfg, "SELECT COUNT(*) n FROM order_links")[0]["n"] == 0


def test_ws_order_stream_failure_keeps_executions(cfg, monkeypatch, caplog):
    import optbot.ws as wsmod

    class WS:
        def __init__(self, **kw):
            self.subs = []

        def execution_stream(self, callback):
            self.subs.append("execution")

        def order_stream(self, callback):
            raise RuntimeError("topic denied")
    monkeypatch.setattr(wsmod, "WebSocket", WS)
    w = ExecutionWatcher(cfg, lambda rows: None, on_orders=lambda rows: None)
    w.start()  # не падает: уведомления об исполнениях работают
    assert w.ws.subs == ["execution"] and "подписка на ордера не удалась" in caplog.text


def test_sync_refreshes_active_orders_created_before_window(j, cfg, client, fake):
    """order/history окном видит ордер только по времени создания — активный ордер дочитывается по id."""
    o = fake.add_order(BTC_PUT, created_ms=NOW - 3 * DAY_MS)
    o["updatedTime"] = str(NOW - 3 * DAY_MS)
    j.sync(client, NOW - 3 * DAY_MS + H)
    assert q(cfg, "SELECT status FROM orders")[0]["status"] == "New"
    fake.orders.remove(o)
    o.update(orderStatus="Filled", cumExecQty=o["qty"], leavesQty="0", updatedTime=str(NOW))
    fake.history.append(o)
    j.sync(client, NOW + H)                             # окно [last-1ч, now] создание ордера не захватывает
    assert q(cfg, "SELECT status FROM orders")[0]["status"] == "Filled"
    assert any(c.get("orderId") == o["orderId"] for c in fake.called("get_order_history"))


def test_sync_job_summary_not_lost_if_undelivered(b, fake, monkeypatch):
    """Сводка не дошла ни до кого — исполнения снова «не объявлены», следующая сверка повторит."""
    tg = TG()
    b._app = NS(bot=tg.bot)
    run(b._sync_job(NS()))
    with db.connect(b.journal.cfg.db_file) as c:
        c.execute("UPDATE sync_state SET last_ms = last_ms - 3600000 WHERE stream = 'executions:history_until'")
    fake.add_exec(BTC_PUT, exec_id="missed", t=int(time.time() * 1000) - 5 * 60_000)

    async def down(*a, **kw):
        raise NetworkError("telegram down")
    tg.bot.send_message = down
    run(b._sync_job(NS()))
    tg.bot.send_message = tg._rec("send")
    run(b._sync_job(NS()))
    texts = [t for k, t, _ in tg.out if k == "send"]
    assert texts and "Сверка журнала: 1" in texts[0]


def test_sync_job_summary_error_keeps_unnotified(b, fake, monkeypatch):
    b._app = NS(bot=TG().bot)
    run(b._sync_job(NS()))
    with db.connect(b.journal.cfg.db_file) as c:
        c.execute("UPDATE sync_state SET last_ms = last_ms - 3600000 WHERE stream = 'executions:history_until'")
    fake.add_exec(BTC_PUT, exec_id="missed", t=int(time.time() * 1000) - 5 * 60_000)
    monkeypatch.setattr(b.journal, "describe_many", lambda ids: 1 / 0)
    run(b._sync_job(NS()))                               # не падает
    assert q(b.journal.cfg, "SELECT notified FROM executions WHERE exec_id='missed'")[0]["notified"] == 0
