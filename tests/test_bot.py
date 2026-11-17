"""Telegram-бот на подставном Telegram и подставном Bybit: доступ, подтверждение, сбои, уведомления."""
import asyncio
import contextlib
import html
import os
import re
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
import requests
from telegram.error import BadRequest, Forbidden, NetworkError, TimedOut
from telegram.ext import ApplicationHandlerStop

from optbot import bot as botmod
from optbot.bot import GUIDE_LANGS, HELP, OptionsBot
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


def test_unparsed_response_after_send_looked_up_not_forgotten(b, tg):
    """Ответ на отправку не разобран (ордер ушёл) — как сбой связи: статус по orderLinkId, запись журнала цела."""
    run(b.cmd_buy(tg.upd(TRADER), tg.ctx(BTC_PUT, "0.01", "20")))
    b.c.http.after["place_order"] = KeyError("result")
    run(b.on_callback(tg.upd(TRADER, data=f"ok:{pid_from(tg)}"), tg.ctx()))
    final = tg.last("edit")
    assert "ордер НА БИРЖЕ" in final and "Не повторяйте" in final
    link = b.c.http.called("place_order")[0]["orderLinkId"]
    conn = __import__("optbot.db", fromlist=["db"]).connect(b.cfg.db_file)
    try:
        assert conn.execute("SELECT COUNT(*) FROM order_links WHERE link_id = ?", (link,)).fetchone()[0] == 1
    finally:
        conn.close()


def test_symbol_held_until_status_lookup_after_network_error(b, tg):
    """Сбой связи после отправки ручного ордера — символ занят до выяснения статуса: сделка по сигналу с ним
    в это время не начнётся (ордер мог дойти до биржи); после выяснения — свободен."""
    run(b.cmd_buy(tg.upd(TRADER), tg.ctx(BTC_PUT, "0.01", "20")))
    b.c.http.after["place_order"] = requests.exceptions.ReadTimeout("timeout")
    seen = {}

    def lookup(req, *a, **kw):
        seen["held"] = dict(b._guard._manual)
        try:
            b._guard.claim_exec({req.symbol})
        except ValueError as e:
            seen["refused"] = str(e)
        return "найден"
    b.c.order_status_after_failure = lookup
    run(b.on_callback(tg.upd(TRADER, data=f"ok:{pid_from(tg)}"), tg.ctx()))
    assert seen["held"] and "ручной ордер" in seen["refused"]
    assert not b._guard.busy                                         # после выяснения — свободен


def test_symbol_released_on_network_error_before_send(b, tg):
    run(b.cmd_buy(tg.upd(TRADER), tg.ctx(BTC_PUT, "0.01", "20")))
    real = b.c.place_order

    from optbot.bybit_client import BybitNetworkError

    def fail(req):
        raise BybitNetworkError("до отправки")                       # req.sent не выставлен
    b.c.place_order = fail
    run(b.on_callback(tg.upd(TRADER, data=f"ok:{pid_from(tg)}"), tg.ctx()))
    b.c.place_order = real
    assert "Сбой связи" in tg.last("edit") and not b._guard.busy


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


def _open_custom_price(bb, tg, uid=TRADER, chat="dm"):
    run(bb.cmd_orders(tg.upd(uid, chat=chat), tg.ctx()))
    run(bb.on_callback(tg.upd(uid, chat=chat, data=next(x for x in tg.buttons() if x.startswith("am:"))), tg.ctx()))
    u = tg.upd(uid, chat=chat, data=next(x for x in tg.buttons() if x.endswith(":other")))
    run(bb.on_callback(u, tg.ctx()))
    return u


def test_custom_price_prompt_only_for_menu_owner(b, tg):
    """Поле ответа открывается только у автора меню (selective + упоминание), а не у всей группы."""
    b.c.http.add_order(BTC_PUT)
    seen = {}
    orig = TG._rec(tg, "reply")

    async def rec(*a, **kw):
        seen.update(kw, text=a[0] if a else kw.get("text"))
        return await orig(*a, **kw)
    run(b.cmd_orders(tg.upd(TRADER, ), tg.ctx()))
    run(b.on_callback(tg.upd(TRADER, data=next(x for x in tg.buttons() if x.startswith("am:"))), tg.ctx()))
    u = tg.upd(TRADER, data=next(x for x in tg.buttons() if x.endswith(":other")))
    u.effective_message.reply_text = rec
    run(b.on_callback(u, tg.ctx()))
    assert seen["reply_markup"].selective is True
    assert f'href="tg://user?id={TRADER}"' in seen["text"] and seen["parse_mode"] == "HTML"


