import asyncio
import hmac
import json
import logging
from contextlib import asynccontextmanager, suppress

import httpx
from fastapi import FastAPI, HTTPException, Request

from .bot import Bot
from .providers import HttpTranslator
from .store import Store
from .telegram import Telegram


def create_app(settings, *, client=None, register=True, run_worker=True):
    @asynccontextmanager
    async def lifespan(app):
        settings.validate()
        store = Store(settings.database)
        http = client or httpx.AsyncClient(timeout=settings.timeout, follow_redirects=False)
        worker = None
        try:
            translator = HttpTranslator(settings, http)
            await translator.check_ready()
            telegram = Telegram(settings.token, http)
            username = await telegram.register(settings) if register else "test_bot"
            bot = Bot(settings, store, telegram, translator, username)
            app.state.store = store
            app.state.bot = bot
            if run_worker:
                worker = asyncio.create_task(bot.run())
            logging.getLogger(__name__).info("Translator ready; provider=%s", settings.provider)
            yield
        finally:
            if worker:
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker
            store.close()
            if client is None:
                await http.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def health():
        return {"ok": True, "translation_configured": settings.provider != "disabled"}

    @app.post("/telegram/webhook")
    async def webhook(request: Request):
        secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(secret.encode(), settings.secret.encode()):
            raise HTTPException(403, "Forbidden")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 1_048_576:
                raise HTTPException(413, "Update too large")
        try:
            update = json.loads(body)
            if not isinstance(update, dict) or type(update.get("update_id")) is not int:
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, "Invalid update") from None
        app.state.store.enqueue(update)
        return {"ok": True}

    return app
