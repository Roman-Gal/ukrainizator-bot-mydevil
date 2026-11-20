import asyncio
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from translator.app import create_app
from translator.bot import PREFIX, Bot
from translator.config import Settings
from translator.jokes import ANTIRUSSIAN_JOKES
from translator.language import Language, detect_language
from translator.providers import ApiError, HttpTranslator, Translation
from translator.store import Store
from translator.telegram import Telegram, split_text

BASE = Settings(
    token="123:test_token",
    secret="test_secret_123456789",
    public_url="https://example.test",
    database=":memory:",
    provider="http",
    api_url="https://translate.test/translate",
)
RU = "Привет, сегодня мы обсуждаем очень важные новости."
UK = "Це українське повідомлення, перекладати його не потрібно."
EN = "Hello everyone, how are you doing today?"


def update(text=EN, **fields):
    return {
        "update_id": 10,
        "message": {
            "message_id": 42,
            "chat": {"id": -100, "type": "supergroup"},
            "from": {"id": 7, "is_bot": False},
            "text": text,
            **fields,
        },
    }


class LanguageTests(unittest.TestCase):
    def test_ukrainian(self):
        self.assertTrue(detect_language(UK).ukrainian)

    def test_russian(self):
        self.assertTrue(detect_language(RU).clear_russian())

    def test_english(self):
        self.assertEqual(detect_language(EN).code, "en")

    def test_no_jokes_for_short_or_ambiguous_text(self):
        for text in [
            "ок",
            "так",
            "😊 123",
            "https://example.com",
            "Привет, а як твої справи сьогодні?",
        ]:
            with self.subTest(text=text):
                self.assertFalse(detect_language(text).clear_russian())

    def test_low_confidence_no_joke(self):
        self.assertFalse(Language("ru", 0.6, 100).clear_russian())

    def test_reported_russian_phrase_with_eighteen_letters(self):
        language = detect_language("А что такое случилось?")
        self.assertEqual(language.letters, 18)
        self.assertEqual(language.code, "ru")
        self.assertTrue(language.clear_russian())

    def test_short_russian_messages(self):
        for text in ["Привет", "Привет, как дела?", "как дела?", "спасибо", "Я пишу по русски"]:
            with self.subTest(text=text):
                self.assertTrue(detect_language(text).clear_russian())

    def test_other_cyrillic_languages_are_not_russian(self):
        for text in [
            "Това е български текст.",
            "Ово је порука на српском језику.",
            "Ова е порака на македонски јазик.",
        ]:
            with self.subTest(text=text):
                self.assertFalse(detect_language(text).clear_russian())

    def test_ukrainian_greetings_and_names_do_not_trigger(self):
        for text in [
            "Привіт",
            "Привіт, як справи?",
            "Доброго дня! Як справи?",
            "дякую",
            "мама",
            "Анна",
            "ок",
            "так",
            "до завтра",
        ]:
            with self.subTest(text=text):
                self.assertFalse(detect_language(text).clear_russian())

    def test_twenty_distinct_jokes(self):
        self.assertEqual(len(set(ANTIRUSSIAN_JOKES)), 20)

    def test_long_emoji_split_roundtrip(self):
        text = "🌻" * 4500 + "переклад" * 1000
        parts = split_text(text)
        self.assertEqual("".join(parts), text)
        self.assertTrue(all(len(p.encode("utf-16-le")) // 2 <= 4000 for p in parts))


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(":memory:")
        self.telegram = AsyncMock()
        self.translator = AsyncMock()
        self.translator.translate.return_value = Translation("Привіт усім, як ваші справи?", "en")
        self.bot = Bot(BASE, self.store, self.telegram, self.translator, "our_bot")

    async def asyncTearDown(self):
        self.store.close()

    async def test_english_translation_reply_in_topic(self):
        parts = await self.bot.prepare(update(message_thread_id=123))
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0]["message_thread_id"], 123)
        self.assertEqual(parts[0]["reply_parameters"]["message_id"], 42)
        self.assertIn(PREFIX, parts[0]["text"])
        self.translator.translate.assert_awaited_once_with(EN)

    async def test_ukrainian_skips_provider(self):
        self.assertEqual(await self.bot.prepare(update(UK)), [])
        self.translator.translate.assert_not_awaited()

    async def test_bots_stickers_commands_and_links_ignored(self):
        for item in [
            update(**{"from": {"is_bot": True}}),
            update("/other"),
            update("https://example.com"),
            update("😊 123"),
            update("", sticker={"file_id": "x"}),
            update(via_bot={"id": 8}),
            update(is_automatic_forward=True),
        ]:
            self.assertEqual(await self.bot.prepare(item), [])
        self.translator.translate.assert_not_awaited()

    async def test_caption(self):
        item = update("", caption=EN)
        self.assertEqual(len(await self.bot.prepare(item)), 1)
        self.translator.translate.assert_awaited_once_with(EN)

    async def test_joke_and_persistent_cooldown(self):
        self.bot.settings = replace(BASE, joke_cooldown=300)
        self.translator.translate.return_value = Translation(
            "Привіт, сьогодні ми обговорюємо важливі новини.", "ru"
        )
        first = (await self.bot.prepare(update(RU)))[0]["text"]
        second = (await self.bot.prepare(update(RU)))[0]["text"]
        self.assertTrue(any(joke in first for joke in ANTIRUSSIAN_JOKES))
        self.assertFalse(any(joke in second for joke in ANTIRUSSIAN_JOKES))
        third = (await self.bot.prepare(update(RU, **{"from": {"id": 8}})))[0]["text"]
        self.assertTrue(any(joke in third for joke in ANTIRUSSIAN_JOKES))

    async def test_reported_short_phrase_and_repeated_messages_receive_reactions(self):
        text = "А что такое случилось?"
        self.translator.translate.return_value = Translation("Що відбулося?", "ru")
        with patch("translator.bot.random.choice", return_value="[reaction]"):
            for _ in range(2):
                result = await self.bot.prepare(update(text))
                self.assertEqual(len(result), 1)
                self.assertIn("[reaction]", result[0]["text"])
                self.assertIn("Що відбулося?", result[0]["text"])

    async def test_short_greeting_receives_a_reaction(self):
        self.translator.translate.return_value = Translation("Привіт", "ru")
        with patch("translator.bot.random.choice", return_value="[reaction]"):
            result = await self.bot.prepare(update("Привет"))
        self.assertIn("[reaction]", result[0]["text"])

    async def test_jokes_can_be_disabled(self):
        self.bot.settings = replace(BASE, jokes=False)
        self.translator.translate.return_value = Translation("Переклад", "ru")
        result = (await self.bot.prepare(update(RU)))[0]["text"]
        self.assertFalse(any(joke in result for joke in ANTIRUSSIAN_JOKES))

    async def test_provider_disagreement_suppresses_joke(self):
        result = (await self.bot.prepare(update(RU)))[0]["text"]
        self.assertFalse(any(joke in result for joke in ANTIRUSSIAN_JOKES))

    async def test_provider_detected_ukrainian_skipped(self):
        self.translator.translate.return_value = Translation("Український текст", "uk")
        self.assertEqual(await self.bot.prepare(update(EN)), [])

    async def test_allowlist(self):
        self.bot.settings = replace(BASE, allowed_chats=frozenset({123}))
        self.assertEqual(await self.bot.prepare(update()), [])
        self.translator.translate.assert_not_awaited()

    async def test_manual_reply_and_other_bot_commands(self):
        self.assertEqual(await self.bot.prepare(update("/translate@another_bot hello")), [])
        result = await self.bot.prepare(update("/translate@our_bot", reply_to_message={"text": EN}))
        self.assertEqual(len(result), 1)
        self.translator.translate.assert_awaited_once_with(EN)

    async def test_manual_multiline_text(self):
        result = await self.bot.prepare(update("/translate\n" + EN))
        self.assertEqual(len(result), 1)
        self.translator.translate.assert_awaited_once_with(EN)

    async def test_disabled_mode_is_honest(self):
        self.bot.settings = replace(BASE, provider="disabled")
        self.assertEqual(await self.bot.prepare(update()), [])
        result = await self.bot.prepare(update("/translate hello"))
        self.assertIn("ще не підключено", result[0]["text"])
        self.translator.translate.assert_not_awaited()

    async def test_channel_own_output_and_automatic_forward_ignored(self):
        item = update(PREFIX + "переклад")
        item["channel_post"] = item.pop("message")
        self.assertEqual(await self.bot.prepare(item), [])

    async def test_channel_translation_without_reply(self):
        item = update()
        item["channel_post"] = item.pop("message")
        item["channel_post"]["chat"]["type"] = "channel"
        result = await self.bot.prepare(item)
        self.assertNotIn("reply_parameters", result[0])

    async def test_duplicate_update_and_saved_delivery_checkpoint(self):
        self.store.enqueue(update())
        self.store.enqueue(update())
        self.translator.translate.return_value = Translation("а" * 6000, "en")
        self.telegram.send.side_effect = [True, ApiError("telegram", 429, retry_after=30)]
        with patch("translator.bot.asyncio.sleep", new_callable=AsyncMock):
            await self.bot.process_once()
        row = self.store.db.execute("SELECT * FROM jobs").fetchone()
        self.assertEqual(row["sent"], 1)
        self.assertEqual(row["status"], "pending")
        self.assertGreater(row["ready_at"], 0)
        self.store.db.execute("UPDATE jobs SET ready_at=0")
        self.store.db.commit()
        self.telegram.send.side_effect = None
        with patch("translator.bot.asyncio.sleep", new_callable=AsyncMock):
            await self.bot.process_once()
        self.translator.translate.assert_awaited_once()
        self.assertEqual(self.telegram.send.await_count, 3)
        self.assertIsNone(self.store.next())
        self.store.enqueue(update())
        self.assertIsNone(self.store.next())

    async def test_permanent_failure_does_not_spin(self):
        self.translator.translate.side_effect = ApiError("translation", 403, retryable=False)
        self.store.enqueue(update())
        await self.bot.process_once()
        self.assertIsNone(self.store.next())
        row = self.store.db.execute("SELECT * FROM jobs").fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["body"], "{}")


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_libretranslate_readiness_with_prefix(self):
        def handle(request):
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.url.path, "/api/languages")
            return httpx.Response(
                200,
                json=[
                    {"code": "en", "targets": ["uk", "ru"]},
                    {"code": "ru", "targets": ["uk", "en"]},
                    {"code": "uk", "targets": ["en", "ru"]},
                ],
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            await HttpTranslator(
                replace(
                    BASE, provider="libretranslate", api_url="https://translate.test/api/translate"
                ),
                client,
            ).check_ready()

    async def test_libretranslate_missing_routes_rejected(self):
        for languages in [
            [],
            {},
            [{"code": "uk"}],
            [{"code": "uk"}, {"code": "en", "targets": ["uk"]}, {"code": "ru", "targets": None}],
        ]:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda _: httpx.Response(200, json=languages))
            ) as client:
                with self.assertRaises(ApiError) as error:
                    await HttpTranslator(
                        replace(BASE, provider="libretranslate"), client
                    ).check_ready()
                self.assertFalse(error.exception.retryable)

    async def test_libretranslate_unavailable(self):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(503))
        ) as client:
            with self.assertRaises(ApiError):
                await HttpTranslator(replace(BASE, provider="libretranslate"), client).check_ready()

    async def test_libretranslate_no_key_required(self):
        def handle(request):
            self.assertNotIn("api_key", json.loads(request.content))
            self.assertNotIn("Authorization", request.headers)
            return httpx.Response(
                200, json={"translatedText": "Привіт", "detectedLanguage": {"language": "en"}}
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            result = await HttpTranslator(
                replace(BASE, provider="libretranslate"), client
            ).translate(EN)
        self.assertEqual(result.text, "Привіт")

    async def test_normalized_contract(self):
        def handle(request):
            self.assertEqual(
                json.loads(request.content),
                {"text": EN, "source_language": "auto", "target_language": "uk"},
            )
            self.assertEqual(request.headers["Authorization"], "Bearer secret")
            return httpx.Response(
                200, json={"translated_text": "Привіт", "source_language": "EN-US"}
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            result = await HttpTranslator(replace(BASE, api_key="secret"), client).translate(EN)
        self.assertEqual(result, Translation("Привіт", "en"))

    async def test_libretranslate_contract(self):
        def handle(request):
            data = json.loads(request.content)
            self.assertEqual(data["target"], "uk")
            self.assertEqual(data["q"], EN)
            self.assertEqual(data["api_key"], "key")
            return httpx.Response(
                200,
                json={
                    "translatedText": "Привіт",
                    "detectedLanguage": {"language": "en", "confidence": 99},
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            result = await HttpTranslator(
                replace(BASE, provider="libretranslate", api_key="key"), client
            ).translate(EN)
        self.assertEqual(result.source_language, "en")

    async def test_invalid_provider_responses(self):
        for response in [
            httpx.Response(200, json=[]),
            httpx.Response(200, json={}),
            httpx.Response(200, json={"translated_text": ""}),
            httpx.Response(200, text="not json"),
        ]:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda _: response)
            ) as client:
                with self.assertRaises(ApiError):
                    await HttpTranslator(BASE, client).translate(EN)

    async def test_timeout_and_retryable_status(self):
        def timeout(request):
            raise httpx.ReadTimeout("secret must not leak", request=request)

        for handler in [timeout, lambda _: httpx.Response(503), lambda _: httpx.Response(429)]:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with self.assertRaises(ApiError) as error:
                    await HttpTranslator(BASE, client).translate(EN)
                self.assertTrue(error.exception.retryable)
                self.assertNotIn("secret", str(error.exception))

    async def test_telegram_registration_and_retry_after(self):
        calls = []

        def handle(request):
            calls.append((request.url.path.rsplit("/", 1)[-1], json.loads(request.content)))
            return httpx.Response(200, json={"ok": True, "result": {"username": "our_bot"}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            self.assertEqual(await Telegram(BASE.token, client).register(BASE), "our_bot")
        webhook = calls[-1][1]
        self.assertEqual(webhook["secret_token"], BASE.secret)
        self.assertNotIn(BASE.token, webhook["url"])
        self.assertFalse(webhook["drop_pending_updates"])
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 17}}
                )
            )
        ) as client:
            with self.assertRaises(ApiError) as error:
                await Telegram(BASE.token, client).send({"text": "hi"})
            self.assertEqual(error.exception.retry_after, 17)