def test_reply_to_someone_elses_prompt_explains(make_bot, cfg, tg):
    """Второй трейдер ответил на чужой вопрос «Своя цена…» — не молчим, объясняем; ордер не трогаем."""
    bb = make_bot(with_cfg(cfg, tg_trader_ids={TRADER, 11}))
    o = bb.c.http.add_order(BTC_PUT, qty="0.05", price="20")
    _open_custom_price(bb, tg)
    (chat, mid), _ = next(iter(bb.awaiting.items()))
    u = tg.upd(11, text="160", reply_to=mid)
    u.effective_message.chat_id = chat
    run(bb.on_reply(u, tg.ctx()))
    assert "Этот вопрос для user10" in tg.last() and "/orders" in tg.last()
    assert o["price"] == "20" and (chat, mid) in bb.awaiting  # автор меню всё ещё может ответить


def test_amend_rejected_if_changed_by_other_after_preview(make_bot, cfg, tg):
    """Оба открыли превью; первый подтвердил — подтверждение второго не откатывает его изменение."""
    bb = make_bot(with_cfg(cfg, tg_trader_ids={TRADER, 11}))
    o = bb.c.http.add_order(BTC_PUT, qty="0.05", price="20")
    run(bb.cmd_amend(tg.upd(TRADER), tg.ctx(BTC_PUT, o["orderId"], "25")))
    pid_a = pid_from(tg)
    run(bb.cmd_amend(tg.upd(11), tg.ctx(BTC_PUT, o["orderId"], "20", "0.04")))
    pid_b = pid_from(tg)
    run(bb.on_callback(tg.upd(TRADER, data=f"ok:{pid_a}"), tg.ctx()))
    assert o["price"] == "25"
    run(bb.on_callback(tg.upd(11, data=f"ok:{pid_b}"), tg.ctx()))
    assert "изменили после превью" in tg.last("edit") and "0.05 @ 25" in tg.last("edit")
    assert (o["qty"], o["price"]) == ("0.05", "25")
    assert len(bb.c.http.called("amend_order")) == 1


def _race_bot(make_bot, cfg, tg, monkeypatch):
    """Два трейдера с превью одного ордера; чтение ордера медленное — проверка и отправка разнесены."""
    bb = make_bot(with_cfg(cfg, tg_trader_ids={TRADER, 11}))
    o = bb.c.http.add_order(BTC_PUT, qty="0.05", price="20")
    run(bb.cmd_amend(tg.upd(TRADER), tg.ctx(BTC_PUT, o["orderId"], "25")))
    pid_a = pid_from(tg)
    run(bb.cmd_amend(tg.upd(11), tg.ctx(BTC_PUT, o["orderId"], "20", "0.04")))
    pid_b = pid_from(tg)
    slow = bb.c.get_order

    def get_order(*a, **kw):
        r = slow(*a, **kw)
        r = dict(r) if r else r  # снимок, как от настоящего API (подделка отдаёт живой dict)
        time.sleep(0.2)  # оба подтверждения успевают прочитать ордер до отправки
        return r
    monkeypatch.setattr(bb.c, "get_order", get_order)

    async def both():
        await asyncio.gather(bb.on_callback(tg.upd(TRADER, data=f"ok:{pid_a}"), tg.ctx()),
                             bb.on_callback(tg.upd(11, data=f"ok:{pid_b}"), tg.ctx()))
    return bb, o, both


def test_simultaneous_amend_confirms_serialized(make_bot, cfg, tg, monkeypatch):
    bb, o, both = _race_bot(make_bot, cfg, tg, monkeypatch)
    run(both())
    assert len(bb.c.http.called("amend_order")) == 1
    assert sum("изменили после превью" in t for k, t, _ in tg.out if k == "edit") == 1
    assert bb._order_locks == {}  # блокировки не копятся


