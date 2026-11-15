# Bybit Options Bot — план и контекст

Управление торговлей опционами Bybit (API V5, `category=option`) через скрипт и Telegram-бот.
Запуск — на VPS пользователя. Язык общения с пользователем — русский.

## Требования
- Базовые активы: BTC, ETH, SOL (USDT-settled опционы, символы вида `BTC-27MAR26-70000-P-USDT`).
  Список задаётся в `.env` (`BASE_COINS`), расширяемый.
- Аккаунт: UTA. Для бота — отдельные новые API-ключи (права: Contract/Options Trade + Read,
  БЕЗ вывода средств, привязка к IP VPS).
- Разработка и проверки — сначала на testnet (`BYBIT_ENV=testnet`), mainnet — только после явного согласия пользователя.
- Безопасность: доступ по ролям (см. «Telegram: доступ»); каждый ордер подтверждается кнопкой;
  локальные лимиты `MAX_QTY_<COIN>` и `MAX_ORDER_PREMIUM_USDT` проверяются до отправки.

## Готово
- `optbot/config.py` — настройки из `.env`.
- `optbot/bybit_client.py` — `OptionsClient`:
  инструменты (кеш 5 мин), `expiries`, `resolve_symbol` (принимает короткие формы),
  `chain` (страйки вокруг ATM с котировками и греками), `ticker`,
  `balance`, `positions`, `open_orders`, `greeks`,
  `prepare_order` (округление по шагам + лимиты, без отправки), `place_order` (по цене / по IV / market,
  `orderLinkId` генерируется — для опционов обязателен), `cancel_order`, `cancel_all`,
  `close_position_request` (reduce-only на весь размер).
- Smoke-тесты на `BYBIT_ENV=demo` пройдены (2026-11-15): справочник, экспирации, цепочка, баланс,
  лимитный ордер далеко от рынка → отмена (`smoke.py`, `smoke_order.py`).
  Сверены реальные поля: tickers — `bid1Price/ask1Price/bid1Iv/ask1Iv/markPrice/markIv/delta/gamma/vega/theta/
  underlyingPrice/openInterest`; open orders возвращают `orderIv` и для ценовых ордеров.
  Исправлено: `prepare_order` брал `askPrice/bidPrice` (таких полей нет) → оценка премии market/IV-ордеров
  молча шла по markPrice; теперь `ask1Price/bid1Price`. Кеш инструментов защищён `RLock` (бот зовёт из потоков).
  Исполняемые ордера на demo (2026-11-15): покупка 0.01 BTC-пута и продажа 1 SOL-колла (исполнены по лучшей
  цене стакана), закрытие через `close` — BTC лимиткой, SOL по рынку; позиции обнулились.
  Сверены поля `positions`: `side` Buy/Sell, `size` без знака, `avgPrice`, `markPrice`, `unrealisedPnl`,
  греки уже на всю позицию (со знаком); `get_coin_greeks`: `totalDelta/Gamma/Vega/Theta`.
- Изменение ордера: `amend_request` (берёт недостающее из открытого ордера, проверяет лимиты) + `amend_order`;
  CLI `amend SYMBOL ID [--price|--iv] [--qty]`, бот `/amend SYMBOL ID PRICE|ivNN [QTY]` с подтверждением.
  Проверено на demo.
- `optbot/fmt.py` — общее форматирование (CLI и бот): цепочка компактная (телефон) и `--wide`,
  тикер, баланс, позиции, ордера, греки, превью ордера с рынком и премией; `parse_iv`, `to_dec`.
- `optbot/cli.py` — `python -m optbot.cli …`: `expiries`, `chain BASE EXP [-n N] [--wide]`, `ticker`, `balance`,
  `positions`, `orders`, `greeks`, `buy/sell SYMBOL QTY (--price|--iv|--market) [--tif] [--reduce-only]`,
  `cancel SYMBOL ID`, `cancel-all [BASE]`, `close SYMBOL [--price]`. Все торговые действия — с превью и y/N.
  Проверено на demo: чтение, лимиты, ордер по цене и по IV далеко от рынка → cancel / cancel-all.
- `optbot/bot.py` — `python -m optbot.bot` (PTB v22 + `[rate-limiter]`, pybit через `asyncio.to_thread`):
  /id, /exp, /chain (актив и экспирация кнопками, «Обновить» правит то же сообщение), /t, /bal, /pos,
  /orders (кнопки отмены — только трейдерам), /greeks, /buy и /sell (`PRICE | ivNN | mkt`), /close,
  /cancel, /cancelall. Проверено обработчиками с подставным Telegram против demo, включая группы
  (ордер далеко от рынка → подтверждение инициатором → отмена кнопкой, уведомления в группы).
  В живом Telegram ещё не запускался — в `.env` пусты токен и списки id.

