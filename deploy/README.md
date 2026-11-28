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
      --exclude __pycache__ --exclude /backups ./ ap@SERVER:trader/
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

**Две БД (с 2026-11-27).** Рыночная — `data/market.db` (`MARKET_DB_PATH`: срезы, цепочка, котировки ног, свечи и
HAR, DVOL, индекс страха и жадности, ставка FRED) и БД счёта — `data/account-<env>.db` (`DB_PATH`: журналы сделок и
сигналов, книги, состояние бота); бот открывает БД счёта с подключённой рыночной. Старую общую
`data/market-<env>.db` разделить один раз (бот откажется стартовать, пока она не разделена):
```
systemctl --user stop options-bot                  # не ближе минуты к :00/:15/:30/:45 и не во время «Выставить»
cd ~/trader && git pull
df -h data && ls -lh data/market-demo.db           # свободно — не меньше ~1.5 размера БД
.venv/bin/python -m optbot.market --split          # рынок и счёт; число строк сверяется
ls -lh data/                                       # market.db, account-demo.db, market-demo.db.pre-split
systemctl --user start options-bot
```
Ошибка — исходная БД не тронута (недоделанные файлы удалены), повтор возможен. Пока идёт разделение, общая БД
заблокирована на запись. Отметка в БД «бот работает» (бот не остановлен или упал) — отказ; точно остановлен —
`--split --force`. Бот без разделённой БД (или с БД чужой роли, схемой новее кода) выходит с кодом 3 — служба его по кругу не перезапускает; временный сбой БД (занята, диск) — код 1, перезапуск как обычно
(`RestartPreventExitStatus`; после обновления `options-bot.service` — `cp` и `systemctl --user daemon-reload`).
Рыночная БД уже есть (разделена общая БД другого окружения: у demo и mainnet общий рынок) —
создаётся только БД счёта (бот другого окружения может работать), ноги на слежении и их котировки дописываются в рыночную, остальная история рынка этой БД
остаётся в `….pre-split`. Рыночная БД с данными, но не из разделения (её создал другой экземпляр) — отказ; дописать в неё
только ноги на слежении — `--merge`. `--from` с именем не `market-<BYBIT_ENV>.db` — отказ (журнал чужого счёта попал
бы в БД счёта этого окружения), переименованная копия — `--any-name`. Новые файлы — только владельцу (umask 077, как
у службы). БД счёта есть, а рыночной нет (не тот `MARKET_DB_PATH`, восстановлена одна копия) — бот не стартует
(код 3): пустая рыночная — без истории; начать с пустой — `python -m optbot.market --init`. `DB_PATH` и
`MARKET_DB_PATH` с одним именем файла (в разных папках) — отказ запуска: копии по имени смешались бы. `--split` откажет, если старую БД держит открытой другой процесс (бот не остановлен) — `--force` это не снимает; таблицы не из схемы бота (research) остаются только в `….pre-split` — `--split` о них предупредит. Старая БД ищется только рядом с `DB_PATH`; если `DB_PATH` перенесён в другую папку, а старая осталась в `data/`, — при старте предупреждение (она может быть чужой — другого экземпляра того же окружения), разделять её — только явно `--split --from data/market-<env>.db`. `….pre-split` удалить, когда всё проверено (уже есть — `--split` откажет, не затрёт). Старые копии `backups/market-demo-…` (до
разделения) бот больше не чистит — удалить вручную, когда пройдёт `BACKUP_KEEP_DAYS`.

**Резервные копии.** Бот раз в сутки (`BACKUP_DAILY_UTC`, 03:20 UTC) делает копии обеих БД (`account-<env>-…`,
`market-…`) через sqlite backup API
(согласованный снимок при работающем боте, не копирование файла) в `~/trader/backups/`, проверяет её
(`PRAGMA quick_check`), сжимает gzip (`.db.gz`, `BACKUP_COMPRESS`), хранит `BACKUP_KEEP_DAYS` (14) дней;
сбой — срочное администратору. Вручную:
`.venv/bin/python -m optbot.backup`. Копия на том же диске — забирать на свой компьютер (ниже).