def test_race_reproduces_without_lock(make_bot, cfg, tg, monkeypatch):
    """Контроль: без блокировки тот же сценарий даёт две отправки — тест выше действительно ловит гонку."""
    bb, o, both = _race_bot(make_bot, cfg, tg, monkeypatch)
    @contextlib.asynccontextmanager
    async def no_lock(oid):
        yield
    monkeypatch.setattr(bb, "_order_lock", no_lock)
    run(both())
    assert len(bb.c.http.called("amend_order")) == 2


def test_cancel_waits_for_amend_on_same_order(make_bot, cfg, tg, monkeypatch):
    bb, o, _ = _race_bot(make_bot, cfg, tg, monkeypatch)
    pid_a = next(pid for pid, p in bb.pending.items() if p.who.user_id == TRADER)

    async def go():
        await asyncio.gather(bb.on_callback(tg.upd(TRADER, data=f"ok:{pid_a}"), tg.ctx()),
                             bb.cmd_cancel(tg.upd(11), tg.ctx(BTC_PUT, o["orderId"])))
    run(go())
    names = [n for n, _ in bb.c.http.calls if n in ("amend_order", "cancel_order")]
    assert names == ["amend_order", "cancel_order"] and bb.c.http.orders == []


def test_cancel_not_blocked_by_status_lookup(make_bot, cfg, tg, monkeypatch):
    """Сбой связи при amend: выяснение статуса идёт без блокировки — отмена ордера другим трейдером не ждёт."""
    from optbot.bybit_client import BybitNetworkError
    bb = make_bot(with_cfg(cfg, tg_trader_ids={TRADER, 11}))
    o = bb.c.http.add_order(BTC_PUT, qty="0.05", price="20")
    run(bb.cmd_amend(tg.upd(TRADER), tg.ctx(BTC_PUT, o["orderId"], "25")))
    pid = pid_from(tg)
    bb.c.http.after["amend_order"] = BybitNetworkError("timeout")
    order = []

    def lookup(*a, **kw):
        time.sleep(0.3)
        order.append("lookup")
        return "статус"
    monkeypatch.setattr(bb.c, "amend_status_after_failure", lookup)

    async def go():
        async def cancel():
            await asyncio.sleep(0.1)  # отмена приходит, пока идёт выяснение статуса
            await bb.cmd_cancel(tg.upd(11), tg.ctx(BTC_PUT, o["orderId"]))
            order.append("cancel")
        await asyncio.gather(bb.on_callback(tg.upd(TRADER, data=f"ok:{pid}"), tg.ctx()), cancel())
    run(go())
    assert order == ["cancel", "lookup"] and bb.c.http.orders == []


def test_cancel_status_lookup_outside_lock(b, tg, monkeypatch):
    from optbot.bybit_client import BybitNetworkError
    o = b.c.http.add_order(BTC_PUT)
    b.c.http.fail["cancel_order"] = BybitNetworkError("timeout")
    held = []
    real = b.c.get_order
    monkeypatch.setattr(b.c, "get_order", lambda *a, **kw: held.append(dict(b._order_locks)) or real(*a, **kw))
    run(b.cmd_cancel(tg.upd(TRADER), tg.ctx(BTC_PUT, o["orderId"])))
    assert held == [{}] and "Сбой связи" in tg.last()


def test_amend_menu_audited(b, tg, caplog):
    b.c.http.add_order(BTC_PUT)
    with caplog.at_level("INFO", logger="optbot.trade"):
        run(b.cmd_orders(tg.upd(TRADER), tg.ctx()))
        run(b.on_callback(tg.upd(TRADER, data=next(x for x in tg.buttons() if x.startswith("am:"))), tg.ctx()))
    assert any("amend.menu" in r.getMessage() for r in caplog.records)


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


