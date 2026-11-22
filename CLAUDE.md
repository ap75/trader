# Bybit options bot

Управление опционами Bybit (V5, BTC/ETH/SOL, USDT) через CLI и Telegram-бот. План и статус — `PLAN.md`.

## Правила работы

После **каждого** изменения кода, даже небольшого, до отчёта и коммита:

1. **Тесты на изменение** — новые или дополненные в `tests/` (подставной Bybit `tests/fakes.py`,
   подставной Telegram в `tests/test_bot.py`). Изменение без тестов не считается готовым.
2. **Код-ревью** диффа; найденное исправить или показать пользователю.
3. **Полный прогон** `.venv/bin/pytest` — все тесты зелёные, в том числе после исправлений по ревью.

4. **Перед коммитом — актуальность документации**: `docs/guides.py` (PDF ru/uk: admin, trader, viewer — пересобрать),
   `PLAN.md`, `CLAUDE.md`, `.env.example`, `--help`/справка бота. Новые команды, настройки, файлы данных и шаги
   развёртывания должны быть описаны.

Коммиты: на английском, 1–3 строки, без `Co-Authored-By`. Коммитить по просьбе пользователя.

## Безопасность

- `BYBIT_ENV=demo` сейчас. Переход на mainnet — только с явного согласия пользователя. При переходе — напомнить: в боевом `.env`
  малые `MAX_QTY_*`/`MAX_POSITION_*` (на demo `MAX_QTY_BTC`/`MAX_POSITION_BTC` подняты до 0.5 — не переносить).
- Бот работает **на сервере** (`~/trader`, служба `options-bot`, `systemctl --user`). Локально `optbot.bot` не запускать:
  один токен — один экземпляр (Telegram `Conflict`). Рабочая БД — на сервере; локальная `data/` устарела,
  `collector`/`journal` локально дают старые данные — запускать на сервере.
- Обновление сервера: `git pull`; изменились зависимости — `.venv/bin/pip install -r requirements.txt`; затем
  `systemctl --user restart options-bot` не ближе минуты к :00/:15/:30/:45 (иначе пропуск среза сборщика)
  и не во время сделки по сигналу («Выставить»: до минуты и дольше) — перезапуск оборвёт её между ногами без итога.
- Реальные ордера в проверках — далеко от рынка и сразу отменять (на demo пользователь разрешал мелкие сделки).
- `.env` (ключи, токен) не коммитить, не показывать и не передавать во внешние сервисы.

## Команды

```
.venv/bin/pip install -r requirements-dev.txt   # зависимости и pytest
.venv/bin/pytest                                # тесты (--cov=optbot — покрытие)
.venv/bin/python -m optbot.bot                  # бот (только на сервере — см. «Безопасность»)
.venv/bin/python -m optbot.cli --help           # CLI
.venv/bin/python smoke_order.py                 # ордер далеко от рынка -> отмена (только demo/testnet)
.venv/bin/python -m optbot.collector --once|--stats      # сборщик: срез вручную | что накоплено (на сервере)
.venv/bin/python -m optbot.journal --sync | --report 7d [--csv f]   # журнал сделок (на сервере)
.venv/bin/python -m optbot.cli signals BTC       # сигнал этапа 3 (pctl — по истории БД: на сервере)
.venv/bin/python -m optbot.sigjournal --evaluate | --stats 30d [--base BTC] [--csv f]   # журнал сигналов (на сервере)
.venv/bin/python -m optbot.paper --status | --stats 30d [--csv f] | --skips 7d | --step | --check-delivery-fees   # книги E1 (на сервере)
.venv/bin/python -m optbot.backup                # резервная копия БД сейчас (бот — раз в сутки, BACKUP_*)
.venv/bin/python -m optbot.perpbook --status | --stats 30d | --step   # бумажная книга перпетуалов EMA 4h (на сервере)
.venv/bin/python -m optbot.basisbook --status | --stats 30d | --step | --rf   # книга базиса BTC/ETH (+SOL наблюдение) | ставка FRED (на сервере)
.venv/bin/python -m optbot.reports --books 30d [--horizon short] [--base BTC] [--csv f] | --stress   # отчёты E3 (на сервере)
.venv/bin/python -m optbot.vol --show | --fit [--base BTC]   # прогноз HAR7: модели | докачать свечи и переоценить (на сервере)
deploy/offsite/pull_backups.py --help            # компьютер: выгрузка копий с сервера, ротация 7/4/6 (таймер — deploy/README.md)
cp deploy/options-bot.service ~/.config/systemd/user/   # сервер: служба systemctl --user, шаги — deploy/README.md
<venv с reportlab>/bin/python docs/guides.py docs/   # PDF-инструкции (ru/uk: admin, trader, viewer)
```

## Особенности Bybit (сверено на demo)

