"""Telegram-бот для опционов Bybit (python-telegram-bot v22, async).

Запуск: python -m optbot.bot

Где работает:
- личка — для пользователей из TELEGRAM_TRADER_IDS / TELEGRAM_VIEWER_IDS (остальным доступна только /id);
- группы из TELEGRAM_ALLOWED_CHAT_IDS; из любой другой группы бот молча выходит (id группы пишется в лог).

Права — всегда по пользователю (from_user.id), не по чату:
- трейдеры (TELEGRAM_TRADER_IDS) — торговля и просмотр;
- зрители (TELEGRAM_VIEWER_IDS) — просмотр;
- ALLOW_ALL_GROUP_MEMBERS_VIEW=true — просмотр всем участникам разрешённых групп;
- сообщения от имени чата (анонимные админы, каналы; sender_chat) к торговле не допускаются;
- SENSITIVE_COMMANDS_DM_ONLY=true — баланс/позиции/ордера/греки только в личке.

Торговля:
- каждый ордер / закрытие / cancel-all подтверждается кнопкой; нажать может только инициатор,
  подтверждение живёт PENDING_TTL сек; лимиты MAX_QTY_* / MAX_ORDER_PREMIUM_USDT проверяются
  при подготовке и повторно при отправке;
- все торговые действия пишутся в лог optbot.trade с user id и chat id;
- о выполненных действиях уведомляются все разрешённые группы (кроме чата, где действие сделано).

Кнопки: в /orders — «Изменить» (bid/mid/ask/mark или своя цена ответом) и «Отменить»;
в /pos — «Закрыть по рынку» / «по mid». Все торговые кнопки ведут к тому же подтверждению.

Исполнения: приватный WebSocket Bybit (optbot.ws) — сообщение о каждой сделке по опционам
(включая сделанные вне бота) во все разрешённые группы и трейдерам в личку.

Лимиты Telegram: AIORateLimiter (≈30 сообщений/с всего, 20/мин на группу, повтор после RetryAfter);
обновления цепочки и навигация по кнопкам редактируют сообщение, а не шлют новое;
отказы неразрешённым пользователям — молча.
"""
from __future__ import annotations

import asyncio
import functools
import html
import logging
import secrets
import sys
import time
from dataclasses import dataclass
from decimal import InvalidOperation
from typing import Callable

from telegram import BotCommand, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    AIORateLimiter, Application, ApplicationHandlerStop, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, TypeHandler, filters,
)

from .bybit_client import LOOKUP_DELAYS, BybitError, BybitNetworkError, OptionsClient, OrderRequest, RiskError, _d
from .collector import Collector, seconds_to_next_slot
from .config import Settings, settings
from .ws import ExecutionWatcher
from .fmt import (
    env_banner, fmt_amend_preview, fmt_balance, fmt_chain, fmt_expiries, fmt_greeks, fmt_order_preview, fmt_orders,
    fmt_positions, fmt_ticker, parse_iv, px, short_symbol, to_dec,
)

log = logging.getLogger("optbot")
audit_log = logging.getLogger("optbot.trade")

PENDING_TTL = 60  # сек на подтверждение
CHAIN_N = 6       # страйков от ATM по умолчанию
TOKEN_TTL = 1800  # сек жизни кнопок в /orders и /pos

GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}
# Служебные аккаунты Telegram, от имени которых приходят сообщения «от чата»:
# GroupAnonymousBot (анонимный админ), Channel_Bot (пост от канала), Telegram (авто-форвард из канала)
SERVICE_USER_IDS = {1087968824, 136817688, 777000}

HELP = """Просмотр:
/exp [BASE] — экспирации (кнопками)
/chain BASE [EXP] [N] — цепочка
/t SYMBOL — котировка и греки
/bal /greeks [BASE] — счёт*
/pos [BASE] — позиции (кнопки «Закрыть»)*
/orders [BASE] — ордера (кнопки «Изменить», «Отменить»)*
/id — ваш user id и chat id

Торговля (только трейдеры):
/buy SYMBOL QTY PRICE|ivNN|mkt
/sell SYMBOL QTY PRICE|ivNN|mkt
/close SYMBOL [PRICE] — закрыть позицию
/amend SYMBOL ORDER_ID PRICE|ivNN [QTY]
   (проще — кнопкой «Изменить» в /orders)
/cancel SYMBOL ORDER_ID
/cancelall [BASE]

SYMBOL: BTC-17NOV26-60000-P (суффикс -USDT не обязателен)
Примеры: /buy BTC-17NOV26-60000-P 0.01 150
         /sell ETH-17NOV26-1900-C 0.1 iv55"""


def pre(text: str) -> str:
    return f"<pre>{html.escape(text)}</pre>"


def kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) for t, d in r] for r in rows])


def chunks(items: list, n: int) -> list[list]:
    return [items[i:i + n] for i in range(0, len(items), n)]


@dataclass
class Who:
    """Кто прислал апдейт и что ему можно."""
    user_id: int | None
    name: str
    chat_id: int | None
    chat_title: str
    is_group: bool
    anon: bool        # сообщение от имени чата (sender_chat) / служебный аккаунт
    can_view: bool
    can_trade: bool

    @property
    def where(self) -> str:
        return f"группе «{self.chat_title}»" if self.is_group else "личке"


