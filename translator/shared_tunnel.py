"""Attach the existing ngrok endpoint to the tested local gateway."""

import asyncio
import json
import secrets
import sys
from pathlib import Path

import httpx
from dotenv import dotenv_values, set_key

from .config import Settings
from .providers import ApiError
from .telegram import Telegram


def endpoint_config(endpoint, upstream):
    policy = endpoint.get("traffic_policy")
    if policy and not isinstance(policy, dict):
        raise ValueError(
            "Existing ngrok traffic policy requires manual preservation; endpoint unchanged."
        )
    result = {"name": endpoint["name"], "url": endpoint["url"], "upstream": {"url": upstream}}
    for key in ("inspect", "pooling_enabled", "traffic_policy"):
        if endpoint.get(key) is not None:
            if key != "traffic_policy" or policy:
                result[key] = endpoint[key]
    return result


async def register_shared_webhooks(settings, public_url):
    """Register each bot with its own secret; never discard pending updates."""
    planner_path = Path(__file__).resolve().parents[2] / "telegram-task-planner" / ".env"
    planner = dotenv_values(planner_path)
    if not planner.get("TELEGRAM_BOT_TOKEN") or not planner.get("WEBHOOK_SECRET"):
        raise ValueError("The planner credentials are missing; no webhook changed.")
    if planner["TELEGRAM_BOT_TOKEN"] == settings.token:
        raise ValueError("The two bots must use distinct tokens.")
    async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
        await Telegram(settings.token, client).call(
            "setWebhook",
            {
                "url": public_url + settings.webhook_path,
                "secret_token": settings.secret,
                "allowed_updates": ["message", "channel_post"],
                "drop_pending_updates": False,
            },
        )
        await Telegram(planner["TELEGRAM_BOT_TOKEN"], client).call(
            "setWebhook",
            {
                "url": public_url + "/telegram/webhook",
                "secret_token": planner["WEBHOOK_SECRET"],
                "allowed_updates": ["message", "callback_query", "my_chat_member", "chat_member"],
                "max_connections": 10,
                "drop_pending_updates": False,
            },
        )
    set_key(Path(__file__).resolve().parents[1] / ".env", "PUBLIC_BASE_URL", public_url)
    set_key(planner_path, "PUBLIC_BASE_URL", public_url)


def connect():
    settings = Settings.from_env()
    if settings.port != 8081 or settings.webhook_path != "/translator/telegram/webhook":
        raise ValueError(
            "Shared mode requires PORT=8081 and PUBLIC_WEBHOOK_PATH=/translator/telegram/webhook."
        )
    with httpx.Client(timeout=20, trust_env=False) as client:
        health = client.get("http://127.0.0.1:8082/gateway/health")
        health.raise_for_status()
        if health.json().get("service") != "telegram-shared-gateway":
            raise ValueError("The shared gateway is not running on port 8082.")
        for path in ("/health", "/translator/health"):
            client.get("http://127.0.0.1:8082" + path).raise_for_status()
        response = client.get("http://127.0.0.1:4040/api/endpoints")
        response.raise_for_status()
        endpoint = next(
            (
                e
                for e in response.json().get("endpoints", [])
                if e.get("url", "").rstrip("/") == settings.public_url
            ),
            None,
        )
        if endpoint is None:
            raise ValueError(
                "PUBLIC_BASE_URL does not match an endpoint of the existing ngrok agent."
            )
        if endpoint.get("upstream", {}).get("url") == "http://127.0.0.1:8082":
            print("Shared tunnel is already connected.")
            return
        original = endpoint_config(endpoint, endpoint["upstream"]["url"])
        backup = Path("data/ngrok-endpoint-before-shared.json")
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_text(json.dumps(original, indent=2), encoding="utf-8")
        # Never PUT an existing endpoint: ngrok stops it before validating its
        # replacement, and the free plan may reject its former generated URL.
        # Allocate a fresh automatic URL first, leaving the old tunnel alive.
        created = client.post(
            "http://127.0.0.1:4040/api/tunnels",
            json={
                "name": "shared_gateway_" + secrets.token_hex(4),
                "proto": "http",
                "addr": "http://127.0.0.1:8082",
                "schemes": ["https"],
            },
        )
        created.raise_for_status()
        public_url = created.json()["public_url"].rstrip("/")
        for path in ("/health", "/translator/health"):
            client.get(
                public_url + path, headers={"ngrok-skip-browser-warning": "1"}
            ).raise_for_status()
        asyncio.run(register_shared_webhooks(settings, public_url))
        print(f"Shared tunnel connected: {public_url}. Existing endpoint retained.")


if __name__ == "__main__":
    try:
        connect()
    except (httpx.HTTPError, ApiError, ValueError, KeyError) as error:
        # HTTP errors can include URLs. Log only the error class.
        print(f"Shared tunnel configuration failed ({type(error).__name__}).", file=sys.stderr)
        sys.exit(1)