# ---------- /guide ----------
def _guide(bb, tg, uid, *args, lang_code=None, chat="dm", sender_chat=None, fail_fid=False, fail=None):
    docs = []

    async def reply_document(document, filename, caption=None, do_quote=None):
        assert do_quote is False
        if fail:
            raise fail
        if fail_fid and isinstance(document, str):
            raise BadRequest("Wrong file identifier/http url specified")
        docs.append((filename, document, caption))
        return NS(document=NS(file_id=f"fid-{filename}"))
    u = tg.upd(uid, chat=chat, sender_chat=sender_chat)
    u.effective_user.language_code = lang_code
    u.effective_message.reply_document = reply_document
    run(bb.cmd_guide(u, tg.ctx(*args)))
    return docs


@pytest.mark.parametrize("uid,args,code,expect,title", [
    (TRADER, (), None, "guide-trader-ru.pdf", "Инструкция трейдера"),
    (TRADER, (), "uk", "guide-trader-uk.pdf", "Інструкція трейдера"),
    (TRADER, ("RU",), "uk", "guide-trader-ru.pdf", "Инструкция трейдера"),   # аргумент важнее языка Telegram
    (VIEWER, (), "en", "guide-viewer-ru.pdf", "Инструкция для просмотра"),
    (VIEWER, ("uk",), None, "guide-viewer-uk.pdf", "Інструкція для перегляду"),
])
def test_guide_by_role_and_language(b, tg, uid, args, code, expect, title):
    docs = _guide(b, tg, uid, *args, lang_code=code)
    assert [(d[0], d[2]) for d in docs] == [(expect, title)]
    assert docs[0][1].startswith(b"%PDF")   # настоящий файл из docs/


def test_guide_never_admin(b, tg):
    sent = []
    for uid in (TRADER, VIEWER):
        for lang in GUIDE_LANGS:
            sent += [d[0] for d in _guide(b, tg, uid, lang)]
    assert len(sent) == 4 and not any("admin" in n for n in sent)
    assert _guide(b, tg, TRADER, "admin") == [] and "Формат: /guide" in tg.last()


def test_guide_in_group(b, tg):
    assert [d[0] for d in _guide(b, tg, TRADER, chat="g")] == ["guide-trader-ru.pdf"]


def test_guide_open_view_member_and_anon_get_viewer_guide(make_bot, cfg, tg):
    bb = make_bot(with_cfg(cfg, allow_all_group_members_view=True))
    assert [d[0] for d in _guide(bb, tg, STRANGER, chat="g")] == ["guide-viewer-ru.pdf"]
    bb._guide_sent.clear()
    anon = _guide(bb, tg, TRADER, chat="g", sender_chat=NS(id=GROUP, title="Desk"))
    assert [d[0] for d in anon] == ["guide-viewer-ru.pdf"]   # от имени группы — не трейдер


def test_guide_stranger_gets_nothing(b, tg):
    assert _guide(b, tg, STRANGER) == [] and tg.out == []


def test_guide_cooldown_per_chat_and_file_id_reused(b, tg, monkeypatch):
    first = _guide(b, tg, TRADER, chat="g")
    assert first and isinstance(first[0][1], bytes)
    n = len(tg.out)
    assert _guide(b, tg, TRADER, chat="g") == [] and len(tg.out) == n   # повтор в группе — молча
    assert [d[1] for d in _guide(b, tg, TRADER)] == ["fid-guide-trader-ru.pdf"]   # личка — свой чат; file_id
    assert _guide(b, tg, TRADER) == [] and "только что отправлена" in tg.last()   # в личке — объяснение
    monkeypatch.setattr("optbot.bot.GUIDE_COOLDOWN", 0)
    assert [d[1] for d in _guide(b, tg, TRADER, chat="g")] == ["fid-guide-trader-ru.pdf"]
    assert len(b._guide_ids) == 1   # одна запись на файл


def test_guide_send_error_is_clear_and_allows_retry(b, tg, caplog):
    assert _guide(b, tg, TRADER, fail=BadRequest("Chat not found")) == []
    assert "Инструкция недоступна" in tg.last() and "Chat not found" in caplog.text
    assert not b._guide_sent and not b._guide_ids   # ничего не ушло — повтор разрешён сразу
    assert [d[0] for d in _guide(b, tg, TRADER)] == ["guide-trader-ru.pdf"]


