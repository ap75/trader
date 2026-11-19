"""Telegram-бот для опционов Bybit (python-telegram-bot v22, async).

Запуск: python -m optbot.bot

Где работает:
- личка — для пользователей из TELEGRAM_TRADER_IDS / TELEGRAM_VIEWER_IDS (остальным доступна только /id);
- группы из TELEGRAM_ALLOWED_CHAT_IDS; из любой другой группы бот молча выходит (id группы пишется в лог).

Права — всегда по пользователю (from_user.id), не по чату:
- администраторы (TELEGRAM_ADMIN_IDS) — всё, что трейдеру, и функции только для администратора (в личку);
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

Сигналы: /signals [BASE] — рекомендация стратегии с ограниченным риском (optbot.signals); кнопка
«Выставить» (трейдер) — превью ног и подтверждение, исполнение — optbot.multileg (сначала покупки, IOC от ask/bid,
неполное — срочно администратору); каждая выдача пишется в журнал сигналов (optbot.sigjournal), итоги после экспирации — задачей
с периодом JOURNAL_SYNC_MIN со сводкой в группы, статистика — /signals_stats; кнопка «Обновить» пересчитывает
в том же сообщении (не чаще SIGNAL_CACHE_TTL сек на актив).

Автосигналы — только администраторам в личку (в группы не идут), в журнал с source=auto:
- сводка по всем активам раз в сутки в AUTO_SIGNALS_DAILY_UTC;
- смена рекомендации по активу (нет → есть, есть → другая, есть → нет; другие страйки той же стратегии —
  не смена) — проверка после каждого среза сборщика, смена должна продержаться AUTO_CHANGE_CONFIRM проверок
  подряд, сообщение не чаще раза в AUTO_SIGNALS_CHANGE_HOURS часов на актив; в журнал — только доставленное.
  Точка отсчёта — первые AUTO_CHANGE_CONFIRM одинаковых проверок после старта (о смене за время простоя
  не сообщается); сводка — снимок, состояние смены не меняет.

Инструкции: /guide [ru|uk] — PDF из docs/ под права спросившего (трейдера или зрителя; администратора —
никогда), язык по Telegram или аргументу. Одну и ту же — в чат не чаще GUIDE_COOLDOWN сек (в группе повтор
молча); file_id Telegram запоминается до смены файла (mtime и размер).

Лимиты Telegram: AIORateLimiter (≈30 сообщений/с всего, 20/мин на группу, повтор после RetryAfter);
обновления цепочки и навигация по кнопкам редактируют сообщение, а не шлют новое;
отказы неразрешённым пользователям — молча.
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
import html
import logging
import os
import urllib.parse
import urllib.request
import secrets
import sys
import time
from dataclasses import dataclass
from datetime import time as dtime, timezone
from decimal import InvalidOperation
from pathlib import Path
from typing import Callable

from telegram import BotCommand, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, NetworkError, TelegramError, TimedOut
from telegram.ext import (
    AIORateLimiter, Application, ApplicationHandlerStop, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, TypeHandler, filters,
)

from .bybit_client import (LOOKUP_DELAYS, BybitError, BybitNetworkError, OptionsClient, OrderRequest,
                           PortfolioRiskError, RiskError, _d)
from . import db
from .collector import Collector, seconds_to_next_slot
from . import backup as backup_mod
from . import paper as paper_mod
from .paper import PaperEngine
from .linkid import parse_link_id
from .journal import Journal, fmt_report, parse_period
from .multileg import MultiLegExecutor, SymbolGuard, fmt_result, leg_side, precheck, quote_mid
from .signals import STRATEGIES, Signal, SignalEngine, fmt_signal, fmt_signal_brief, signal_key, strategy_name
from .sigjournal import SETTLED_PER_MESSAGE, SignalJournal, compute_stats, fmt_settled, fmt_stats
from .config import ROOT, Settings, settings
from .ws import ExecutionWatcher
from .fmt import (
    chunks, env_banner, fmt_amend_preview, fmt_balance, fmt_chain, fmt_expiries, fmt_greeks, fmt_order_preview, fmt_orders,
    fmt_positions, fmt_ticker, parse_iv, px, short_symbol, to_dec,
)

log = logging.getLogger("optbot")
audit_log = logging.getLogger("optbot.trade")

PENDING_TTL = 60  # сек на подтверждение
CHAIN_N = 6       # страйков от ATM по умолчанию
TOKEN_TTL = 1800  # сек жизни кнопок в /orders и /pos
LIST_MAX = 20     # /orders и /pos: строк (и кнопок) на странице — длина сообщения Telegram ≤ 4096; дальше ◀ ▶
ANNOUNCE_GIVEUP_MS = 86_400_000  # итог не доставлен никому столько после расчёта — больше не отправлять
EXEC_CARD_MAX_AGE_S = 1800   # «Выставить» на карточке старше 30 мин — отказ: страйки подбирались под другой рынок
EXEC_MIN_LEFT_MS = 3_600_000  # «Выставить»: до экспирации меньше часа — не выставлять (ликвидность, поставка)
SIGNAL_CACHE_TTL = 30  # сек: /signals и «Обновить» по тому же активу чаще — без нового расчёта
TG_MAX_TEXT = 4096       # длина сообщения Telegram (после разметки)
AUTO_RETRY_S = 3600     # сообщение о смене никому не дошло — следующая попытка не раньше
AUTO_CHANGE_CONFIRM = 2  # проверок подряд: столько должна продержаться смена рекомендации до сообщения

GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}
# Служебные аккаунты Telegram, от имени которых приходят сообщения «от чата»:
# GroupAnonymousBot (анонимный админ), Channel_Bot (пост от канала), Telegram (авто-форвард из канала)
SERVICE_USER_IDS = {1087968824, 136817688, 777000}

# PDF-инструкции (docs/guides.py). Инструкцию администратора бот не отдаёт — только трейдера и зрителя.
GUIDE_DIR = Path(ROOT) / "docs"
GUIDE_LANGS = ("ru", "uk")
GUIDE_COOLDOWN = 60  # сек: та же инструкция в тот же чат не чаще — защита от флуда документами
GUIDE_CAPTIONS = {"trader": {"ru": "Инструкция трейдера", "uk": "Інструкція трейдера"},
                  "viewer": {"ru": "Инструкция для просмотра", "uk": "Інструкція для перегляду"}}


def _read_stable(path: Path) -> tuple[tuple, bytes]:
    """((mtime, размер), содержимое); ValueError, если файл меняется во время чтения (git pull пишет его)."""
    st = path.stat()
    data = path.read_bytes()
    st2 = path.stat()
    ver = (st.st_mtime_ns, st.st_size)
    # git пишет файл частями: размер и время могут совпасть на недописанном — конец PDF обязателен
    if (st2.st_mtime_ns, st2.st_size) != ver or len(data) != st.st_size or b"%%EOF" not in data[-1024:]:
        raise ValueError(f"{path.name} неполный или меняется во время чтения")
    return ver, data

HELP = """Просмотр:
/exp [BASE] — экспирации (кнопками)
/chain BASE [EXP] [N] — цепочка
/t SYMBOL — котировка и греки
/signals [BASE] — сигнал: стратегия, ноги, риск (кнопка «Выставить» — трейдеру)
/signals_stats [BASE] [ПЕРИОД] [csv] — как сработали сигналы (по умолчанию 30d)
/bal /greeks [BASE] — счёт*
/pos [BASE] — позиции (кнопки «Закрыть»)*
/orders [BASE] — ордера (кнопки «Изменить», «Отменить»)*
/journal [ПЕРИОД] [csv] — журнал сделок и PnL*
   ПЕРИОД: сегодня | вчера | 7d | 2026-11-01 | 2026-11-01..2026-11-15
/id — ваш user id и chat id (и роль)
/guide [ru|uk] — инструкция (PDF)

Торговля (только трейдеры):
/buy SYMBOL QTY PRICE|ivNN|mkt
/sell SYMBOL QTY PRICE|ivNN|mkt
/close SYMBOL [PRICE] — закрыть позицию
/amend SYMBOL ORDER_ID PRICE|ivNN [QTY]
   (проще — кнопкой «Изменить» в /orders)
/cancel SYMBOL ORDER_ID
/cancelall [BASE]

Администратору (TELEGRAM_ADMIN_IDS) — всё, что трейдеру, и автосигналы в личку:
   сводка раз в сутки и сообщение при смене рекомендации

