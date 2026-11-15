# Bybit options bot

Управление опционами Bybit (V5, BTC/ETH/SOL, USDT) через CLI и Telegram-бот. План и статус — `PLAN.md`.

## Правила работы

После **каждого** изменения кода, даже небольшого, до отчёта и коммита:

1. **Тесты на изменение** — новые или дополненные в `tests/` (подставной Bybit `tests/fakes.py`,
   подставной Telegram в `tests/test_bot.py`). Изменение без тестов не считается готовым.
2. **Код-ревью** диффа; найденное исправить или показать пользователю.
3. **Полный прогон** `.venv/bin/pytest` — все тесты зелёные, в том числе после исправлений по ревью.

Коммиты: 1–3 строки, без `Co-Authored-By`. Коммитить по просьбе пользователя.

## Безопасность

- `BYBIT_ENV=demo` сейчас. Переход на mainnet — только с явного согласия пользователя.
- Реальные ордера в проверках — далеко от рынка и сразу отменять (на demo пользователь разрешал мелкие сделки).
- `.env` (ключи, токен) не коммитить, не показывать и не передавать во внешние сервисы.

## Команды

```
.venv/bin/pip install -r requirements-dev.txt   # зависимости и pytest
.venv/bin/pytest                                # тесты (--cov=optbot — покрытие)
.venv/bin/python -m optbot.bot                  # бот
.venv/bin/python -m optbot.cli --help           # CLI
.venv/bin/python smoke_order.py                 # ордер далеко от рынка -> отмена (только demo/testnet)
<venv с reportlab>/bin/python docs/guides.py docs/   # PDF-инструкции (ru/uk: admin, trader, viewer)
```

## Особенности Bybit (сверено на demo)

- `get_open_orders` с `orderId` **игнорирует** `symbol` — символ результата сверять отдельно.
- retCode 10000/10016/10019 — состояние неизвестно (как сбой сети); после сбоя искать ордер по `orderLinkId`.
- `execution/list`, `order/history`: окно времени ≤ 7 дней, пагинация `nextPageCursor`.
- Цена поставки опционов — `/v5/market/new-delivery-price` (`/delivery-price` для опционов пуст).
