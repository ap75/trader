"""Telegram-бот на подставном Telegram и подставном Bybit: доступ, подтверждение, сбои, уведомления."""
import asyncio
import html
import re
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
import requests
from telegram.error import Forbidden, NetworkError
from telegram.ext import ApplicationHandlerStop

from optbot import bot as botmod
from optbot.bot import OptionsBot
from tests.helpers import with_cfg
from tests.fakes import BTC_PUT

TRADER, VIEWER, STRANGER = 10, 20, 30
GROUP, GROUP2, FOREIGN = -100111, -100222, -100999


def strip(t):
    return html.unescape(re.sub(r"<[^>]+>", "", t or ""))


class TG:
    """Записывает всё, что бот отправил или отредактировал."""

    def __init__(self):
        self.out: list[tuple[str, str, list]] = []  # (вид, текст, callback_data кнопок)
        self.n = 1000
        self.bot = NS(leave_chat=AsyncMock(), send_message=self._rec("send"))

    def _rec(self, kind):
        async def f(*a, **kw):
            text = kw.get("text", a[-1] if a else "")
            mk = kw.get("reply_markup")
            btns = [b.callback_data for r in mk.inline_keyboard for b in r] if hasattr(mk, "inline_keyboard") else []
            self.out.append((kind, strip(text), btns))
            self.n += 1
            return NS(edit_text=self._rec("edit"), chat_id=0, message_id=self.n)
        return f

    def last(self, kind=None) -> str:
        return next(t for k, t, _ in reversed(self.out) if kind in (None, k))

    def buttons(self) -> list:
        return next(b for _, _, b in reversed(self.out) if b)

    def ctx(self, *args):
        return NS(args=list(args), bot=self.bot, application=NS(create_task=lambda coro: coro.close()))

    def upd(self, uid, chat="dm", data=None, sender_chat=None, text=None, reply_to=None):
        chats = {"dm": NS(id=uid, type="private", title=None),
                 "g": NS(id=GROUP, type="supergroup", title="Desk"),
                 "foreign": NS(id=FOREIGN, type="supergroup", title="Чужая")}
        c = chats[chat]
        u = NS(id=uid, full_name=f"user{uid}")
        m = NS(sender_chat=sender_chat, reply_text=self._rec("reply"), chat=c, chat_id=c.id, text=text,
               reply_to_message=NS(message_id=reply_to) if reply_to else None)
        q = None
        if data:
            q = NS(data=data, from_user=u, answer=AsyncMock(), edit_message_text=self._rec("edit"),
                   message=NS(edit_text=self._rec("edit")))
        return NS(effective_user=u, effective_chat=c, effective_message=m, callback_query=q, my_chat_member=None)


@pytest.fixture
def tg():
    return TG()


@pytest.fixture
def make_bot(make_client):
    def make(cfg):
        return OptionsBot(make_client(cfg), cfg)
    return make


@pytest.fixture
def b(make_bot, cfg):
    return make_bot(cfg)


def run(coro):
    return asyncio.run(coro)


def pid_from(tg) -> str:
    return tg.buttons()[0].split(":")[1]


# ---------- доступ ----------
def test_gate_dm_and_allowed_group_pass(b, tg):
    run(b.gate(tg.upd(TRADER, "dm"), tg.ctx()))
    run(b.gate(tg.upd(TRADER, "g"), tg.ctx()))


def test_gate_foreign_group_leaves(b, tg):
    with pytest.raises(ApplicationHandlerStop):
        run(b.gate(tg.upd(TRADER, "foreign"), tg.ctx()))
    tg.bot.leave_chat.assert_awaited_once_with(FOREIGN)


@pytest.mark.parametrize("uid,chat,sender,view,trade", [
    (TRADER, "dm", None, True, True),
    (VIEWER, "dm", None, True, False),
    (STRANGER, "dm", None, False, False),
    (TRADER, "g", NS(title="Desk"), False, False),  # анонимный админ / от имени чата
    (777000, "dm", None, False, False),             # служебный аккаунт Telegram
])
def test_roles(b, tg, uid, chat, sender, view, trade):
    w = b.who(tg.upd(uid, chat, sender_chat=sender))
    assert (w.can_view, w.can_trade) == (view, trade)


