import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    token: str = field(default="", repr=False)
    secret: str = field(default="", repr=False)
    public_url: str = ""
    webhook_path: str = "/telegram/webhook"
    host: str = "127.0.0.1"
    port: int = 8081
    database: str = "./data/translator.sqlite"
    allowed_chats: frozenset[int] = frozenset()
    provider: str = "libretranslate"
    api_url: str = "http://127.0.0.1:5000/translate"
    api_key: str = field(default="", repr=False)
    timeout: float = 60
    jokes: bool = True
    russian_confidence: float = 0.75
    russian_min_letters: int = 3
    joke_cooldown: int = 0

    @classmethod
    def from_env(cls):
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
        database = os.getenv('DATABASE_URL', '')
        if not database.startswith(('postgresql://', 'postgres://')):
            raise ValueError('Hosting edition requires PostgreSQL DATABASE_URL.')
        return cls(
            token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            secret=os.getenv("WEBHOOK_SECRET", "").strip(),
            public_url=os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/"),
            webhook_path=os.getenv("PUBLIC_WEBHOOK_PATH", "/telegram/webhook").strip(),
            host=os.getenv("HOST", "127.0.0.1"),
            port=int(os.getenv("PORT", "8081")),
            database=database,
            allowed_chats=frozenset(
                int(v.strip()) for v in os.getenv("ALLOWED_CHAT_IDS", "").split(",") if v.strip()
            ),
            provider=os.getenv("TRANSLATION_PROVIDER", "libretranslate").strip().lower(),
            api_url=os.getenv("TRANSLATION_API_URL", "http://127.0.0.1:5000/translate").strip(),
            api_key=os.getenv("TRANSLATION_API_KEY", "").strip(),
            timeout=float(os.getenv("API_TIMEOUT_SECONDS", "60")),
            jokes=os.getenv("RUSSIAN_JOKES_ENABLED", "true").lower() == "true",
            russian_confidence=float(os.getenv("RUSSIAN_CONFIDENCE", "0.75")),
            russian_min_letters=int(os.getenv("RUSSIAN_MIN_LETTERS", "3")),
            joke_cooldown=int(os.getenv("JOKE_COOLDOWN_SECONDS", "0")),
        )

    def validate(self):
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", self.token):
            raise ValueError("Заповніть TELEGRAM_BOT_TOKEN у .env токеном нового бота.")
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", self.secret):
            raise ValueError("WEBHOOK_SECRET: потрібно 16–256 символів A-Z, a-z, 0-9, _ або -.")
        public = urlsplit(self.public_url)
        if (
            public.scheme != "https"
            or not public.netloc
            or public.path not in ("", "/")
            or public.query
            or public.fragment
            or public.username
        ):
            raise ValueError("PUBLIC_BASE_URL має бути HTTPS origin ngrok без шляху.")
        if not 1 <= self.port <= 65535 or not 1 <= self.timeout <= 120:
            raise ValueError("Неправильний PORT або API_TIMEOUT_SECONDS (1–120).")
        if not re.fullmatch(r"/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+", self.webhook_path):
            raise ValueError("PUBLIC_WEBHOOK_PATH має бути абсолютним URL-шляхом без query.")
        if self.provider not in {"disabled", "http", "libretranslate", "deepl"}:
            raise ValueError("TRANSLATION_PROVIDER: disabled, http, libretranslate або deepl.")
        if self.provider != "disabled":
            api = urlsplit(self.api_url)
            if (
                api.scheme not in {"https", "http"}
                or not api.netloc
                or api.username
                or api.fragment
            ):
                raise ValueError("Задайте TRANSLATION_API_URL — повний URL endpoint перекладу.")
            if api.scheme == "http" and api.hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError("API потребує HTTPS (HTTP дозволений тільки для localhost).")
            if self.provider == "deepl" and (
                not self.api_key
                or api.scheme != "https"
                or api.hostname not in {"api-free.deepl.com", "api.deepl.com"}
                or api.path != "/v2/translate"
                or api.query
                or api.port not in {None, 443}
            ):
                raise ValueError(
                    "DeepL потребує API-ключа й офіційного HTTPS endpoint /v2/translate."
                )
        if (
            not 0.5 <= self.russian_confidence <= 1
            or self.russian_min_letters < 3
            or self.joke_cooldown < 0
        ):
            raise ValueError("Поріг жартів: confidence 0.5–1, min letters >=3, cooldown >=0.")
