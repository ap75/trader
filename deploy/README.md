# Установка на сервер (пользовательская служба systemd)

Подробно — PDF администратора (`docs/guide-admin-ru.pdf`, раздел «Развёртывание на VPS»).
Один процесс — `optbot.bot`: Telegram-бот, сборщик срезов и сверка журнала. Python 3.11+.

Служба пользовательская (`systemctl --user`): root не нужен, ничего не ставится в систему, `/etc` не меняется,
входящих портов нет — другим сервисам сервера (Hestia, nginx, сайты, чужие боты) бот не мешает.
Проект — в `~/trader`; другой путь — поменять в `options-bot.service`.

**Перед запуском:** локальный бот остановить (один токен — один экземпляр, иначе Telegram отвечает
`Conflict: terminated by other getUpdates request`); IP сервера — в whitelist API-ключа Bybit; проверить
`BYBIT_ENV` в `~/trader/.env` (mainnet — реальные деньги).

```
# один раз, администратор: служба стартует после перезагрузки и не гаснет при выходе из SSH
sudo loginctl enable-linger ap
loginctl show-user ap -p Linger                # Linger=yes

# под своим пользователем по SSH (после su/sudo -u: export XDG_RUNTIME_DIR=/run/user/$(id -u))
cd ~/trader && chmod 600 .env && mkdir -p data
systemctl --user list-unit-files | grep options-bot   # имя свободно — пусто
mkdir -p ~/.config/systemd/user
cp deploy/options-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now options-bot
systemctl --user status options-bot            # active (running)
journalctl --user -u options-bot -f            # лог; действия трейдеров — строки optbot.trade
```

Код с компьютера:
`rsync -av --exclude .venv --exclude data --include .env.example --exclude '.env*' --exclude .git --exclude __pycache__ ./ ap@SERVER:trader/`

**Обновление:** rsync, при изменении зависимостей `.venv/bin/pip install -r requirements.txt`, затем
`systemctl --user restart options-bot` — не ближе минуты к :00/:15/:30/:45, иначе сборщик пропустит срез
(восполнить: `.venv/bin/python -m optbot.collector --once`). Автоперезапуск по `.path` не нужен; если всё же
делать — следить за файлом-триггером, не за папкой проекта (бот сам пишет в `data/`).

Накопленную базу можно перенести: при остановленном локальном боте
`sqlite3 data/market-demo.db ".backup /tmp/market-demo.db"`, затем `scp` в `~/trader/data/`. Рост — около 1 МБ в сутки.