@dataclass
class Pending:
    text: str
    action: Callable[[], dict]        # синхронная функция, выполняется в потоке
    done: Callable[[dict], str]       # форматирование результата
    event: str                        # имя события для аудита
    who: Who
    created: float
    lookup: Callable[[], str] | None = None  # после сбоя сети: узнать, что на самом деле на бирже


def guarded(level: str, sensitive: bool = False):
    """level: 'any' | 'view' | 'trade'. Разрешённость чата уже проверена в gate()."""
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(self: "OptionsBot", update: Update, context: ContextTypes.DEFAULT_TYPE):
            w = self.who(update)
            if level == "view" and not w.can_view or level == "trade" and not w.can_trade:
                log.warning("Отказ: %s (уровень %s) user=%s chat=%s anon=%s",
                            fn.__name__, level, w.user_id, w.chat_id, w.anon)
                if level == "trade":
                    audit_log.warning("denied cmd=%s user=%s chat=%s anon=%s",
                                      fn.__name__, w.user_id, w.chat_id, w.anon)
                    if w.anon:
                        await self.reply(update, "Торговля от имени группы/канала запрещена — "
                                                 "отключите анонимность и повторите от своего имени")
                    elif w.can_view:
                        await self.reply(update, "Нет прав на торговлю")
                return  # остальным — молча (не спамим в группах)
            if sensitive and w.is_group and self.cfg.sensitive_commands_dm_only:
                await self.reply(update, "Эта команда доступна только в личке с ботом")
                return
            try:
                await fn(self, update, context, w)
            except (BybitError, RiskError, ValueError, InvalidOperation) as e:
                if level == "trade":
                    audit_log.info("rejected cmd=%s user=%s chat=%s args=%r err=%s",
                                   fn.__name__, w.user_id, w.chat_id, context.args, e)
                await self.reply(update, f"Ошибка: {e}")
            except Exception:
                log.exception("Сбой обработчика %s", fn.__name__)
                await self.reply(update, "Внутренняя ошибка, см. лог")
        return wrapper
    return deco


