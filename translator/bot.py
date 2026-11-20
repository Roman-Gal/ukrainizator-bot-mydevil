import asyncio
import json
import logging
import random

from .jokes import ANTIRUSSIAN_JOKES
from .language import detect_language
from .providers import ApiError, Translator
from .telegram import split_text

log = logging.getLogger(__name__)
PREFIX = "🇺🇦 Переклад українською:\n"
HELP = (
    "🇺🇦 Перекладаю іншомовні повідомлення та підписи до медіа українською.\n"
    "Українські повідомлення, стікери, команди й повідомлення ботів пропускаю.\n\n"
    "/translate текст — переклад вручну. Можна відповісти цією командою на повідомлення.\n"
    "/status — стан підключення перекладача.\n\n"
    "Для впевнено визначеної російської додаю випадковий мовний жарт. "
    "Однозначні короткі фрази теж обробляю; неоднозначні — без жартів."
)


class Bot:
    def __init__(self, settings, store, telegram, translator: Translator, username=""):
        self.settings = settings
        self.store = store
        self.telegram = telegram
        self.translator = translator
        self.username = username.lower()

    async def prepare(self, update):
        message = update.get("message") or update.get("channel_post")
        if not isinstance(message, dict):
            return []
        chat = message.get("chat", {})
        chat_id = chat.get("id")
        if not isinstance(chat_id, int) or not isinstance(message.get("message_id"), int):
            return []
        if self.settings.allowed_chats and chat_id not in self.settings.allowed_chats:
            return []
        if (
            message.get("from", {}).get("is_bot")
            or message.get("via_bot")
            or message.get("is_automatic_forward")
        ):
            return []
        text = message.get("text") or message.get("caption") or ""
        if not isinstance(text, str) or not text.strip() or text.startswith(PREFIX):
            return []
        force = False
        if text.startswith("/"):
            command_parts = text.split(maxsplit=1)
            first = command_parts[0]
            argument = command_parts[1] if len(command_parts) == 2 else ""
            command, _, target = first.partition("@")
            if target and target.lower() != self.username:
                return []
            if command in {"/start", "/help"}:
                return self.replies(message, HELP)
            if command == "/status":
                state = (
                    "API ще не підключено. Автопереклад вимкнено."
                    if self.settings.provider == "disabled"
                    else "API налаштовано. Доступність перевіряється під час перекладу."
                )
                return self.replies(message, "🇺🇦 Бот працює. " + state)
            if command != "/translate":
                return []
            original = message.get("reply_to_message", {})
            text = argument.strip() or original.get("text") or original.get("caption") or ""
            if not text:
                return self.replies(
                    message,
                    "Додайте текст після /translate або дайте команду у відповідь на повідомлення.",
                )
            force = True
        language = await asyncio.to_thread(detect_language, text)
        if not force and (language.ukrainian or language.letters < 2):
            return []
        if self.settings.provider == "disabled":
            if force or chat.get("type") == "private":
                return self.replies(
                    message, "Перекладач ще не підключено. Потрібно налаштувати API у .env."
                )
            return []
        translation = await self.translator.translate(text)
        if not force and (
            translation.source_language == "uk" or translation.text.strip() == text.strip()
        ):
            return []
        joke = ""
        # Provider disagreement suppresses jokes. Local confidence is mandatory.
        source_ok = translation.source_language in {"ru", "unknown"}
        if (
            self.settings.jokes
            and source_ok
            and language.clear_russian(
                self.settings.russian_confidence, self.settings.russian_min_letters
            )
        ):
            sender = (
                message.get("from", {}).get("id")
                or message.get("sender_chat", {}).get("id")
                or chat_id
            )
            if self.store.claim_joke(chat_id, sender, self.settings.joke_cooldown):
                joke = random.choice(ANTIRUSSIAN_JOKES) + "\n\n"
            log.info("Russian message detected; reaction=%s", "included" if joke else "cooldown")
        return self.replies(message, PREFIX + joke + translation.text)

    @staticmethod
    def replies(message, text):
        payloads = []
        for part in split_text(text):
            payload = {
                "chat_id": message["chat"]["id"],
                "text": part,
                "link_preview_options": {"is_disabled": True},
            }
            if message["chat"].get("type") != "channel":
                payload["reply_parameters"] = {
                    "message_id": message["message_id"],
                    "allow_sending_without_reply": True,
                }
            if "message_thread_id" in message:
                payload["message_thread_id"] = message["message_thread_id"]
            payloads.append(payload)
        return payloads

    async def process_once(self):
        job = self.store.next()
        if job is None:
            return False
        try:
            if job["plan"] is None:
                payloads = await self.prepare(json.loads(job["body"]))
                self.store.plan(job["id"], payloads)
            else:
                payloads = json.loads(job["plan"])
            for index in range(job["sent"], len(payloads)):
                await self.telegram.send(payloads[index])
                self.store.sent(job["id"], index + 1)
                # Space out sends; Telegram 429 handling enforces further limits.
                await asyncio.sleep(1.1)
            self.store.finish(job["id"])
        except ApiError as error:
            attempts = job["attempts"] + 1
            if error.retryable and attempts < 6:
                self.store.retry(job["id"], attempts, max(error.retry_after, min(300, 2**attempts)))
            else:
                self.store.finish(job["id"], "failed")
            log.warning("Update %s: %s; attempt %s", job["id"], error, attempts)
        except Exception as error:
            # Never log arbitrary exception strings: SDKs may include credentials.
            log.error("Update %s: internal error %s", job["id"], type(error).__name__)
            self.store.finish(job["id"], "failed")
        return True

    async def run(self):
        while True:
            if not await self.process_once():
                await asyncio.sleep(0.3)