def test_allow_all_group_members_view(make_bot, cfg, tg):
    bb = make_bot(with_cfg(cfg, allow_all_group_members_view=True))
    assert bb.who(tg.upd(STRANGER, "g")).can_view
    assert not bb.who(tg.upd(STRANGER, "g")).can_trade
    assert not bb.who(tg.upd(STRANGER, "dm")).can_view


def test_id_available_to_anyone(b, tg):
    run(b.cmd_id(tg.upd(STRANGER), tg.ctx()))
    assert str(STRANGER) in tg.last()


def test_viewer_cannot_trade(b, tg):
    run(b.cmd_buy(tg.upd(VIEWER), tg.ctx(BTC_PUT, "0.01", "20")))
    assert "Нет прав на торговлю" in tg.last()
    assert b.c.http.called("place_order") == []


def test_stranger_gets_silence(b, tg):
    run(b.cmd_buy(tg.upd(STRANGER), tg.ctx(BTC_PUT, "0.01", "20")))
    run(b.cmd_bal(tg.upd(STRANGER), tg.ctx()))
    assert tg.out == []


def test_anonymous_admin_cannot_trade(b, tg):
    run(b.cmd_buy(tg.upd(TRADER, "g", sender_chat=NS(title="Desk")), tg.ctx(BTC_PUT, "0.01", "20")))
    assert "анонимность" in tg.last()


def test_sensitive_dm_only(b, tg):
    run(b.cmd_bal(tg.upd(VIEWER, "g"), tg.ctx()))
    assert "только в личке" in tg.last()
    run(b.cmd_bal(tg.upd(VIEWER, "dm"), tg.ctx()))
    assert "только в личке" not in tg.last()


def test_sensitive_allowed_in_group_when_flag_off(make_bot, cfg, tg):
    bb = make_bot(with_cfg(cfg, sensitive_commands_dm_only=False))
    run(bb.cmd_bal(tg.upd(VIEWER, "g"), tg.ctx()))
    assert "только в личке" not in tg.last()


def test_viewer_button_denied(b, tg):
    u = tg.upd(VIEWER, data="ok:abc")
    run(b.on_callback(u, tg.ctx()))
    u.callback_query.answer.assert_awaited_with("Нет прав")


# ---------- подтверждение ----------
def test_buy_preview_then_confirm(b, tg):
    run(b.cmd_buy(tg.upd(TRADER), tg.ctx(BTC_PUT, "0.01", "20")))
    assert "BUY 0.01" in tg.last() and "встанет в стакан" in tg.last()
    assert b.c.http.called("place_order") == []          # до подтверждения ничего не отправлено
    pid = pid_from(tg)
    run(b.on_callback(tg.upd(TRADER, data=f"ok:{pid}"), tg.ctx()))
    assert "Ордер принят" in tg.last("edit")
    assert len(b.c.http.called("place_order")) == 1


def test_only_initiator_confirms(make_bot, cfg, tg):
    bb = make_bot(with_cfg(cfg, tg_trader_ids={TRADER, 11}))
    run(bb.cmd_buy(tg.upd(TRADER, "g"), tg.ctx(BTC_PUT, "0.01", "20")))
    u = tg.upd(11, "g", data=f"ok:{pid_from(tg)}")
    run(bb.on_callback(u, tg.ctx()))
    u.callback_query.answer.assert_awaited_with("Подтвердить может только инициатор", show_alert=True)
    assert bb.c.http.called("place_order") == []


def test_double_press_sends_once(b, tg):
    run(b.cmd_buy(tg.upd(TRADER), tg.ctx(BTC_PUT, "0.01", "20")))
    pid = pid_from(tg)
    run(b.on_callback(tg.upd(TRADER, data=f"ok:{pid}"), tg.ctx()))
    u = tg.upd(TRADER, data=f"ok:{pid}")
    run(b.on_callback(u, tg.ctx()))
    u.callback_query.answer.assert_awaited_with("Подтверждение устарело")
    assert len(b.c.http.called("place_order")) == 1


