"""Live smoke test for the configured translator, without contacting Telegram."""

import asyncio
import sys

import httpx

from .config import Settings
from .providers import ApiError, HttpTranslator


async def check():
    settings = Settings.from_env()
    if settings.provider == "disabled":
        print("Перекладач вимкнений у .env.", file=sys.stderr)
        return 1
    async with httpx.AsyncClient(timeout=settings.timeout) as client:
        translator = HttpTranslator(settings, client)
        try:
            await translator.check_ready()
            for text in (
                "Hello everyone, how are you doing today?",
                "Привет, сегодня мы обсуждаем очень важные новости.",
                "Dzień dobry, jak się dzisiaj czujesz?",
            ):
                result = await translator.translate(text)
                print(f"{result.source_language}: {text}\nuk: {result.text}\n")
        except ApiError as error:
            print(str(error), file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(check()))
