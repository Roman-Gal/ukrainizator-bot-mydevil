import httpx

from .providers import ApiError


class Telegram:
    def __init__(self, token, client):
        self.base = f"https://api.telegram.org/bot{token}"
        self.client = client

    async def call(self, method, payload=None):
        try:
            response = await self.client.post(f"{self.base}/{method}", json=payload or {})
            data = response.json()
        except (httpx.RequestError, ValueError):
            raise ApiError("telegram") from None
        if not isinstance(data, dict) or not data.get("ok"):
            code = (
                data.get("error_code", response.status_code)
                if isinstance(data, dict)
                else response.status_code
            )
            params = data.get("parameters", {}) if isinstance(data, dict) else {}
            retry_after = params.get("retry_after", 0) if isinstance(params, dict) else 0
            if not isinstance(retry_after, (int, float)):
                retry_after = 0
            raise ApiError(
                "telegram", code, retryable=code == 429 or code >= 500, retry_after=retry_after
            )
        return data["result"]

    async def register(self, settings):
        me = await self.call("getMe")
        await self.call(
            "setMyCommands",
            {
                "commands": [
                    {"command": "help", "description": "Як працює перекладач"},
                    {"command": "status", "description": "Стан перекладача"},
                    {"command": "translate", "description": "Перекласти текст або відповідь"},
                ]
            },
        )
        await self.call(
            "setWebhook",
            {
                "url": settings.public_url.rstrip("/") + settings.webhook_path,
                "secret_token": settings.secret,
                "allowed_updates": ["message", "channel_post"],
                "drop_pending_updates": False,
            },
        )
        return me["username"]

    async def send(self, payload):
        return await self.call("sendMessage", payload)


def split_text(text, limit=4000):
    """Keep supplementary characters within the UTF-16 length budget too."""
    parts = []
    current = []
    units = 0
    for char in text:
        size = 2 if ord(char) > 0xFFFF else 1
        if units + size > limit:
            parts.append("".join(current))
            current, units = [], 0
        current.append(char)
        units += size
    if current:
        parts.append("".join(current))
    return parts