SYMBOL: BTC-17NOV26-60000-P (суффикс -USDT не обязателен)
Примеры: /buy BTC-17NOV26-60000-P 0.01 150
         /sell ETH-17NOV26-1900-C 0.1 iv55"""


def pre(text: str) -> str:
    return f"<pre>{html.escape(text)}</pre>"


def kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) for t, d in r] for r in rows])


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
    is_admin: bool = False

    @property
    def where(self) -> str:
        return f"группе «{self.chat_title}»" if self.is_group else "личке"


@dataclass
class AutoState:
    """Автосигналы по активу: подтверждённая рекомендация и кандидат на смену. Точка отсчёта (ready) — тоже
    после AUTO_CHANGE_CONFIRM одинаковых проверок подряд: один сбойный срез после старта её не задаёт."""
    reported: str | None = None     # signal_key подтверждённого (точка отсчёта или сообщённая смена)
    ready: bool = False             # точка отсчёта установлена (до этого — только набор серии, без сообщений)
    reported_at: float = 0.0        # когда было последнее сообщение О СМЕНЕ (пауза AUTO_SIGNALS_CHANGE_HOURS)
    pending: str | None = None      # новое значение, которое ждёт подтверждения
    streak: int = 0                 # сколько проверок подряд оно держится
    retry_at: float = 0.0           # сообщение о смене не доставлено — следующая попытка не раньше

    def break_streak(self) -> None:
        """Проверка пропущена или кандидат исчез: «подряд» начинается заново."""
        self.pending, self.streak = None, 0

    def report(self, key: str | None, now: float | None = None) -> None:
        """Подтверждено (точка отсчёта, now=None) или сообщено о смене (now — начало паузы): кандидат и ожидание
        повтора сбрасываются."""
        self.reported, self.ready, self.pending, self.streak, self.retry_at = key, True, None, 0, 0.0
        if now is not None:
            self.reported_at = now


@dataclass
class Pending:
    text: str
    action: Callable[[], dict]        # синхронная функция, выполняется в потоке
    done: Callable[[dict], str]       # форматирование результата
    event: str                        # имя события для аудита
    who: Who
    created: float
    lookup: Callable[[], str] | None = None  # после сбоя сети: узнать, что на самом деле на бирже
    order_id: str | None = None       # действие над существующим ордером — выполняется под его блокировкой
    release: Callable[[], None] | None = None  # после выяснения статуса: освободить символ (ManualHold)


class ManualHold:
    """Символ ручного ордера, оставленный занятым после сбоя связи на отправке: ордер мог дойти до биржи,
    пока статус не выяснен — сделка по сигналу с этим символом не начнётся (иначе ордер смешается с её ногой)."""

    def __init__(self, guard: SymbolGuard):
        self.guard, self.symbol = guard, None

    def keep(self, symbol: str) -> None:
        self.symbol = symbol

    def release(self) -> None:
        if self.symbol is not None:
            self.guard.release_manual(self.symbol)
            self.symbol = None


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
        self._tg_outage: tuple[float, float, float] | None = None  # серия сбоев опроса: начало, последний, отчёт
        self.journal = Journal(cfg)
        self.signals = SignalEngine(client, cfg)
        self.paper = PaperEngine(client, cfg, self.signals)  # бумажные книги E1: шаг после каждого среза
        self._paper_task: asyncio.Task | None = None
        self._fng_task: asyncio.Task | None = None
        self.sigjournal = SignalJournal(cfg)
        self._sig_cache: dict[str, tuple[float, Signal]] = {}  # актив -> (когда, Signal)
        self._sig_locks = {b: asyncio.Lock() for b in cfg.base_coins}  # актив -> блокировка расчёта
        self.pending: dict[str, Pending] = {}
        self.tokens: dict[str, dict] = {}            # короткие ключи для данных кнопок (лимит 64 байта)
        self._last_sweep = 0.0                      # когда чистились устаревшие токены (не чаще раза в минуту)
        # (chat_id, id сообщения-вопроса) -> (токен меню amend, время); живут сутки, чтобы на поздний ответ
        # сказать «меню устарело», а не молчать
        self.awaiting: dict[tuple[int, int], tuple[str, float]] = {}
        self.watcher: ExecutionWatcher | None = None
        self._app: Application | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._dm_failed: set[int] = set()
        # срочные администратору: ключ -> когда последний раз отправлено (повтор — не чаще ADMIN_ALERT_QUIET_MIN)
        self._alert_last: dict[str, float] = {}
        # повторы недошедших срочных -> исходный текст (снимаются при остановке, текст — в сообщение об остановке)
        self._alert_retries: dict[asyncio.Task, str] = {}
        self.c.on_access_error = self.alert_threadsafe  # нет доступа к бирже (ключ, права, IP, регион)
        self.c.on_limit = self.alert_threadsafe         # сработал лимит риска (MAX_*, маржа)
        # orderId -> [блокировка, сколько держат/ждут]: изменения/отмены одного ордера идут строго по очереди
        self._order_locks: dict[str, list] = {}
        self._guide_ids: dict[str, tuple[tuple, str]] = {}  # файл -> ((mtime, размер), file_id): без повторной загрузки
        self._guide_sent: dict[tuple[int | None, str], float] = {}  # (чат, файл) -> когда отправлена
        self._auto: dict[str, AutoState] = {}  # автосигналы: актив -> что сообщено администраторам
        # сводка и проверка смены — по очереди (одновременно: оба увидели бы старое состояние и написали дважды)
        self._auto_lock = asyncio.Lock()
        self._auto_task: asyncio.Task | None = None  # проверка смены, запущенная сборщиком
        self._auto_gen = 0  # растёт при пропуске проверки: опоздавшая проверка своих итогов не применяет
        # одна сделка по сигналу за раз (два подтверждения не пройдут «символ свободен» одновременно, общий
        # страйк двух сигналов не перемешает ноги) и не по символам, где летит ручной ордер — и наоборот
        self._guard = SymbolGuard()
        # (чат, id сигнала) -> (когда последний раз показан в этом чате, в т.ч. «Обновить»; цены ног на карточке):
        # превью сравнивает с тем, что видели в этом чате, а не с первой выдачей в журнале (могла быть дни назад)
        self._card_shown: dict[tuple[int, int], tuple[float, dict, float | None]] = {}  # + макс. риск карточки

    # ---------- доступ ----------
    def who(self, update: Update) -> Who:
        u, chat, msg = update.effective_user, update.effective_chat, update.effective_message
        # У callback_query effective_message — сообщение бота, его sender_chat не про нажавшего
        from_chat = update.callback_query is None and msg is not None and msg.sender_chat is not None
        anon = u is None or from_chat or u.id in SERVICE_USER_IDS
        uid = u.id if u else None
        is_group = chat is not None and chat.type in GROUP_TYPES
        is_admin = not anon and uid in self.cfg.tg_admin_ids
        can_trade = is_admin or (not anon and uid in self.cfg.tg_trader_ids)
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
            is_admin=is_admin,
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
    @contextlib.asynccontextmanager
    async def _order_lock(self, order_id: str | None):
        """Последовательность действий над одним ордером. Проверка «ордер не изменился после превью»
        и отправка — два запроса к бирже; без блокировки два почти одновременных подтверждения оба
        видят старый ордер, и второе молча затирает первое."""
        if not order_id:
            yield
            return
        entry = self._order_locks.setdefault(order_id, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if not entry[1]:
                self._order_locks.pop(order_id, None)

    def _tok_sweep(self) -> None:
        now = time.monotonic()
        if now - self._last_sweep < 60:  # не на каждую кнопку: в /orders их до 42 на сообщение
            return
        self._last_sweep = now
        for k in [k for k, v in self.tokens.items() if now - v["_t"] > TOKEN_TTL]:
            del self.tokens[k]
        for k in [k for k, (_, ts) in self.awaiting.items() if now - ts > 86400]:
            del self.awaiting[k]

    def _tok(self, **data) -> str:
        """Короткий токен кнопки (устаревшие чистятся не чаще раза в минуту)."""
        self._tok_sweep()
        t = secrets.token_hex(4)
        self.tokens[t] = {**data, "_t": time.monotonic()}
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
    async def edit(q, text_html: str, markup=None) -> bool:
        """-> изменилось ли сообщение (False — тот же текст, Telegram «not modified»)."""
        try:
            await q.edit_message_text(text_html, parse_mode=ParseMode.HTML, reply_markup=markup)
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise
            return False
        return True

    # ---------- общие ----------
    @guarded("any")
    async def cmd_id(self, update: Update, context: ContextTypes.DEFAULT_TYPE, w: Who):
        role = ("администратор" if w.is_admin else "трейдер" if w.can_trade else "просмотр" if w.can_view
                else "нет доступа")
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

    @guarded("view")
    async def cmd_guide(self, update: Update, context: ContextTypes.DEFAULT_TYPE, w: Who):
        """PDF-инструкция под права: трейдеру — трейдера, остальным — зрителя. Язык — аргумент или язык Telegram."""
        args = [a.lower() for a in context.args]
        if len(args) > 1 or args and args[0] not in GUIDE_LANGS:
            raise ValueError("Формат: /guide [ru|uk]")
        code = (getattr(update.effective_user, "language_code", None) or "").lower()
        lang = args[0] if args else "uk" if code.startswith("uk") else "ru"
        role = "trader" if w.can_trade else "viewer"
        name = f"guide-{role}-{lang}.pdf"
        path = GUIDE_DIR / name
        sent_key, now = (w.chat_id, name), time.monotonic()
        if now - self._guide_sent.get(sent_key, -GUIDE_COOLDOWN) < GUIDE_COOLDOWN:
            if not w.is_group:  # в группе повтор молча: ответ на каждый повтор — тоже флуд
                await self.reply(update, "Эта инструкция только что отправлена — см. выше")
            return
        self._guide_sent = {k: t for k, t in self._guide_sent.items() if now - t < GUIDE_COOLDOWN}
        self._guide_sent[sent_key] = now  # до отправки: параллельный повтор не пройдёт
        # do_quote=False: в той же теме группы, и удалённая команда не мешает отправке
        send = functools.partial(update.effective_message.reply_document, filename=name,
                                 caption=GUIDE_CAPTIONS[role][lang], do_quote=False)
        try:
            ver, doc = await self._guide_doc(path)
            try:
                msg = await send(document=doc)
            except BadRequest:
                if not isinstance(doc, str):
                    raise
                # запомненный file_id отклонён (текст ошибки не разбираем) — один раз загрузить файл заново;
                # если и загрузка не прошла, дело в чате, а не в file_id — запись вернуть
                cached = self._guide_ids.pop(name, None)
                try:
                    ver, doc = await self._guide_doc(path)
                    msg = await send(document=doc)
                except Exception:
                    if cached:
                        self._guide_ids.setdefault(name, cached)
                    raise
        except TimedOut:  # файл мог и дойти: повтор не раньше чем через минуту, ошибку в чат не пишем
            log.warning("Инструкция %s: таймаут отправки в %s — могла дойти", name, w.chat_id)
            return
        except Exception as e:
            self._guide_sent.pop(sent_key, None)  # точно не отправлено — повтор разрешён
            if isinstance(e, (OSError, BadRequest, ValueError)):  # BadRequest — подкласс NetworkError: раньше
                log.error("Инструкция %s не отправлена: %s", name, e)
                raise ValueError("Инструкция недоступна — сообщите администратору") from None
            if isinstance(e, (Forbidden, NetworkError)):  # сбой Telegram; ответ в чат может и не дойти
                log.warning("Инструкция %s в %s не отправлена: %s", name, w.chat_id, e)
                raise ValueError("Не удалось отправить инструкцию — повторите позже") from None
            raise
        fid = getattr(getattr(msg, "document", None), "file_id", None)
        if fid:
            self._guide_ids[name] = (ver, fid)

    async def _guide_doc(self, path: Path) -> tuple[tuple, str | bytes]:
        """(версия, документ): file_id, если этот PDF уже загружали (те же mtime и размер), иначе содержимое."""
        cached = self._guide_ids.get(path.name)
        if cached:
            st = await self.call(path.stat)
            if cached[0] == (st.st_mtime_ns, st.st_size):
                return cached
        return await self.call(_read_stable, path)

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

    # ---------- сигналы ----------
    async def _signal(self, base: str, fresh: bool = False) -> tuple[Signal, bool]:
        """-> (своя копия сигнала, из кэша ли). Расчёт — запросы к Bybit (тикеры, свечи, справочник) и чтение
        истории. Повторы чаще SIGNAL_CACHE_TTL — из кэша; одновременные запросы по одному активу ждут один расчёт,
        а не запускают свои (каждый пошёл бы в ту же медленную биржу). Копия — номер записи журнала не попадёт
        в чужую карточку через общий кэш. fresh — мимо кэша (результат кладётся в кэш)."""
        base = self.signals.check_base(base)
        async with self._sig_locks[base]:
            hit = self._sig_cache.get(base)
            cached = bool(not fresh and hit and time.monotonic() - hit[0] < SIGNAL_CACHE_TTL)
            if cached:
                sig = hit[1]
            else:
                sig = await self.call(self.signals.compute, base)
                self._sig_cache[base] = (time.monotonic(), sig)
        return dataclasses.replace(sig), cached

    async def _record_signal(self, sig: Signal, source: str, user_id: int | None, name: str,
                             chat_id: int | None) -> None:
        """Журнал сигналов (этап 4). Вне блокировки актива: занятая БД не держит чужие нажатия; сбой записи
        не мешает показать карточку. Повтор тому же человеку shown не накручивает (signal_views)."""
        try:
            await self.call(self.sigjournal.record, sig, source, user_id, name, chat_id)
        except Exception:
            log.exception("Журнал сигналов: %s не записан", sig.metrics.base)

    async def _signal_view(self, base: str, w: Who) -> tuple[str, InlineKeyboardMarkup, bool]:
        """-> (карточка, кнопки, из кэша ли)."""
        sig, cached = await self._signal(base)
        await self._record_signal(sig, "bot", w.user_id, w.name, w.chat_id)
        base = sig.metrics.base
        row = [("🔄 Обновить", f"sig:{base}")]
        if sig.id and sig.legs:  # в журнале — можно выставить (права проверяются при нажатии)
            row.append(("📝 Выставить", f"ex:{sig.id}"))
            # «Обновить» с тем же итогом Telegram не правит (edit_date старый) — свежесть помним сами
            now = time.time()
            self._card_shown = {k: v for k, v in self._card_shown.items() if now - v[0] <= EXEC_CARD_MAX_AGE_S}
            prices = {(l.symbol, leg_side(l.side)): l.price for l in sig.legs}
            self._card_shown[(w.chat_id, sig.id)] = (now, prices, sig.profile.max_loss if sig.profile else None)
        return pre(fmt_signal(sig, self.cfg.env)), kb([row]), cached

    @guarded("view")
    async def cmd_signals(self, update: Update, context: ContextTypes.DEFAULT_TYPE, w: Who):
        if not context.args:
            await update.effective_message.reply_text(
                "Сигнал по активу:", reply_markup=kb([[(b, f"sig:{b}") for b in self.cfg.base_coins]]))
            return
        text, markup, _ = await self._signal_view(context.args[0], w)
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)

    @guarded("view")
    async def cmd_signals_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE, w: Who):
        if not self.cfg.journal_enabled:
            raise ValueError("Журнал выключен (JOURNAL_ENABLED=false) — сигналы не записываются")
        base, period, want_csv = None, [], False
        for a in context.args:
            if a.lower() == "csv":
                want_csv = True
            elif a.upper() in self.cfg.base_coins and base is None:
                base = a.upper()
            elif a.upper() in self.cfg.base_coins:
                raise ValueError("Один актив: /signals_stats [BASE] [ПЕРИОД] [csv]")
            elif not period and self._is_period(a):  # слова периода — у parse_period, не свой список
                period = [a]
            elif a.isalpha():
                raise ValueError(f"Актив {a!r}: один из {', '.join(self.cfg.base_coins)}")
            elif not period:
                period = [a]
            else:
                raise ValueError("Формат: /signals_stats [BASE] [ПЕРИОД] [csv], например /signals_stats BTC 30d")
        start, end, label = parse_period(period or ["30d"])
        rows = await asyncio.to_thread(self.sigjournal.rows, start, end, base)
        await self.reply(update, f"{env_banner(self.cfg.env)}\n\n{fmt_stats(compute_stats(rows, label, base))}")
        if want_csv:
            # имена запросивших — только трейдеру в личке: в группе файл увидят и зрители
            data = await asyncio.to_thread(self.sigjournal.csv_bytes, start, end, base,
                                           w.can_trade and not w.is_group, rows)
            name = self._csv_name(f"signals-{self.cfg.env}-{(base or 'all').lower()}", label)
            await update.effective_chat.send_document(document=data, filename=name)

    async def _sigeval_job(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Итоги сигналов после экспирации; о посчитанных — сводка в группы (и трейдерам в личку)."""
        try:
            res = await asyncio.to_thread(self.sigjournal.evaluate, self.c)
        except Exception:
            log.exception("Журнал сигналов: итоги не посчитаны")
            return
        if res.settled or res.errors:
            (log.warning if res.errors else log.info)("Журнал сигналов: %s", res)
        try:  # и посчитанные вручную (`--evaluate`): сводку шлёт только бот
            rows = await asyncio.to_thread(self.sigjournal.unannounced)
            for chunk in chunks(rows, SETTLED_PER_MESSAGE):
                text = fmt_settled(chunk)
                # не доставлено никуда (Telegram недоступен) — не отмечаем: попробуем в следующий раз;
                # отмечаем по сообщению — доставленные части не повторятся
                if not self._targets():  # слать некуда (нет групп и личек) — не копить: отметить и не повторять
                    log.info("Журнал сигналов: итогов %d — получателей нет", len(chunk))
                elif not await self._broadcast(pre(f"{env_banner(self.cfg.env)}\n{text}"), "итогов сигналов"):
                    # все получатели отказывают (бота убрали из группы) — не повторять вечно: через сутки бросить
                    # бросаем только просроченные строки; свежие — повтор при следующем проходе
                    stale = [r["id"] for r in chunk if time.time() * 1000 - r["settled_ms"] > ANNOUNCE_GIVEUP_MS]
                    if stale:
                        log.error("Журнал сигналов: итоги %s не доставлены за сутки — больше не отправляются", stale)
                        await self._mark_announced(stale)
                    log.warning("Журнал сигналов: сводка итогов никуда не доставлена — повтор при следующем проходе")
                    return
                await self._mark_announced([r["id"] for r in chunk])
        except Exception:
            log.exception("Журнал сигналов: сводка итогов не отправлена")

    # ---------- срочные администратору ----------
    ALERT_RETRY_S = (60, 120, 300, 600, 1200)  # не дошло — повторы (важное: сделки вне бота, исполнение не полностью)

    async def admin_alert(self, key: str, text: str, quiet: bool = True, retry: bool = False) -> bool:
        """Срочное администраторам в личку. Одно и то же (ключ) — не чаще ADMIN_ALERT_QUIET_MIN (quiet=False —
        всегда: запуск/остановка, исполнение не полностью). retry — не дошло никому: повторить позже (фоном).
        -> отправлено ли (хоть кому-то)."""
        if not self.cfg.tg_admin_ids:
            return False
        now, when = time.monotonic(), time.strftime("%H:%M UTC", time.gmtime())  # время события — до отправки
        last = self._alert_last.get(key)
        if quiet and last is not None and now - last < self.cfg.admin_alert_quiet_min * 60:
            log.info("Срочное %s — повтор в «тишине», не отправлено: %s", key, text)
            return False
        # ключ занят до отправки: одновременные одинаковые (сбой ключа сразу в нескольких запросах) — одно сообщение
        self._alert_last[key] = now
        log.warning("Срочное администратору (%s): %s", key, text)
        try:
            got = await self._send_admins(pre(f"{env_banner(self.cfg.env)}\n🚨 {text}"))
        except Exception:  # не TelegramError (сбой HTTP-клиента и т.п.) — как «не дошло»
            log.exception("Срочное %s: сбой отправки", key)
            got = []
        if not got:
            # никому не дошло — «тишину» не начинаем: следующий повтор попробует снова
            if self._alert_last.get(key) == now:
                if last is None:
                    self._alert_last.pop(key, None)
                else:
                    self._alert_last[key] = last
            if retry and self._loop is not None:
                # на голом цикле, не app.create_task: остановка бота не ждёт повторов (до ~40 мин) — их снимает
                # post_stop, а недоставленное уходит в сообщении об остановке
                marked = f"{text}\n(событие в {when}, доставлено с задержкой)"
                t = self._loop.create_task(self._retry_alert(key, marked))
                self._alert_retries[t] = marked
                t.add_done_callback(lambda t: self._alert_retries.pop(t, None))
            return False
        if len(self._alert_last) > 500:  # ключи с суммами/символами копятся — старые не нужны
            cut = now - self.cfg.admin_alert_quiet_min * 60
            self._alert_last = {k: v for k, v in self._alert_last.items() if v >= cut}
        return True

    async def _retry_alert(self, key: str, text: str) -> None:
        """Повторы недошедшего — без «тишины» (повторяются только важные: каждое — своё событие)."""
        for d in self.ALERT_RETRY_S:
            await asyncio.sleep(d)
            if await self.admin_alert(key, text, quiet=False):
                return
        log.error("Срочное %s так и не доставлено: %s", key, text)

    def alert_threadsafe(self, key: str, text: str) -> None:
        """admin_alert из любого потока (клиент биржи работает в to_thread). Без запущенного бота — только лог."""
        if self._loop is None or self._app is None or not self.cfg.tg_admin_ids:
            log.warning("Срочное %s не отправлено (бот не запущен или нет администраторов): %s", key, text)
            return
        try:
            self._loop.call_soon_threadsafe(lambda: self._app.create_task(self.admin_alert(key, text)))
        except RuntimeError:  # цикл уже закрыт (остановка)
            log.warning("Срочное %s не поставлено (бот останавливается): %s", key, text)

    def _state(self, key: str, value: str | None = None) -> str | None:
        """bot_state: чтение (value=None) или запись значения."""
        conn = db.connect(self.cfg.db_file)
        try:
            with conn:
                if value is not None:
                    conn.execute("INSERT INTO bot_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE "
                                 "SET value = excluded.value", (key, value))
                    return value
                r = conn.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
                return r["value"] if r else None
        finally:
            conn.close()

    async def _announce_start(self) -> None:
        """Запуск: отметка «работает» в БД; осталась с прошлого раза — остановка была нештатной (падение, kill)."""
        try:
            prev = await asyncio.to_thread(self._state, "running")
        except Exception:
            log.exception("Состояние бота в БД не прочитано — о прошлой остановке не сообщаю")
            prev = None
        try:  # отдельно от чтения: сбой записи не прячет уже найденную нештатную остановку
            await asyncio.to_thread(self._state, "running", str(int(time.time())))
        except Exception:
            log.exception("Отметка запуска в БД не записана — о следующей нештатной остановке не узнаем")
        text = f"🟢 Бот запущен (pid {os.getpid()})"
        if prev and prev != "0":
            when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(int(prev))) if prev.isdigit() else prev
            text += (f"\n⚠️ Прошлая остановка была нештатной (падение или kill; тот запуск — {when}). "
                     "Проверьте /pos и /orders.")
        await self.admin_alert("start", text, quiet=False, retry=True)  # «нештатная» не должна потеряться

    async def _announce_stop(self, undelivered: list[str] | None = None) -> None:
        """Штатная остановка: сообщение и снятие отметки «работает»."""
        try:
            await asyncio.to_thread(self._state, "running", "0")
        except Exception:
            log.exception("Отметка остановки в БД не записана — следующий запуск сообщит «нештатная»")
        # идущую сделку по сигналу библиотека дожидается до post_stop (задача create_task) — к этому моменту
        # она завершена; оборвать её может только kill по таймауту остановки — тогда post_stop не выполнится
        text = "🔴 Бот остановлен штатно."
        if undelivered:  # ждали повтора — не терять: хотя бы в этом сообщении
            text += "\n\nНе доставлено раньше (Telegram был недоступен):\n" + "\n\n".join(undelivered)
        await self.admin_alert("stop", text[:TG_MAX_TEXT - 200], quiet=False)

    # ---------- автосигналы администраторам ----------
    async def _send_admins(self, msg: str) -> list[int]:
        """Администраторам в личку (в группы — никогда), без кнопок. -> кому доставлено."""
        if not self._app:
            return []
        got = []
        for uid in sorted(self.cfg.tg_admin_ids):
            try:
                await self._app.bot.send_message(uid, msg, parse_mode=ParseMode.HTML)
                got.append(uid)
            except TelegramError as e:  # не начинал диалог с ботом / заблокировал — в лог, попробуем в следующий раз
                log.warning("Автосигнал администратору %s не доставлен: %s", uid, e)
        return got

    @staticmethod
    def _pack(head: str, parts: list[str]) -> list[str]:
        """Части сводки -> сообщения <pre> не длиннее TG_MAX_TEXT; первое — с заголовком."""
        limit = TG_MAX_TEXT - 100  # запас на <pre> и экранирование
        msgs, cur = [], head
        for p in parts:
            if len(p) > limit:  # одна часть длиннее лимита — обрезать с пометкой (не терять остальное)
                p = p[: limit - 30] + "\n… (обрезано, см. /signals)"
            if cur != head and len(pre(f"{cur}\n\n{p}")) > TG_MAX_TEXT:  # заголовок один не отправляем
                msgs.append(cur)
                cur = p
            else:
                cur = f"{cur}\n\n{p}"
        msgs.append(cur)
        return [pre(m) for m in msgs]

    async def _record_auto(self, sig: Signal, uids: list[int]) -> None:
        """В журнал — как показ тем, кому доставлено (тот же человек потом в /signals — не новый показ).
        Недоставленное — не показ (иначе /signals_stats считал бы то, чего никто не видел); поэтому запись —
        после отправки, и номера записи в автосообщениях нет."""
        for uid in uids:
            await self._record_signal(sig, "auto", uid, "auto", uid)

    async def _auto_signals(self, bases: list[str] | None = None) -> dict[str, Signal | Exception]:
        """Свежий расчёт по активам параллельно (мимо кэша: проверка идёт по новому срезу сборщика)."""
        bases = self.cfg.base_coins if bases is None else bases
        res = await asyncio.gather(*(self._signal(b, fresh=True) for b in bases), return_exceptions=True)
        return {b: (r if isinstance(r, Exception) else r[0]) for b, r in zip(bases, res)}

    async def _auto_daily_job(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Сводка по всем активам: сигнал (стратегия, ноги, риск) или почему его нет."""
        async with self._auto_lock:
            now = time.time()
            sigs = await self._auto_signals()
            parts = []
            for base, sig in sigs.items():
                if not isinstance(sig, Exception):
                    try:
                        parts.append(fmt_signal_brief(sig))
                        continue
                    except Exception as e:  # один актив не ломает сводку по остальным
                        sigs[base] = sig = e
                log.error("Автосигналы: %s не посчитан", base, exc_info=sig)
                parts.append(f"{base}: не посчитан — ошибка, см. лог")
            head = f"{env_banner(self.cfg.env)}\n🤖 Сигналы на {time.strftime('%Y-%m-%d %H:%M', time.gmtime(now))} UTC"
            parts.append("Подробно — /signals BASE. Не торговый совет: решение за трейдером.")
            got = None
            for msg in self._pack(head, parts):  # много активов / длинные причины — несколькими сообщениями
                sent = await self._send_admins(msg)
                got = sent if got is None else [u for u in got if u in sent]  # получил ВСЮ сводку
                if not got:  # часть никому не дошла — остальные части без неё бессмысленны
                    break
            if not got:
                log.warning("Автосигналы: сводка никому не доставлена")
                return
            # сводка — снимок, а не подтверждение: состояние смены не трогает (один сбойный замер в 09:05
            # иначе стал бы точкой отсчёта и дал ложное «нет → есть» через две проверки)
            for sig in sigs.values():
                if not isinstance(sig, Exception):
                    await self._record_auto(sig, got)

    def _kick_paper(self) -> None:
        """Шаг бумажных книг — отдельной задачей (запросы к бирже не задерживают сборщик); прошлый идёт — пропуск."""
        if self._paper_task and not self._paper_task.done():
            log.warning("Бумажные книги: прошлый шаг ещё идёт — этот пропущен")
            return
        self._paper_task = asyncio.get_running_loop().create_task(self._paper_job())

    async def _fng_job(self) -> None:
        try:
            n = await asyncio.to_thread(self.collector.update_fear_greed)
            if n:
                log.info("Сборщик: индекс страха и жадности +%d дн.", n)
        except Exception:
            log.exception("Сборщик: индекс страха и жадности не обновлён")

    async def _paper_job(self) -> None:
        try:
            res = await asyncio.to_thread(self.paper.step)
            (log.warning if res.errors else log.info)("Бумажные книги: %s", res)
        except Exception:
            log.exception("Бумажные книги: шаг не выполнен")

    def _break_streaks(self, bases) -> None:
        for b in bases:
            if st := self._auto.get(b):
                st.break_streak()

    def _kick_auto_change(self, bases: list[str]) -> None:
        """Проверка смены после среза — отдельной задачей: медленная биржа не задерживает сборщик (его пропущенный
        слот — потерянный срез). Предыдущая ещё идёт — новую не начинаем."""
        if self._auto_task and not self._auto_task.done():
            log.warning("Автосигналы: прошлая проверка смены ещё идёт — эта пропущена")
            self._auto_gen += 1  # идущая проверка посчитана по старому срезу: её результат серию не продолжит
            self._break_streaks(self.cfg.base_coins)  # пропущенная проверка рвёт серию «подряд»
            return
        self._auto_task = asyncio.get_running_loop().create_task(self._auto_change_job(None, bases))

    async def _auto_change_job(self, context: ContextTypes.DEFAULT_TYPE | None,
                               bases: list[str] | None = None) -> None:
        """Сообщение, когда рекомендация по активу сменилась и продержалась AUTO_CHANGE_CONFIRM проверок подряд, —
        не чаще раза в AUTO_SIGNALS_CHANGE_HOURS. Смена внутри паузы не теряется: после паузы сообщается, если текущее
        всё ещё отличается от сообщённого. Ошибки не выходят наружу (задача без ожидающего)."""
        try:
            async with self._auto_lock:
                await self._auto_change_check(self.cfg.base_coins if bases is None else bases)
        except Exception:
            log.exception("Автосигналы: проверка смены не выполнена")

    async def _auto_change_check(self, bases: list[str]) -> None:
        # без свежего среза — пропуск, а пропуск рвёт серию «подряд»
        self._break_streaks([b for b in self.cfg.base_coins if b not in bases])
        gen = self._auto_gen
        sigs = await self._auto_signals(bases)
        if gen != self._auto_gen:  # пока считали, следующий слот пропустил проверку и сбросил серии
            log.warning("Автосигналы: проверка смены опоздала на слот — итоги не применены")
            return
        for base, sig in sigs.items():
            try:  # сбой по одному активу не мешает проверить остальные
                await self._auto_change_base(base, sig)
            except Exception:
                log.exception("Автосигналы: смена по %s не проверена", base)
                self._break_streaks([base])

    async def _auto_change_base(self, base: str, sig: Signal | Exception) -> None:
        st = self._auto.get(base)
        if isinstance(sig, Exception):
            log.warning("Автосигналы: %s не посчитан — смена не проверена", base, exc_info=sig)
            self._break_streaks([base])  # пропуск рвёт серию: «подряд» — значит без пропусков
            return
        key, now = signal_key(sig), time.time()
        if st is None:
            st = self._auto[base] = AutoState()
        if st.ready and key == st.reported:  # вернулось к сообщённому — кандидата и ожидания повтора больше нет
            st.break_streak()
            st.retry_at = 0.0
            return
        # смена должна продержаться AUTO_CHANGE_CONFIRM проверок подряд: один срез с широким спредом
        # или без котировки не даёт «есть → нет → есть»
        if st.streak == 0 or key != st.pending:  # нет кандидата (streak 0; pending None — ещё и «сигнала нет»)
            st.pending, st.streak, st.retry_at = key, 0, 0.0  # или другой — повтор прошлого не держит его
        st.streak += 1
        if not st.ready:  # после старта: точка отсчёта — то, что продержалось AUTO_CHANGE_CONFIRM проверок, без сообщения
            if st.streak >= AUTO_CHANGE_CONFIRM:
                st.report(key)
            return
        hold = self.cfg.auto_signals_change_hours * 3600
        if st.streak < AUTO_CHANGE_CONFIRM or now - st.reported_at < hold or now < st.retry_at:
            return
        prev = st.reported
        # без «Обновить»: обновление заменило бы сообщение карточкой и стёрло «что на что сменилось»
        head = f"🔔 {base}: рекомендация сменилась — {strategy_name(prev)} → {strategy_name(key)}\n"
        tail = f"\n\nСвежий расчёт — /signals {base}"
        text = pre(fmt_signal(sig, self.cfg.env, head=head) + tail)
        if len(text) > TG_MAX_TEXT:  # длинная карточка (много причин и отказов) — краткая, иначе не уйдёт никогда
            text = pre(f"{env_banner(self.cfg.env)}\n{head}\n{fmt_signal_brief(sig)}{tail}")
        got = await self._send_admins(text)
        if got:  # дошло хоть кому-то — сообщено (остальные увидят в сводке)
            st.report(key, now)
            await self._record_auto(sig, got)
        else:  # личка закрыта или сеть: не долбить каждые 15 мин — повтор через AUTO_RETRY_S
            st.retry_at = now + AUTO_RETRY_S
            log.warning("Автосигналы: смена по %s никому не доставлена — повтор через %d мин", base,
                        AUTO_RETRY_S // 60)

    async def _mark_announced(self, ids: list[int]) -> None:
        """Отметить отправленное; БД занята — ещё две попытки (иначе та же сводка уйдёт в группы снова)."""
        for attempt in range(3):
            try:
                await asyncio.to_thread(self.sigjournal.mark_announced, ids)
                return
            except Exception:
                if attempt == 2:
                    raise
                await asyncio.sleep(2)

    # ---------- счёт (чувствительное) ----------
    @staticmethod
    def _base_arg(context) -> str | None:
        return context.args[0].upper() if context.args else None

    @guarded("view", sensitive=True)
    async def cmd_bal(self, update: Update, context: ContextTypes.DEFAULT_TYPE, w: Who):
        await self.reply(update, fmt_balance(await self.call(self.c.balance)))

    @guarded("view", sensitive=True)
    async def cmd_pos(self, update: Update, context: ContextTypes.DEFAULT_TYPE, w: Who):
        await self.reply(update, *await self._list_view("pos", self._base_arg(context), w.can_trade))

    @staticmethod
    def _is_period(a: str) -> bool:
        try:
            parse_period([a])
            return True
        except ValueError:
            return False

    @staticmethod
    def _csv_name(prefix: str, label: str) -> str:
        return f"{prefix}-{label.replace('..', '_').replace(' ', '').replace('.', '')}.csv"

    @staticmethod
    def _list_key(what: str, it: dict) -> tuple:
        """Устойчивый порядок строк /pos, /orders: листание — от последней показанной строки, а не по номеру
        страницы, чтобы исполненный или новый ордер не сдвигал строки мимо экрана."""
        if what == "pos":
            return it["symbol"], it.get("side", "")
        return it["symbol"], int(it.get("createdTime") or 0), it["orderId"]

    async def _list_view(self, what: str, base: str | None, trade: bool,
                         anchor: tuple[str, tuple] | None = None) -> tuple[str, InlineKeyboardMarkup | None]:
        """/pos или /orders: LIST_MAX строк (с кнопками строк для трейдера) и ◀ ▶. anchor — ("a", ключ) —
        строки после ключа, ("b", ключ) — перед ним. Каждый раз заново с биржи — текущее состояние."""
        items = await self.call(self.c.positions if what == "pos" else self.c.open_orders, base)
        keyed = sorted(((self._list_key(what, it), it) for it in items), key=lambda kv: kv[0])
        keys, items = [k for k, _ in keyed], [it for _, it in keyed]
        start = 0
        if anchor and anchor[0] == "a":
            start = sum(k <= anchor[1] for k in keys)
        elif anchor and anchor[0] == "b":
            start = max(0, sum(k < anchor[1] for k in keys) - LIST_MAX)
        if start >= len(items):  # дальше ничего не осталось — последняя страница
            start = max(0, len(items) - LIST_MAX)
        chunk = items[start:start + LIST_MAX]
        rows = []
        if trade:  # кнопки строк — только трейдерам
            for it in chunk:
                label = short_symbol(it["symbol"])
                if what == "pos":
                    t = self._tok(symbol=it["symbol"])
                    rows.append([(f"Закрыть {label} · рынок", f"cp:{t}:mkt"), ("· mid", f"cp:{t}:mid")])
                else:
                    t = self._tok(symbol=it["symbol"], oid=it["orderId"])
                    label = f"{label} {it['side']} {it['qty']}"
                    if it.get("orderType") == "Limit":
                        rows.append([(f"✏ {label}", f"am:{t}"), ("✖ Отменить", f"cx:{t}")])
                    else:
                        rows.append([(f"✖ Отменить {label}", f"cx:{t}")])
        text = fmt_positions(chunk) if what == "pos" else fmt_orders(chunk)
        # и когда список сократился до одной страницы, но показан хвост: выше есть строки — нужен «Назад»
        if start > 0 or start + len(chunk) < len(items):
            text += f"\n\nСтроки {start + 1}–{start + len(chunk)} из {len(items)}"
            nav = []
            if start > 0:
                nav.append(("◀ Назад", f"pg:{self._tok(what=what, base=base, trade=trade, dir='b', key=keys[start])}"))
            if start + len(chunk) < len(items):
                nav.append(("Далее ▶", f"pg:{self._tok(what=what, base=base, trade=trade, dir='a', key=keys[start + len(chunk) - 1])}"))
            rows.append(nav)
        return text, kb(rows) if rows else None

    @guarded("view", sensitive=True)
    async def cmd_greeks(self, update: Update, context: ContextTypes.DEFAULT_TYPE, w: Who):
        await self.reply(update, fmt_greeks(await self.call(self.c.greeks, self._base_arg(context))))

    @guarded("view", sensitive=True)
    async def cmd_orders(self, update: Update, context: ContextTypes.DEFAULT_TYPE, w: Who):
        await self.reply(update, *await self._list_view("orders", self._base_arg(context), w.can_trade))

    # ---------- торговля ----------
    async def _ask_confirm(self, update: Update, context, w: Who, text: str, event: str,
                           action: Callable[[], dict], done: Callable[[dict], str], q=None,
                           lookup: Callable[[], str] | None = None, order_id: str | None = None,
                           release: Callable[[], None] | None = None):
        pid = secrets.token_hex(6)
        self.pending[pid] = Pending(text, action, done, event, w, time.monotonic(), lookup, order_id, release)
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
        hold = ManualHold(self._guard)

        def lookup() -> str:
            return self.c.order_status_after_failure(req)

        def place() -> dict:
            # orderLinkId с хешем пользователя; «кто выставил» пишется в журнал ДО отправки на биржу
            if not req.link_id:
                req.link_id = self.journal.register_order(w.user_id, w.name, w.chat_id)
            try:
                # идёт сделка по сигналу с этим символом: ручной ордер смешался бы с её ногами (и откатом).
                # Пометка «ручной в отправке» — атомарно с проверкой: сделка по символу не начнётся, пока он летит
                return self._guarded_send(req.symbol, lambda: self.c.place_order(req), req, hold)
            except Exception as e:
                if not req.sent or (isinstance(e, BybitError) and not isinstance(e, BybitNetworkError)):
                    self.journal.forget_order(req.link_id)  # точно не на бирже — не оставлять «призрак»
                    req.link_id = None
                raise

        await self._ask_confirm(
            update, context, w, fmt_order_preview(req, req.ticker), event,
            action=place,
            done=lambda r: f"✅ Ордер принят\n{req.describe()}\norderId {r.get('orderId')}",
            lookup=lookup, release=hold.release,
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

    def _guarded_send(self, symbol: str, send: Callable[[], dict], req: OrderRequest | None = None,
                      hold: ManualHold | None = None) -> dict:
        """Ручной ордер/изменение по символу: не во время сделки по сигналу с ним (и она не начнётся, пока летит).
        Сбой связи после отправки — символ остаётся занятым до выяснения статуса (hold.release)."""
        self._guard.claim_manual(symbol)
        kept = False
        try:
            return send()
        except BybitNetworkError:
            if hold is not None and req is not None and req.sent:
                hold.keep(symbol)
                kept = True
            raise
        finally:
            if not kept:
                self._guard.release_manual(symbol)

    async def _confirm_amend(self, update: Update, context, w: Who, symbol: str, order_id: str,
                             qty, price, iv, q=None):
        req, orig = await self.call(self.c.amend_request, symbol, order_id, qty, price, iv)
        oid = orig["orderId"]

        def lookup() -> str:
            return self.c.amend_status_after_failure(oid, req)

        hold = ManualHold(self._guard)
        await self._ask_confirm(
            update, context, w, fmt_amend_preview(req, orig, req.ticker), "amend",
            action=lambda: self._guarded_send(req.symbol, lambda: self.c.amend_order(oid, req), req, hold),
            done=lambda r: f"✅ Ордер изменён\n{req.describe()}\norderId {r.get('orderId')}",
            q=q, lookup=lookup, order_id=oid, release=hold.release,
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
        tok = self._tok(symbol=inst.symbol, oid=order_id, user=w.user_id, name=w.name,
                        prices={k: str(v) for k, v in prices.items()})
        self.audit("amend.menu", w, symbol=inst.symbol, order_id=order_id)
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
            # selective + упоминание: поле ответа открывается только у автора меню, а не у всей группы
            mention = f'<a href="tg://user?id={w.user_id}">{html.escape(w.name)}</a>'
            prompt = await update.effective_message.reply_text(
                f"{mention}, новая цена для {html.escape(short_symbol(d['symbol']))} — ответом на это сообщение:\n"
                "160   или   iv50   или   160 0.02 (цена и количество)",
                parse_mode=ParseMode.HTML,
                reply_markup=ForceReply(selective=True, input_field_placeholder="160 / iv50 / 160 0.02"),
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
            raise ValueError(f"Этот вопрос для {d.get('name') or 'другого трейдера'}. "
                             "Чтобы изменить ордер самому — /orders → ✏️")
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
            price = quote_mid(await self.call(self.c.ticker, d["symbol"]))[2]
        req = await self.call(self.c.close_position_request, d["symbol"], price)
        await self._confirm_order(update, context, w, req, "close")

    async def _do_cancel(self, update: Update, context, w: Who, symbol: str, order_id: str):
        inst = await self.call(self.c.resolve_symbol, symbol)
        self.audit("cancel.request", w, symbol=inst.symbol, order_id=order_id)
        try:
            async with self._order_lock(order_id):  # без выяснения статуса — оно не должно держать ордер
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
        if p.event == "signal_exec":
            # многоногая сделка — до минуты: фоновой задачей, иначе бот (обновления — по одному) не отвечает никому,
            # в т.ч. на срочную отмену/закрытие. Пересечения по символам — SymbolGuard
            context.application.create_task(self._run_confirmed(q, context, w, pid, p))
            return
        await self._run_confirmed(q, context, w, pid, p)

    async def _run_confirmed(self, q, context, w: Who, pid: str, p: Pending):
        """Выполнить подтверждённое действие и показать итог (в том же сообщении)."""
        try:
            # блокировка — только на проверку и отправку: выяснение статуса после сбоя (до 1–2 мин)
            # не должно задерживать, например, срочную отмену этого ордера другим трейдером
            async with self._order_lock(p.order_id):
                res = await self.call(p.action)
        except BybitNetworkError as e:
            # запрос мог дойти до биржи — не говорим «ошибка», выясняем фактическое состояние
            try:
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
            finally:
                if p.release:  # статус выяснен (или не удалось) — символ снова свободен
                    p.release()
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
        try:
            await self.edit(q, pre(f"{p.text}\n\n{result}"))
        except TelegramError:  # сообщение удалено / сбой Telegram — итог отдельным сообщением
            log.warning("Не удалось показать итог %s", p.event, exc_info=True)
            try:
                await context.bot.send_message(w.chat_id, pre(result), parse_mode=ParseMode.HTML)
            except TelegramError:
                log.warning("Итог %s не доставлен", p.event, exc_info=True)
        self.notify_bg(context, result, w)

    @guarded("any")
    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE, w: Who):
        q = update.callback_query
        kind, _, rest = (q.data or "").partition(":")
        need_trade = kind in ("ok", "no", "cx", "am", "ap", "cp", "ex")
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

        if kind == "pg":  # pg:TOKEN — соседняя страница /pos, /orders в том же сообщении
            try:
                pg = self._tok_get(rest)
            except ValueError:
                pg = None
            if not pg or "what" not in pg:
                await q.answer("Кнопка устарела — повторите команду")
                return
            if w.is_group and self.cfg.sensitive_commands_dm_only:  # как у самих команд: счёт — только в личке
                await q.answer("Только в личке с ботом")
                return
            await q.answer()
            # вид — как у того, кто запросил список (сообщение общее): кнопки трейдера не пропадут от листания
            # зрителем, а зритель не добавит их в чужой список (нажать их он всё равно не сможет — нет прав)
            text, markup = await self._list_view(pg["what"], pg["base"], pg["trade"], (pg["dir"], pg["key"]))
            await self.edit(q, pre(text), markup)
            return
        if kind == "sig":  # sig:BASE — сигнал (выбор актива или «Обновить») в том же сообщении
            # ответ на кнопку — после расчёта (подсказка зависит от результата), но обязательно, и при ошибке тоже
            toast = None
            try:
                text, markup, cached = await self._signal_view(rest, w)
                # тот же текст — Telegram не меняет сообщение; без подсказки «Обновить» выглядит сломанной
                if not await self.edit(q, text, markup):
                    toast = (f"Без изменений: расчёт моложе {SIGNAL_CACHE_TTL} с" if cached
                             else "Без изменений: рынок тот же")
            except Exception:
                toast = "Не удалось — см. сообщение ниже"
                raise
            finally:
                with contextlib.suppress(TelegramError):  # долгий расчёт: «query is too old» не мешает
                    await q.answer(toast)
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
        elif kind == "ex":  # ex:SIGNAL_ID — «Выставить» из карточки сигнала
            await self._exec_signal(update, context, w, int(rest))

    # ---------- «Выставить» сигнал (этап 5) ----------
    async def _exec_signal(self, update: Update, context, w: Who, sid: int):
        """Превью многоногой сделки по записи журнала (ноги и кол-во — как в сигнале; цены — свежие) и
        подтверждение. Исполнение — MultiLegExecutor: сначала покупки, потом продажи, IOC от ask/bid."""
        msg = update.callback_query.message
        shown = getattr(msg, "edit_date", None) or getattr(msg, "date", None)  # «Обновить» правит то же сообщение
        # тот же id = та же сделка (ноги) — её снова показали кому-то: рекомендация актуальна, карточка свежая
        card_ts, card_px, card_risk = self._card_shown.get((w.chat_id, sid), (0, {}, None))
        seen = max(shown.timestamp() if shown is not None else 0, card_ts)
        if time.time() - seen > EXEC_CARD_MAX_AGE_S:
            raise ValueError(f"Карточке больше {EXEC_CARD_MAX_AGE_S // 60} мин — нажмите «Обновить» и выставляйте "
                             "по свежей")
        row = await self.call(self.sigjournal.get, sid)
        now_ms = int(time.time() * 1000)
        if row is None:
            raise ValueError(f"Сигнал #{sid} не найден в журнале")
        if row["settled_ms"] is not None or (row["expiry_ms"] or 0) <= now_ms + EXEC_MIN_LEFT_MS:
            raise ValueError(f"Сигнал #{sid} устарел (экспирация {row['expiry']} прошла или ближе часа) — "
                             "запросите свежий /signals")
        legs = [(d["symbol"], leg_side(d["side"]), _d(str(d["qty"])), float(d["price"]))
                for d in row["legs"]]
        hide_account = w.is_group and self.cfg.sensitive_commands_dm_only  # баланс, маржа, дельта — как /bal

        def checked(legs: list[tuple]) -> tuple:
            """precheck; в группе отказ лимитов — без цифр счёта. -> котировки, строки лимитов и ноги с кол-вом
            после округления к шагу биржи (их и исполнять)."""
            try:
                quotes, limits = precheck(self.c, self.cfg, legs)
            except PortfolioRiskError:  # текст — с данными счёта (символы позиций, баланс, дельта)
                if hide_account:
                    raise RiskError("Отказ по состоянию счёта (позиции/ордера, маржа, дельта или премия за сутки) — "
                                    "подробности: в личке с ботом /signals BASE и «Выставить» там") from None
                raise
            qty = {(q.req.symbol, q.req.side): q.req.qty for q in quotes}
            return quotes, limits, [(s, side, qty.get((s, side), q), p) for s, side, q, p in legs]

        # все проверки заранее: биржа и цена — по ноге (худшая цена попыток), портфель — по сделке целиком
        quotes, limits, legs = await self.call(checked, legs)
        lines = [f"📝 Выставить сигнал #{sid}: {STRATEGIES.get(row['strategy'], row['strategy'])} · {row['expiry']}",
                 f"Кол-во: {row['qty']:g} · ноги по порядку (сначала покупки):"]
        mid_net = 0.0
        if card_px:  # цены карточки, которую видели (а не первой выдачи сигнала)
            legs = [(s, side, q, card_px.get((s, side), p)) for s, side, q, p in legs]
        plans = {(s, side): p for s, side, _, p in legs}
        for lq in quotes:
            sym, side, qty, bid, ask, mid = lq.req.symbol, lq.req.side, lq.req.qty, lq.bid, lq.ask, lq.mid
            plan = plans.get((sym, side), 0.0)
            mid_net += float(mid * qty) * (1 if side == "Sell" else -1)
            lines.append(f" {side.upper():4} {qty} {short_symbol(sym):<22} bid {px(bid)} / ask {px(ask)} "
                         f"· mid {px(mid)} (в сигнале {px(plan)})")
        plan_net = sum(p * float(q) * (1 if side == "Sell" else -1) for _, side, q, p in legs)
        lines.append(f"{'Кредит' if mid_net >= 0 else 'Дебет'} по mid сейчас: {abs(mid_net):.2f} USDT "
                     f"(в сигнале {abs(plan_net):.2f}) · макс. риск по сигналу "
                     f"{card_risk if card_risk is not None else (row['max_loss'] or 0):.2f}")
        if limits and hide_account:
            lines.append("Лимиты (сделка целиком): проверены (подробности — в личке)")
        elif limits:
            lines.append("Лимиты (сделка целиком): " + "; ".join(limits))
        prev = await self.call(self.sigjournal.executions, sid)
        if prev:
            done_qty = sum(e["qty"] or 0 for e in prev)
            # открытая позиция по ногам сюда не дошла бы (precheck: символ занят) — значит, прошлые закрыты
            lines.append(f"⚠️ Уже выставлялся {len(prev)} раз (исполнено {done_qty:g}) — откроется новая позиция")
        passive = self.cfg.passive_first_leg_seconds
        if passive > 0 and quotes and quotes[0].req.side == "Buy":
            lines.append(f"Первая покупка — сначала PostOnly по mid на {passive:g} с, остаток — IOC")
        else:
            passive = 0
        lines.append(f"Каждая нога — IOC от ask/bid, не дальше mid ±{self.cfg.multileg_slippage_pct:g}%, "
                     f"{self.cfg.multileg_steps} попыт.; не собралось — исполненное закроется reduce-only")
        est = passive + len(legs) * self.cfg.multileg_steps * (1.2 + self.cfg.multileg_step_delay_s)
        lines.append(f"Обычно до ~{est:.0f} с; при задержках биржи и закрытии лишнего — дольше. В это время другие "
                     "сделки по сигналам и ручные ордера по этим символам отклоняются")

        def run() -> dict:
            self._guard.claim_exec({s for s, *_ in legs})
            try:
                res = execute()
            finally:
                self._guard.release_exec()  # до записи в журнал: занятая БД не держит символы закрытыми
            try:
                self.sigjournal.record_execution(sid, res, w.user_id, w.name, w.chat_id)
            except Exception:
                log.exception("Журнал сигналов: исполнение #%s не записано", sid)
            return {"result": res}

        def execute():
            # портфель мог измениться за время подтверждения (другие сделки) — ещё раз, по свежим данным.
            # Ничего ещё не отправлено: сбой связи здесь — обычная ошибка, а не «исход неизвестен»
            if row["expiry_ms"] <= time.time() * 1000 + EXEC_MIN_LEFT_MS:  # за время подтверждения
                raise ValueError(f"До экспирации {row['expiry']} меньше часа — не выставляю")
            try:
                _, _, fresh = checked(legs)  # исполняем то, что проверено сейчас (кол-во после округления)
            except BybitNetworkError as e:
                raise BybitError(f"сбой связи при проверке, ордера не отправлялись: {e}") from e
            ex = MultiLegExecutor(self.c, self.cfg, link=lambda leg: self.journal.register_signal_order(
                sid, leg, w.user_id, w.name, w.chat_id), forget=self.journal.forget_order)
            return ex.execute(fresh)

        def done(r: dict) -> str:
            res = r["result"]
            try:
                text = fmt_result(res, sid)
            except Exception as e:  # итог не сформирован — срочное админу всё равно уходит
                log.exception("Итог сделки по сигналу #%s не сформирован", sid)
                text = (f"⚠️ Итог сделки по сигналу #{sid} не сформирован ({type(e).__name__}): статус {res.status}, "
                        f"осталось: {'; '.join(res.left) or '—'} — проверьте /pos и /orders")
            if res.alert:  # неполное исполнение / осталась позиция / исход неизвестен — администратору сразу
                context.application.create_task(self.admin_alert(
                    f"multileg:{sid}", f"Многоногий ордер исполнен не полностью ({w.name})\n\n{text}", quiet=False,
                    retry=True))
            return text

        await self._ask_confirm(update, context, w, "\n".join(lines), "signal_exec", action=run, done=done)

    # ---------- сборщик рыночных данных ----------
    async def _backup_job(self, context: ContextTypes.DEFAULT_TYPE | None) -> None:
        """Ежедневная копия БД (sqlite backup API); сбой — срочно администратору."""
        try:
            path = await asyncio.to_thread(backup_mod.backup, self.cfg)
            log.info("Резервная копия БД: %s", path)
        except Exception as e:
            log.exception("Резервная копия БД не сделана")
            await self.admin_alert("backup", f"Резервная копия БД не сделана: {e}")

    async def _ping_health(self, ok: bool) -> None:
        """Внешний мониторинг: пинг после среза (сбой среза — /fail). Пинги пропали — сервис сообщит сам."""
        url = self.cfg.healthcheck_url
        if not url:
            return
        if not ok:  # /fail — к пути, не после ?query
            u = urllib.parse.urlsplit(url)
            url = urllib.parse.urlunsplit(u._replace(path=u.path.rstrip("/") + "/fail"))
        else:
            url = url.rstrip("/")

        def get():
            req = urllib.request.Request(url, headers={"User-Agent": "options-bot"})
            with urllib.request.urlopen(req, timeout=10) as r:
                r.read(64)
        try:
            await asyncio.to_thread(get)
        except Exception as e:  # сбой мониторинга — не сбой бота: только лог (без адреса — в нём ключ проверки)
            log.warning("Мониторинг: пинг не отправлен — %s", type(e).__name__)

    async def _collect_job(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Задача JobQueue: срез в БД. Ошибки не роняют бота — следующий проход по расписанию. Затем — пинг
        мониторинга и проверка смены автосигналов отдельной задачей (по свежему срезу; срез не удался — без
        проверки)."""
        try:
            res = await asyncio.to_thread(self.collector.collect)
        except Exception:
            log.exception("Сборщик: проход не выполнен")
            await self._ping_health(False)
            self._break_streaks(self.cfg.base_coins)  # среза нет — история IV старая: смену не проверяем,
            return                                    # а пропуск рвёт серию «подряд»
        (log.warning if res.errors else log.info)("Сборщик: %s", res)
        if self.cfg.fng_url and not (self._fng_task and not self._fng_task.done()):
            self._fng_task = asyncio.get_running_loop().create_task(self._fng_job())  # сеть — не на пути среза
        if self.cfg.paper.enabled and (res.snapshots or res.skipped):
            self._kick_paper()
        if self._auto_change_on():
            # только активы со срезом в этом слоте (записан сейчас или уже был — повторный запуск); по активу
            # с ошибкой сбора история IV старая — его смену не проверяем
            fresh = [b for b in self.cfg.base_coins if b not in res.failed_bases]
            if res.snapshots or res.skipped:
                self._kick_auto_change(fresh)
            else:
                self._break_streaks(self.cfg.base_coins)
        await self._ping_health(bool(res.snapshots or res.skipped))  # последним: медленный мониторинг не задержит

    # ---------- журнал сделок ----------
    async def _sync_job(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Сверка журнала с биржей. Исполнения, которых не было в WebSocket, — одним сводным уведомлением."""
        try:
            res = await asyncio.to_thread(self.journal.sync, self.c)
        except Exception:
            log.exception("Журнал: сверка не выполнена")
            return
        (log.warning if res.errors else log.info)("Журнал: %s", res)
        await self._check_delivery_fees()
        missed = res.missed
        if not missed:
            return
        log.warning("Журнал: %d исполнений без уведомления найдено сверкой (бот был выключен или обрыв WebSocket)",
                    len(missed))
        sent = 0
        try:
            lines = []
            srcs = await asyncio.to_thread(self.journal.describe_many, [r.get("orderLinkId") for r in missed[:10]])
            for r in missed[:10]:
                src = srcs[r.get("orderLinkId")]
                lines.append(f"{r.get('side', '?').upper()} {r.get('execQty')} {short_symbol(r.get('symbol', '?'))} "
                             f"@ {r.get('execPrice')} · {src}")
            more = f"\n… и ещё {len(missed) - 10}" if len(missed) > 10 else ""
            sent = await self._broadcast(pre(f"{env_banner(self.cfg.env)}\n🔄 Сверка журнала: {len(missed)} исполн., "
                                             f"пропущенных уведомлениями\n" + "\n".join(lines) + more), "сверки")
        except Exception:
            log.exception("Журнал: сводка сверки не отправлена")
        if not sent:  # никому не дошло — вернуть «не объявлено», объявит следующая сверка
            await asyncio.to_thread(self.journal.unnotify, [r.get("execId") for r in missed])
        ext = [r for r in missed if r.get("execType", "Trade") == "Trade"
               and parse_link_id(r.get("orderLinkId"))["source"] == "external"]
        if ext and sent:  # сделки вне бота, пропущенные WebSocket, — тоже срочно администратору (одним сообщением;
            # сводка не дошла — исполнения вернутся в следующую сверку, сообщим тогда)
            body = "; ".join(f"{r.get('side', '?').upper()} {r.get('execQty')} {r.get('symbol', '?')} @ "
                             f"{r.get('execPrice')}" for r in ext[:10]) + (f" … и ещё {len(ext) - 10}" if len(ext) > 10 else "")
            await self.admin_alert("external-sync",
                                   f"Сделки не через бота (найдены сверкой): {body}. Проверьте /pos.",
                                   quiet=False, retry=True)

    async def _check_delivery_fees(self) -> None:
        """Новая поставка в деньгах в журнале — сверить формулу комиссии поставки бумажных книг (справки Bybit
        расходятся): администратору — первое совпадение и каждое расхождение."""
        def run():
            conn = db.connect(self.cfg.db_file)
            try:
                return paper_mod.check_delivery_fees(self.cfg, conn)
            finally:
                conn.close()
        if not self.cfg.paper.enabled:
            return
        try:
            got = await asyncio.to_thread(run)
        except Exception:
            log.exception("Проверка комиссии поставки не выполнена")
            return
        rep = [x for x in got if x["report"]]
        for x in got:
            if not x["report"]:
                log.info("%s", paper_mod.fmt_delivery_check(x))
        if rep:  # одним сообщением (первая проверка может найти много старых поставок)
            body = "\n\n".join(paper_mod.fmt_delivery_check(x) for x in rep[:5])
            more = f"\n\n… и ещё {len(rep) - 5} — python -m optbot.paper --check-delivery-fees" if len(rep) > 5 else ""
            await self.admin_alert(f"delivery-fee:{rep[-1]['key']}", body + more, quiet=False, retry=True)

    @guarded("view", sensitive=True)
    async def cmd_journal(self, update: Update, context: ContextTypes.DEFAULT_TYPE, w: Who):
        args = [a for a in context.args if a.lower() != "csv"]
        want_csv = len(args) != len(context.args)
        if len(args) > 1:
            raise ValueError("Формат: /journal [сегодня|вчера|7d|ДАТА|ДАТА..ДАТА] [csv]")
        if not self.cfg.journal_enabled:
            raise ValueError("Журнал выключен (JOURNAL_ENABLED=false)")
        start, end, label = parse_period(args)
        rep = await asyncio.to_thread(self.journal.report, start, end, label)
        await self.reply(update, f"{env_banner(self.cfg.env)}\n\n{fmt_report(rep)}")
        if want_csv:
            data = await asyncio.to_thread(self.journal.csv_bytes, start, end)
            name = self._csv_name(f"journal-{self.cfg.env}", label)
            await update.effective_chat.send_document(document=data, filename=name)

    # ---------- исполнения (WebSocket) ----------
    def _on_ws_executions(self, rows: list[dict]) -> None:
        """Вызывается из потока pybit: только передаёт в asyncio (запись в БД может ждать блокировку —
        поток WebSocket ждать не должен)."""
        if self._loop and self._app:
            self._loop.call_soon_threadsafe(lambda: self._app.create_task(self._ws_executions(rows)))

    def _on_ws_orders(self, rows: list[dict]) -> None:
        """Вызывается из потока pybit."""
        if self._loop and self._app:
            self._loop.call_soon_threadsafe(lambda: self._app.create_task(self._ws_orders(rows)))

    async def _ws_executions(self, rows: list[dict]) -> None:
        """Запись в журнал и уведомление только о ещё не объявленных (дубли WS и объявленное сверкой — нет)."""
        try:
            rows = await asyncio.to_thread(self.journal.record_executions, rows, "ws")
        except Exception:
            log.exception("Журнал: исполнения из WebSocket не записаны")  # уведомление важнее — шлём всё
        if rows:
            await self.on_executions(rows)

    async def _ws_orders(self, rows: list[dict]) -> None:
        try:
            await asyncio.to_thread(self.journal.record_orders, rows)
        except Exception:
            log.exception("Журнал: ордера из WebSocket не записаны")

    async def on_executions(self, rows: list[dict]):
        by_order: dict[str, list[dict]] = {}
        for r in rows:
            by_order.setdefault(r.get("orderId", "?"), []).append(r)
        texts, outside = [], []
        srcs = await asyncio.to_thread(self.journal.describe_many,
                                       [rs[-1].get("orderLinkId") for rs in by_order.values()])
        for oid, rs in by_order.items():
            qty = sum((_d(r.get("execQty")) for r in rs), _d(0))
            value = sum((_d(r.get("execQty")) * _d(r.get("execPrice")) for r in rs), _d(0))
            fee = sum((_d(r.get("execFee")) for r in rs), _d(0))
            last = rs[-1]
            avg = value / qty if qty else _d(0)
            etype = last.get("execType", "Trade")
            kind = {"Trade": "", "Delivery": " (поставка/экспирация)", "Settle": " (расчёт)",
                    "BustTrade": " (ликвидация)"}.get(etype, f" ({etype})")
            src = srcs[last.get("orderLinkId")]
            if etype == "Trade" and parse_link_id(last.get("orderLinkId"))["source"] == "external":
                outside.append((f"external:{oid}", f"Сделка не через бота: {last.get('side', '?').upper()} {qty} "
                                f"{last.get('symbol', '?')} @ {px(avg)} (ордер {oid[:8]}) — позиция изменена вне "
                                "бота (вручную на бирже или другой программой). Проверьте /pos."))
            left = _d(last.get("leavesQty"))
            status = "ордер исполнен полностью" if left == 0 else f"остаток {left}"
            texts.append(f"💥 Исполнение{kind}\n"
                         f"{last.get('side', '?').upper()} {qty} {short_symbol(last.get('symbol', '?'))} @ {px(avg)}\n"
                         f"комиссия {fee:.4g} · {status}\nордер {oid[:8]} · {src}")
            audit_log.info("exec symbol=%s side=%s qty=%s avg=%s fee=%s type=%s order_id=%s link_id=%s",
                           last.get("symbol"), last.get("side"), qty, avg, fee, etype, oid, last.get("orderLinkId"))
        if texts:
            await self._broadcast(pre(f"{env_banner(self.cfg.env)}\n" + "\n\n".join(texts)), "об исполнении")
        if outside:  # срочно администратору одним сообщением — после общего уведомления; не дошло — повторы
            more = f"\n\n… и ещё {len(outside) - 10} ордеров вне бота" if len(outside) > 10 else ""
            await self.admin_alert(outside[0][0], "\n\n".join(t for _, t in outside[:10]) + more, quiet=False,
                                   retry=True)

    def _targets(self) -> list[int]:
        """Получатели уведомлений: разрешённые группы и лички трейдеров и администраторов (кроме недоступных)."""
        dms = (self.cfg.tg_trader_ids | self.cfg.tg_admin_ids) - self._dm_failed
        return sorted(self.cfg.tg_allowed_chat_ids) + sorted(dms)

    async def _broadcast(self, msg: str, what: str) -> int:
        """В разрешённые группы и в личку трейдерам (кроме тех, кому личка недоступна). -> сколько доставлено."""
        if not self._app:
            return 0
        sent = 0
        for chat_id in self._targets():
            try:
                await self._app.bot.send_message(chat_id, msg, parse_mode=ParseMode.HTML)
                sent += 1
            except TelegramError as e:
                permanent = isinstance(e, Forbidden) or (
                    isinstance(e, BadRequest) and "chat not found" in str(e).lower())
                if chat_id > 0 and permanent:
                    self._dm_failed.add(chat_id)  # не начинал диалог / заблокировал бота — больше не пробуем
                log.warning("Уведомление %s в %s не доставлено: %s", what, chat_id, e)
        return sent

    TG_OUTAGE_GAP_S = 120      # сбои опроса Telegram с паузой больше — уже новая серия
    TG_OUTAGE_REPORT_S = 300   # серия дольше — раз в столько секунд ERROR «Telegram недоступен N мин»

    @staticmethod
    def _from_polling(err: BaseException) -> bool:
        """Ошибка самого опроса getUpdates (её повторяет библиотека), а не команды/задачи (их не повторяет никто)."""
        tb = err.__traceback__
        while tb is not None:
            if tb.tb_frame.f_code.co_name == "polling_action_cb":
                return True
            tb = tb.tb_next
        return False

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        """Сетевой сбой опроса getUpdates (502 Bad Gateway и т.п.) — одной строкой WARNING: библиотека сама повторяет
        запрос, сообщения не теряются. Затянувшаяся серия — строкой ERROR раз в 5 мин. Таймауты опроса библиотека
        повторяет молча, сюда они не попадают. Ошибки команд, кнопок и задач — ERROR с трейсбеком (их не повторяют)."""
        err = context.error
        if (update is None and isinstance(err, NetworkError) and not isinstance(err, BadRequest)
                and self._from_polling(err)):
            now = time.monotonic()
            start, last, reported = self._tg_outage or (now, now, now)
            if now - last > self.TG_OUTAGE_GAP_S:
                start = reported = now
            log.warning("Telegram: сбой опроса (%s: %s) — библиотека повторит", type(err).__name__, err)
            if now - start >= self.TG_OUTAGE_REPORT_S and now - reported >= self.TG_OUTAGE_REPORT_S:
                log.error("Telegram недоступен уже %d мин (последняя ошибка: %s)", (now - start) // 60, err)
                reported = now
            self._tg_outage = (start, now, reported)
            return
        log.error("Необработанная ошибка (update: %s)", update, exc_info=err)

    # ---------- сборка ----------
    COMMANDS = [
        ("help", "cmd_help", "справка"),
        ("id", "cmd_id", "ваш user id и chat id"),
        ("guide", "cmd_guide", "инструкция (PDF)"),
        ("exp", "cmd_exp", "экспирации"),
        ("chain", "cmd_chain", "цепочка опционов"),
        ("t", "cmd_ticker", "котировка инструмента"),
        ("signals", "cmd_signals", "сигнал: стратегия и риск"),
        ("signals_stats", "cmd_signals_stats", "статистика сигналов"),
        ("bal", "cmd_bal", "баланс"),
        ("pos", "cmd_pos", "позиции"),
        ("orders", "cmd_orders", "открытые ордера"),
        ("greeks", "cmd_greeks", "греки портфеля"),
        ("journal", "cmd_journal", "журнал сделок и PnL"),
        ("buy", "cmd_buy", "купить опцион"),
        ("sell", "cmd_sell", "продать опцион"),
        ("close", "cmd_close", "закрыть позицию"),
        ("amend", "cmd_amend", "изменить ордер"),
        ("cancel", "cmd_cancel", "отменить ордер"),
        ("cancelall", "cmd_cancelall", "отменить все ордера"),
    ]

    def _auto_daily_on(self) -> bool:
        return bool(self.cfg.tg_admin_ids) and self.cfg.auto_signals_daily_utc is not None

    def _auto_change_on(self) -> bool:
        return bool(self.cfg.tg_admin_ids) and self.cfg.auto_signals_change_hours > 0

    def _schedule_auto(self, jq) -> None:
        """Автосигналы администраторам: сводка раз в сутки; проверка смены — после среза сборщика (или таймером)."""
        c = self.cfg
        if self._auto_daily_on():
            h, m = c.auto_signals_daily_utc
            jq.run_daily(self._auto_daily_job, time=dtime(h, m, tzinfo=timezone.utc), name="auto_signals_daily")
            log.info("Автосигналы: сводка администраторам (%d) ежедневно в %02d:%02d UTC", len(c.tg_admin_ids), h, m)
        if self._auto_change_on():
            if c.collector_enabled:
                how = "после каждого среза сборщика"  # вызов — из _collect_job
            else:  # без сборщика — своим таймером с тем же периодом (история IV не обновляется)
                jq.run_repeating(self._auto_change_job, interval=c.collector_interval_min * 60,
                                 first=seconds_to_next_slot(time.time(), c.collector_interval_min),
                                 name="auto_signals_change")
                how = f"каждые {c.collector_interval_min} мин (сборщик выключен)"
            log.info("Автосигналы: смена рекомендации — проверка %s, сообщение не чаще раза в %g ч",
                     how, c.auto_signals_change_hours)

    def build_app(self) -> Application:
        async def post_init(app: Application):
            await app.bot.set_my_commands([BotCommand(n, d) for n, _, d in self.COMMANDS])
            self._app, self._loop = app, asyncio.get_running_loop()
            try:  # уведомление о запуске не должно мешать запуску
                await self._announce_start()
            except Exception:
                log.exception("Сообщение о запуске не отправлено")
            self.watcher = ExecutionWatcher(self.cfg, self._on_ws_executions, self._on_ws_orders)
            try:
                await asyncio.to_thread(self.watcher.start)
            except Exception:
                log.exception("WebSocket исполнений не подключён — уведомлений об исполнениях не будет")

            if app.job_queue is None and (self.cfg.collector_enabled or self.cfg.journal_enabled or self._auto_daily_on() or self._auto_change_on() or self.cfg.backup_daily_utc):
                log.warning("Сборщик, сверка журнала, итоги сигналов, автосигналы и резервная копия БД выключены: нет JobQueue — "
                            "установите python-telegram-bot[job-queue] (pip install -r requirements.txt)")
                return
            if app.job_queue is not None:
                self._schedule_auto(app.job_queue)
                if self.cfg.backup_daily_utc:
                    h, m = self.cfg.backup_daily_utc
                    app.job_queue.run_daily(self._backup_job, time=dtime(h, m, tzinfo=timezone.utc), name="backup")
                    log.info("Резервная копия БД: ежедневно в %02d:%02d UTC в %s (хранить %d дн.)", h, m,
                             self.cfg.backup_dir_abs, self.cfg.backup_keep_days)
            if self.cfg.journal_enabled:
                # при старте (догрузить пропущенное, пока бот был выключен) и далее по расписанию
                app.job_queue.run_repeating(self._sync_job, interval=self.cfg.journal_sync_min * 60, first=10,
                                            name="journal_sync")
                log.info("Журнал: сверка через 10 с и далее каждые %d мин", self.cfg.journal_sync_min)
                # итоги сигналов — с периодом сверки журнала (цена поставки — после 08:00 UTC)
                app.job_queue.run_repeating(self._sigeval_job, interval=self.cfg.journal_sync_min * 60, first=120,
                                            name="signals_eval")
                if not self.cfg.collector_enabled:
                    log.warning("Сборщик выключен: у сигналов будет только итог на экспирации — без просадки "
                                "и «50% прибыли» (нет котировок ног)")
            if self.cfg.collector_enabled:
                first = seconds_to_next_slot(time.time(), self.cfg.collector_interval_min)
                app.job_queue.run_repeating(self._collect_job, interval=self.cfg.collector_interval_min * 60,
                                            first=first, name="collector")
                log.info("Сборщик: каждые %d мин, первый срез через %.0f с, БД %s",
                         self.cfg.collector_interval_min, first, self.cfg.db_file)

        async def post_stop(app: Application):  # Telegram ещё доступен (post_shutdown — уже нет)
            undelivered = [x for t, x in self._alert_retries.items() if not t.done()]
            for t in list(self._alert_retries):  # повторы недошедших — не держать остановку
                t.cancel()
            with contextlib.suppress(Exception):
                await self._announce_stop(undelivered)

        async def post_shutdown(app: Application):
            if self._auto_task and not self._auto_task.done():  # не слать в остановленный Telegram
                self._auto_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._auto_task
            if self._paper_task and not self._paper_task.done():  # шаг в потоке доработает сам (запись — транзакциями)
                self._paper_task.cancel()
            if self._fng_task and not self._fng_task.done():
                self._fng_task.cancel()
            if self.watcher:
                await asyncio.to_thread(self.watcher.stop)

        app = (
            Application.builder()
            .token(self.cfg.tg_token)
            .rate_limiter(AIORateLimiter(max_retries=3))
            .post_init(post_init)
            .post_stop(post_stop)
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
    if not (cfg.tg_admin_ids or cfg.tg_trader_ids or cfg.tg_viewer_ids or
            (cfg.allow_all_group_members_view and cfg.tg_allowed_chat_ids)):
        errors.append("Никому нет доступа: заполните TELEGRAM_ADMIN_IDS / TELEGRAM_TRADER_IDS / TELEGRAM_VIEWER_IDS")
    neg = {i for i in cfg.tg_admin_ids if i <= 0}
    if neg:  # автосигналы и функции администратора — только в личку: группе их слать нельзя
        errors.append(f"TELEGRAM_ADMIN_IDS: id пользователей положительные, а не {sorted(neg)}")
    bad = {i for i in cfg.tg_allowed_chat_ids if i > 0}
    if bad:
        errors.append(f"TELEGRAM_ALLOWED_CHAT_IDS: id групп отрицательные, а не {sorted(bad)}")
    svc = (cfg.tg_admin_ids | cfg.tg_trader_ids | cfg.tg_viewer_ids) & SERVICE_USER_IDS
    if svc:
        errors.append(f"В списках пользователей служебные id Telegram: {sorted(svc)}")
    no_qty = [c for c in cfg.base_coins if not cfg.max_order_qty.get(c, 0) > 0]
    if no_qty:  # лимит кол-ва на ордер обязателен для каждой монеты, иначе ордера без ограничения размера
        errors.append("Не задан лимит кол-ва на ордер: " + ", ".join(f"MAX_QTY_{c}" for c in no_qty)
                      + " (обязателен для каждой монеты из BASE_COINS)")
    if cfg.fng_url and not cfg.fng_url.startswith("https://"):
        errors.append("FNG_URL: нужен https://-адрес (или пусто — без индекса страха и жадности)")
    if cfg.healthcheck_url and not cfg.healthcheck_url.startswith("https://"):
        errors.append("HEALTHCHECK_URL: нужен https://-адрес проверки")
    return errors


def config_warnings(cfg: Settings) -> list[str]:
    """Нефатальное: бот стартует, но часть функций не работает."""
    out = []
    if not cfg.tg_admin_ids:
        out.append("TELEGRAM_ADMIN_IDS пуст — срочные уведомления (доступ к бирже, лимиты, сделки вне бота, "
                   "запуск и остановка), автосигналы и проверка комиссии поставки никому не отправляются")
    if not cfg.healthcheck_url:
        out.append("HEALTHCHECK_URL не задан — о падении бота внешний мониторинг не сообщит")
    elif not cfg.collector_enabled:
        out.append("HEALTHCHECK_URL задан, но сборщик выключен — пинги идут после срезов, их не будет: "
                   "мониторинг покажет бот лежащим")
    elif cfg.collector_interval_min > 15:
        out.append(f"HEALTHCHECK_URL: пинг раз в COLLECTOR_INTERVAL_MIN={cfg.collector_interval_min} мин — период "
                   f"проверки в сервисе мониторинга поставьте не меньше (не 15)")
    return out


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
    for w in config_warnings(settings):
        log.warning("%s", w)

    log.info("Старт: env=%s, активы=%s, администраторов=%d, трейдеров=%d, зрителей=%d, групп=%d, "
             "просмотр всем в группах=%s, счёт только в личке=%s",
             settings.env, ",".join(settings.base_coins), len(settings.tg_admin_ids), len(settings.tg_trader_ids),
             len(settings.tg_viewer_ids), len(settings.tg_allowed_chat_ids),
             settings.allow_all_group_members_view, settings.sensitive_commands_dm_only)
    app = OptionsBot(OptionsClient(settings), settings).build_app()
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
