"""Срочные уведомления администратору: доступ к бирже, лимиты риска, сделки вне бота, запуск/остановка."""
import asyncio
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest
import requests
from pybit.exceptions import FailedRequestError, InvalidRequestError

from optbot import bot as botmod
from optbot.bybit_client import BybitError, LimitError, OrderRequest, PortfolioLimitError, \
    PortfolioRiskError, RiskError
from optbot.bot import OptionsBot
from tests.fakes import EXP, set_chain
from tests.helpers import with_cfg
from tests.test_bot import BTC_PUT, exec_row

ADMIN = 50


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def ab(make_client, cfg):
    """Бот с администратором и «отправкой» в список."""
    c = with_cfg(cfg, tg_admin_ids={ADMIN})
    bot = OptionsBot(make_client(c), c)
    sent = []

    async def send(chat_id, text, **kw):
        sent.append((chat_id, text))
    bot._app = NS(bot=NS(send_message=send))
    bot.sent = sent
    return bot


# ---------- отправка и «тишина» ----------
def test_alert_quiet_per_key(ab, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(botmod.time, "monotonic", lambda: clock[0])
    assert run(ab.admin_alert("k", "раз"))
    assert not run(ab.admin_alert("k", "два"))                       # в «тишине» — нет
    assert run(ab.admin_alert("other", "другое"))                    # другой ключ — да
    clock[0] += ab.cfg.admin_alert_quiet_min * 60 + 1
    assert run(ab.admin_alert("k", "три"))                           # «тишина» прошла
    assert run(ab.admin_alert("k", "всегда", quiet=False))           # без «тишины» — всегда
    texts = [t for _, t in ab.sent]
    assert [c for c, _ in ab.sent] == [ADMIN] * 4 and "🚨 раз" in texts[0] and "два" not in " ".join(texts)


def test_alert_without_admins_not_sent(make_client, cfg):
    bot = OptionsBot(make_client(cfg), cfg)                           # TELEGRAM_ADMIN_IDS пуст
    bot._app = NS(bot=NS(send_message=lambda *a, **k: (_ for _ in ()).throw(AssertionError("sent"))))
    assert not run(bot.admin_alert("k", "x"))


def test_alert_threadsafe_from_thread(ab):
    async def go():
        ab._loop = asyncio.get_running_loop()
        tasks = []
        ab._app.create_task = lambda coro: tasks.append(asyncio.ensure_future(coro))
        await asyncio.to_thread(ab.alert_threadsafe, "t", "из потока")
        await asyncio.sleep(0)
        await asyncio.gather(*tasks)
    run(go())
    assert ab.sent and "из потока" in ab.sent[0][1]


def test_bot_wires_client_callbacks(ab):
    assert ab.c.on_limit == ab.alert_threadsafe and ab.c.on_access_error == ab.alert_threadsafe


# ---------- доступ к бирже ----------
@pytest.mark.parametrize("exc,key", [
    (InvalidRequestError("r", "API key is invalid.", 10003, "0", {}), "access:10003"),
    (InvalidRequestError("r", "Your api key has expired.", 33004, "0", {}), "access:33004"),
    (FailedRequestError("r", "Forbidden: region", 403, "0", {}), "access:http403"),
    (FailedRequestError("r", "Unauthorized", 401, "0", {}), "access:http401"),
])
def test_access_errors_notified(client, exc, key):
    got = []
    client.on_access_error = lambda k, t: got.append((k, t))

    def fail(**kw):
        raise exc
    with pytest.raises(BybitError):
        client._call(fail)
    assert got and got[0][0] == key and "Нет доступа к Bybit" in got[0][1]


def test_other_errors_not_access(client):
    got = []
    client.on_access_error = lambda k, t: got.append(k)
    for exc in (InvalidRequestError("r", "params error", 10001, "0", {}),
                FailedRequestError("r", "Bad Gateway", 502, "0", {}), requests.exceptions.ReadTimeout("t")):
        def fail(**kw):
            raise exc
        with pytest.raises(BybitError):
            client._call(fail)
    assert got == []


def test_callback_failure_does_not_break_call(client):
    client.on_access_error = lambda k, t: 1 / 0

    def fail(**kw):
        raise InvalidRequestError("r", "API key is invalid.", 10003, "0", {})
    with pytest.raises(BybitError, match="10003"):                    # ошибка биржи, а не ZeroDivisionError
        client._call(fail)


# ---------- лимиты риска ----------
def test_qty_limit_notified_and_is_limit_error(make_client, cfg, fake):
    set_chain(fake)
    c = with_cfg(cfg, max_order_qty={"BTC": "0.01"})
    cl = make_client(c)
    got = []
    cl.on_limit = lambda k, t: got.append((k, t))
    sym = f"BTC-{EXP}-65000-C-USDT"
    with pytest.raises(LimitError, match="MAX_QTY_BTC") as e:
        cl.prepare_order(OrderRequest(symbol=sym, side="Buy", qty=Decimal("0.02"), price=Decimal("100")))
    assert isinstance(e.value, RiskError)                             # старые обработчики ловят как раньше
    assert got == [("limit:MAX_QTY_BTC", got[0][1])] and "ордер не отправлен" in got[0][1]


def test_typo_check_is_not_limit(make_client, cfg, fake):
    """Защита от опечатки (цена далеко от mark) — не лимит риска: администратору не пишем."""
    set_chain(fake)
    cl = make_client(cfg)
    got = []
    cl.on_limit = lambda k, t: got.append(k)
    with pytest.raises(RiskError) as e:
        cl.prepare_order(OrderRequest(symbol=f"BTC-{EXP}-65000-C-USDT", side="Buy", qty=Decimal("0.01"),
                                      price=Decimal("99999")))
    assert not isinstance(e.value, LimitError) and got == []


def test_portfolio_limit_is_portfolio_and_limit(make_client, cfg, fake):
    set_chain(fake)
    c = with_cfg(cfg, max_position={"BTC": "0.01"})
    cl = make_client(c)
    got = []
    cl.on_limit = lambda k, t: got.append(k)
    sym = f"BTC-{EXP}-65000-C-USDT"
    t = cl.ticker(sym)
    r = OrderRequest(symbol=sym, side="Buy", qty=Decimal("0.02"), price=Decimal(t["ask1Price"]),
                     time_in_force="IOC", portfolio_checked=True, quote=t)
    r = cl.prepare_order(r)
    with pytest.raises(PortfolioLimitError) as e:
        cl.check_strategy([r])
    assert isinstance(e.value, PortfolioRiskError) and isinstance(e.value, LimitError)
    assert got == ["limit:MAX_POSITION_BTC"]


# ---------- сделки вне бота ----------
def test_external_execution_alerts_admin(ab):
    run(ab.on_executions([exec_row(orderLinkId="", orderId="ext123456")]))
    alerts = [t for c, t in ab.sent if "Сделка не через бота" in t]
    assert len(alerts) == 1 and BTC_PUT in alerts[0] and "Проверьте /pos" in alerts[0]


def test_bot_execution_no_alert(ab):
    run(ab.on_executions([exec_row(orderLinkId="ob-1")]))             # ордер бота
    assert not [t for _, t in ab.sent if "не через бота" in t]


def test_external_delivery_no_alert(ab):
    run(ab.on_executions([exec_row(orderLinkId="", execType="Delivery")]))  # поставка — не «сделка вне бота»
    assert not [t for _, t in ab.sent if "не через бота" in t]


# ---------- запуск и остановка ----------
def test_start_stop_and_abnormal_restart(ab):
    run(ab._announce_start())
    assert "Бот запущен" in ab.sent[-1][1] and "нештатной" not in ab.sent[-1][1]
    run(ab._announce_stop())
    assert "остановлен штатно" in ab.sent[-1][1]
    run(ab._announce_start())
    assert "нештатной" not in ab.sent[-1][1]                         # после штатной — обычный запуск
    run(ab._announce_start())                                         # без остановки (падение, kill)
    assert "Прошлая остановка была нештатной" in ab.sent[-1][1] and "/pos" in ab.sent[-1][1]


def test_failed_send_does_not_start_quiet(ab, monkeypatch):
    """Никому не дошло (Telegram недоступен) — «тишина» не начинается: повтор отправится."""
    from telegram.error import TelegramError
    real = ab._app.bot.send_message

    async def down(chat_id, text, **kw):
        raise TelegramError("down")
    ab._app.bot.send_message = down
    assert not run(ab.admin_alert("k", "раз"))
    ab._app.bot.send_message = real
    assert run(ab.admin_alert("k", "два")) and "два" in ab.sent[-1][1]


def test_access_error_in_response_dict_notified(client):
    got = []
    client.on_access_error = lambda k, t: got.append(k)
    with pytest.raises(BybitError):
        client._call(lambda **kw: {"retCode": 10005, "retMsg": "Permission denied", "result": {}})
    assert got == ["access:10005"]


def test_http_401_is_definite_not_network(client):
    """401/403 — отказ до обработки: определённая ошибка, а не «сбой связи, выясняю статус»."""
    from optbot.bybit_client import BybitNetworkError

    def fail(**kw):
        raise FailedRequestError("r", "Unauthorized", 401, "0", {})
    with pytest.raises(BybitError) as e:
        client._call(fail)
    assert not isinstance(e.value, BybitNetworkError)


def test_start_db_failure_still_announces(ab, monkeypatch):
    monkeypatch.setattr(ab, "_state", lambda *a: 1 / 0)
    run(ab._announce_start())
    assert "Бот запущен" in ab.sent[-1][1]


def test_sync_found_external_alerts_admin(ab, monkeypatch):
    missed = [exec_row(orderLinkId="", execId="e1"), exec_row(orderLinkId="ob-1", execId="e2")]
    monkeypatch.setattr(ab.journal, "sync", lambda c: NS(errors=[], missed=missed))
    run(ab._sync_job(None))
    alerts = [t for _, t in ab.sent if "найдены сверкой" in t]
    assert len(alerts) == 1 and alerts[0].count(BTC_PUT) == 1


def test_sync_external_not_alerted_when_summary_failed(ab, monkeypatch):
    """Сводка сверки никому не дошла (исполнения вернутся в следующую сверку) — срочное тогда, не сейчас."""
    missed = [exec_row(orderLinkId="", execId="e1")]
    monkeypatch.setattr(ab.journal, "sync", lambda c: NS(errors=[], missed=missed))
    monkeypatch.setattr(ab.journal, "unnotify", lambda ids: None)

    async def nobody(msg, what):
        return 0
    monkeypatch.setattr(ab, "_broadcast", nobody)
    run(ab._sync_job(None))
    assert not [t for _, t in ab.sent if "найдены сверкой" in t]


# ---------- ревью 2 ----------
def test_concurrent_same_key_one_message(ab):
    """Одновременные одинаковые (сбой ключа сразу в нескольких запросах) — одно сообщение, а не по числу запросов."""
    real = ab._app.bot.send_message

    async def slow(chat_id, text, **kw):
        await asyncio.sleep(0.01)
        await real(chat_id, text, **kw)
    ab._app.bot.send_message = slow

    async def go():
        return await asyncio.gather(*(ab.admin_alert("access:10003", "нет доступа") for _ in range(4)))
    assert sorted(run(go())) == [False, False, False, True] and len(ab.sent) == 1


def test_important_alert_retried_until_delivered(ab, monkeypatch):
    from telegram.error import TelegramError
    real, n = ab._app.bot.send_message, []

    async def flaky(chat_id, text, **kw):
        n.append(1)
        if len(n) <= 2:
            raise TelegramError("down")
        await real(chat_id, text, **kw)
    ab._app.bot.send_message = flaky
    monkeypatch.setattr(ab, "ALERT_RETRY_S", (0, 0, 0))

    async def go():
        ab._loop = asyncio.get_running_loop()
        assert not await ab.admin_alert("external:x", "сделка вне бота", quiet=False, retry=True)
        while ab._alert_retries:
            await asyncio.gather(*ab._alert_retries)
    run(go())
    assert len(n) == 3 and "сделка вне бота" in ab.sent[-1][1] and "доставлено с задержкой" in ab.sent[-1][1]


def test_unexpected_send_error_releases_key(ab, monkeypatch):
    async def boom(msg):
        raise RuntimeError("client closed")
    monkeypatch.setattr(ab, "_send_admins", boom)
    assert not run(ab.admin_alert("k", "раз"))
    assert "k" not in ab._alert_last


def test_retries_cancelled_on_stop(ab):
    """Повтор недошедшего (спит до минут) снимается при остановке — не держит её до kill по таймауту."""
    async def go():
        ab._loop = asyncio.get_running_loop()
        t = ab._loop.create_task(asyncio.sleep(3600))
        ab._alert_retries[t] = "Многоногий ордер исполнен не полностью"
        app = ab.build_app()
        await app.post_stop(app)
        await asyncio.sleep(0)
        return t.cancelled()
    assert run(go())
    stop = ab.sent[-1][1]                                            # недоставленное — в сообщении об остановке
    assert "остановлен штатно" in stop and "Не доставлено раньше" in stop and "не полностью" in stop


def test_start_write_failure_keeps_abnormal_warning(ab, monkeypatch):
    real = ab._state

    def state(key, value=None):
        if value is not None:
            raise RuntimeError("database is locked")
        return "1731900000"                                          # прошлый запуск не снял отметку
    monkeypatch.setattr(ab, "_state", state)
    run(ab._announce_start())
    assert "Прошлая остановка была нештатной" in ab.sent[-1][1]


def test_external_batch_one_alert(ab):
    run(ab.on_executions([exec_row(orderLinkId="", orderId="a1"), exec_row(orderLinkId="", orderId="b2")]))
    alerts = [t for _, t in ab.sent if "Сделка не через бота" in t]
    assert len(alerts) == 1 and alerts[0].count("Сделка не через бота") == 2


def test_external_batch_capped(ab):
    run(ab.on_executions([exec_row(orderLinkId="", orderId=f"o{i:02d}") for i in range(15)]))
    alerts = [t for _, t in ab.sent if "Сделка не через бота" in t]
    assert len(alerts) == 1 and alerts[0].count("Сделка не через бота") == 10 and "и ещё 5" in alerts[0]
    assert len(alerts[0]) < 4096