def test_cancel_button(b, tg):
    run(b.cmd_buy(tg.upd(TRADER), tg.ctx(BTC_PUT, "0.01", "20")))
    run(b.on_callback(tg.upd(TRADER, data=f"no:{pid_from(tg)}"), tg.ctx()))
    assert "Отменено" in tg.last("edit") and b.c.http.called("place_order") == []


def test_confirm_expires(b, tg):
    run(b.cmd_buy(tg.upd(TRADER), tg.ctx(BTC_PUT, "0.01", "20")))
    pid = pid_from(tg)
    b.pending[pid].created -= botmod.PENDING_TTL + 1
    run(b.on_callback(tg.upd(TRADER, data=f"ok:{pid}"), tg.ctx()))
    assert "истекло" in tg.last("edit") and b.c.http.called("place_order") == []


def test_risk_error_shown(b, tg):
    run(b.cmd_buy(tg.upd(TRADER), tg.ctx(BTC_PUT, "0.01", "1500")))
    assert "Ошибка" in tg.last() and "Опечатка" in tg.last()


def test_network_failure_after_send_reports_status(b, tg):
    run(b.cmd_buy(tg.upd(TRADER), tg.ctx(BTC_PUT, "0.01", "20")))
    b.c.http.after["place_order"] = requests.exceptions.ReadTimeout("timeout")
    run(b.on_callback(tg.upd(TRADER, data=f"ok:{pid_from(tg)}"), tg.ctx()))
    assert "Выясняю статус" in " ".join(t for k, t, _ in tg.out if k == "edit")
    final = tg.last("edit")
    assert "Сбой связи" in final and "ордер НА БИРЖЕ" in final and "Не повторяйте" in final


def test_unexpected_error_does_not_hang(b, tg):
    run(b.cmd_buy(tg.upd(TRADER), tg.ctx(BTC_PUT, "0.01", "20")))
    b.c.place_order = lambda req: 1 / 0  # сбой в коде бота/клиента, не ответ биржи
    run(b.on_callback(tg.upd(TRADER, data=f"ok:{pid_from(tg)}"), tg.ctx()))
    assert "Внутренняя ошибка" in tg.last("edit")


def test_group_notified_about_trade(b, tg):
    run(b.cmd_buy(tg.upd(TRADER, "g"), tg.ctx(BTC_PUT, "0.01", "20")))
    ctx = tg.ctx()
    tasks = []
    ctx.application = NS(create_task=lambda coro: tasks.append(coro))

    async def go():
        await b.on_callback(tg.upd(TRADER, "g", data=f"ok:{pid_from(tg)}"), ctx)
        for t in tasks:
            if asyncio.iscoroutine(t) and t.cr_code.co_name != "_expire":
                await t
            else:
                t.close()
    run(go())
    sent = [t for k, t, _ in tg.out if k == "send"]
    assert sent and all("Ордер принят" in s and "orderId" in s for s in sent)
    assert len(sent) == 1  # исходная группа -100111 уведомление не получает, вторая — получает


# ---------- изменение «Своя цена…» ----------
def test_late_reply_to_custom_price(b, tg):
    b.awaiting[(TRADER, 99)] = ("deadtoken", time.monotonic())
    run(b.on_reply(tg.upd(TRADER, text="160", reply_to=99), tg.ctx()))
    assert "Меню устарело" in tg.last()


def test_unrelated_reply_ignored(b, tg):
    run(b.on_reply(tg.upd(TRADER, text="hello", reply_to=12345), tg.ctx()))
    assert tg.out == []