class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.context = TestClient(create_app(BASE, register=False, run_worker=False))
        self.client = self.context.__enter__()
        self.headers = {"X-Telegram-Bot-Api-Secret-Token": BASE.secret}

    def tearDown(self):
        self.context.__exit__(None, None, None)

    def test_secret(self):
        self.assertEqual(self.client.post("/telegram/webhook", json=update()).status_code, 403)
        self.assertEqual(
            self.client.post("/telegram/webhook", json=update(), headers=self.headers).status_code,
            200,
        )

    def test_invalid_bodies(self):
        for body in [b"not json", b"[]", b"{}", b'{"update_id": true}', b'{"update_id": "1"}']:
            self.assertEqual(
                self.client.post(
                    "/telegram/webhook", content=body, headers=self.headers
                ).status_code,
                400,
            )

    def test_large_body(self):
        self.assertEqual(
            self.client.post(
                "/telegram/webhook", content=b"x" * 1_048_577, headers=self.headers
            ).status_code,
            413,
        )

    def test_health_and_dedup(self):
        self.assertTrue(self.client.get("/health").json()["ok"])
        for _ in range(2):
            self.client.post("/telegram/webhook", json=update(), headers=self.headers)
        rows = self.client.app.state.store.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        self.assertEqual(rows, 1)


