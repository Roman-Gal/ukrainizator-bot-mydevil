import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from translator.config import Settings
from translator.gateway import create_gateway
from translator.shared_tunnel import connect, endpoint_config
from translator.telegram import Telegram


class GatewayTests(unittest.TestCase):
    def test_webhooks_go_to_separate_bots_with_body_and_secret_intact(self):
        seen = []

        def handle(request):
            seen.append(
                (
                    request.url.port,
                    request.url.path,
                    request.content,
                    request.headers.get("x-telegram-bot-api-secret-token"),
                )
            )
            return httpx.Response(
                403 if request.headers.get("x-telegram-bot-api-secret-token") == "wrong" else 200,
                json={"ok": True},
            )

        http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        try:
            with TestClient(create_gateway(client=http)) as client:
                for path, secret in [
                    ("/telegram/webhook", "planner-secret"),
                    ("/translator/telegram/webhook", "translator-secret"),
                    ("/translator/telegram/webhook", "wrong"),
                ]:
                    response = client.post(
                        path,
                        content=b'{"update_id":1}',
                        headers={
                            "content-type": "application/json",
                            "x-telegram-bot-api-secret-token": secret,
                        },
                    )
                    self.assertEqual(response.status_code, 403 if secret == "wrong" else 200)
            self.assertEqual([item[0] for item in seen], [8080, 8081, 8081])
            self.assertTrue(
                all(
                    item[1] == "/telegram/webhook" and item[2] == b'{"update_id":1}'
                    for item in seen
                )
            )
            self.assertEqual(
                [item[3] for item in seen], ["planner-secret", "translator-secret", "wrong"]
            )
        finally:
            asyncio.run(http.aclose())

    def test_health_routes(self):
        http = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"port": request.url.port})
            )
        )
        try:
            with TestClient(create_gateway(client=http)) as client:
                self.assertEqual(client.get("/health").json()["port"], 8080)
                self.assertEqual(client.get("/translator/health").json()["port"], 8081)
                self.assertEqual(
                    client.get("/gateway/health").json()["service"], "telegram-shared-gateway"
                )
        finally:
            asyncio.run(http.aclose())

    def test_upstream_failure_is_retryable(self):
        def handle(request):
            raise httpx.ConnectError("private details", request=request)

        http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        try:
            with TestClient(create_gateway(client=http)) as client:
                response = client.post("/translator/telegram/webhook", json={"update_id": 1})
                self.assertEqual(response.status_code, 502)
                self.assertNotIn("private", response.text)
        finally:
            asyncio.run(http.aclose())

    def test_oversized_requests_are_not_forwarded(self):
        def handle(request):
            self.fail("Oversized request reached an upstream")

        http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        try:
            with TestClient(create_gateway(client=http)) as client:
                self.assertEqual(
                    client.post(
                        "/translator/telegram/webhook", content=b"x" * 1_048_577
                    ).status_code,
                    413,
                )
        finally:
            asyncio.run(http.aclose())

    def test_unknown_paths_are_not_proxied(self):
        with TestClient(create_gateway()) as client:
            self.assertEqual(client.get("/http://example.com").status_code, 404)
            self.assertEqual(client.post("/other").status_code, 404)


class SharedTunnelTests(unittest.TestCase):
    def test_failed_migration_does_not_replace_or_delete_existing_tunnel(self):
        calls = []
        settings = replace(
            Settings(),
            public_url="https://old.ngrok.app",
            webhook_path="/translator/telegram/webhook",
        )

        def handle(request):
            calls.append((request.method, request.url.path))
            if request.url.path == "/gateway/health":
                return httpx.Response(200, json={"service": "telegram-shared-gateway"})
            if request.url.path == "/api/endpoints":
                return httpx.Response(
                    200,
                    json={
                        "endpoints": [
                            {
                                "name": "planner",
                                "url": settings.public_url,
                                "upstream": {"url": "http://localhost:8080"},
                            }
                        ]
                    },
                )
            if request.url.path == "/api/tunnels":
                return httpx.Response(429, json={"error": "account limit"})
            return httpx.Response(200, json={"ok": True})

        http = httpx.Client(transport=httpx.MockTransport(handle))
        with tempfile.TemporaryDirectory() as folder:
            backup = Path(folder) / "previous-endpoint.json"
            with (
                patch("translator.shared_tunnel.Settings.from_env", return_value=settings),
                patch("translator.shared_tunnel.httpx.Client", return_value=http),
                patch("translator.shared_tunnel.Path", return_value=backup),
            ):
                with self.assertRaises(httpx.HTTPStatusError):
                    connect()
        self.assertFalse(any(method in {"DELETE", "PUT"} for method, _ in calls))
        self.assertIn(("POST", "/api/tunnels"), calls)

    def test_endpoint_preserves_url_and_inspection(self):
        old = {
            "name": "command_line",
            "url": "https://example.ngrok.app",
            "upstream": {"url": "http://localhost:8080"},
            "inspect": True,
            "pooling_enabled": False,
            "uri": "/api/endpoints/command_line",
            "metrics": {},
        }
        config = endpoint_config(old, "http://127.0.0.1:8082")
        self.assertEqual(config["url"], old["url"])
        self.assertEqual(config["upstream"]["url"], "http://127.0.0.1:8082")
        self.assertEqual(old["upstream"]["url"], "http://localhost:8080")
        self.assertTrue(config["inspect"])
        self.assertFalse(config["pooling_enabled"])
        self.assertNotIn("uri", config)
        self.assertNotIn("metrics", config)

    def test_unknown_policy_is_not_discarded(self):
        with self.assertRaises(ValueError):
            endpoint_config({"traffic_policy": "existing YAML policy"}, "http://127.0.0.1:8082")


class RegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_webhook_path(self):
        calls = []

        def handle(request):
            calls.append((request.url.path, json.loads(request.content)))
            return httpx.Response(200, json={"ok": True, "result": {"username": "our_bot"}})

        settings = replace(
            Settings(),
            token="123:test_token",
            public_url="https://example.ngrok.app",
            webhook_path="/translator/telegram/webhook",
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            await Telegram(settings.token, client).register(settings)
        self.assertEqual(
            calls[-1][1]["url"], "https://example.ngrok.app/translator/telegram/webhook"
        )