def test_amend_via_command(b, tg):
    o = b.c.http.add_order(BTC_PUT, qty="0.05", price="20")
    run(b.cmd_amend(tg.upd(TRADER), tg.ctx(BTC_PUT, o["orderId"], "25")))
    assert "25" in tg.last()
    run(b.on_callback(tg.upd(TRADER, data=f"ok:{pid_from(tg)}"), tg.ctx()))
    assert "Ордер изменён" in tg.last("edit") and o["price"] == "25"


# ---------- исполнения ----------
def exec_row(**kw):
    r = dict(orderId="x1", symbol=BTC_PUT, side="Buy", execQty="0.01", execPrice="150", execFee="0.09",
             execType="Trade", orderLinkId="ob-1", leavesQty="0")
    r.update(kw)
    return r


def test_executions_to_groups_and_traders(b, tg):
    b._app = NS(bot=tg.bot)
    run(b.on_executions([exec_row()]))
    texts = [t for k, t, _ in tg.out if k == "send"]
    assert len(texts) == 3 and all("Исполнение" in t for t in texts)  # 2 группы + 1 трейдер


def test_dm_disabled_only_on_permanent_errors(b, tg):
    async def send(chat_id, text, **kw):
        if chat_id == TRADER:
            raise NetworkError("blip")
        if chat_id == 11:
            raise Forbidden("bot was blocked by the user")
    b.cfg = with_cfg(b.cfg, tg_trader_ids={TRADER, 11})
    b._app = NS(bot=NS(send_message=send))
    run(b.on_executions([exec_row()]))
    assert b._dm_failed == {11}


def test_execution_outside_bot_marked(b, tg):
    b._app = NS(bot=tg.bot)
    run(b.on_executions([exec_row(orderLinkId="")]))
    assert "вне бота" in tg.last("send")


# ---------- отмена ----------
def test_cancel_network_failure_reports_status(b, tg):
    o = b.c.http.add_order(BTC_PUT)
    b.c.http.fail["cancel_order"] = requests.exceptions.ReadTimeout("timeout")
    run(b.cmd_cancel(tg.upd(TRADER), tg.ctx(BTC_PUT, o["orderId"])))
    assert "ВСЁ ЕЩЁ АКТИВЕН" in tg.last()


def test_cancel_ok(b, tg):
    o = b.c.http.add_order(BTC_PUT)
    run(b.cmd_cancel(tg.upd(TRADER), tg.ctx(BTC_PUT, o["orderId"])))
    assert "Отменён" in tg.last() and b.c.http.orders == []


# ---------- кнопки /orders и /pos ----------
def test_orders_buttons_and_amend_menu(b, tg):
    o = b.c.http.add_order(BTC_PUT, qty="0.05", price="20")
    run(b.cmd_orders(tg.upd(TRADER), tg.ctx()))
    btns = tg.buttons()
    am = next(x for x in btns if x.startswith("am:"))
    assert any(x.startswith("cx:") for x in btns)
    run(b.on_callback(tg.upd(TRADER, data=am), tg.ctx()))
    menu = tg.buttons()
    assert {x.rsplit(":", 1)[1] for x in menu} >= {"bid", "mid", "ask", "mark", "other", "x"}
    run(b.on_callback(tg.upd(TRADER, data=next(x for x in menu if x.endswith(":mid"))), tg.ctx()))
    run(b.on_callback(tg.upd(TRADER, data=f"ok:{pid_from(tg)}"), tg.ctx()))
    assert "Ордер изменён" in tg.last("edit") and o["price"] == "100"  # mid (95+105)/2


def test_viewer_sees_orders_without_buttons(b, tg):
    b.c.http.add_order(BTC_PUT)
    run(b.cmd_orders(tg.upd(VIEWER), tg.ctx()))
    assert tg.out[-1][2] == []


def test_amend_menu_other_user(make_bot, cfg, tg):
    bb = make_bot(with_cfg(cfg, tg_trader_ids={TRADER, 11}))
    bb.c.http.add_order(BTC_PUT)
    run(bb.cmd_orders(tg.upd(TRADER), tg.ctx()))
    run(bb.on_callback(tg.upd(TRADER, data=next(x for x in tg.buttons() if x.startswith("am:"))), tg.ctx()))
    u = tg.upd(11, data=next(x for x in tg.buttons() if x.endswith(":mid")))
    run(bb.on_callback(u, tg.ctx()))
    u.callback_query.answer.assert_awaited_with("Это меню другого пользователя", show_alert=True)


