import json
import unittest
from dataclasses import replace

import httpx

from translator.config import Settings
from translator.providers import ApiError, HttpTranslator


class DeepLTests(unittest.IsolatedAsyncioTestCase):
    settings = Settings(
        token="123:test_token",
        secret="test-only-webhook-secret-1234567890",
        public_url="https://bot.example.com",
        provider="deepl",
        api_url="https://api-free.deepl.com/v2/translate",
        api_key="test-only-api-key:fx",
    )

    async def test_native_contract_and_detected_language(self):
        def handler(request):
            self.assertEqual(
                request.headers["Authorization"], "DeepL-Auth-Key test-only-api-key:fx"
            )
            self.assertEqual(json.loads(request.content), {"text": ["Hello"], "target_lang": "UK"})
            return httpx.Response(
                200, json={"translations": [{"text": "Привіт", "detected_source_language": "EN"}]}
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await HttpTranslator(self.settings, client).translate("Hello")
        self.assertEqual(result.text, "Привіт")
        self.assertEqual(result.source_language, "en")

    async def test_readiness_checks_usage_without_sending_text(self):
        def handler(request):
            self.assertEqual(request.method, "GET")
            self.assertEqual(str(request.url), "https://api-free.deepl.com/v2/usage")
            self.assertEqual(request.content, b"")
            return httpx.Response(200, json={"character_count": 10, "character_limit": 100})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await HttpTranslator(self.settings, client).check_ready()

    async def test_quota_error_is_not_retried_and_response_body_does_not_leak(self):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(456, text="private details"))
        ) as client:
            with self.assertRaises(ApiError) as caught:
                await HttpTranslator(self.settings, client).translate("Hello")
        self.assertFalse(caught.exception.retryable)
        self.assertNotIn("private details", str(caught.exception))
        self.assertNotIn(self.settings.api_key, str(caught.exception))

    def test_configuration_rejects_non_deepl_hosts_and_empty_key(self):
        self.settings.validate()
        for change in [{"api_url": "https://other.example.com/v2/translate"}, {"api_key": ""}]:
            with self.assertRaises(ValueError):
                replace(self.settings, **change).validate()
