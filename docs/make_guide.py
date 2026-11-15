"""Генерация PDF-инструкции по боту опционов (reportlab, шрифты DejaVu для кириллицы)."""
import sys
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    CondPageBreak, KeepTogether, PageBreak, Paragraph, Preformatted, SimpleDocTemplate, Spacer, Table, TableStyle,
)

OUT = sys.argv[1]
F = "/usr/share/fonts/truetype/dejavu/"
pdfmetrics.registerFont(TTFont("Sans", F + "DejaVuSans.ttf"))
pdfmetrics.registerFont(TTFont("Sans-Bold", F + "DejaVuSans-Bold.ttf"))
pdfmetrics.registerFont(TTFont("Sans-Oblique", F + "DejaVuSans-Oblique.ttf"))
pdfmetrics.registerFont(TTFont("Mono", F + "DejaVuSansMono.ttf"))
pdfmetrics.registerFont(TTFont("Mono-Bold", F + "DejaVuSansMono-Bold.ttf"))
from reportlab.pdfbase.pdfmetrics import registerFontFamily  # noqa: E402
registerFontFamily("Sans", normal="Sans", bold="Sans-Bold", italic="Sans-Oblique", boldItalic="Sans-Bold")
registerFontFamily("Mono", normal="Mono", bold="Mono-Bold", italic="Mono", boldItalic="Mono-Bold")

INK = colors.HexColor("#1d2330")
MUTED = colors.HexColor("#5b6475")
ACCENT = colors.HexColor("#1f5fae")
RULE = colors.HexColor("#d7dce4")
CODE_BG = colors.HexColor("#f3f5f8")
WARN_BG = colors.HexColor("#fff6e5")
WARN_BORDER = colors.HexColor("#e0a33a")

S = {
    "title": ParagraphStyle("title", fontName="Sans-Bold", fontSize=22, leading=27, textColor=INK, spaceAfter=4),
    "subtitle": ParagraphStyle("subtitle", fontName="Sans", fontSize=11, leading=15, textColor=MUTED, spaceAfter=14),
    "h1": ParagraphStyle("h1", fontName="Sans-Bold", fontSize=15, leading=19, textColor=ACCENT,
                         spaceBefore=14, spaceAfter=6),
    "h2": ParagraphStyle("h2", fontName="Sans-Bold", fontSize=11.5, leading=15, textColor=INK,
                         spaceBefore=9, spaceAfter=4),
    "body": ParagraphStyle("body", fontName="Sans", fontSize=9.6, leading=13.6, textColor=INK, spaceAfter=5,
                           alignment=TA_LEFT),
    "bullet": ParagraphStyle("bullet", fontName="Sans", fontSize=9.6, leading=13.6, textColor=INK,
                             leftIndent=13, bulletIndent=3, spaceAfter=2.5),
    "cell": ParagraphStyle("cell", fontName="Sans", fontSize=8.8, leading=11.6, textColor=INK),
    "cellh": ParagraphStyle("cellh", fontName="Sans-Bold", fontSize=8.8, leading=11.6, textColor=INK),
    "code": ParagraphStyle("code", fontName="Mono", fontSize=8.4, leading=11.2, textColor=INK),
    "note": ParagraphStyle("note", fontName="Sans", fontSize=9.2, leading=13, textColor=INK),
}
W = A4[0] - 36 * mm  # ширина текста


S["lead"] = ParagraphStyle("lead", parent=S["body"])


def p(text, style="body"):
    # абзац-вводная («…:») не отрывается от следующего блока
    if style == "body" and text.rstrip().endswith(":"):
        style = "lead"
    return Paragraph(text, S[style])


def c(text):
    """Инлайн-код."""
    return f'<font name="Mono" size="8.8" color="#0b3d7a">{escape(text)}</font>'


def bullets(items):
    return [Paragraph(t, S["bullet"], bulletText="•") for t in items]


def code(text):
    t = Table([[Preformatted(text.strip("\n"), S["code"])]], colWidths=[W])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), CODE_BG),
        ("BOX", (0, 0), (-1, -1), 0.5, RULE),
        ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return KeepTogether([t, Spacer(1, 6)])


def note(text, bg=WARN_BG, border=WARN_BORDER):
    t = Table([[Paragraph(text, S["note"])]], colWidths=[W])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), bg),
        ("LINEBEFORE", (0, 0), (0, -1), 3, border),
        ("LEFTPADDING", (0, 0), (-1, -1), 9), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return KeepTogether([t, Spacer(1, 7)])