@pytest.mark.parametrize("err,cooldown_kept,reply", [
    (TimedOut(), True, None),                                    # мог дойти: ни ошибки в чат, ни повтора сразу
    (NetworkError("reset"), False, "повторите позже"),
    (Forbidden("blocked"), False, "повторите позже"),
    (BadRequest("Not enough rights to send documents"), False, "Инструкция недоступна"),
])
def test_guide_telegram_errors(b, tg, caplog, err, cooldown_kept, reply):
    assert _guide(b, tg, TRADER, fail=err) == []
    assert bool(b._guide_sent) is cooldown_kept
    assert (tg.out[-1][1] if tg.out else None) is None if reply is None else reply in tg.last()
    assert "Сбой обработчика" not in caplog.text   # не «Внутренняя ошибка»


def test_guide_chat_error_keeps_file_id(b, tg):
    _guide(b, tg, TRADER)
    cached = dict(b._guide_ids)
    b._guide_sent.clear()
    assert _guide(b, tg, TRADER, fail=BadRequest("Chat not found")) == []   # и file_id, и загрузка отклонены
    assert b._guide_ids == cached   # дело в чате — file_id сохранён


def test_guide_truncated_pdf_not_sent(b, tg, monkeypatch, tmp_path):
    (tmp_path / "guide-trader-ru.pdf").write_bytes(b"%PDF-1.4 ... half written")
    monkeypatch.setattr("optbot.bot.GUIDE_DIR", tmp_path)
    assert _guide(b, tg, TRADER) == [] and "Инструкция недоступна" in tg.last() and not b._guide_ids


def test_guide_sent_pruned(b, tg, monkeypatch):
    b._guide_sent[(123, "guide-trader-ru.pdf")] = time.monotonic() - 3600
    _guide(b, tg, TRADER)
    assert (123, "guide-trader-ru.pdf") not in b._guide_sent


def test_guide_file_changing_during_read(b, tg, monkeypatch, tmp_path):
    src = tmp_path / "guide-trader-ru.pdf"
    src.write_bytes(b"%PDF full\n%%EOF\n")
    monkeypatch.setattr("optbot.bot.GUIDE_DIR", tmp_path)
    real = type(src).read_bytes

    def partial(self):
        data = real(self)
        self.write_bytes(b"%PDF full, now longer\n%%EOF\n")   # git pull дописывает файл
        return data
    monkeypatch.setattr(type(src), "read_bytes", partial)
    assert _guide(b, tg, TRADER) == [] and "Инструкция недоступна" in tg.last() and not b._guide_sent


def test_guide_stale_file_id_reuploads(b, tg):
    _guide(b, tg, TRADER)
    b._guide_sent.clear()
    docs = _guide(b, tg, TRADER, fail_fid=True)
    assert len(docs) == 1 and isinstance(docs[0][1], bytes)
    b._guide_sent.clear()
    assert [d[1] for d in _guide(b, tg, TRADER)] == ["fid-guide-trader-ru.pdf"]   # новый file_id запомнен


def test_guide_new_pdf_after_update_uploaded_again(b, tg, monkeypatch, tmp_path):
    src = tmp_path / "guide-trader-ru.pdf"
    src.write_bytes(b"%PDF old\n%%EOF\n")
    monkeypatch.setattr("optbot.bot.GUIDE_DIR", tmp_path)
    _guide(b, tg, TRADER)
    src.write_bytes(b"%PDF new\n%%EOF\n")
    os.utime(src, (1, 1))   # другой mtime — как после git pull
    b._guide_sent.clear()
    assert [d[1] for d in _guide(b, tg, TRADER)] == [b"%PDF new\n%%EOF\n"]


def test_guide_missing_file(b, tg, monkeypatch, tmp_path, caplog):
    monkeypatch.setattr("optbot.bot.GUIDE_DIR", tmp_path)
    assert _guide(b, tg, TRADER) == []
    assert "Инструкция недоступна" in tg.last() and "guide-trader-ru.pdf" in caplog.text


def test_guide_in_command_menu_and_help(b):
    assert any(n == "guide" for n, _, _ in b.COMMANDS) and "/guide" in HELP


# ---------- сигналы ----------
def _sig_market(fake):
    from tests.fakes import NOW, set_chain, set_klines
    set_chain(fake, iv=0.6)
    set_klines(fake, rv=0.4)
    return NOW


