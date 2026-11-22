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

Код — из GitHub:
```
git clone git@github.com:ap75/trader.git ~/trader      # первый раз
cd ~/trader && git pull                                # обновление
```

**Альтернатива — rsync с компьютера** (если на сервере нет доступа к GitHub). Из корня проекта на компьютере:
```
rsync -av --exclude .venv --exclude data --include .env.example --exclude '.env*' --exclude .git \
      --exclude __pycache__ ./ ap@SERVER:trader/
```
Не трогает на сервере `.env`, `data/`, `.venv` и `.git`. Способы не смешивать: если сервер обновляется через
`git pull`, rsync поверх него оставит рабочую копию с правками относительно последнего коммита — следующий
`git pull` споткнётся. Отправляется ровно то, что лежит в папке на компьютере, включая незакоммиченные правки —
перед rsync проверьте `git status`. Ключ `--delete` не добавлять: он удалит на сервере файлы, которых нет локально.

**Обновление:** `git pull` (или rsync), при изменении зависимостей `.venv/bin/pip install -r requirements.txt`, затем
`systemctl --user restart options-bot` — не ближе минуты к :00/:15/:30/:45, иначе сборщик пропустит срез
(восполнить: `.venv/bin/python -m optbot.collector --once`). Автоперезапуск по `.path` не нужен; если всё же
делать — следить за файлом-триггером, не за папкой проекта (бот сам пишет в `data/`).

**После переезда** бот на компьютере не запускать (один токен — один экземпляр); рабочая база — на сервере.

**Резервные копии.** Бот раз в сутки (`BACKUP_DAILY_UTC`, 03:20 UTC) делает копию базы через sqlite backup API
(согласованный снимок при работающем боте, не копирование файла) в `~/trader/backups/`, проверяет её
(`PRAGMA quick_check`), сжимает gzip (`.db.gz`, `BACKUP_COMPRESS`), хранит `BACKUP_KEEP_DAYS` (14) дней;
сбой — срочное администратору. Вручную:
`.venv/bin/python -m optbot.backup`. Копия на том же диске — забирать на свой компьютер по расписанию,
например cron на компьютере (раз в сутки после 03:30 UTC):
```
rsync -a --exclude '*.part*' ap@SERVER:trader/backups/ ~/trader-backups/   # недописанные .part — не брать
```
Восстановление: остановить бот, распаковать копию в `data/market-<env>.db`
(`gunzip -c backups/market-demo-ГГГГММДД-ЧЧММ.db.gz > data/market-demo.db`; старые `-wal`/`-shm` убрать),
запустить.

**Внешний мониторинг.** О своём падении бот сообщить не может. healthchecks.io (бесплатно): создать проверку
с периодом 15 мин и grace 30 мин, уведомления — на почту/Telegram; адрес пинга (`https://hc-ping.com/<uuid>`) —
в `HEALTHCHECK_URL` в `.env`. Бот пингует после каждого среза сборщика (сбой среза — `/fail`): пинги пропали —
бот лежит или сборщик не работает. Адрес — секрет (в лог не пишется).

Накопленную базу можно перенести: при остановленном локальном боте
`sqlite3 data/market-demo.db ".backup /tmp/market-demo.db"`, затем `scp` в `~/trader/data/`. Рост — около 1 МБ в сутки.
