# Telegram Chat AI Bot

Python Telegram group assistant using the OpenAI Responses API, exact reply-chain
context and a local SQLite rolling cache. Application version: **1.3.0**.

[Русский](#russian) · [English](#english)

<a id="russian"></a>
## Русский

### Что это за бот

Бот для Telegram group/supergroup на Python 3.12, aiogram 3, OpenAI Async SDK,
Telethon и aiosqlite. Работает через long polling, без входящего HTTP-порта.
Есть три явных способа обратиться к модели:

- `@username_бота объясни Docker` — только текущий вопрос.
- Reply на сообщение с `@username_бота что обсуждается в этой ветке?` — вопрос
  и доступные предки выбранного сообщения через MTProto, без соседних веток.
- `/context 50 подведи итог разговора` — вопрос и последние сообщения текущего
  chat/topic из локального SQLite, строго до ID команды.

Обычные сообщения сохраняются **только локально**: Telegram → Ubuntu → SQLite.
Они не вызывают OpenAI, embeddings, moderation или Telethon fetch. Recent history
не добавляется к обычному mention автоматически; два режима контекста не смешиваются.
Бот не отвечает в личных чатах и каналах. Для запуска AI нужны текст и реальный
human user ID: сообщения других ботов, captions, анонимных sender_chat, service
events и edits сами по себе не вызывают модель.

Username/ID получаются через getMe; mention и command проверяются по Telegram
entities с учётом UTF-16. Reply без нового mention не вызывает AI. Ответы — plain
text в исходном чате/теме, первая часть — reply. Длинные ответы разбиваются до
4096 UTF-16 units, preview ссылок выключен, во время запроса показывается typing.
Модель отвечает на языке пользователя; локальные служебные ответы — на русском.

### Требования и получение ключей

Рекомендуемый сервер: **Ubuntu 24.04 LTS и Python 3.12**. Проверки выполнены на
Python 3.12; системного Python 3.10 из Ubuntu 22.04 недостаточно. Нужны DNS/CA и
исходящий HTTPS к Telegram/OpenAI, а для reply-chain — MTProto TCP к Telegram DC.
Запускайте ровно один polling-процесс на bot token.

1. В [@BotFather](https://t.me/BotFather) выполните `/newbot` и сохраните token.
2. Отключите **Group Privacy** через `/setprivacy` → бот → **Disable**. Если бот
   уже был в группе, удалите его и добавьте снова для применения настройки.
3. Разрешите боту отправлять сообщения в группе. Администраторские права для
   обычного сценария не обязательны. Privacy Disable нужен для получения обычных
   сообщений; получение update не означает отправку его в OpenAI.
4. Создайте [OpenAI API key](https://platform.openai.com/api-keys). Проект должен
   иметь API billing и доступ к модели с Responses API; подписка ChatGPT не
   заменяет настройки API проекта. Модель задаётся через `OPENAI_MODEL`.
5. Для reply-chain дополнительно получите API ID/hash в **API development tools**
   на [my.telegram.org](https://my.telegram.org). Это credentials приложения
   Telegram, не номер телефона. Runtime входит только как bot; SMS/login userbot
   не используются. Без обоих значений mention и `/context` работают, enrichment
   пропускается. Если заполнено только одно значение, конфигурация невалидна.

Не отправляйте ключи или `.env` в Telegram и не включайте их в Git.

### Локальные вариативные ответы

Версия 1.3.0 добавляет готовые реакции на отдельные слова и фразы, например
«ептиль», «ёптиль», «сижу», «привет», «норм» и «чекаво». Они содержат ненормативную
лексику, отправляются без OpenAI и сохраняются как ответы нашего бота в cache.
Проверка не зависит от регистра и не совпадает с частью слова. Для каждого trigger
варианты перемешиваются и расходуются без повторов до завершения цикла; соседние
циклы не дают одинаковый ответ подряд. Порядок общий для всех чатов и сбрасывается
при restart. Сообщения с Telegram entity mention/text_mention/bot_command
пропускают эти реакции: явные обращения к AI и команды сохраняют свои проверки,
rate limiter и контекст. Edits, боты и анонимные отправители не запускают реакции.

### Локальная настройка и запуск

```bash
git clone https://github.com/megabars/telegram-chat-ai-bot.git
cd telegram-chat-ai-bot
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
cp .env.example .env
chmod 600 .env
```

Заполните `.env`: обязательны `TELEGRAM_BOT_TOKEN` и `OPENAI_API_KEY`.
Для локальной разработки переопределите production state paths:

```dotenv
LOCAL_HISTORY_DB_PATH=./data/history.sqlite3
TELEGRAM_MTPROTO_SESSION_PATH=./data/telegram.session
```

API ID/hash задавайте вместе либо оставьте оба пустыми. Чтобы явно отключить
Telethon, установите `REPLY_CONTEXT_ENABLED=false`. Ограничьте нужные группы
через `ALLOWED_CHAT_IDS`, например `-1001234567890,-987654321`.

```bash
.venv/bin/python -m app.main --check-config
.venv/bin/python -m app.main
```

`--check-config` проверяет формат настроек без сети и без проверки действительности
ключей. `Ctrl+C` закрывает handlers, SQLite и клиентов. Альтернатива после настройки
`.env`: `PYTHON_BIN=python3.12 ./install.sh`.

### Конфигурация

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | `required / обязательно` | Token BotFather |
| `OPENAI_API_KEY` | `required / обязательно` | Ключ OpenAI API |
| `OPENAI_MODEL` | `gpt-6-luna` | Модель с поддержкой Responses API |
| `TELEGRAM_API_ID` | `empty / пусто` | API ID для optional reply-chain |
| `TELEGRAM_API_HASH` | `empty / пусто` | API hash того же Telegram application |
| `ALLOWED_CHAT_IDS` | `empty / пусто` | Числовые chat IDs через запятую; пусто — любые группы |
| `MAX_INPUT_CHARS` | `12000` | Максимальная длина текущего вопроса |
| `MAX_CONCURRENT_REQUESTS` | `5` | Лимит OpenAI; отдельно тот же лимит Telethon fetch |
| `RATE_LIMIT_REQUESTS` | `10` | Запросов на user ID в скользящем окне |
| `RATE_LIMIT_PERIOD_SECONDS` | `60` | Окно rate limiter в секундах |
| `OPENAI_TIMEOUT_SECONDS` | `60` | Общий OpenAI timeout вместе с ожиданием очереди |
| `MAX_OUTPUT_TOKENS` | `2048` | Максимум output tokens, включая reasoning |
| `LOG_LEVEL` | `INFO` | DEBUG/INFO/WARNING/ERROR/CRITICAL; без текстов переписки |
| `REPLY_CONTEXT_ENABLED` | `true` | Включить optional MTProto enrichment |
| `REPLY_CONTEXT_MAX_DEPTH` | `30` | Максимум ancestors, не включая вопрос |
| `REPLY_CONTEXT_MAX_CHARS` | `30000` | Бюджет reply input: JSON, вопрос, markers |
| `REPLY_CONTEXT_FETCH_TIMEOUT_SECONDS` | `10` | Общий срок fetch, включая очередь и peer resolution |
| `TELEGRAM_MTPROTO_SESSION_PATH` | `/var/lib/telegram-openai-bot/telegram.session` | Постоянная секретная bot session |
| `LOCAL_HISTORY_ENABLED` | `true` | Сохранять group updates локально |
| `LOCAL_HISTORY_DB_PATH` | `/var/lib/telegram-openai-bot/history.sqlite3` | Постоянный rolling cache |
| `LOCAL_HISTORY_MAX_MESSAGES` | `5000` | Сколько записей оставить на chat после cleanup |
| `LOCAL_HISTORY_CLEANUP_THRESHOLD` | `5100` | При достижении — cleanup до MAX_MESSAGES |
| `CONTEXT_COMMAND_DEFAULT_MESSAGES` | `50` | N при отсутствии числа в /context |
| `CONTEXT_COMMAND_MAX_MESSAGES` | `200` | Максимально допустимое N |
| `CONTEXT_MAX_CHARS` | `50000` | Recent input: JSON, вопрос и marker |
| `CONTEXT_RESPECT_TOPICS` | `true` | Выбирать только текущую forum topic |

Boolean values — только `true`/`false`, числовые лимиты положительны, таймауты конечны.
Cleanup threshold должен быть больше max messages; default N не больше max N.
SQLite history и Telethon session должны иметь разные пути. `.env` читается из
working directory; environment имеет приоритет. Используйте простой `NAME=value`
без `export`, `$VAR`, shell-команд и multiline values: systemd также читает этот
файл как EnvironmentFile. `source .env` не нужен. OpenAI endpoint фиксирован на
`https://api.openai.com/v1`; `OPENAI_BASE_URL` его не меняет.

### Команда /context и rolling cache

```text
/context 20 подведи итог
/context@username_бота 50 какие решения мы приняли?
/context 100 кто предложил перейти на PostgreSQL?
/context что обсуждают?
/context 50
/context
```

Без N берётся 50; без вопроса — «Кратко подведи итог последних сообщений этой
ветки разговора. Выдели основные темы, решения и открытые вопросы.» N означает
**максимум предыдущих сообщений**, а не обязательное количество. Если их 17 при
N=50, используются 17. При пустой истории — локальный ответ без API. Нулевой,
отрицательный, нецелый N или N>200 дают локальную ошибку. Единственный нечисловой
аргумент (`/context abc`) считается ошибочным N; вопрос без N должен содержать
несколько слов. `/context@other_bot` не выполняется нашим ботом.

SQL выбирает только текущий chat и, по умолчанию, текущую forum topic, включая
General (ID 1). В обычной supergroup reply thread не считается forum topic.
`CONTEXT_RESPECT_TOPICS=false` разрешает объединить темы текущего чата. Команда
сама не входит в input (`message_id < command_id`). После чтения создаётся
immutable snapshot: новые сообщения и edits во время AI не меняют этот запрос.
История идёт от старой к новой; текущий вопрос последний. Все команды сохраняются
и могут появиться в более поздних snapshots. Собственные успешно отправленные
ответы сохраняются вручную, включая части длинного ответа; они имеют роль assistant.
Другие сообщения имеют роль user; модель видит имя, timestamp и текст, без
служебных chat/message/database IDs.

`CONTEXT_MAX_CHARS` включает JSON, markers и вопрос; инструкции приложения вне
бюджета. При обрезке приоритет у свежих сообщений и marker
`[Earlier messages omitted due to context size limit]`. Вопрос не урезается ради
истории, его предел — MAX_INPUT_CHARS. Если бюджет слишком мал даже для истории,
сохраняется только вопрос. Нет скрытого второго запроса для summarization.

Cache хранит доставленные group/supergroup text, caption или media placeholder,
не скачивает файлы и не выполняет OCR/STT. Service events пропускаются; edits
обновляют запись без AI; PK `(chat_id,message_id)` устраняет дубли. Сообщения
других ботов сохраняются только если Telegram их доставил. Telethon не используется
для backfill cache. Удаления в Telegram надёжно не синхронизируются: **это rolling
cache, а не идеальная архивная копия**, старые записи вытесняются cleanup.

Лимит — на весь chat_id со всеми topics: 0…5099 без очистки, при 5100 одна короткая
SQL-транзакция оставляет последние 5000. Другие чаты не затрагиваются; общий лимит
на всю таблицу отсутствует. Один async connection, WAL, busy_timeout=5000,
synchronous=NORMAL, индексы chat/message и chat/topic/message. Counters загружаются
при startup; нет COUNT всей таблицы на каждое сообщение или per-cleanup VACUUM.
Физический файл может не уменьшаться после DELETE: свободные pages переиспользуются.
Нет TTL; неактивный chat остаётся до rolling replacement или ручной очистки.
`LOCAL_HISTORY_ENABLED=false` прекращает запись, `/context` отвечает локально;
старый файл не удаляется, mention/reply-chain продолжают работать.

### Контекст цепочки ответов

Только reply с новым mention запускает выбор конкретных ancestors. Telethon
создаётся один раз, работает RPC-only (`receive_updates=False`, `catch_up=False`),
без getHistory, userbot и второго listener. Используются getMessages по IDs;
peer resolution может дополнительно запрашивать metadata access hash.
Сессия должна принадлежать текущему bot ID; она хранит authorization key и
peer metadata, **не переписку**. Максимум 30 ancestors и 10 секунд на весь fetch.
Topic/chat boundaries, циклы, удалённые parents, timeout и FloodWait останавливают
обход; доступная часть используется с marker недоступности. Если контекста нет,
модель получает только вопрос. Ближайшие ancestors имеют приоритет при 30000-char
budget. Отдельный marker: `[Earlier messages omitted due to context limit]`.
Reply без mention не запускает fetch. Recent cache сюда автоматически не добавляется.

### Установка на Ubuntu через systemd

Команды ниже выполняются на Ubuntu 24.04. Создание `.env` из template — **только
при первой установке**; существующий `.env` не заменяйте.

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv ca-certificates git rsync

git clone https://github.com/megabars/telegram-chat-ai-bot.git
cd telegram-chat-ai-bot

id telegrambot >/dev/null 2>&1 || sudo useradd --system --user-group \
  --home-dir /opt/telegram-openai-bot --no-create-home \
  --shell /usr/sbin/nologin telegrambot
sudo install -d -o root -g telegrambot -m 750 /opt/telegram-openai-bot
sudo rsync -a --chmod=D755,F644 --exclude='.git' --exclude='.venv' --exclude='.env' --exclude='.env.*' \
  --exclude='.state' --exclude='data' --exclude='*.session*' --exclude='*.sqlite*' \
  --exclude='__pycache__' --exclude='.pytest_cache' --exclude='.ruff_cache' \
  --exclude='._*' \
  ./ /opt/telegram-openai-bot/
sudo install -m 644 .env.example /opt/telegram-openai-bot/.env.example
sudo install -o telegrambot -g telegrambot -m 600 \
  .env.example /opt/telegram-openai-bot/.env
sudo nano /opt/telegram-openai-bot/.env
sudo chmod 755 /opt/telegram-openai-bot/install.sh
sudo bash -c 'cd /opt/telegram-openai-bot && PYTHON_BIN=python3.12 ./install.sh --systemd'
sudo systemctl enable --now telegram-openai-bot
sudo systemctl status telegram-openai-bot --no-pager
sudo journalctl -u telegram-openai-bot -n 40 --no-pager
```

Installer устанавливает `requirements.lock`, проверяет зависимости/конфигурацию,
копирует unit и вызывает daemon-reload; сервис запускается отдельной командой.
Код/venv принадлежат root и доступны сервису на чтение; runtime — **telegrambot**.
`.env` — telegrambot/600. `StateDirectory=telegram-openai-bot` создаёт persistent
`/var/lib/telegram-openai-bot` с 700, DB/session/sidecars — 600. Unit сохраняет
ProtectSystem=strict, NoNewPrivileges, пустой CapabilityBoundingSet, private
/tmp/devices, Restart=on-failure и graceful SIGTERM, TimeoutStopSec=45. Не меняйте
сервис на root ради SQLite. Для другого state path потребуется разрешить запись
в unit. Runtime не пишет в `/opt`.

### Обновление, обслуживание и очистка

Обновляйте checkout на сервере, сохраняя `.env`, session, DB и venv. Команды
исключают state/data/SQLite/session и секретные env-файлы, не используют `--delete`.
Новые параметры при необходимости добавьте в рабочую `.env`, сохранив credentials.

```bash
cd ~/telegram-chat-ai-bot
git pull --ff-only origin main
sudo systemctl stop telegram-openai-bot
sudo rsync -a --chmod=D755,F644 --exclude='.git' --exclude='.venv' --exclude='.env' --exclude='.env.*' \
  --exclude='.state' --exclude='data' --exclude='*.session*' --exclude='*.sqlite*' \
  --exclude='__pycache__' --exclude='.pytest_cache' --exclude='.ruff_cache' \
  --exclude='._*' \
  ./ /opt/telegram-openai-bot/
sudo install -m 644 .env.example /opt/telegram-openai-bot/.env.example
sudo chmod 755 /opt/telegram-openai-bot/install.sh
sudo bash -c 'cd /opt/telegram-openai-bot && PYTHON_BIN=python3.12 ./install.sh --systemd'
sudo systemctl start telegram-openai-bot
sudo systemctl status telegram-openai-bot --no-pager
sudo journalctl -u telegram-openai-bot -n 40 --no-pager
```

```bash
sudo systemctl restart telegram-openai-bot
sudo systemctl stop telegram-openai-bot
sudo systemctl start telegram-openai-bot
sudo journalctl -u telegram-openai-bot -f
sudo sh -c 'du -h /var/lib/telegram-openai-bot/history.sqlite3*'
sudo stat -c '%a %U:%G %n' /var/lib/telegram-openai-bot \
  /var/lib/telegram-openai-bot/history.sqlite3 \
  /var/lib/telegram-openai-bot/telegram.session
```

В journal ожидаются `Local history started mode=sqlite_wal`, `Bot started` и
`Reply context enabled=True`, если MTProto настроен и доступен. Metadata содержат
IDs, requested/selected counts, chars, duration и cleanup counts, **не тексты или
ключи**. DEBUG также не включает SDK/SQL body logging. Не включайте HTTP tracing.
Файла session может ещё не быть, если enrichment отключён или не авторизован.

Полная очистка только local cache:

```bash
sudo systemctl stop telegram-openai-bot
sudo rm -f /var/lib/telegram-openai-bot/history.sqlite3 \
  /var/lib/telegram-openai-bot/history.sqlite3-wal \
  /var/lib/telegram-openai-bot/history.sqlite3-shm \
  /var/lib/telegram-openai-bot/history.sqlite3-journal
sudo systemctl start telegram-openai-bot
```

Не удаляйте `telegram.session`: это отдельная авторизация. Очистка SQLite не удаляет
ничего в Telegram; cache начнёт наполняться заново. Автоматическая backup-система
не добавлена; этот cache не является критическим permanent archive.

При каждом старте бот выполняет `deleteWebhook(drop_pending_updates=True)`: старые
updates после downtime отбрасываются, webhook отключается. Это предотвращает
неожиданные старые платные запросы, но оставляет пробелы в cache. Перезапуск сохраняет
DB и bot session, обнуляет in-memory rate limiter; exactly-once AI responses после
сбоя не гарантируются даже при дедупликации cache.

### Ограничения и диагностика

| Симптом | Проверка |
| --- | --- |
| Нет ответа | group/supergroup, настоящий mention или /context entity, Group Privacy Disable, повторное добавление, право отправки, ALLOWED_CHAT_IDS |
| Пустой /context | Получены ли новые сообщения после запуска; cache не загружает прежнюю переписку |
| Нет reply-chain | API ID/hash вместе; MTProto connectivity/session owner; в journal reason fallback |
| SQLite PermissionError | State path вне /opt, owner telegrambot, directory 700 / DB 600, StateDirectory unit |
| Telegram polling conflict | Другой процесс с тем же token или webhook; нужен один экземпляр |
| OpenAI 401/403/404 | Ключ, project/model access, доступность выбранной модели |
| OpenAI 429 | API quota/billing/provider rate limit; приложение не делает SDK retries |
| timeout/network | DNS, CA, HTTPS/MTProto, настроенные deadlines |
| empty_output | Output budget мог исчерпаться на reasoning; проверьте MAX_OUTPUT_TOKENS |
| start-limit-hit | Исправьте причину и выполните systemctl reset-failed/start |

Лимит по умолчанию — 10 запросов/60 секунд **на user ID суммарно по всем группам**;
попытки, дошедшие до AI, учитываются и при API ошибке. Concurrency=5, число активных
Telegram handlers не больше concurrency×4, общий AI timeout включает очередь.
Ошибки модели дают безопасный локальный ответ. Telegram FloodWait допускает одну
повторную отправку с ожиданием до 30 секунд, без нового AI request. Отмена запроса
не гарантирует отсутствия расходов, если провайдер уже начал обработку.

### Проверка и устройство проекта

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/ruff check app tests
.venv/bin/ruff format --check app tests
.venv/bin/python -m compileall -q app
.venv/bin/python -m pip check
bash -n install.sh
```

**245 офлайн-тестов** проходят на Python 3.12; сеть в тестах блокируется,
SQLite — только temporary databases. Есть проверки 100 обычных updates → 0 AI/fetch,
/context → 1 AI, snapshot, topics, roles/injection, edits, outgoing messages,
5100→5000 cleanup per chat, concurrency, lifecycle, deadlines, UTF-16 и безопасных
логов. Тесты не проверяют права реального bot token или доступ API проекта.
GitHub Actions запускает этот набор и проверки кода при push/pull_request без ключей.

```text
app/config.py                    .env validation и defaults
app/main.py                      startup/polling/shutdown
app/handlers/messages.py         cache writes и явные AI triggers
app/services/openai_service.py   один stateless Responses call
app/services/telegram_history.py exact ancestor MTProto fetch
app/services/local_history.py    async SQLite rolling cache
app/utils/                       mentions, commands, topics, rate limit, formatting
telegram-openai-bot.service       hardened non-root systemd
install.sh                       воспроизводимая установка production lock
requirements.lock                pinned production dependencies
requirements-dev.txt             test/lint dependencies
tests/                           офлайн regression suite
```

Никаких SQL/shell/tool calls от модели. Telegram history — untrusted context с
отдельной приоритетной инструкцией; это снижает риск prompt injection, но не обещает
абсолютной защиты модели. Credentials берутся только из environment/.env. Git
исключает env-файлы, keys, sessions, DBs/sidecars, data/state, venv, logs, caches
и archives. Перед публикацией проверяйте staged files, а не только рабочую папку.
`store=False` отключает Responses state retrieval, **не является обещанием Zero
Data Retention**; политики хранения переданных явно вопросов/контекста определяет
[OpenAI data controls](https://developers.openai.com/api/docs/guides/your-data).
Для production задайте ALLOWED_CHAT_IDS и ограничьте доступ к state/journal.

<a id="english"></a>
## English

### What the bot does

A Python 3.12 Telegram group/supergroup assistant built with aiogram 3, the official
async OpenAI SDK, Telethon and aiosqlite. It uses long polling and needs no inbound
HTTP port. The model is invoked through three explicit interactions:

- `@bot_username explain Docker` sends the current question only.
- Reply to a message with `@bot_username what is this thread discussing?` sends
  the question and available ancestors of that message via MTProto, excluding
  neighboring branches.
- `/context 50 summarize the discussion` sends the question and recent messages
  from the local SQLite cache for the current chat/topic, strictly before the command ID.

Ordinary messages are stored **locally only**: Telegram → server → SQLite. They
never trigger OpenAI, embeddings, moderation or Telethon fetch. Recent history is
not automatically added to mentions; the two context sources are not combined.
Private chats and channels are unsupported. AI requires text from an identifiable
human user; other bots, captions, anonymous sender_chat, service events and edits
cannot initiate a model call.

Bot username/ID come from getMe. Mentions and commands use Telegram entities with
UTF-16 offsets. A reply without a fresh mention does not invoke AI. Answers are
plain text in the original chat/topic, with the first part replying to the request.
Long answers are split at 4096 UTF-16 units; link previews are off and typing is
shown while processing. Model answers follow the user's language; local status
and error messages are in Russian.

### Requirements and credentials

Recommended server: **Ubuntu 24.04 LTS with Python 3.12**. Validation uses Python
3.12; Ubuntu 22.04's system Python 3.10 is insufficient. Provide DNS, CA certificates
and outbound HTTPS to Telegram/OpenAI; reply chains additionally need outbound
MTProto TCP to Telegram data centers. Run exactly one polling process per bot token.

1. Create a bot using `/newbot` in [@BotFather](https://t.me/BotFather).
2. Disable **Group Privacy** using `/setprivacy` → bot → **Disable**. Remove and
   re-add an existing bot to its group after changing this setting.
3. Allow the bot to send messages. Administrator permissions are not required for
   the ordinary group scenario. Privacy Disable enables delivery of ordinary
   messages; receiving an update does not send it to OpenAI.
4. Create an [OpenAI API key](https://platform.openai.com/api-keys) for a project
   with API billing and access to a Responses API model. A ChatGPT subscription
   does not replace API project setup. Configure the model through OPENAI_MODEL.
5. Optionally obtain API ID/hash under **API development tools** at
   [my.telegram.org](https://my.telegram.org) for reply-chain enrichment. These are
   Telegram application credentials. Runtime authenticates only as a bot, without
   phone/SMS prompts or a userbot. If both are absent, mentions and /context still
   work without enrichment; providing only one is a configuration error.

Never send keys or .env to Telegram or commit them to Git.

### Local rotating replies

Version 1.3.0 adds predefined reactions to Russian words and phrases, including
«ептиль», «ёптиль», «сижу», «привет», «норм» and «чекаво». These replies contain
profanity, never call OpenAI and are cached as outgoing bot messages. Matching is
case-insensitive and respects word boundaries. Each trigger uses a shuffled cycle
without repeats until exhausted; consecutive cycles avoid repeating the previous
reply. Rotation is shared across chats and resets on restart. Messages containing
mention/text_mention/bot_command Telegram entities bypass these reactions, preserving
explicit AI routing, validation, rate limits and context. Edits, bots and anonymous
senders do not trigger reactions.

### Local setup and startup

```bash
git clone https://github.com/megabars/telegram-chat-ai-bot.git
cd telegram-chat-ai-bot
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
cp .env.example .env
chmod 600 .env
```

Fill .env with TELEGRAM_BOT_TOKEN and OPENAI_API_KEY. Override the production state
paths for local development:

```dotenv
LOCAL_HISTORY_DB_PATH=./data/history.sqlite3
TELEGRAM_MTPROTO_SESSION_PATH=./data/telegram.session
```

Set Telegram API ID/hash together or leave both blank. Set REPLY_CONTEXT_ENABLED=false
to disable Telethon explicitly. Restrict groups through ALLOWED_CHAT_IDS, for example
`-1001234567890,-987654321`.

```bash
.venv/bin/python -m app.main --check-config
.venv/bin/python -m app.main
```

--check-config validates settings without network access; it does not verify keys
or model access. Ctrl+C closes handlers, SQLite and clients. Alternatively, after
configuring .env, run `PYTHON_BIN=python3.12 ./install.sh`.

### Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | `required / обязательно` | BotFather token |
| `OPENAI_API_KEY` | `required / обязательно` | OpenAI API key |
| `OPENAI_MODEL` | `gpt-6-luna` | Responses API model |
| `TELEGRAM_API_ID` | `empty / пусто` | API ID for optional reply chains |
| `TELEGRAM_API_HASH` | `empty / пусто` | API hash for the same Telegram application |
| `ALLOWED_CHAT_IDS` | `empty / пусто` | Comma-separated chat IDs; empty permits all groups |
| `MAX_INPUT_CHARS` | `12000` | Maximum current-question length |
| `MAX_CONCURRENT_REQUESTS` | `5` | OpenAI concurrency; separate same-sized Telethon fetch limit |
| `RATE_LIMIT_REQUESTS` | `10` | Requests per user ID within the sliding window |
| `RATE_LIMIT_PERIOD_SECONDS` | `60` | Rate-limit window in seconds |
| `OPENAI_TIMEOUT_SECONDS` | `60` | OpenAI timeout including queue time |
| `MAX_OUTPUT_TOKENS` | `2048` | Output-token budget including reasoning |
| `LOG_LEVEL` | `INFO` | DEBUG/INFO/WARNING/ERROR/CRITICAL; no conversation bodies |
| `REPLY_CONTEXT_ENABLED` | `true` | Enable optional MTProto enrichment |
| `REPLY_CONTEXT_MAX_DEPTH` | `30` | Maximum ancestors, excluding the question |
| `REPLY_CONTEXT_MAX_CHARS` | `30000` | Reply input budget: JSON, question and markers |
| `REPLY_CONTEXT_FETCH_TIMEOUT_SECONDS` | `10` | Fetch deadline including queue and peer resolution |
| `TELEGRAM_MTPROTO_SESSION_PATH` | `/var/lib/telegram-openai-bot/telegram.session` | Persistent sensitive bot session |
| `LOCAL_HISTORY_ENABLED` | `true` | Save group updates locally |
| `LOCAL_HISTORY_DB_PATH` | `/var/lib/telegram-openai-bot/history.sqlite3` | Persistent rolling cache |
| `LOCAL_HISTORY_MAX_MESSAGES` | `5000` | Rows retained per chat after cleanup |
| `LOCAL_HISTORY_CLEANUP_THRESHOLD` | `5100` | Cleanup to MAX_MESSAGES when this count is reached |
| `CONTEXT_COMMAND_DEFAULT_MESSAGES` | `50` | N when /context omits the count |
| `CONTEXT_COMMAND_MAX_MESSAGES` | `200` | Largest permitted N |
| `CONTEXT_MAX_CHARS` | `50000` | Recent input: JSON, question and marker |
| `CONTEXT_RESPECT_TOPICS` | `true` | Select only the current forum topic |

Booleans accept only true/false; numeric limits must be positive and timeouts finite.
Cleanup threshold must exceed max messages; default N cannot exceed maximum N.
History and Telethon session paths must differ. .env is read from the working
directory and environment variables take precedence. Use simple NAME=value lines,
without export, shell commands, $VAR substitution or multiline values: systemd
also reads this file as EnvironmentFile. Do not source it. The OpenAI endpoint is
fixed to https://api.openai.com/v1; OPENAI_BASE_URL does not override it.

### /context and the rolling cache

```text
/context 20 summarize the discussion
/context@bot_username 50 what decisions did we make?
/context 100 who suggested PostgreSQL?
/context what are they discussing?
/context 50
/context
```

Without N, the default is 50. Without a question, the bot uses a Russian summary
prompt asking for main topics, decisions and open questions. N is an **upper bound
on previous messages**, not a required count: if only 17 exist, all 17 are used.
An empty history produces a local response without AI. Zero, negative, noninteger
N or N>200 produce local errors. A single nonnumeric argument (/context abc) is
interpreted as invalid N; a question without N must contain multiple words.
Commands addressed to another bot do not execute here.

SQL filters the current chat and, by default, the current forum topic, including
General (ID 1). Ordinary supergroup reply-thread IDs are not forum topics.
CONTEXT_RESPECT_TOPICS=false allows combining topics within the same chat. The
command itself is excluded with message_id < command_id. The result becomes an
immutable snapshot before the model request; subsequent messages/edits cannot
change its input. History is chronological and the current question is last.
Commands are cached and can appear in future snapshots. Successful outgoing bot
answers, including split parts, are saved manually and carry the assistant role.
Other messages use the user role. The model receives author names, timestamps and
text, without internal chat/message/database IDs.

CONTEXT_MAX_CHARS includes JSON, markers and the current question; application
instructions are outside this budget. Trimming favors recent messages and adds
`[Earlier messages omitted due to context size limit]`. The question is never
shortened for history; its own limit is MAX_INPUT_CHARS. If the budget cannot fit
history, only the question remains. No hidden second summarization call is made.

The cache stores delivered group/supergroup text, captions or compact media
placeholders. Files are not downloaded; no OCR/STT is performed. Service events
are skipped, edits update rows without AI, and the (chat_id,message_id) primary
key prevents duplicate rows. Other bots' messages can be stored only when Telegram
delivers them. Telethon does not backfill the cache. Telegram deletions are not
reliably synchronized: **this is a rolling cache, not a perfect Telegram archive**;
old records eventually disappear through rolling cleanup.

The storage limit covers the entire chat, including every topic: 0…5099 rows
require no cleanup; at 5100 a short SQL transaction retains the latest 5000.
Other chats are unaffected; there is no whole-table cap. The service uses one
async connection, WAL, busy_timeout=5000, synchronous=NORMAL and indexed queries.
Counters load once at startup. There is no per-message whole-table COUNT or
per-cleanup VACUUM. File size may remain unchanged after DELETE because free pages
are reused. There is no TTL; inactive chat records remain until rolling replacement
or manual removal. LOCAL_HISTORY_ENABLED=false stops saving and makes /context
respond locally; existing data is retained and mentions/reply chains still work.

### Reply-chain context

Only a reply with a fresh mention fetches exact ancestor messages. A singleton
Telethon client runs RPC-only with receive_updates=False and catch_up=False;
there is no getHistory, userbot or second listener. Fetches use exact message IDs;
peer resolution may also request access-hash metadata. The session must belong
to the current bot ID; it stores an authorization key and peer metadata, **not
conversation bodies**. Defaults: 30 ancestors and a 10-second total fetch deadline.
Chat/topic boundaries, cycles, deleted parents, timeout and FloodWait end traversal;
the available portion is used with an unavailable-history marker. With no context,
only the question is sent. Closest ancestors take priority within the 30000-character
budget, with `[Earlier messages omitted due to context limit]` when truncated.
A reply without mention triggers no fetch. Recent cached messages are not added.

### Ubuntu installation with systemd

Run these commands on Ubuntu 24.04. Creating .env from the template is for **first
installation only**; never replace an existing configured .env.

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv ca-certificates git rsync

git clone https://github.com/megabars/telegram-chat-ai-bot.git
cd telegram-chat-ai-bot

id telegrambot >/dev/null 2>&1 || sudo useradd --system --user-group \
  --home-dir /opt/telegram-openai-bot --no-create-home \
  --shell /usr/sbin/nologin telegrambot
sudo install -d -o root -g telegrambot -m 750 /opt/telegram-openai-bot
sudo rsync -a --chmod=D755,F644 --exclude='.git' --exclude='.venv' --exclude='.env' --exclude='.env.*' \
  --exclude='.state' --exclude='data' --exclude='*.session*' --exclude='*.sqlite*' \
  --exclude='__pycache__' --exclude='.pytest_cache' --exclude='.ruff_cache' \
  --exclude='._*' \
  ./ /opt/telegram-openai-bot/
sudo install -m 644 .env.example /opt/telegram-openai-bot/.env.example
sudo install -o telegrambot -g telegrambot -m 600 \
  .env.example /opt/telegram-openai-bot/.env
sudo nano /opt/telegram-openai-bot/.env
sudo chmod 755 /opt/telegram-openai-bot/install.sh
sudo bash -c 'cd /opt/telegram-openai-bot && PYTHON_BIN=python3.12 ./install.sh --systemd'
sudo systemctl enable --now telegram-openai-bot
sudo systemctl status telegram-openai-bot --no-pager
sudo journalctl -u telegram-openai-bot -n 40 --no-pager
```

The installer installs requirements.lock, checks dependencies/configuration,
installs the unit and reloads systemd. Starting the service is a separate step.
Code/venv belong to root and are readable by the **telegrambot** runtime user.
.env belongs to telegrambot with mode 600. StateDirectory creates the persistent
/var/lib/telegram-openai-bot directory with mode 700; DB/session/sidecars use 600.
The unit keeps ProtectSystem=strict, NoNewPrivileges, an empty CapabilityBoundingSet,
private tmp/devices, Restart=on-failure and graceful SIGTERM with TimeoutStopSec=45.
Do not run the service as root to enable SQLite. Custom state paths need write
permission in the unit. Runtime does not write into /opt.

### Updating, operations and clearing the cache

Update the server checkout while retaining .env, session, DB and venv. Commands
exclude state/data/SQLite/session and secret env files and do not use --delete.
Add new settings to the existing .env as needed, preserving credentials.

```bash
cd ~/telegram-chat-ai-bot
git pull --ff-only origin main
sudo systemctl stop telegram-openai-bot
sudo rsync -a --chmod=D755,F644 --exclude='.git' --exclude='.venv' --exclude='.env' --exclude='.env.*' \
  --exclude='.state' --exclude='data' --exclude='*.session*' --exclude='*.sqlite*' \
  --exclude='__pycache__' --exclude='.pytest_cache' --exclude='.ruff_cache' \
  --exclude='._*' \
  ./ /opt/telegram-openai-bot/
sudo install -m 644 .env.example /opt/telegram-openai-bot/.env.example
sudo chmod 755 /opt/telegram-openai-bot/install.sh
sudo bash -c 'cd /opt/telegram-openai-bot && PYTHON_BIN=python3.12 ./install.sh --systemd'
sudo systemctl start telegram-openai-bot
sudo systemctl status telegram-openai-bot --no-pager
sudo journalctl -u telegram-openai-bot -n 40 --no-pager
```

```bash
sudo systemctl restart telegram-openai-bot
sudo systemctl stop telegram-openai-bot
sudo systemctl start telegram-openai-bot
sudo journalctl -u telegram-openai-bot -f
sudo sh -c 'du -h /var/lib/telegram-openai-bot/history.sqlite3*'
sudo stat -c '%a %U:%G %n' /var/lib/telegram-openai-bot \
  /var/lib/telegram-openai-bot/history.sqlite3 \
  /var/lib/telegram-openai-bot/telegram.session
```

Expected journal entries include Local history started mode=sqlite_wal, Bot started,
and Reply context enabled=True when MTProto is configured and available. Logs
contain IDs, requested/selected counts, chars, durations and cleanup counts, **not
conversation bodies or keys**. DEBUG does not enable SDK/SQL body logging. Do not
enable external HTTP tracing. The session file may be absent if enrichment is
turned off or has not authenticated.

To clear only the local cache completely:

```bash
sudo systemctl stop telegram-openai-bot
sudo rm -f /var/lib/telegram-openai-bot/history.sqlite3 \
  /var/lib/telegram-openai-bot/history.sqlite3-wal \
  /var/lib/telegram-openai-bot/history.sqlite3-shm \
  /var/lib/telegram-openai-bot/history.sqlite3-journal
sudo systemctl start telegram-openai-bot
```

Do not delete telegram.session: it is separate authorization state. Clearing SQLite
does not remove anything from Telegram; new updates refill the cache. No automatic
backup system is included; the cache is not a critical permanent archive.

At every startup the bot calls deleteWebhook(drop_pending_updates=True), disabling
the webhook and discarding old pending updates after downtime. This avoids unexpected
old paid requests but creates gaps in the cache. Restart preserves the DB and bot
session and resets the in-memory rate limiter. Cache deduplication does not guarantee
exactly-once AI responses following a crash.

### Limits and troubleshooting

| Symptom | Check |
| --- | --- |
| No answer | group/supergroup, actual mention or /context entity, privacy Disable, re-add bot, send permission, ALLOWED_CHAT_IDS |
| Empty /context | New updates delivered since startup; older Telegram history is not backfilled |
| Missing reply chain | Both API ID/hash, MTProto connectivity/session owner; journal fallback reason |
| SQLite PermissionError | State path outside /opt, telegrambot owner, directory 700 / DB 600, StateDirectory |
| Telegram polling conflict | Another process using this token or a webhook; run one instance |
| OpenAI 401/403/404 | Key, project/model access and configured model availability |
| OpenAI 429 | API quota/billing/provider rate limit; SDK requests are not retried |
| timeout/network | DNS, CA, HTTPS/MTProto and configured deadlines |
| empty_output | Reasoning may consume the output budget; check MAX_OUTPUT_TOKENS |
| start-limit-hit | Fix the startup error, then systemctl reset-failed/start |

Defaults allow 10 requests/60 seconds **per user ID across all groups**; attempts
that reach AI count even if the API fails. OpenAI concurrency is 5; active Telegram
handlers are capped at concurrency×4. The AI deadline includes queue time. Model
failures receive a safe local response. Telegram FloodWait allows one send retry
with a wait up to 30 seconds, without another model request. Cancelling a request
does not guarantee zero charges if the provider has started processing it.

### Verification and project layout

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/ruff check app tests
.venv/bin/ruff format --check app tests
.venv/bin/python -m compileall -q app
.venv/bin/python -m pip check
bash -n install.sh
```

**245 offline tests** pass on Python 3.12. Tests block network
access and use temporary SQLite databases only. Coverage includes 100 ordinary
updates → 0 AI/fetch, /context → 1 AI, snapshots, topics, roles/injection, edits,
outgoing messages, per-chat 5100→5000 cleanup, concurrency, lifecycle, deadlines,
UTF-16 and safe logs. Tests do not verify real credentials or API project access.
GitHub Actions runs the suite and code checks on push/pull_request without keys.

```text
app/config.py                    .env validation and defaults
app/main.py                      startup/polling/shutdown
app/handlers/messages.py         cache writes and explicit AI triggers
app/services/openai_service.py   one stateless Responses call
app/services/telegram_history.py exact ancestor MTProto fetch
app/services/local_history.py    async SQLite rolling cache
app/utils/                       mentions, commands, topics, rate limit, formatting
telegram-openai-bot.service       hardened non-root systemd
install.sh                       reproducible production-lock installation
requirements.lock                pinned production dependencies
requirements-dev.txt             test/lint dependencies
tests/                           offline regression suite
```

The model cannot run SQL, shell commands or tools. History is untrusted context
with a separate higher-priority instruction; this reduces injection risk without
promising absolute model protection. Credentials come from environment/.env only.
Git excludes env files, keys, sessions, DBs/sidecars, data/state, venv, logs, caches
and archives. Inspect staged files before publication, not just the working directory.
store=False disables Responses state retrieval; **it is not a Zero Data Retention
promise**. Consult [OpenAI data controls](https://developers.openai.com/api/docs/guides/your-data)
for retention of explicitly submitted questions/context. Restrict ALLOWED_CHAT_IDS
and state/journal access in production.

## Official references / Официальные ссылки

- [Telegram privacy mode](https://core.telegram.org/bots/features#privacy-mode)
- [Telegram bot message delivery](https://core.telegram.org/bots/faq#what-messages-will-my-bot-get)
- [aiogram polling](https://docs.aiogram.dev/en/latest/dispatcher/dispatcher.html)
- [OpenAI Responses API](https://developers.openai.com/api/docs/guides/migrate-to-responses)
- [Telethon client API](https://docs.telethon.dev/en/stable/modules/client.html)
- [aiosqlite API](https://aiosqlite.omnilib.dev/en/stable/api.html)
- [SQLite PRAGMA](https://sqlite.org/pragma.html)