**Копии на своём компьютере** (`deploy/offsite/`). Скрипт `pull_backups.py` (только стандартный Python) по ssh
забирает с сервера `.db.gz` и хранит в `OPTBOT_BACKUP_DEST` (например `backups/` рабочей копии проекта —
в `.gitignore`): `daily/` — 7 последних дней, `weekly/` — 4 последних
воскресенья, `monthly/` — 6 последних первых чисел (дата из имени файла, UTC). Таймер systemd раз в сутки
(06:00 UTC) с `Persistent=true`: компьютер был выключен — запуск при включении (до 10 попыток с паузой в минуту,
пока поднимается сеть). Скачанное проверяется (gzip CRC и что внутри база SQLite); даты позже завтрашней
(сбой часов сервера) не учитываются; два запуска в одну папку не мешают друг другу (блокировка).
Сбой выгрузки виден только в `journalctl --user -u optbot-pull-backups` — чтобы узнать о нём, заведите отдельную
проверку healthchecks.io (период 1 день, grace — сколько компьютер бывает выключен, например 3 дня) и впишите
её адрес в `OPTBOT_BACKUP_HEALTHCHECK`: успех — пинг, сбой всех попыток — `/fail`. Сервер хранит 14 дней — пропущенные воскресенья и первые числа догружаются, если компьютер
не был выключен дольше. Файл качается один раз (в другие папки — жёсткая ссылка), проверяется (gzip CRC) до того,
как попасть в папки; старые удаляются только после успешной загрузки. Несжатые `.db` не забираются.

Установка на компьютере (один раз), из корня проекта:
```
# 1. ключ без пароля только для копий (таймер работает без ssh-агента) и вход по нему
ssh-keygen -t ed25519 -N "" -C optbot-backup -f ~/.ssh/optbot_backup
ssh-copy-id -i ~/.ssh/optbot_backup.pub ap@SERVER
ssh -i ~/.ssh/optbot_backup -o IdentitiesOnly=yes -o BatchMode=yes ap@SERVER ls trader/backups   # без пароля
# 2. скрипт, настройки (адрес сервера — здесь, не в репозитории), таймер
install -Dm755 deploy/offsite/pull_backups.py ~/.local/bin/optbot-pull-backups
cp -n deploy/offsite/optbot-pull-backups.env.example ~/.config/optbot-pull-backups.env   # вписать сервер
cp deploy/offsite/optbot-pull-backups.service deploy/offsite/optbot-pull-backups.timer ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now optbot-pull-backups.timer
# 3. проверка: запустить сейчас и посмотреть итог
systemctl --user start optbot-pull-backups && journalctl --user -u optbot-pull-backups -n 20
ls -l backups/*/          # или своя OPTBOT_BACKUP_DEST
```
Когда последний запуск и следующий: `systemctl --user list-timers optbot-pull-backups`. Вход по паролю ssh
просит в терминале — у таймера не сработает (`BatchMode`): нужен именно ключ. После обновления скрипта в
репозитории — повторить `install` из шага 2.
Восстановление: остановить бот, распаковать копии (`gunzip -c backups/account-demo-ГГГГММДД-ЧЧММ.db.gz >
data/account-demo.db`, `gunzip -c backups/market-ГГГГММДД-ЧЧММ.db.gz > data/market.db`; старые `-wal`/`-shm`
убрать), запустить.

**Внешний мониторинг.** О своём падении бот сообщить не может. healthchecks.io (бесплатно): создать проверку
с периодом 15 мин и grace 30 мин, уведомления — на почту/Telegram; адрес пинга (`https://hc-ping.com/<uuid>`) —
в `HEALTHCHECK_URL` в `.env`. Бот пингует после каждого среза сборщика (сбой среза — `/fail`): пинги пропали —
бот лежит или сборщик не работает. Адрес — секрет (в лог не пишется).

Накопленные базы можно перенести: при остановленном боте `sqlite3 data/market.db ".backup /tmp/market.db"` (и так
же `data/account-<env>.db`), затем `scp` в `~/trader/data/`.
