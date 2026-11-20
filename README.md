# Ukrainizator Bot — MyDevil 🇺🇦

Серверна версія українського перекладача: Python 3.12, FastAPI, PostgreSQL і DeepL API. Працює на MyDevil FreeBSD без ngrok та локальних LibreTranslate/Argos/ONNX/CTranslate2.

Локальна версія з ngrok, LibreTranslate та Windows-панеллю збережена окремо: [ukrainizator-bot](https://github.com/Roman-Gal/ukrainizator-bot), початкова ревізія `2720478c40a8ddc66d2f29f5d2064901f2c74cee`. Початкова Node.js-реалізація також збережена у [telegram-ukrainian-translator](https://github.com/Roman-Gal/telegram-ukrainian-translator).

## Конфігурація

У приватному `.env` (0600) налаштуйте Telegram-токен, окремий `WEBHOOK_SECRET`, PostgreSQL `DATABASE_URL`, `PUBLIC_BASE_URL` та DeepL-ключ. Приклад без справжніх credentials — `.env.example`.

```sh
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m translator
```

`psycopg` використовує системну libpq; готові Linux binary wheels на FreeBSD не потрібні. Для Lingua FreeBSD використовує зафіксовану чисту Python-версію 1.4.2, інші ОС — 2.2.0.

Зовнішній webhook: `/translator/telegram/webhook` через WorkLook. Внутрішній FastAPI endpoint: `/telegram/webhook`, тільки `127.0.0.1:18381`. Health: `/health`. Перевірка DeepL-ключа використовує `/v2/usage`, переклад — `/v2/translate`, target language `UK`. Текст повідомлень передається DeepL.

PostgreSQL зберігає inbox, checkpoint доставки, дедуплікацію та cooldown. Після обробки текст/план очищуються; залишаються ID і статуси. Session advisory lock забороняє два workers на одній БД. Конфігурація production вимагає PostgreSQL; SQLite fixtures залишено тільки для сумісності локальних тестів.

## Тести й запуск

`python -m unittest discover -s tests -v` використовує mocks без реальних Telegram/DeepL запитів. З `TRANSLATOR_TEST_DATABASE_URL` сховище тестується на відокремленій PostgreSQL, із власними тестовими схемами. Не використовувати production-БД для тестової змінної.

На хості один FreeBSD daemon supervisor керує одним worker, cron відновлює його після рестарту. Резервні копії — pg_dump із приватним .pgpass. Жодних токенів, приватних .env або баз у репозиторії немає.

Один бот-токен має одну активну адресу вебхука. Старі локальні процеси слід зупинити до остаточного знімка БД та переключення; `drop_pending_updates=false` зберігає очікувані Telegram updates.