def test_custom_price_reply(b, tg):
    o = b.c.http.add_order(BTC_PUT, qty="0.05", price="20")
    run(b.cmd_orders(tg.upd(TRADER), tg.ctx()))
    run(b.on_callback(tg.upd(TRADER, data=next(x for x in tg.buttons() if x.startswith("am:"))), tg.ctx()))
    run(b.on_callback(tg.upd(TRADER, data=next(x for x in tg.buttons() if x.endswith(":other"))), tg.ctx()))
    (chat, mid), _ = next(iter(b.awaiting.items()))
    u = tg.upd(TRADER, text="30 0.04", reply_to=mid)
    u.effective_message.chat_id = chat
    run(b.on_reply(u, tg.ctx()))
    run(b.on_callback(tg.upd(TRADER, data=f"ok:{pid_from(tg)}"), tg.ctx()))
    assert (o["price"], o["qty"]) == ("30", "0.04")


def test_cancel_button_from_orders(b, tg):
    b.c.http.add_order(BTC_PUT)
    run(b.cmd_orders(tg.upd(TRADER), tg.ctx()))
    run(b.on_callback(tg.upd(TRADER, data=next(x for x in tg.buttons() if x.startswith("cx:"))), tg.ctx()))
    assert b.c.http.orders == []


@pytest.mark.parametrize("how,otype,price", [("mkt", "Market", None), ("mid", "Limit", "100")])
def test_close_buttons(b, tg, how, otype, price):
    b.c.http.positions.append({"symbol": BTC_PUT, "side": "Buy", "size": "0.03", "avgPrice": "90",
                               "markPrice": "100", "unrealisedPnl": "0.3"})
    run(b.cmd_pos(tg.upd(TRADER), tg.ctx()))
    run(b.on_callback(tg.upd(TRADER, data=next(x for x in tg.buttons() if x.endswith(":" + how))), tg.ctx()))
    run(b.on_callback(tg.upd(TRADER, data=f"ok:{pid_from(tg)}"), tg.ctx()))
    sent = b.c.http.called("place_order")[0]
    assert (sent["orderType"], sent["side"], sent["reduceOnly"], sent.get("price")) == (otype, "Sell", True, price)


def test_close_mid_without_quotes(b, tg):
    b.c.http.positions.append({"symbol": BTC_PUT, "side": "Buy", "size": "0.03"})
    b.c.http.tickers[BTC_PUT].update(bid1Price="0", ask1Price="0", markPrice="0")
    run(b.cmd_pos(tg.upd(TRADER), tg.ctx()))
    run(b.on_callback(tg.upd(TRADER, data=next(x for x in tg.buttons() if x.endswith(":mid"))), tg.ctx()))
    assert "> 0" in tg.last() and b.c.http.called("place_order") == []


def test_stale_token(b, tg):
    run(b.on_callback(tg.upd(TRADER, data="cx:nonexistent"), tg.ctx()))
    assert "устарела" in tg.last()


# ---------- рынок ----------
def test_chain_and_ticker(b, tg):
    run(b.cmd_chain(tg.upd(VIEWER), tg.ctx("BTC", "27NOV26", "1")))
    assert "*65000" in tg.last() and "Strike" in tg.last()
    run(b.cmd_ticker(tg.upd(VIEWER), tg.ctx("BTC-27NOV26-60000-P")))
    assert "60000" in tg.last()


def test_cancelall_confirm(b, tg):
    b.c.http.add_order(BTC_PUT)
    run(b.cmd_cancelall(tg.upd(TRADER), tg.ctx("BTC")))
    run(b.on_callback(tg.upd(TRADER, data=f"ok:{pid_from(tg)}"), tg.ctx()))
    assert "Отменено ордеров: 1" in tg.last("edit")
