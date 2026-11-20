from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx

from .config import Settings


class ApiError(Exception):
    """Safe to log: never includes credentials, URLs or message bodies."""

    def __init__(self, service, status=None, *, retryable=True, retry_after=0):
        super().__init__(f"{service} failure (status={status or 'network/response'})")
        self.retryable = retryable
        self.retry_after = retry_after


async def post_json(client, url, payload, *, service, headers=None):
    try:
        response = await client.post(url, json=payload, headers=headers)
    except httpx.RequestError:
        raise ApiError(service) from None
    if response.is_error:
        raise ApiError(
            service,
            response.status_code,
            retryable=response.status_code == 429 or response.status_code >= 500,
        )
    try:
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError
        return data
    except (ValueError, TypeError):
        raise ApiError(service, retryable=False) from None


@dataclass(frozen=True)
class Translation:
    text: str
    source_language: str = "unknown"


class Translator(Protocol):
    async def translate(self, text: str) -> Translation: ...


class HttpTranslator:
    """Normalized HTTP contract, or LibreTranslate's native request/response."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client

    async def check_ready(self):
        """Check Ukrainian support before installing the Telegram webhook."""
        if self.settings.provider == "deepl":
            url = urlsplit(self.settings.api_url)
            try:
                response = await self.client.get(
                    urlunsplit((url.scheme, url.netloc, "/v2/usage", "", "")),
                    headers={"Authorization": f"DeepL-Auth-Key {self.settings.api_key}"},
                )
                response.raise_for_status()
                usage = response.json()
            except httpx.HTTPStatusError as error:
                raise ApiError("DeepL readiness", error.response.status_code) from None
            except (httpx.RequestError, ValueError):
                raise ApiError("DeepL readiness") from None
            if not isinstance(usage, dict):
                raise ApiError("DeepL usage response", retryable=False)
            return
        if self.settings.provider != "libretranslate":
            return
        url = urlsplit(self.settings.api_url)
        languages_url = urlunsplit(
            (url.scheme, url.netloc, url.path.rsplit("/", 1)[0] + "/languages", url.query, "")
        )
        try:
            response = await self.client.get(languages_url)
            response.raise_for_status()
            languages = response.json()
        except httpx.HTTPStatusError as error:
            raise ApiError("LibreTranslate languages", error.response.status_code) from None
        except (httpx.RequestError, ValueError):
            raise ApiError(
                "LibreTranslate languages; start scripts/start-libretranslate.ps1"
            ) from None
        if not isinstance(languages, list):
            raise ApiError("LibreTranslate languages response", retryable=False)
        by_code = {
            item.get("code"): item
            for item in languages
            if isinstance(item, dict) and isinstance(item.get("code"), str)
        }
        if "uk" not in by_code:
            raise ApiError("LibreTranslate Ukrainian model missing; load en,uk,ru", retryable=False)
        for source in ("en", "ru"):
            targets = by_code.get(source, {}).get("targets")
            if not isinstance(targets, list) or "uk" not in targets:
                raise ApiError(
                    f"LibreTranslate {source}->uk route missing; load en,uk,ru", retryable=False
                )

    async def translate(self, text):
        s = self.settings
        if s.provider == "disabled":
            raise ApiError("translation not configured", retryable=False)
        if s.provider == "deepl":
            payload = {"text": [text], "target_lang": "UK"}
            headers = {"Authorization": f"DeepL-Auth-Key {s.api_key}"}
        elif s.provider == "libretranslate":
            payload = {"q": text, "source": "auto", "target": "uk", "format": "text"}
            if s.api_key:
                payload["api_key"] = s.api_key
            headers = {}
        else:
            payload = {"text": text, "source_language": "auto", "target_language": "uk"}
            headers = {"Authorization": f"Bearer {s.api_key}"} if s.api_key else {}
        data = await post_json(
            self.client, s.api_url, payload, service="translation", headers=headers
        )
        if s.provider == "deepl":
            translations = data.get("translations")
            if (
                not isinstance(translations, list)
                or len(translations) != 1
                or not isinstance(translations[0], dict)
            ):
                raise ApiError("DeepL response", retryable=False)
            translated = translations[0].get("text")
            source = translations[0].get("detected_source_language", "unknown")
        elif s.provider == "libretranslate":
            translated = data.get("translatedText")
            detected = data.get("detectedLanguage", {})
            source = (
                detected.get("language", "unknown") if isinstance(detected, dict) else "unknown"
            )
        else:
            translated = data.get("translated_text")
            source = data.get("source_language", "unknown")
        if not isinstance(translated, str) or not translated.strip() or len(translated) > 100_000:
            raise ApiError("translation", retryable=False)
        if not isinstance(source, str):
            source = "unknown"
        return Translation(translated.strip(), source.lower().split("-")[0])