## Telegram: доступ (реализовано)
- Работает в личке и в группах из `TELEGRAM_ALLOWED_CHAT_IDS` (id отрицательные). В любом другом чате
  бот не отвечает и выходит (`TypeHandler` в group=-1 до всех обработчиков); id чата пишется в лог WARNING —
  так узнаётся id новой группы.
- Права по `from_user.id`: `TELEGRAM_TRADER_IDS` — торговля+просмотр, `TELEGRAM_VIEWER_IDS` — просмотр,
  `ALLOW_ALL_GROUP_MEMBERS_VIEW` (false) — просмотр всем в разрешённых группах (в личке не действует).
  Неразрешённым — молчание; зрителю на торговую команду — «Нет прав».
- Сообщения с `sender_chat` (анонимные админы, каналы) и служебные id Telegram к торговле не допускаются.
- `/id` — всем (в личке и разрешённых группах): user id, chat id, роль. `/start` незнакомцу в личке — его id.
- `SENSITIVE_COMMANDS_DM_ONLY` (по умолчанию true): /bal, /pos, /orders, /greeks в группах отвечают
  «только в личке».
- Подтверждение ордера/close/cancel-all: нажать может только инициатор, TTL 60 с, повтор нажатия не шлёт дважды.
- Аудит: логгер `optbot.trade` — `<event>.request/confirm/done/error/expired/cancelled_by_user`, `denied`,
  `rejected`, всё с user id и chat id.
- Уведомления о выполненных действиях (ордер принят, отмена, cancel-all) — во все разрешённые группы,
  кроме исходной. Исполнения (fills) пока не отслеживаются — нужен WebSocket (п. «Потом»).
- Лимиты Telegram: `AIORateLimiter(max_retries=3)` (30/с всего, 20/мин на группу, ожидание RetryAfter),
  навигация кнопками правит сообщения, уведомления шлются фоновой задачей.
- Старт запрещён, если нет токена, никому не выдан доступ, в `TELEGRAM_ALLOWED_CHAT_IDS` положительный id
  или в списках пользователей служебные id. Старая переменная `TELEGRAM_ALLOWED_IDS` больше не используется.

## Защита, кнопки, исполнения (реализовано 2026-11-15)
- `prepare_order` (клиент), всё проверяется и при превью, и повторно при отправке:
  - стакан: для market — есть встречные заявки, спред ≤ `MAX_SPREAD_PCT` (20), хватает глубины (25 уровней);
    для пересекающих рынок лимиток и market — худшая цена исполнения хуже mark не более `MAX_SLIPPAGE_PCT` (15).
    Пассивные лимитки (не пересекают рынок) не ограничиваются. Ордера по IV — без проверки стакана.
  - портфель (не для reduce-only; уменьшать можно всегда): `MAX_POSITION_<COIN>` — |позиция| по символу
    с учётом открытых ордеров в ту же сторону; `MAX_NET_DELTA_<COIN>` — |дельта опционов| по активу (get_coin_greeks
    + qty×delta тикера); `MAX_DAILY_PREMIUM_USDT` — с начала суток UTC: cumExecValue + остаток активных ордеров,
    все опционные не-reduce-only ордера счёта (не только бота). При amend изменяемый ордер исключается.
  - цена округляется к шагу в пассивную сторону (покупка вниз, продажа вверх).
  - превью показывает оценку исполнения и состояние лимитов (`OrderRequest.info`).
- Бот: `/orders` — кнопки «✏ Изменить» (меню bid/mid/ask/mark или «Своя цена / кол-во…» ответом на
  ForceReply) и «✖ Отменить»; `/pos` — «Закрыть · рынок» / «· mid». Данные кнопок — короткие токены
  в памяти (30 мин), меню изменения нажимает только тот, кто его открыл.
- `optbot/ws.py` — приватный WebSocket (pybit, demo/testnet/mainnet), поток execution по опционам →
  «💥 Исполнение» во все разрешённые группы и трейдерам в личку (если начинали диалог с ботом);
  сделки вне бота тоже видны; аудит `exec` в `optbot.trade`.
- Проверено на demo: отказы по лимитам, проскальзыванию, спреду; глубина — на подставном стакане;
  кнопки и меню — подставным Telegram; WS — живыми сделками 0.01 BTC-пута.
- `.env` — права 600; git-репозиторий создан (без коммитов). `SENSITIVE_COMMANDS_DM_ONLY=false` на время тестов.

## Дальше
1. Живой Telegram на demo — в работе (токен, трейдер, группа «Pavel A» настроены); проверить:
   таблица цепочки на телефоне, кнопки, работа в группе (privacy mode у бота можно оставить включённым —
   команды он получает), выход из чужой группы.
2. testnet: пока не проверялся (всё проверено на demo).
3. Деплой: `deploy/options-bot.service` (systemd, venv, `Restart=always`), README по установке на VPS.
4. Потом: автотесты (pytest, подставной Bybit), алерты по грекам/PnL,
   мультиноговые ордера (спреды) через batch, дельта-хедж перпетуалами.