- `get_open_orders` с `orderId` **игнорирует** `symbol` — символ результата сверять отдельно.
- retCode 10000/10016/10019 — состояние неизвестно (как сбой сети); после сбоя искать ордер по `orderLinkId`.
- `execution/list`, `order/history`: окно времени ≤ 7 дней, пагинация `nextPageCursor`.
- Цена поставки опционов — `/v5/market/new-delivery-price` (`/delivery-price` для опционов пуст).
  `deliveryTime` там — **00:00 UTC дня поставки**, не 08:00 (`journal.nearest_price`: ±1 ч, иначе начало дня).
- `asset/delivery-record`: окно ≤ 30 дней.
- `execution/list`, `order/history` для option без `baseCoin` отдают все базовые активы (не только BTC).
- `order/history` фильтрует окно startTime/endTime по **времени создания** ордера; по `orderId` — без окна.
- `market/kline` (linear): от новых к старым, первая свеча — текущая незакрытая (`perp_closes` её отбрасывает).
- Поставка опционов: в `execution/list` её нет (execType=Delivery не приходит) — только `asset/delivery-record`;
  там `side` — сторона **закрытия** (длинная позиция → Sell), `position` без знака; вне денег `fee` = 0.
- Комиссия опциона за сделку: min(feeRate·индекс·кол-во, 7% премии) — у дешёвых OTM-ног срабатывает потолок.
- Маржа: в `REGULAR_MARGIN` покрытие короткой ноги длинной не учитывается (IM как у непокрытой);
  в `PORTFOLIO_MARGIN` `positionIM` пустой — IM только `totalInitialMargin` счёта. Demo сейчас — REGULAR_MARGIN.

## Данные

SQLite `data/market-<BYBIT_ENV>.db` (gitignored): сборщик (`collector.py`) и журнал сделок (`journal.py`);
сигналы (`signals.py`) читают `snapshots` для IV percentile; журнал сигналов (`sigjournal.py`) — таблица `signals`,
путь сделки — по `leg_marks`, итог — по `delivery_prices`. Кнопка «Выставить» — `multileg.py` (ноги IOC, сначала покупки),
итог исполнения — таблица `signal_executions`, ордера — `order_links` с `source=signal`. Пороги сигналов — `SIG_*` (`config.SignalSettings`).
Бумажные книги E1 (`paper.py`, шаг после каждого среза): `books`, `paper_trades`, `book_equity`, `paper_skips`
(отказы книг: первый `ts`, последний `last_ts`), `paper_signals` (итог расчёта сигнала на каждом шаге, `--skips`);
каждая книга — в двух версиях: без фильтра издержек и `…:cost` (`SIG_MAX_COST_PCT`);
ноги — на слежение (`tracked_symbols`, reason `book:<id>`); длинный горизонт и календарь — `signals.py`
(`horizon="long"`, `calendar_signal`); тег event — `events.csv` (`events.py`, дописывать вперёд).
Рыночный фон (`context.py`: фондирование, OI, индекс страха и жадности `FNG_URL`) — справочно в карточке и в `metrics`
журналов сигналов и книг; в тестах `FNG_URL` пуст (без сети). История для проверок (сборщик): `chain_snapshots`
(полная цепочка, `CHAIN_*`), `perp_context` (фондирование, OI), `fear_greed` (по дням).
`bot_state` — отметка «бот работает» (нештатная остановка видна при следующем старте; срочные администратору — `bot.admin_alert`).
Прогноз HAR7 (`vol.py`, research этап 6): часовые свечи перпетуала — `perp_h1` (вся история Bybit, докачка раз
в сутки), оценки коэффициентов — `har_models` (раз в месяц), эталон — `config/har_reference.json` (копия из research:
папки `research/` на сервере нет). Книги короткого горизонта «`…:har`» (и «`…:har:cost`») — решение по IV7/HAR7;
итог шага — `paper_signals.kind = short_har`; журнал сигналов пишет `har7` и `har_strategy` в `metrics`.
Книга перпетуалов EMA 4h (`perpbook.py`, research этап 3; параметры — `config/perp_ema.json`): `perp_books`,
`perp_trades`, `perp_equity`; шаг — в задаче бумажных книг после опционных; только бумага, ордеров нет.
Книга базиса (`basisbook.py`, спот лонг + перп шорт; параметры и ставка — `config/basis.json`): `basis_books`,
`basis_events`, `basis_equity`; шаг — после перпетуалов; только бумага; SOL — «наблюдение» (вне Σ). Ставка «сверх
ставки» — FRED DTB3 раз в сутки (`rates.py`, `RF_URL`, `bot_state` `rf:DTB3`; в тестах `RF_URL` пуст).
Отчёты E3 (`reports.py`): `/books` (метрики книг и систем по `book_equity`/`perp_equity`), `/stress` (Black-76 по IV и
underlying ног из `leg_marks`, бета ETH/SOL к BTC — по `perp_h1`), еженедельно `BOOKS_WEEKLY_UTC`; % — от `PAPER_CAPITAL_USDT`.
Схема — `db.MIGRATIONS`, только дописывать (одна миграция — список операторов без `;`).
Тесты пишут во временную БД (фикстура `cfg`), не в `data/`.