class WorkerIntegrationTests(unittest.TestCase):
    def test_libretranslate_failure_prevents_telegram_registration(self):
        calls = []

        def handle(request):
            calls.append(request.url.path)
            return httpx.Response(200, json=[{"code": "en", "targets": []}])

        http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        try:
            with self.assertRaises(ApiError):
                with TestClient(create_app(replace(BASE, provider="libretranslate"), client=http)):
                    pass
            self.assertEqual(calls, ["/languages"])
        finally:
            asyncio.run(http.aclose())

    def test_webhook_queue_worker_provider_and_telegram(self):
        sent = []
        calls = []

        def handle(request):
            method = request.url.path.rsplit("/", 1)[-1]
            data = json.loads(request.content)
            calls.append(method)
            if request.url.host == "translate.test":
                self.assertEqual(data["text"], RU)
                return httpx.Response(
                    200,
                    json={
                        "translated_text": "Привіт, сьогодні обговорюємо важливі новини.",
                        "source_language": "ru",
                    },
                )
            if method == "getMe":
                return httpx.Response(200, json={"ok": True, "result": {"username": "our_bot"}})
            if method == "sendMessage":
                sent.append(data)
            return httpx.Response(200, json={"ok": True, "result": True})

        http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        try:
            with TestClient(create_app(BASE, client=http)) as client:
                response = client.post(
                    "/telegram/webhook",
                    json=update(RU),
                    headers={"X-Telegram-Bot-Api-Secret-Token": BASE.secret},
                )
                self.assertEqual(response.status_code, 200)
                deadline = time.monotonic() + 5
                while not sent and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertEqual(len(sent), 1)
                self.assertIn("Привіт, сьогодні", sent[0]["text"])
                self.assertTrue(any(joke in sent[0]["text"] for joke in ANTIRUSSIAN_JOKES))
                self.assertIn("setWebhook", calls)
        finally:
            asyncio.run(http.aclose())


class PersistenceAndConfigTests(unittest.TestCase):
    def test_restart_keeps_queue_and_cooldown(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "queue.sqlite")
            store = Store(path)
            store.enqueue(update())
            self.assertTrue(store.claim_joke(-100, 7, 300))
            store.close()
            store = Store(path)
            self.assertEqual(store.next()["id"], 10)
            self.assertFalse(store.claim_joke(-100, 7, 300))
            store.close()

    def test_settings_validation(self):
        BASE.validate()
        for changes in [
            {"token": ""},
            {"secret": ""},
            {"public_url": "http://x"},
            {"public_url": "https://x/path"},
            {"provider": "deepl"},
            {"api_url": "http://external.example/"},
            {"port": 0},
        ]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(BASE, **changes).validate()


if __name__ == "__main__":
    unittest.main()