def test_signals_without_base_shows_buttons(b, tg):
    run(b.cmd_signals(tg.upd(VIEWER), tg.ctx()))
    assert tg.buttons() == ["sig:BTC", "sig:ETH", "sig:SOL"]


def test_signals_card_and_refresh(b, tg, fake, monkeypatch):
    now = _sig_market(fake)
    monkeypatch.setattr("optbot.signals.time", NS(time=lambda: now / 1000, monotonic=time.monotonic))
    run(b.cmd_signals(tg.upd(VIEWER, "g"), tg.ctx("btc")))   # просмотр, в группе — не чувствительное
    assert "Железный кондор" in tg.last("reply") and tg.buttons() == ["sig:BTC", "ex:1"]  # «Выставить» — у записанного в журнал
    run(b.on_callback(tg.upd(VIEWER, data="sig:BTC"), tg.ctx()))
    assert "Железный кондор" in tg.last("edit")
    assert fake.called("place_order") == []


def test_signals_refresh_cached(b, tg, fake, monkeypatch):
    now = _sig_market(fake)
    monkeypatch.setattr("optbot.signals.time", NS(time=lambda: now / 1000, monotonic=time.monotonic))
    run(b.cmd_signals(tg.upd(VIEWER), tg.ctx("BTC")))
    n = len(fake.called("get_tickers"))
    u = tg.upd(VIEWER, data="sig:BTC")                     # другое сообщение (меню) — карточка его меняет
    run(b.on_callback(u, tg.ctx()))
    assert len(fake.called("get_tickers")) == n            # в пределах SIGNAL_CACHE_TTL — без запросов
    assert u.callback_query.answer.await_args.args == (None,)  # сообщение изменилось — «без изменений» не врём
    u = tg.upd(VIEWER, data="sig:BTC")                     # «Обновить» на той же карточке: текст тот же
    u.callback_query.edit_message_text = AsyncMock(side_effect=BadRequest("Message is not modified"))
    run(b.on_callback(u, tg.ctx()))
    assert "Без изменений" in u.callback_query.answer.await_args.args[0]
    monkeypatch.setattr(botmod, "SIGNAL_CACHE_TTL", 0)
    run(b.on_callback(tg.upd(VIEWER, data="sig:BTC"), tg.ctx()))
    assert len(fake.called("get_tickers")) > n


def test_signals_concurrent_presses_share_one_compute(b, tg, fake, monkeypatch):
    now = _sig_market(fake)
    monkeypatch.setattr("optbot.signals.time", NS(time=lambda: now / 1000, monotonic=time.monotonic))

    async def go():
        await asyncio.gather(*[b.on_callback(tg.upd(VIEWER, data="sig:BTC"), tg.ctx()) for _ in range(3)])
    run(go())
    assert len(fake.called("get_tickers")) == 1


def test_signals_same_card_after_cache_says_so(b, tg, fake, monkeypatch):
    now = _sig_market(fake)
    monkeypatch.setattr("optbot.signals.time", NS(time=lambda: now / 1000, monotonic=time.monotonic))
    monkeypatch.setattr(botmod, "SIGNAL_CACHE_TTL", 0)
    u = tg.upd(VIEWER, data="sig:BTC")
    u.callback_query.edit_message_text = AsyncMock(side_effect=BadRequest("Message is not modified"))
    run(b.on_callback(u, tg.ctx()))
    assert "рынок тот же" in u.callback_query.answer.await_args.args[0]


def test_signals_late_answer_still_edits(b, tg, fake, monkeypatch):
    now = _sig_market(fake)
    monkeypatch.setattr("optbot.signals.time", NS(time=lambda: now / 1000, monotonic=time.monotonic))
    u = tg.upd(VIEWER, data="sig:BTC")
    u.callback_query.answer = AsyncMock(side_effect=BadRequest("Query is too old"))
    run(b.on_callback(u, tg.ctx()))
    assert "Железный кондор" in tg.last("edit")


def test_signals_unknown_base_no_lock(b, tg):
    run(b.cmd_signals(tg.upd(VIEWER), tg.ctx("XXX")))
    assert "BASE_COINS" in tg.last() and set(b._sig_locks) == {"BTC", "ETH", "SOL"}