class OptionsBot:
    def __init__(self, client: OptionsClient, cfg: Settings):
        self.c = client
        self.cfg = cfg
        self.collector = Collector(client, cfg)
        self.pending: dict[str, Pending] = {}
        self.tokens: dict[str, dict] = {}            # короткие ключи для данных кнопок (лимит 64 байта)
        # (chat_id, id сообщения-вопроса) -> (токен меню amend, время); живут сутки, чтобы на поздний ответ
        # сказать «меню устарело», а не молчать
        self.awaiting: dict[tuple[int, int], tuple[str, float]] = {}
        self.watcher: ExecutionWatcher | None = None
        self._app: Application | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._dm_failed: set[int] = set()

    # ---------- доступ ----------
    def who(self, update: Update) -> Who:
        u, chat, msg = update.effective_user, update.effective_chat, update.effective_message
        # У callback_query effective_message — сообщение бота, его sender_chat не про нажавшего
        from_chat = update.callback_query is None and msg is not None and msg.sender_chat is not None
        anon = u is None or from_chat or u.id in SERVICE_USER_IDS
        uid = u.id if u else None
        is_group = chat is not None and chat.type in GROUP_TYPES
        can_trade = not anon and uid in self.cfg.tg_trader_ids
        can_view = (
            can_trade
            or (not anon and uid in self.cfg.tg_viewer_ids)
            or (is_group and self.cfg.allow_all_group_members_view)
        )
        return Who(
            user_id=uid,
            name=(msg.sender_chat.title if from_chat else u.full_name if u else "?") or "?",
            chat_id=chat.id if chat else None,
            chat_title=(chat.title or "") if chat else "",
            is_group=is_group,
            anon=anon,
            can_view=can_view,
            can_trade=can_trade,
        )

    async def gate(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Первый обработчик для всех апдейтов: пропускает только личку и разрешённые группы."""
        chat = update.effective_chat
        if chat is None or chat.type == ChatType.PRIVATE or chat.id in self.cfg.tg_allowed_chat_ids:
            if update.my_chat_member and chat and chat.type != ChatType.PRIVATE:
                log.info("Статус бота в чате %s (%r): %s", chat.id, chat.title,
                         update.my_chat_member.new_chat_member.status)
            return
        mcm = update.my_chat_member
        if mcm and mcm.new_chat_member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
            raise ApplicationHandlerStop  # нас уже удалили
        log.warning("Чат %s (%s, %r) не в TELEGRAM_ALLOWED_CHAT_IDS — выхожу",
                    chat.id, chat.type, chat.title)
        try:
            await context.bot.leave_chat(chat.id)
        except TelegramError as e:
            log.warning("leave_chat(%s) не удался: %s", chat.id, e)
        raise ApplicationHandlerStop

    def audit(self, event: str, w: Who, **kv):
        extra = " ".join(f"{k}={v!r}" for k, v in kv.items())
        audit_log.info("%s user=%s name=%r chat=%s %s", event, w.user_id, w.name, w.chat_id, extra)

    async def notify(self, bot, text: str, w: Who):
        """Уведомление о торговом действии во все разрешённые группы, кроме исходного чата."""
        msg = pre(f"🔔 {env_banner(self.cfg.env)}\n{w.name} (id {w.user_id}) в {w.where}:\n{text}")
        for gid in sorted(self.cfg.tg_allowed_chat_ids - {w.chat_id}):
            try:
                await bot.send_message(gid, msg, parse_mode=ParseMode.HTML)
            except TelegramError as e:
                log.warning("Уведомление в %s не доставлено: %s", gid, e)

    def notify_bg(self, context, text: str, w: Who):
        context.application.create_task(self.notify(context.bot, text, w))

    # ---------- утилиты ----------
    def _tok(self, **data) -> str:
        now = time.monotonic()
        for k in [k for k, v in self.tokens.items() if now - v["_t"] > TOKEN_TTL]:
            del self.tokens[k]
        for k in [k for k, (_, ts) in self.awaiting.items() if now - ts > 86400]:
            del self.awaiting[k]
        t = secrets.token_hex(4)
        self.tokens[t] = {**data, "_t": now}
        return t

    def _tok_get(self, t: str) -> dict:
        v = self.tokens.get(t)
        if not v or time.monotonic() - v["_t"] > TOKEN_TTL:
            raise ValueError("Кнопка устарела — запросите список заново")
        return v

    @staticmethod
    async def call(fn, *a, **kw):
        return await asyncio.to_thread(fn, *a, **kw)

    @staticmethod
    async def reply(update: Update, text: str, markup=None, raw_html: str = ""):
        return await update.effective_message.reply_text(
            pre(text) + raw_html, parse_mode=ParseMode.HTML, reply_markup=markup,
        )

    @staticmethod
    async def edit(q, text_html: str, markup=None):
        try:
            await q.edit_message_text(text_html, parse_mode=ParseMode.HTML, reply_markup=markup)
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise

    # ---------- общие ----------
    @guarded("any")
    async def cmd_id(self, update: Update, context: ContextTypes.DEFAULT_TYPE, w: Who):
        role = "трейдер" if w.can_trade else "просмотр" if w.can_view else "нет доступа"
        lines = [f"user id: {w.user_id}", f"chat id: {w.chat_id}", f"права: {role}"]
        sc = update.effective_message.sender_chat
        if sc:
            lines.insert(1, f"от имени чата: {sc.id} (к торговле не допускается)")
        await self.reply(update, "\n".join(lines))

    @guarded("any")
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE, w: Who):
        if w.can_view:
            await self.reply(update, f"{env_banner(self.cfg.env)}\n\n{HELP}")
        elif not w.is_group:  # в группах неразрешённым молчим
            await self.reply(update, f"Нет доступа. Ваш user id: {w.user_id}")

    @guarded("view")
    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE, w: Who):
        note = "\n* в группах скрыты" if self.cfg.sensitive_commands_dm_only else ""
        await self.reply(update, f"{env_banner(self.cfg.env)}\n\n{HELP}{note}")

    # ---------- рынок ----------
    def _bases_kb(self) -> InlineKeyboardMarkup:
        return kb([[(b, f"exp:{b}") for b in self.cfg.base_coins]])

    async def _expiries_kb(self, base: str) -> InlineKeyboardMarkup:
        exps = await self.call(self.c.expiries, base)
        btns = [(code, f"ch:{base}:{code}:{CHAIN_N}") for code, _ in exps[:30]]
        return kb(chunks(btns, 3))

    async def _chain_view(self, base: str, exp: str, n: int) -> tuple[str, InlineKeyboardMarkup]:
        ch = await self.call(self.c.chain, base, exp, n)
        markup = kb([[("🔄 Обновить", f"ch:{base}:{exp}:{n}"), ("◀ Экспирации", f"exp:{base}")]])
        hint = f"\n<i>Символ: {html.escape(base)}-{html.escape(exp.upper())}-СТРАЙК-C|P</i>"
        return pre(fmt_chain(ch)) + hint, markup

    @guarded("view")
    async def cmd_exp(self, update: Update, context: ContextTypes.DEFAULT_TYPE, w: Who):
        if not context.args:
            await update.effective_message.reply_text("Базовый актив:", reply_markup=self._bases_kb())
            return
        base = context.args[0].upper()
        exps = await self.call(self.c.expiries, base)
        await self.reply(update, fmt_expiries(base, exps), await self._expiries_kb(base))

    @guarded("view")
    async def cmd_chain(self, update: Update, context: ContextTypes.DEFAULT_TYPE, w: Who):
        a = context.args
        if not a:
            await update.effective_message.reply_text("Базовый актив:", reply_markup=self._bases_kb())
        elif len(a) == 1:
            base = a[0].upper()
            await update.effective_message.reply_text(
                f"{base}: экспирация?", reply_markup=await self._expiries_kb(base))
        else:
            n = int(a[2]) if len(a) > 2 else CHAIN_N
            text, markup = await self._chain_view(a[0].upper(), a[1].upper(), max(1, min(n, 20)))
            await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)

    @guarded("view")
    async def cmd_ticker(self, update: Update, context: ContextTypes.DEFAULT_TYPE, w: Who):
        if not context.args:
            raise ValueError("Укажите символ: /t BTC-17NOV26-60000-P")
        inst = await self.call(self.c.resolve_symbol, " ".join(context.args))
        await self.reply(update, fmt_ticker(await self.call(self.c.ticker, inst.symbol)))

    # ---------- счёт (чувствительное) ----------
    @staticmethod
    def _base_arg(context) -> str | None:
        return context.args[0].upper() if context.args else None

    @guarded("view", sensitive=True)
    async def cmd_bal(self, update: Update, context: ContextTypes.DEFAULT_TYPE, w: Who):
        await self.reply(update, fmt_balance(await self.call(self.c.balance)))

    @guarded("view", sensitive=True)
    async def cmd_pos(self, update: Update, context: ContextTypes.DEFAULT_TYPE, w: Who):
        positions = await self.call(self.c.positions, self._base_arg(context))
        rows = []
        if w.can_trade:  # кнопки — только трейдерам
            for p in positions:
                t = self._tok(symbol=p["symbol"])
                rows.append([(f"Закрыть {short_symbol(p['symbol'])} · рынок", f"cp:{t}:mkt"), ("· mid", f"cp:{t}:mid")])
        await self.reply(update, fmt_positions(positions), kb(rows) if rows else None)

    @guarded("view", sensitive=True)
    async def cmd_greeks(self, update: Update, context: ContextTypes.DEFAULT_TYPE, w: Who):
        await self.reply(update, fmt_greeks(await self.call(self.c.greeks, self._base_arg(context))))

    @guarded("view", sensitive=True)
    async def cmd_orders(self, update: Update, context: ContextTypes.DEFAULT_TYPE, w: Who):
        orders = await self.call(self.c.open_orders, self._base_arg(context))
        rows = []
        if w.can_trade:  # кнопки — только трейдерам
            for o in orders:
                t = self._tok(symbol=o["symbol"], oid=o["orderId"])
                label = f"{short_symbol(o['symbol'])} {o['side']} {o['qty']}"
                if o.get("orderType") == "Limit":
                    rows.append([(f"✏ {label}", f"am:{t}"), ("✖ Отменить", f"cx:{t}")])
                else:
                    rows.append([(f"✖ Отменить {label}", f"cx:{t}")])
        await self.reply(update, fmt_orders(orders), kb(rows) if rows else None)

    # ---------- торговля ----------
    async def _ask_confirm(self, update: Update, context, w: Who, text: str, event: str,
                           action: Callable[[], dict], done: Callable[[dict], str], q=None,
                           lookup: Callable[[], str] | None = None):
        pid = secrets.token_hex(6)
        self.pending[pid] = Pending(text, action, done, event, w, time.monotonic(), lookup)
        self.audit(f"{event}.request", w, pid=pid, order=text.replace("\n", " | "))
        who_line = f"\nИнициатор: {w.name}" if w.is_group else ""
        body = pre(f"{env_banner(self.cfg.env)}\n{text}{who_line}\n\n"
                   f"Подтвердить может только инициатор, {PENDING_TTL} с")
        markup = kb([[("✅ Подтвердить", f"ok:{pid}"), ("❌ Отмена", f"no:{pid}")]])
        if q is not None:  # превращаем сообщение-меню в подтверждение (меньше сообщений в чате)
            await self.edit(q, body, markup)
            msg = q.message
        else:
            msg = await update.effective_message.reply_text(body, parse_mode=ParseMode.HTML, reply_markup=markup)
        context.application.create_task(self._expire(pid, msg))

    async def _expire(self, pid: str, msg):
        await asyncio.sleep(PENDING_TTL)
        p = self.pending.pop(pid, None)
        if p:
            self.audit(f"{p.event}.expired", p.who, pid=pid)
            try:
                await msg.edit_text(pre(f"{p.text}\n\n⌛ Время подтверждения истекло"), parse_mode=ParseMode.HTML)
            except TelegramError:
                log.debug("не удалось отредактировать истёкшее подтверждение", exc_info=True)

    @staticmethod
    def _parse_spec(spec: str) -> dict:
        s = spec.lower()
        if s in ("m", "mkt", "market"):
            return {"order_type": "Market"}
        if s.startswith("iv"):
            return {"iv": parse_iv(s[2:])}
        price = to_dec(s)
        if price <= 0:
            raise ValueError("Цена должна быть > 0")
        return {"price": price}

    async def _order(self, update: Update, context, w: Who, side: str):
        a = context.args
        if len(a) != 3:
            raise ValueError(f"Формат: /{side.lower()} SYMBOL QTY PRICE|ivNN|mkt")
        req = OrderRequest(symbol=a[0], side=side, qty=to_dec(a[1]), **self._parse_spec(a[2]))
        await self._confirm_order(update, context, w, req, "order")

    async def _confirm_order(self, update: Update, context, w: Who, req: OrderRequest, event: str):
        req = await self.call(self.c.prepare_order, req)

        def lookup() -> str:
            return self.c.order_status_after_failure(req)

        await self._ask_confirm(
            update, context, w, fmt_order_preview(req, req.ticker), event,
            action=lambda: self.c.place_order(req),
            done=lambda r: f"✅ Ордер принят\n{req.describe()}\norderId {r.get('orderId')}",
            lookup=lookup,
        )

    @guarded("trade")
    async def cmd_buy(self, update: Update, context: ContextTypes.DEFAULT_TYPE, w: Who):
        await self._order(update, context, w, "Buy")

    @guarded("trade")
    async def cmd_sell(self, update: Update, context: ContextTypes.DEFAULT_TYPE, w: Who):
        await self._order(update, context, w, "Sell")

    @guarded("trade")
    async def cmd_close(self, update: Update, context: ContextTypes.DEFAULT_TYPE, w: Who):
        a = context.args
        if not a:
            raise ValueError("Формат: /close SYMBOL [PRICE]")
        price = to_dec(a[1]) if len(a) > 1 else None
        req = await self.call(self.c.close_position_request, a[0], price)
        await self._confirm_order(update, context, w, req, "close")

    @guarded("trade")
    async def cmd_amend(self, update: Update, context: ContextTypes.DEFAULT_TYPE, w: Who):
        a = context.args
        if len(a) not in (3, 4):
            raise ValueError("Формат: /amend SYMBOL ORDER_ID PRICE|ivNN [QTY]")
        spec = self._parse_spec(a[2])
        if "order_type" in spec:
            raise ValueError("Для amend укажите цену или ivNN")
        qty = to_dec(a[3]) if len(a) == 4 else None
        await self._confirm_amend(update, context, w, a[0], a[1], qty, spec.get("price"), spec.get("iv"))

    async def _confirm_amend(self, update: Update, context, w: Who, symbol: str, order_id: str,
                             qty, price, iv, q=None):
        req, orig = await self.call(self.c.amend_request, symbol, order_id, qty, price, iv)
        oid = orig["orderId"]

        def lookup() -> str:
            return self.c.amend_status_after_failure(oid, req)

        await self._ask_confirm(
            update, context, w, fmt_amend_preview(req, orig, req.ticker), "amend",
            action=lambda: self.c.amend_order(oid, req),
            done=lambda r: f"✅ Ордер изменён\n{req.describe()}\norderId {r.get('orderId')}",
            q=q, lookup=lookup,
        )

    async def _amend_menu(self, update: Update, w: Who, symbol: str, order_id: str):
        """Меню изменения ордера: цены bid/mid/ask/mark кнопками или своя цена ответом."""
        inst = await self.call(self.c.resolve_symbol, symbol)
        orig = await self.call(self.c.get_order, inst.symbol, order_id)
        if not orig:
            raise ValueError("Ордер уже не активен — обновите /orders")
        t = await self.call(self.c.ticker, inst.symbol)
        bid, ask, mark = _d(t.get("bid1Price")), _d(t.get("ask1Price")), _d(t.get("markPrice"))
        mid = (bid + ask) / 2 if bid and ask else mark
        prices = {k: v for k, v in (("bid", bid), ("mid", mid), ("ask", ask), ("mark", mark)) if v > 0}
        tok = self._tok(symbol=inst.symbol, oid=order_id, user=w.user_id,
                        prices={k: str(v) for k, v in prices.items()})
        text = (f"Изменить ордер {short_symbol(inst.symbol)}\n"
                f"сейчас: {orig['side'].upper()} {orig['qty']} @ {px(orig.get('price'))}\n"
                f"рынок: bid {px(bid)} / ask {px(ask)}  mark {px(mark)}\n\n"
                f"Новая цена (количество прежнее) или «Своя цена…»:")
        btns = [(f"{k} {px(v)}", f"ap:{tok}:{k}") for k, v in prices.items()]
        markup = kb(chunks(btns, 2) + [[("Своя цена / кол-во…", f"ap:{tok}:other"), ("Отмена", f"ap:{tok}:x")]])
        await self.reply(update, text, markup)

    async def _amend_pick(self, update: Update, context, w: Who, tok: str, choice: str):
        q = update.callback_query
        d = self._tok_get(tok)
        if d["user"] != w.user_id:
            await q.answer("Это меню другого пользователя", show_alert=True)
            return
        await q.answer()
        if choice == "x":
            self.tokens.pop(tok, None)
            await self.edit(q, pre("Изменение отменено"))
        elif choice == "other":
            prompt = await update.effective_message.reply_text(
                f"Новая цена для {short_symbol(d['symbol'])} — ответом на это сообщение:\n"
                "160   или   iv50   или   160 0.02 (цена и количество)",
                reply_markup=ForceReply(input_field_placeholder="160 / iv50 / 160 0.02"),
            )
            self.awaiting[(prompt.chat_id, prompt.message_id)] = (tok, time.monotonic())
        else:
            await self._confirm_amend(update, context, w, d["symbol"], d["oid"], None,
                                      _d(d["prices"][choice]), None, q=q)

    @guarded("any")
    async def on_reply(self, update: Update, context: ContextTypes.DEFAULT_TYPE, w: Who):
        """Ответ на вопрос «Своя цена…» из меню изменения ордера. Прочие ответы игнорируются."""
        msg = update.effective_message
        key = (msg.chat_id, msg.reply_to_message.message_id)
        entry = self.awaiting.get(key)
        if not entry or not w.can_trade:
            return  # ответ не на наш вопрос
        tok, _ = entry
        d = self.tokens.get(tok)
        if not d or time.monotonic() - d["_t"] > TOKEN_TTL:
            self.awaiting.pop(key, None)
            raise ValueError("Меню устарело — откройте /orders заново")
        if d["user"] != w.user_id:
            return
        parts = (msg.text or "").split()
        if len(parts) not in (1, 2):
            raise ValueError("Формат: ЦЕНА | ivNN [КОЛИЧЕСТВО], например 160 или iv50 или 160 0.02")
        spec = self._parse_spec(parts[0])
        if "order_type" in spec:
            raise ValueError("Укажите цену или ivNN")
        qty = to_dec(parts[1]) if len(parts) == 2 else None
        self.awaiting.pop(key, None)
        await self._confirm_amend(update, context, w, d["symbol"], d["oid"], qty, spec.get("price"), spec.get("iv"))

    async def _close_button(self, update: Update, context, w: Who, tok: str, how: str):
        d = self._tok_get(tok)
        price = None
        if how == "mid":
            t = await self.call(self.c.ticker, d["symbol"])
            bid, ask = _d(t.get("bid1Price")), _d(t.get("ask1Price"))
            price = (bid + ask) / 2 if bid and ask else _d(t.get("markPrice"))
        req = await self.call(self.c.close_position_request, d["symbol"], price)
        await self._confirm_order(update, context, w, req, "close")

    async def _do_cancel(self, update: Update, context, w: Who, symbol: str, order_id: str):
        inst = await self.call(self.c.resolve_symbol, symbol)
        self.audit("cancel.request", w, symbol=inst.symbol, order_id=order_id)
        try:
            res = await self.call(self.c.cancel_order, inst.symbol, order_id)
        except BybitNetworkError as e:
            self.audit("cancel.unknown", w, symbol=inst.symbol, order_id=order_id, err=str(e))
            try:
                still = await self.call(self.c.get_order, inst.symbol, order_id)
                status = "ордер ВСЁ ЕЩЁ АКТИВЕН — повторите отмену" if still else "ордер больше не активен"
            except Exception as e2:
                status = f"не удалось выяснить ({e2}) — проверьте /orders"
            self.audit("cancel.lookup", w, symbol=inst.symbol, order_id=order_id, status=status)
            raise ValueError(f"Сбой связи с биржей при отмене: {e}\nСтатус: {status}") from e
        except BybitError as e:
            self.audit("cancel.error", w, symbol=inst.symbol, order_id=order_id, err=str(e))
            raise
        self.audit("cancel.done", w, symbol=inst.symbol, order_id=res.get("orderId"))
        text = f"Отменён ордер {short_symbol(inst.symbol)} {res.get('orderId')}"
        await self.reply(update, text)
        self.notify_bg(context, text, w)

    @guarded("trade")
    async def cmd_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE, w: Who):
        a = context.args
        if len(a) != 2:
            raise ValueError("Формат: /cancel SYMBOL ORDER_ID")
        await self._do_cancel(update, context, w, a[0], a[1])

    @guarded("trade")
    async def cmd_cancelall(self, update: Update, context: ContextTypes.DEFAULT_TYPE, w: Who):
        base = self._base_arg(context)
        await self._ask_confirm(
            update, context, w, f"CANCEL ALL option orders: {base or 'ВСЕ базовые активы'}", "cancel_all",
            action=lambda: self.c.cancel_all(base),
            done=lambda r: f"✅ Отменено ордеров: {len(r.get('list', []))}",
            lookup=lambda: f"открытых ордеров сейчас: {len(self.c.open_orders(base))} — проверьте /orders",
        )

    # ---------- кнопки ----------
    async def _on_confirm(self, update: Update, context, w: Who, kind: str, pid: str):
        q = update.callback_query
        p = self.pending.get(pid)
        if not p:
            await q.answer("Подтверждение устарело")
            return
        if w.user_id != p.who.user_id:
            await q.answer("Подтвердить может только инициатор", show_alert=True)
            return
        self.pending.pop(pid)  # до первого await: двойное нажатие не отправит дважды
        if kind == "no":
            self.audit(f"{p.event}.cancelled_by_user", w, pid=pid)
            await q.answer("Отменено")
            await self.edit(q, pre(f"{p.text}\n\n❌ Отменено"))
            return
        if time.monotonic() - p.created > PENDING_TTL:
            self.audit(f"{p.event}.expired", w, pid=pid)
            await q.answer("Время истекло")
            await self.edit(q, pre(f"{p.text}\n\n⌛ Время истекло"))
            return
        self.audit(f"{p.event}.confirm", w, pid=pid)
        await q.answer("Отправляю…")
        await self.edit(q, pre(f"{p.text}\n\n⏳ Отправка…"))
        try:
            res = await self.call(p.action)
        except BybitNetworkError as e:
            # запрос мог дойти до биржи — не говорим «ошибка», выясняем фактическое состояние
            self.audit(f"{p.event}.unknown", w, pid=pid, err=str(e))
            try:  # промежуточное сообщение не должно мешать выяснению статуса
                await self.edit(q, pre(f"{p.text}\n\n⚠️ Сбой связи с биржей: {e}\n"
                                       f"⏳ Выясняю статус (обычно ~{int(sum(LOOKUP_DELAYS))} с, при плохой связи "
                                       "до 1–2 мин)… Не повторяйте команду."))
            except TelegramError:
                log.warning("Не удалось показать «Выясняю статус…»", exc_info=True)
            status = "не удалось выяснить"
            if p.lookup:
                try:
                    status = await self.call(p.lookup)
                except Exception as e2:
                    status = f"не удалось выяснить ({e2})"
            self.audit(f"{p.event}.lookup", w, pid=pid, status=status)
            await self.edit(q, pre(f"{p.text}\n\n⚠️ Сбой связи с биржей: {e}\nСтатус: {status}\n"
                                   "Не повторяйте, пока не проверите /orders и /pos."))
            return
        except (BybitError, RiskError, ValueError) as e:
            self.audit(f"{p.event}.error", w, pid=pid, err=str(e))
            await self.edit(q, pre(f"{p.text}\n\n⛔ Ошибка: {e}"))
            return
        except Exception as e:
            log.exception("Сбой при выполнении %s", p.event)
            self.audit(f"{p.event}.error", w, pid=pid, err=f"{type(e).__name__}: {e}")
            await self.edit(q, pre(f"{p.text}\n\n⛔ Внутренняя ошибка ({type(e).__name__}). "
                                   "Проверьте /orders и /pos — запрос мог быть отправлен."))
            return
        result = p.done(res)
        self.audit(f"{p.event}.done", w, pid=pid, order_id=res.get("orderId"),
                   link_id=res.get("orderLinkId"), n=len(res.get("list", [])) or None)
        await self.edit(q, pre(f"{p.text}\n\n{result}"))
        self.notify_bg(context, result, w)

    @guarded("any")
    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE, w: Who):
        q = update.callback_query
        kind, _, rest = (q.data or "").partition(":")
        need_trade = kind in ("ok", "no", "cx", "am", "ap", "cp")
        if not (w.can_trade if need_trade else w.can_view):
            log.warning("Отказ: кнопка %s user=%s chat=%s", kind, w.user_id, w.chat_id)
            if need_trade:
                audit_log.warning("denied button=%s user=%s chat=%s", kind, w.user_id, w.chat_id)
            await q.answer("Нет прав")
            return

        if kind in ("ok", "no"):
            await self._on_confirm(update, context, w, kind, rest)
            return
        if kind == "ap":  # ap:TOKEN:bid|mid|ask|mark|other|x — выбор в меню изменения
            tok, _, choice = rest.partition(":")
            await self._amend_pick(update, context, w, tok, choice)
            return

        await q.answer()
        if kind == "exp":  # выбор актива / возврат из цепочки -> список экспираций (правка того же сообщения)
            base = rest.upper()
            await self.edit(q, html.escape(f"{base}: экспирация?"), await self._expiries_kb(base))
        elif kind == "ch":  # ch:BASE:EXP:N — цепочка в том же сообщении
            base, exp, n = rest.split(":")
            text, markup = await self._chain_view(base, exp, int(n))
            await self.edit(q, text, markup)
        elif kind == "cx":  # cx:TOKEN (или старый формат cx:SHORT_SYMBOL:ORDER_ID) — отмена из /orders
            if ":" in rest:
                sym, _, oid = rest.partition(":")
            else:
                d = self._tok_get(rest)
                sym, oid = d["symbol"], d["oid"]
            await self._do_cancel(update, context, w, sym, oid)
        elif kind == "am":  # am:TOKEN — меню изменения ордера
            d = self._tok_get(rest)
            await self._amend_menu(update, w, d["symbol"], d["oid"])
        elif kind == "cp":  # cp:TOKEN:mkt|mid — закрыть позицию из /pos
            tok, _, how = rest.partition(":")
            await self._close_button(update, context, w, tok, how)

    # ---------- сборщик рыночных данных ----------
    async def _collect_job(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Задача JobQueue: срез в БД. Ошибки не роняют бота — следующий проход по расписанию."""
        try:
            res = await asyncio.to_thread(self.collector.collect)
        except Exception:
            log.exception("Сборщик: проход не выполнен")
            return
        (log.warning if res.errors else log.info)("Сборщик: %s", res)

    # ---------- исполнения (WebSocket) ----------
    def _on_ws_executions(self, rows: list[dict]) -> None:
        """Вызывается из потока pybit."""
        if self._loop and self._app:
            self._loop.call_soon_threadsafe(lambda: self._app.create_task(self.on_executions(rows)))

    async def on_executions(self, rows: list[dict]):
        by_order: dict[str, list[dict]] = {}
        for r in rows:
            by_order.setdefault(r.get("orderId", "?"), []).append(r)
        texts = []
        for oid, rs in by_order.items():
            qty = sum((_d(r.get("execQty")) for r in rs), _d(0))
            value = sum((_d(r.get("execQty")) * _d(r.get("execPrice")) for r in rs), _d(0))
            fee = sum((_d(r.get("execFee")) for r in rs), _d(0))
            last = rs[-1]
            avg = value / qty if qty else _d(0)
            etype = last.get("execType", "Trade")
            kind = {"Trade": "", "Delivery": " (поставка/экспирация)", "Settle": " (расчёт)",
                    "BustTrade": " (ликвидация)"}.get(etype, f" ({etype})")
            src = "через бота" if (last.get("orderLinkId") or "").startswith("ob-") else "вне бота"
            left = _d(last.get("leavesQty"))
            status = "ордер исполнен полностью" if left == 0 else f"остаток {left}"
            texts.append(f"💥 Исполнение{kind}\n"
                         f"{last.get('side', '?').upper()} {qty} {short_symbol(last.get('symbol', '?'))} @ {px(avg)}\n"
                         f"комиссия {fee:.4g} · {status}\nордер {oid[:8]} · {src}")
            audit_log.info("exec symbol=%s side=%s qty=%s avg=%s fee=%s type=%s order_id=%s link_id=%s",
                           last.get("symbol"), last.get("side"), qty, avg, fee, etype, oid, last.get("orderLinkId"))
        if not texts or not self._app:
            return
        msg = pre(f"{env_banner(self.cfg.env)}\n" + "\n\n".join(texts))
        targets = sorted(self.cfg.tg_allowed_chat_ids) + sorted(self.cfg.tg_trader_ids - self._dm_failed)
        for chat_id in targets:
            try:
                await self._app.bot.send_message(chat_id, msg, parse_mode=ParseMode.HTML)
            except TelegramError as e:
                permanent = isinstance(e, Forbidden) or (
                    isinstance(e, BadRequest) and "chat not found" in str(e).lower())
                if chat_id > 0 and permanent:
                    self._dm_failed.add(chat_id)  # не начинал диалог / заблокировал бота — больше не пробуем
                log.warning("Уведомление об исполнении в %s не доставлено: %s", chat_id, e)

    @staticmethod
    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
        log.error("Необработанная ошибка", exc_info=context.error)

    # ---------- сборка ----------
    COMMANDS = [
        ("help", "cmd_help", "справка"),
        ("id", "cmd_id", "ваш user id и chat id"),
        ("exp", "cmd_exp", "экспирации"),
        ("chain", "cmd_chain", "цепочка опционов"),
        ("t", "cmd_ticker", "котировка инструмента"),
        ("bal", "cmd_bal", "баланс"),
        ("pos", "cmd_pos", "позиции"),
        ("orders", "cmd_orders", "открытые ордера"),
        ("greeks", "cmd_greeks", "греки портфеля"),
        ("buy", "cmd_buy", "купить опцион"),
        ("sell", "cmd_sell", "продать опцион"),
        ("close", "cmd_close", "закрыть позицию"),
        ("amend", "cmd_amend", "изменить ордер"),
        ("cancel", "cmd_cancel", "отменить ордер"),
        ("cancelall", "cmd_cancelall", "отменить все ордера"),
    ]

    def build_app(self) -> Application:
        async def post_init(app: Application):
            await app.bot.set_my_commands([BotCommand(n, d) for n, _, d in self.COMMANDS])
            self._app, self._loop = app, asyncio.get_running_loop()
            self.watcher = ExecutionWatcher(self.cfg, self._on_ws_executions)
            try:
                await asyncio.to_thread(self.watcher.start)
            except Exception:
                log.exception("WebSocket исполнений не подключён — уведомлений об исполнениях не будет")

            if self.cfg.collector_enabled and app.job_queue is None:
                log.warning("Сборщик выключен: нет JobQueue — установите python-telegram-bot[job-queue] "
                            "(pip install -r requirements.txt)")
            elif self.cfg.collector_enabled:
                first = seconds_to_next_slot(time.time(), self.cfg.collector_interval_min)
                app.job_queue.run_repeating(self._collect_job, interval=self.cfg.collector_interval_min * 60,
                                            first=first, name="collector")
                log.info("Сборщик: каждые %d мин, первый срез через %.0f с, БД %s",
                         self.cfg.collector_interval_min, first, self.cfg.db_file)

        async def post_shutdown(app: Application):
            if self.watcher:
                await asyncio.to_thread(self.watcher.stop)

        app = (
            Application.builder()
            .token(self.cfg.tg_token)
            .rate_limiter(AIORateLimiter(max_retries=3))
            .post_init(post_init)
            .post_shutdown(post_shutdown)
            .build()
        )
        app.add_handler(TypeHandler(Update, self.gate), group=-1)  # белый список чатов — до всего остального
        app.add_handler(CommandHandler("start", self.cmd_start))
        for name, meth, _ in self.COMMANDS:
            app.add_handler(CommandHandler(name, getattr(self, meth)))
        app.add_handler(CallbackQueryHandler(self.on_callback))
        app.add_handler(MessageHandler(filters.REPLY & filters.TEXT & ~filters.COMMAND, self.on_reply))
        app.add_error_handler(self.on_error)
        return app


def check_config(cfg: Settings) -> list[str]:
    """Фатальные ошибки конфигурации (пустой список — можно стартовать)."""
    errors = []
    if not cfg.tg_token:
        errors.append("TELEGRAM_BOT_TOKEN не задан")
    if not (cfg.tg_trader_ids or cfg.tg_viewer_ids or
            (cfg.allow_all_group_members_view and cfg.tg_allowed_chat_ids)):
        errors.append("Никому нет доступа: заполните TELEGRAM_TRADER_IDS / TELEGRAM_VIEWER_IDS")
    bad = {i for i in cfg.tg_allowed_chat_ids if i > 0}
    if bad:
        errors.append(f"TELEGRAM_ALLOWED_CHAT_IDS: id групп отрицательные, а не {sorted(bad)}")
    svc = (cfg.tg_trader_ids | cfg.tg_viewer_ids) & SERVICE_USER_IDS
    if svc:
        errors.append(f"В списках пользователей служебные id Telegram: {sorted(svc)}")
    return errors


def main() -> int:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # иначе в лог попадает URL с токеном

    errors = check_config(settings)
    if errors:
        for e in errors:
            print(e, file=sys.stderr)
        return 1
    if settings.allow_all_group_members_view and not settings.tg_allowed_chat_ids:
        log.warning("ALLOW_ALL_GROUP_MEMBERS_VIEW=true, но TELEGRAM_ALLOWED_CHAT_IDS пуст — флаг ни на что не влияет")

    log.info("Старт: env=%s, активы=%s, трейдеров=%d, зрителей=%d, групп=%d, "
             "просмотр всем в группах=%s, счёт только в личке=%s",
             settings.env, ",".join(settings.base_coins), len(settings.tg_trader_ids),
             len(settings.tg_viewer_ids), len(settings.tg_allowed_chat_ids),
             settings.allow_all_group_members_view, settings.sensitive_commands_dm_only)
    app = OptionsBot(OptionsClient(settings), settings).build_app()
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