def table(header, rows, widths):
    data = [[Paragraph(h, S["cellh"]) for h in header]] + [[Paragraph(x, S["cell"]) for x in r] for r in rows]
    t = Table(data, colWidths=[W * w for w in widths], repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), CODE_BG),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, MUTED),
        ("LINEBELOW", (0, 1), (-1, -1), 0.4, RULE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    if t.wrap(W, A4[1])[1] > 70 * mm:  # длинная таблица делится между страницами (шапка повторяется)
        return [t, Spacer(1, 8)]
    return KeepTogether([t, Spacer(1, 8)])


def on_page(canvas, doc):
    canvas.saveState()
    canvas.setFont("Sans", 7.8)
    canvas.setFillColor(MUTED)
    canvas.drawString(18 * mm, 11 * mm, "Бот опционов Bybit — инструкция")
    canvas.drawRightString(A4[0] - 18 * mm, 11 * mm, f"стр. {doc.page}")
    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(0.5)
    canvas.line(18 * mm, 14 * mm, A4[0] - 18 * mm, 14 * mm)
    canvas.restoreState()


story = []
def A(x):
    if isinstance(x, list):
        story.extend(x)
        return
    if isinstance(x, Paragraph) and x.style.name in ("h1", "h2"):
        # заголовок не остаётся внизу страницы: если места мало — с новой страницы
        story.append(CondPageBreak(60 * mm if x.style.name == "h1" else 45 * mm))
    if isinstance(x, KeepTogether) and story and isinstance(story[-1], Paragraph) and story[-1].style.name == "lead":
        # вводная «…:» и стоящий перед ней заголовок переносятся вместе со своим блоком кода
        head = [story.pop()]
        if story and isinstance(story[-1], Paragraph) and story[-1].style.name in ("h1", "h2"):
            head.insert(0, story.pop())
        x = KeepTogether(head + [x])
    story.append(x)


# ---------- титул ----------
A(p("Бот опционов Bybit", "title"))
A(p("Инструкция пользователя · @chain_pilot_bot · версия от 15 ноября 2026", "subtitle"))
A(note(
    "<b>Сейчас бот работает в режиме demo</b> (" + c("BYBIT_ENV=demo") + "): ордера уходят на демо-счёт Bybit, "
    "реальные деньги не задействованы. Переход на mainnet — отдельный осознанный шаг (раздел 9)."
))

A(p("Содержание", "h2"))
toc = [
    "1. Быстрый старт", "2. Где работает бот и кто что может", "3. Команды просмотра",
    "4. Команды по счёту", "5. Торговля", "6. Подтверждение ордера и уведомления",
    "7. Как добавить человека или группу", "8. Запуск и остановка бота", "9. Настройки (.env)",
    "10. Командная строка (CLI)", "11. Если что-то не работает",
]
toc += [""] * (len(toc) % 2)
half = len(toc) // 2
A(Table([[p(toc[k]), p(toc[k + half])] for k in range(half)], colWidths=[W / 2, W / 2],
        style=[("LEFTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0),
               ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))

# ---------- 1 ----------
A(p("1. Быстрый старт", "h1"))
A(p("Откройте в Telegram " + c("@chain_pilot_bot") + " (ссылка t.me/chain_pilot_bot), нажмите «Start» "
    "и попробуйте по порядку:"))
A(code("""
/id                         ваш user id, chat id и роль
/chain BTC                  выбрать экспирацию кнопками -> таблица опционов
/t BTC-27NOV26-60000-P      котировка и греки одного опциона
/bal                        баланс (только в личке)
/help                       список команд
"""))
A(p("Бот понимает только команды (начинаются с «/»). Кнопка меню «/» слева от поля ввода показывает "
    "список команд. Обычный текст бот игнорирует."))

# ---------- 2 ----------
A(p("2. Где работает бот и кто что может", "h1"))
A(p("Чаты", "h2"))
A(bullets([
    "<b>Личка</b> — для пользователей с правами (см. роли ниже). Незнакомцу бот отвечает только на "
    + c("/id") + " и " + c("/start") + " (сообщает его id).",
    "<b>Группы</b> — только из белого списка " + c("TELEGRAM_ALLOWED_CHAT_IDS") + ". Из любой другой "
    "группы бот молча выходит и записывает её id в лог.",
    "В группе бот видит только команды (режим приватности Telegram включён — так и задумано).",
]))
A(p("Роли", "h2"))
A(p("Права проверяются по <b>пользователю</b>, а не по чату: трейдер может торговать и в личке, "
    "и в разрешённой группе; зритель нигде не торгует."))
A(table(
    ["Роль", "Кто", "Может"],
    [
        ["Трейдер", c("TELEGRAM_TRADER_IDS"), "всё: просмотр, счёт, ордера, отмены, закрытие позиций"],
        ["Зритель", c("TELEGRAM_VIEWER_IDS"), "просмотр рынка и счёта; торговать не может"],
        ["Участник группы", "любой в разрешённой группе, если " + c("ALLOW_ALL_GROUP_MEMBERS_VIEW=true"),
         "только просмотр рынка в этой группе"],
        ["Все остальные", "—", "только " + c("/id") + "; на прочее бот молчит"],
    ],
    [0.17, 0.38, 0.45],
))
A(note(
    "<b>Анонимные администраторы</b> группы (пишут «от имени группы») и посты каналов к торговле "
    "не допускаются: Telegram не сообщает, кто это на самом деле. Отключите анонимность в правах "
    "администратора и повторите команду от своего имени."
))
A(p("Счёт только в личке", "h2"))
A(p("При " + c("SENSITIVE_COMMANDS_DM_ONLY=true") + " (по умолчанию) команды " + c("/bal") + ", " + c("/pos")
    + ", " + c("/orders") + " и " + c("/greeks") + " в группах отвечают «Эта команда доступна только в "
    "личке с ботом». Так баланс и позиции не видны всей группе."))

# ---------- 3 ----------
A(p("3. Команды просмотра", "h1"))
A(table(
    ["Команда", "Что делает", "Пример"],
    [
        [c("/exp [BASE]"), "список экспираций с датой и днями до неё; без аргумента — выбор актива кнопками",
         c("/exp ETH")],
        [c("/chain BASE [EXP] [N]"), "цепочка опционов вокруг ATM: N страйков вверх и вниз (по умолчанию 6, "
         "максимум 20). Без EXP — выбор экспирации кнопками", c("/chain BTC 27NOV26 4")],
        [c("/t SYMBOL"), "котировка, IV, греки, OI и объём одного опциона", c("/t SOL-27NOV26-72-C")],
        [c("/id"), "ваш user id, chat id и роль", c("/id")],
        [c("/help"), "краткая справка", c("/help")],
    ],
    [0.27, 0.48, 0.25],
))
A(p("Как читать цепочку", "h2"))
A(code("""
BTC 27NOV26  F=65558  ATM=66000
C.bid C.ask Strike P.bid P.ask   IV
 2375  2420  65000  1855  1865 45.4
 1815  1840 *66000  2280  2295 43.2
 1340  1350  67000  2795  2820 41.6
"""))
A(bullets([
    "<b>F</b> — цена базового актива для этой экспирации; <b>ATM</b> и звёздочка — ближайший к ней страйк.",
    "Слева колл (C), справа пут (P): лучшие bid/ask в USDT за 1 единицу базового актива.",
    "<b>IV</b> — mark implied volatility, %. «-» — нет котировки.",
    "Под таблицей — шаблон символа и кнопки «Обновить» и «Экспирации». Они меняют это же сообщение, "
    "а не присылают новое (так бот не упирается в лимиты Telegram в группах).",
]))
A(p("Символ опциона", "h2"))
A(p("Формат: " + c("BASE-ДДМММГГ-СТРАЙК-C|P") + ", например " + c("BTC-27NOV26-60000-P")
    + ". Суффикс " + c("-USDT") + " можно не писать, регистр не важен. Активы: BTC, ETH, SOL "
    "(список задаётся в " + c("BASE_COINS") + ")."))

# ---------- 4 ----------
A(p("4. Команды по счёту", "h1"))
A(table(
    ["Команда", "Что показывает"],
    [
        [c("/bal"), "equity, доступный баланс, начальная и поддерживающая маржа, монеты на счёте"],
        [c("/pos [BASE]"), "открытые опционные позиции: размер со знаком, средняя цена, mark, uPnL, греки; "
         "трейдеру — кнопки «Закрыть · рынок» и «· mid» под каждой позицией"],
        [c("/orders [BASE]"), "открытые ордера с id; трейдеру — кнопки «Изменить» (значок карандаша) и «✖ Отменить» под каждым ордером"],
        [c("/greeks [BASE]"), "суммарные греки портфеля по активам (Δ, Γ, Vega, Θ)"],
    ],
    [0.25, 0.75],
))

# ---------- 5 ----------
A(p("5. Торговля", "h1"))
A(p("Только для трейдеров. Каждое действие сначала показывает превью и ждёт подтверждения кнопкой (раздел 6)."))
A(table(
    ["Команда", "Описание"],
    [
        [c("/buy SYMBOL QTY ЦЕНА"), "лимитная покупка по цене в USDT"],
        [c("/sell SYMBOL QTY ivNN"), "лимитная продажа по implied volatility: " + c("iv55") + " = 55%"],
        [c("/buy SYMBOL QTY mkt"), "рыночный ордер (" + c("mkt") + ", " + c("market") + " или " + c("m") + ")"],
        [c("/close SYMBOL [ЦЕНА]"), "закрыть позицию целиком reduce-only ордером; без цены — по рынку"],
        [c("/amend SYMBOL ORDER_ID ЦЕНА|ivNN [QTY]"), "изменить открытый лимитный ордер: новая цена или IV, "
         "при желании — количество. Лимиты проверяются заново"],
        [c("/cancel SYMBOL ORDER_ID"), "отменить ордер (id — из " + c("/orders") + "); проще — кнопкой «✖» в "
         + c("/orders")],
        [c("/cancelall [BASE]"), "отменить все опционные ордера (по активу или все) — с подтверждением"],
    ],
    [0.33, 0.67],
))
A(p("Примеры", "h2"))
A(code("""
/buy  BTC-27NOV26-60000-P 0.01 150     купить 0.01 BTC-пута по 150 USDT
/sell ETH-27NOV26-1900-C  0.1  iv55    продать 0.1 ETH-колла по IV 55%
/buy  SOL-27NOV26-72-C    1    mkt     купить 1 SOL-колл по рынку
/close BTC-27NOV26-60000-P 120         закрыть позицию лимиткой по 120
/amend BTC-27NOV26-60000-P <id> 160    изменить цену ордера на 160
/amend BTC-27NOV26-60000-P <id> iv50 0.02   по IV 50% и на 0.02
"""))
A(p("Изменить ордер или закрыть позицию кнопками", "h2"))
A(bullets([
    "<b>/orders → «Изменить»</b>: бот показывает текущий ордер и рынок и предлагает новую цену кнопками "
    "<b>bid / mid / ask / mark</b>. Количество при этом не меняется.",
    "<b>«Своя цена / кол-во…»</b>: бот задаёт вопрос — ответьте на него сообщением " + c("160") + ", "
    + c("iv50") + " или " + c("160 0.02") + " (цена и новое количество).",
    "Меню изменения может нажимать только тот, кто его открыл; кнопки действуют 30 минут.",
    "<b>/pos → «Закрыть · рынок»</b> — рыночный reduce-only ордер на всю позицию; <b>«· mid»</b> — лимитный "
    "по середине между bid и ask.",
    "Во всех случаях дальше то же превью и подтверждение, что и для команд (раздел 6).",
]))
A(p("Что бот проверяет до отправки", "h2"))
A(bullets([
    "Количество округляется вниз до шага инструмента (BTC — 0.01, ETH — 0.1, SOL — 1). Цена — до шага цены "
    "(у BTC — 5 USDT) в «безопасную» сторону: покупка вниз, продажа вверх.",
    "Минимальный и максимальный размер ордера по правилам Bybit.",
    "Лимит количества на ордер: " + c("MAX_QTY_BTC=0.1") + ", " + c("MAX_QTY_ETH=1") + ", "
    + c("MAX_QTY_SOL=10") + ".",
    "Лимит премии на ордер: " + c("MAX_ORDER_PREMIUM_USDT=500") + " (количество × цена; для рыночных и IV-ордеров "
    "— оценка по текущему ask/bid).",
    "<b>Защита от опечаток.</b> Если ордер исполнится сразу (рыночный или лимит через рынок), бот по стакану "
    "считает худшую цену исполнения; если она хуже mark больше чем на " + c("MAX_SLIPPAGE_PCT") + " (15%) — отказ. "
    "Лимитный ордер, который просто встаёт в стакан, не ограничивается.",
    "<b>Рыночные ордера</b> запрещены при спреде больше " + c("MAX_SPREAD_PCT") + " (20%) и если в стакане "
    "не хватает объёма — используйте лимит.",
    "<b>Лимиты портфеля</b>: позиция по символу " + c("MAX_POSITION_<COIN>") + " (с учётом открытых ордеров в ту же "
    "сторону), суммарная дельта опционов по активу " + c("MAX_NET_DELTA_<COIN>") + ", премия за сутки UTC "
    + c("MAX_DAILY_PREMIUM_USDT") + " (по всем опционным ордерам счёта, включая сделанные не через бота). "
    "Уменьшать позицию и дельту можно всегда.",
    "Превью показывает, как ордер исполнится (сразу или встанет в стакан) и сколько останется до лимитов.",
    "Для закрытия позиции (reduce-only) лимиты количества, премии и портфеля не применяются — такой ордер "
    "не может увеличить позицию. Проверка по стакану действует.",
    "Для ордеров по IV цену считает биржа, поэтому проверка по стакану не выполняется.",
]))

# ---------- 6 ----------
A(p("6. Подтверждение ордера и уведомления", "h1"))
A(p("После команды бот присылает превью:"))
A(code("""
[demo]
BUY 0.01 BTC-27NOV26-60000-P-USDT @ 150
рынок: bid 685 / ask 690  mark 688  IV 56.9%
премия: 1.50
не пересекает рынок — встанет в стакан
позиция после: +0.01 (лимит 0.2)
Δ BTC после: -0.0018 (лимит 0.5)
премия за сутки: 1.50 из 2000
Инициатор: Павел Алексеев

Подтвердить может только инициатор, 60 с
     [ Подтвердить ]   [ Отмена ]
"""))
A(bullets([
    "Нажать «Подтвердить» или «Отмена» может <b>только тот, кто отправил команду</b>; остальным Telegram "
    "покажет «Подтвердить может только инициатор».",
    "Подтверждение действует <b>60 секунд</b>, потом сообщение помечается «Время подтверждения истекло» "
    "и ордер не отправляется — повторите команду.",
    "Повторное нажатие ничего не отправит второй раз.",
    "Лимиты проверяются ещё раз в момент отправки.",
    "Результат («Ордер принят», orderId или ошибка Bybit) появляется в том же сообщении.",
]))
A(p("Уведомления в группы", "h2"))
A(p("О принятом и изменённом ордере, отмене и cancel-all бот пишет во все разрешённые группы, кроме той, где "
    "действие сделано: кто, где и что."))
A(p("Уведомления об исполнениях", "h2"))
A(p("Бот слушает поток сделок Bybit (WebSocket) и сообщает о каждом исполнении — во все разрешённые группы "
    "и трейдерам в личку (если трейдер хоть раз писал боту):"))
A(code("""
[demo]
[!] Исполнение
BUY 0.01 BTC-17NOV26-60000-P @ 155
комиссия 0.093 · ордер исполнен полностью
ордер 4af37d48 · через бота
"""))
A(p("Видны и сделки, сделанные не через бота (на сайте или в приложении Bybit) — с пометкой «вне бота», "
    "а также поставка при экспирации."))
A(p("Журнал", "h2"))
A(p("Все торговые действия пишутся в лог (" + c("optbot.trade") + ") с user id и chat id: запрос, "
    "подтверждение, результат, ошибка, истечение, отказ в правах."))

# ---------- 7 ----------
A(p("7. Как добавить человека или группу", "h1"))
A(p("Добавить человека", "h2"))
A(bullets([
    "Человек пишет боту " + c("/id") + " в личке (или в разрешённой группе) и сообщает вам свой user id. "
    "Другой способ — бот " + c("@userinfobot") + ".",
    "Впишите id в " + c("TELEGRAM_TRADER_IDS") + " (торговля) или " + c("TELEGRAM_VIEWER_IDS")
    + " (просмотр) в файле " + c(".env") + "; несколько id — через запятую без пробелов.",
    "Перезапустите бота (раздел 8).",
]))
A(note("Трейдер может выставлять ордера с вашего счёта Bybit. Давайте эту роль только тем, кому доверяете "
       "счёт; при переходе на mainnet списки сохраняются."))
A(p("Добавить группу", "h2"))
A(bullets([
    "Добавьте " + c("@chain_pilot_bot") + " в группу. Бот выйдет и запишет в лог строку вида "
    + c("Чат -5062832909 (group, 'Название') не в TELEGRAM_ALLOWED_CHAT_IDS — выхожу") + ".",
    "Впишите этот id (он отрицательный) в " + c("TELEGRAM_ALLOWED_CHAT_IDS") + " и перезапустите бота.",
    "Добавьте бота в группу ещё раз и проверьте командой " + c("/id") + ".",
    "Если группа станет супергруппой (Telegram делает это сам, например при включении истории для "
    "новых участников), её id изменится на " + c("-100…") + " — повторите шаги.",
]))
A(p("Узнать участников группы бот не может: Telegram не отдаёт ботам список участников — только "
    "администраторов. Поэтому id собираются через " + c("/id") + "."))

# ---------- 8 ----------
A(p("8. Запуск и остановка бота", "h1"))
A(p("Из папки проекта " + c("~/Desktop/work/trader") + ":"))
A(code("""
.venv/bin/python -m optbot.bot          запустить (остановить — Ctrl+C)
"""))
A(bullets([
    "При старте бот пишет в лог режим и число трейдеров, зрителей и групп — сверьте с ожиданием.",
    "Команды, отправленные боту, пока он был выключен, после запуска <b>не выполняются</b> — "
    "отправьте их заново.",
    "Бот не запустится, если: нет токена; никому не выдан доступ; id группы положительный; "
    "в списках оказался служебный id Telegram. Причина пишется в консоль.",
    "Одновременно должен работать только один экземпляр бота с этим токеном.",
    "Для постоянной работы на VPS будет сервис systemd (в планах).",
]))

# ---------- 9 ----------
A(p("9. Настройки (.env)", "h1"))
A(p("Файл " + c(".env") + " в папке проекта. После изменения — перезапуск бота. Файл содержит ключи "
    "API и токен: никому не пересылайте его."))
A(table(
    ["Параметр", "Значение"],
    [
        [c("BYBIT_ENV"), c("demo") + " (сейчас) · " + c("testnet") + " · " + c("mainnet")
         + " — реальные деньги"],
        [c("BYBIT_API_KEY / SECRET"), "ключи Bybit: права Options Trade + Read, без вывода средств, с привязкой к IP"],
        [c("BASE_COINS"), "активы: " + c("BTC,ETH,SOL")],
        [c("MAX_QTY_<COIN>"), "максимальное количество в одном ордере (0 — без лимита)"],
        [c("MAX_ORDER_PREMIUM_USDT"), "максимальная премия одного ордера, USDT"],
        [c("MAX_SLIPPAGE_PCT"), "допустимое отклонение худшей цены исполнения от mark, % (15)"],
        [c("MAX_SPREAD_PCT"), "максимальный спред для рыночного ордера, % (20)"],
        [c("MAX_POSITION_<COIN>"), "максимальная |позиция| по одному символу (0 — без лимита)"],
        [c("MAX_NET_DELTA_<COIN>"), "максимальная |дельта опционов| по активу, в единицах актива (0 — без лимита)"],
        [c("MAX_DAILY_PREMIUM_USDT"), "премия открывающих ордеров за сутки UTC, USDT (0 — без лимита)"],
        [c("TELEGRAM_BOT_TOKEN"), "токен бота от @BotFather"],
        [c("TELEGRAM_ALLOWED_CHAT_IDS"), "разрешённые группы (отрицательные id через запятую)"],
        [c("TELEGRAM_TRADER_IDS"), "user id трейдеров"],
        [c("TELEGRAM_VIEWER_IDS"), "user id зрителей"],
        [c("ALLOW_ALL_GROUP_MEMBERS_VIEW"), c("false") + " · " + c("true")
         + " — просмотр рынка всем в разрешённых группах"],
        [c("SENSITIVE_COMMANDS_DM_ONLY"), c("true") + " · " + c("false") + " — счёт и позиции видны и в группах"],
    ],
    [0.38, 0.62],
))
A(note(
    "<b>Переход на mainnet.</b> Создайте отдельные ключи mainnet (без права вывода, с привязкой к IP), "
    "поставьте " + c("BYBIT_ENV=mainnet") + ", проверьте лимиты " + c("MAX_*") + " и списки трейдеров, "
    "перезапустите бота. В превью ордера вместо " + c("[demo]") + " появится «MAINNET — РЕАЛЬНЫЕ ДЕНЬГИ»."
))

# ---------- 10 ----------
A(p("10. Командная строка (CLI)", "h1"))
A(p("То же самое без Telegram — из папки проекта. Торговые команды спрашивают подтверждение y/N:"))
A(code("""
.venv/bin/python -m optbot.cli expiries BTC
.venv/bin/python -m optbot.cli chain BTC 27NOV26 -n 6 [--wide]
.venv/bin/python -m optbot.cli ticker BTC-27NOV26-60000-P
.venv/bin/python -m optbot.cli balance | positions | orders | greeks
.venv/bin/python -m optbot.cli buy  BTC-27NOV26-60000-P 0.01 --price 150
.venv/bin/python -m optbot.cli sell BTC-27NOV26-60000-P 0.01 --iv 55
.venv/bin/python -m optbot.cli buy  BTC-27NOV26-60000-P 0.01 --market
.venv/bin/python -m optbot.cli amend  BTC-27NOV26-60000-P <orderId> --price 160 [--qty 0.02]
.venv/bin/python -m optbot.cli cancel BTC-27NOV26-60000-P <orderId>
.venv/bin/python -m optbot.cli cancel-all [BTC]
.venv/bin/python -m optbot.cli close BTC-27NOV26-60000-P [--price 120]
"""))
A(p(c("--wide") + " добавляет в цепочку дельты, IV обеих сторон и открытый интерес. "
    + c("--tif IOC|FOK|PostOnly") + " и " + c("--reduce-only") + " — дополнительные параметры ордера."))

# ---------- 11 ----------
A(p("11. Если что-то не работает", "h1"))
A(table(
    ["Симптом", "Причина и что делать"],
    [
        ["Бот не отвечает совсем", "бот не запущен, или вас нет в списках (" + c("/id") + " покажет "
         "«нет доступа»), или вы написали текст без «/»"],
        ["Бот вышел из группы", "группы нет в " + c("TELEGRAM_ALLOWED_CHAT_IDS") + " — раздел 7"],
        ["«Эта команда доступна только в личке»", "включён " + c("SENSITIVE_COMMANDS_DM_ONLY") + "; напишите боту лично"],
        ["«Нет прав на торговлю»", "вы зритель; нужна роль трейдера"],
        ["«Торговля от имени группы… запрещена»", "вы анонимный администратор — отключите анонимность"],
        ["«Подтвердить может только инициатор»", "кнопку жмёт не тот, кто отправил команду"],
        ["«Время подтверждения истекло»", "прошло больше 60 с — повторите команду"],
        ["«Превышен лимит MAX_QTY_…» / «Премия … > лимита»", "уменьшите количество или цену, либо измените лимит в "
         + c(".env")],
        ["«Инструмент … не найден»", "проверьте символ: экспирация есть в " + c("/exp") + ", страйк — в "
         + c("/chain") + "; истёкшие опционы не торгуются"],
        ["«Цена исполнения … хуже mark …»", "ордер исполнился бы сразу по плохой цене — проверьте цену/количество "
         "или поставьте лимит ближе к mark"],
        ["«Спред … > MAX_SPREAD_PCT» / «В стакане можно исполнить только …»", "рынок тонкий — используйте лимитный ордер "
         "или меньшее количество"],
        ["«Позиция … станет …» / «Дельта … станет …» / «Премия за сутки …»", "сработал лимит портфеля — уменьшите "
         "ордер, закройте часть позиции или измените лимит в " + c(".env")],
        ["«Кнопка устарела»", "кнопкам в /orders и /pos больше 30 минут или бот перезапускался — запросите список заново"],
        ["Нет сообщений об исполнениях", "в логе при старте должно быть «WebSocket исполнений подключён»; в личку "
         "уведомления приходят, только если вы писали боту"],
        ["«Ошибка: 10001…» и др. коды Bybit", "ответ биржи: недостаточно маржи, неверная цена и т.п. — текст ошибки "
         "в сообщении"],
    ],
    [0.36, 0.64],
))

doc = SimpleDocTemplate(OUT, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                        topMargin=16 * mm, bottomMargin=20 * mm,
                        title="Бот опционов Bybit — инструкция", author="optbot")
doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
print("ok", OUT)