def test_signals_button_answered_on_error(b, tg, fake):
    fake.fail["get_tickers"] = __import__("requests").exceptions.ConnectionError("down")
    u = tg.upd(VIEWER, data="sig:BTC")
    run(b.on_callback(u, tg.ctx()))
    assert "Не удалось" in u.callback_query.answer.await_args.args[0]  # кнопка не крутится до таймаута
    assert "Ошибка" in tg.last("reply")


def test_signals_access_and_errors(b, tg):
    run(b.cmd_signals(tg.upd(STRANGER), tg.ctx("BTC")))
    assert tg.out == []
    u = tg.upd(STRANGER, data="sig:BTC")
    run(b.on_callback(u, tg.ctx()))
    u.callback_query.answer.assert_awaited_with("Нет прав")
    run(b.cmd_signals(tg.upd(VIEWER), tg.ctx("DOGE")))
    assert "BASE_COINS" in tg.last()


def test_signals_in_menu_and_help():
    assert "signals" in [n for n, _, _ in OptionsBot.COMMANDS]
    assert "/signals" in HELP


# ---------- длинные списки ----------
def _ids(text):
    import re
    return re.findall(r"id ([0-9a-f-]{36})", text)


def _press(b, tg, label, uid=TRADER, chat="dm"):
    """Нажать кнопку листания по подписи (в последнем сообщении)."""
    nav = {("◀" if b.tokens[x[3:]]["dir"] == "b" else "▶"): x for x in tg.buttons() if x.startswith("pg:")}
    u = tg.upd(uid, chat, data=nav[label])
    run(b.on_callback(u, tg.ctx()))
    return u


def test_orders_all_pages_and_paging(b, tg, fake):
    fake.page_size = 7                       # Bybit отдаёт страницами — клиент дочитывает все
    for i in range(45):
        fake.add_order(BTC_PUT, price=str(20 + i))
    assert len(b.c.open_orders("BTC")) == 45
    run(b.cmd_orders(tg.upd(TRADER), tg.ctx("BTC")))
    assert "Строки 1–20 из 45" in tg.last()
    seen = _ids(tg.last())
    btns = tg.buttons()
    assert len(btns) == 41 and btns[-1].startswith("pg:")      # 20 × (изменить, отменить) + «Далее»
    _press(b, tg, "▶")
    assert tg.out[-1][0] == "edit" and "Строки 21–40 из 45" in tg.last()
    seen += _ids(tg.last())
    assert len([x for x in tg.buttons() if x.startswith("pg:")]) == 2   # и «Назад», и «Далее»
    cx = [x for x in tg.buttons() if x.startswith("cx:")][0]  # кнопки строк второй страницы — рабочие
    run(b.on_callback(tg.upd(TRADER, data=cx), tg.ctx()))
    assert len(b.c.open_orders("BTC")) == 44
    _press(b, tg, "▶")                                       # с той же (изменённой) страницы дальше
    assert "Строки 40–44 из 44" in tg.last()                   # отменённый был выше — ничего не пропущено
    seen += _ids(tg.last())
    assert len(set(seen)) == 45                               # все строки побывали на экране, без повторов
    _press(b, tg, "◀")
    assert "Строки 20–39 из 44" in tg.last()                   # 20 строк перед первой показанной


def test_paging_does_not_skip_rows_when_list_shrinks(b, tg, fake):
    for i in range(25):
        fake.add_order(BTC_PUT, price=str(20 + i))
    run(b.cmd_orders(tg.upd(TRADER), tg.ctx("BTC")))
    first = _ids(tg.last())
    all_ids = sorted(o["orderId"] for o in fake.orders)
    fake.orders = [o for o in fake.orders if o["orderId"] != first[0]]   # ордер со страницы 1 исполнился
    _press(b, tg, "▶")
    second = _ids(tg.last())
    assert set(first) | set(second) == set(all_ids)            # ни одна строка не проскочила мимо экрана
    assert "Строки 20–24 из 24" in tg.last()


