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

- `BYBIT_ENV=demo` сейчас. Переход на mainnet — только с явного согласия пользователя.
- Бот работает **на сервере** (`~/trader`, служба `options-bot`, `systemctl --user`). Локально `optbot.bot` не запускать:
  один токен — один экземпляр (Telegram `Conflict`). Рабочая БД — на сервере; локальная `data/` устарела,
  `collector`/`journal` локально дают старые данные — запускать на сервере.
- Обновление сервера: `git pull`; изменились зависимости — `.venv/bin/pip install -r requirements.txt`; затем
  `systemctl --user restart options-bot` не ближе минуты к :00/:15/:30/:45 (иначе пропуск среза сборщика).
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
cp deploy/options-bot.service ~/.config/systemd/user/   # сервер: служба systemctl --user, шаги — deploy/README.md
<venv с reportlab>/bin/python docs/guides.py docs/   # PDF-инструкции (ru/uk: admin, trader, viewer)
```

## Особенности Bybit (сверено на demo)

- `get_open_orders` с `orderId` **игнорирует** `symbol` — символ результата сверять отдельно.
- retCode 10000/10016/10019 — состояние неизвестно (как сбой сети); после сбоя искать ордер по `orderLinkId`.
- `execution/list`, `order/history`: окно времени ≤ 7 дней, пагинация `nextPageCursor`.
- Цена поставки опционов — `/v5/market/new-delivery-price` (`/delivery-price` для опционов пуст).
- `asset/delivery-record`: окно ≤ 30 дней.
- `execution/list`, `order/history` для option без `baseCoin` отдают все базовые активы (не только BTC).
- `order/history` фильтрует окно startTime/endTime по **времени создания** ордера; по `orderId` — без окна.
- `market/kline` (linear): от новых к старым, первая свеча — текущая незакрытая (`perp_closes` её отбрасывает).

## Данные

SQLite `data/market-<BYBIT_ENV>.db` (gitignored): сборщик (`collector.py`) и журнал сделок (`journal.py`);
сигналы (`signals.py`) читают `snapshots` для IV percentile. Пороги сигналов — `SIG_*` (`config.SignalSettings`).
Схема — `db.MIGRATIONS`, только дописывать (одна миграция — список операторов без `;`).
Тесты пишут во временную БД (фикстура `cfg`), не в `data/`.