def test_paging_stale_and_viewer(b, tg, fake):
    for i in range(25):
        fake.add_order(BTC_PUT, price=str(20 + i))
    run(b.cmd_orders(tg.upd(VIEWER), tg.ctx("BTC")))
    assert [x for x in tg.buttons()] and all(x.startswith("pg:") for x in tg.buttons())  # зрителю — листание
    _press(b, tg, "▶", uid=VIEWER)
    assert "Строки 21–25 из 25" in tg.last()
    n = len(tg.out)
    for bad in ("pg:nope", "pg:", "pg:deadbeef"):
        u = tg.upd(VIEWER, data=bad)
        run(b.on_callback(u, tg.ctx()))
        u.callback_query.answer.assert_awaited_with("Кнопка устарела — повторите команду")
    tok = next(x for x in tg.buttons() if x.startswith("pg:"))[3:]
    b.tokens[tok]["_t"] -= 10 ** 6                              # просрочена
    u = tg.upd(VIEWER, data=f"pg:{tok}")
    run(b.on_callback(u, tg.ctx()))
    u.callback_query.answer.assert_awaited_with("Кнопка устарела — повторите команду")
    assert len(tg.out) == n


def test_paging_in_group_respects_dm_only(b, tg, fake):
    for i in range(25):
        fake.add_order(BTC_PUT, price=str(20 + i))
    run(b.cmd_orders(tg.upd(TRADER), tg.ctx("BTC")))
    n = len(tg.out)
    u = _press(b, tg, "▶", chat="g")
    u.callback_query.answer.assert_awaited_with("Только в личке с ботом")
    assert len(tg.out) == n


def test_positions_paging_and_short_list(b, tg, fake):
    fake.page_size = 3
    fake.positions = [{"symbol": f"BTC-27NOV26-{60000 + 500 * i}-P-USDT", "side": "Buy", "size": "0.01",
                       "avgPrice": "100", "markPrice": "90", "unrealisedPnl": "-0.1", "delta": "-0.1", "gamma": "0",
                       "vega": "0", "theta": "0"} for i in range(22)]
    run(b.cmd_pos(tg.upd(VIEWER), tg.ctx("BTC")))
    assert "Строки 1–20 из 22" in tg.last() and len(tg.buttons()) == 1
    run(b.cmd_pos(tg.upd(TRADER), tg.ctx("BTC")))
    _press(b, tg, "▶")
    assert "Строки 21–22 из 22" in tg.last() and len(tg.buttons()) == 2 * 2 + 1
    fake.positions = fake.positions[:2]
    run(b.cmd_pos(tg.upd(VIEWER), tg.ctx("BTC")))
    assert "Строки" not in tg.last()


def test_paging_after_list_shrinks_to_one_page(b, tg, fake):
    """Было 25, на странице 1 — 20; исполнилось 10 из них. «Далее» показывает хвост и «Назад» к остальным."""
    for i in range(25):
        fake.add_order(BTC_PUT, price=str(20 + i))
    run(b.cmd_orders(tg.upd(TRADER), tg.ctx("BTC")))
    first = _ids(tg.last())
    fake.orders = [o for o in fake.orders if o["orderId"] not in first[:10]]
    _press(b, tg, "▶")
    assert "Строки 11–15 из 15" in tg.last()
    _press(b, tg, "◀")
    assert "Строки" not in tg.last() and len(_ids(tg.last())) == 15   # весь список на одной странице


def test_paging_keeps_requester_view(make_bot, cfg, tg, fake):
    """Трейдер запросил /orders в группе, зритель листает — кнопки трейдера в общем сообщении остаются."""
    bb = make_bot(with_cfg(cfg, sensitive_commands_dm_only=False))
    for i in range(25):
        fake.add_order(BTC_PUT, price=str(20 + i))
    run(bb.cmd_orders(tg.upd(TRADER, "g"), tg.ctx("BTC")))
    _press(bb, tg, "▶", uid=VIEWER, chat="g")
    assert "Строки 21–25 из 25" in tg.last() and any(x.startswith("cx:") for x in tg.buttons())
    run(bb.cmd_orders(tg.upd(VIEWER, "g"), tg.ctx("BTC")))
    _press(bb, tg, "▶", uid=TRADER, chat="g")
    assert not any(x.startswith(("cx:", "am:")) for x in tg.buttons())
