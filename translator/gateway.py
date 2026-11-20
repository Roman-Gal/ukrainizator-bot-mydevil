"""Route two independent Telegram bots through one ngrok endpoint."""

from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response


def create_gateway(*, client=None, planner_port=8080, translator_port=8081):
    @asynccontextmanager
    async def lifespan(app):
        app.state.http = client or httpx.AsyncClient(timeout=10, trust_env=False)
        try:
            yield
        finally:
            if client is None:
                await app.state.http.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    async def forward(request, port, path):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 1_048_576:
                return JSONResponse({"error": "too_large"}, status_code=413)
        headers = {
            key: request.headers[key]
            for key in ("content-type", "x-telegram-bot-api-secret-token")
            if key in request.headers
        }
        try:
            upstream = await request.app.state.http.request(
                request.method,
                f"http://127.0.0.1:{port}{path}",
                content=bytes(body),
                headers=headers,
            )
        except httpx.HTTPError:
            return JSONResponse({"error": "upstream_unavailable"}, status_code=502)
        return Response(
            upstream.content,
            status_code=upstream.status_code,
            headers={
                "content-type": upstream.headers.get("content-type", "application/json"),
                "cache-control": "no-store",
            },
        )

    @app.get("/gateway/health")
    async def health():
        return {"ok": True, "service": "telegram-shared-gateway"}

    @app.get("/health")
    async def planner_health(request: Request):
        return await forward(request, planner_port, "/health")

    @app.get("/translator/health")
    async def translator_health(request: Request):
        return await forward(request, translator_port, "/health")

    @app.post("/telegram/webhook")
    async def planner_webhook(request: Request):
        return await forward(request, planner_port, "/telegram/webhook")

    @app.post("/translator/telegram/webhook")
    async def translator_webhook(request: Request):
        return await forward(request, translator_port, "/telegram/webhook")

    return app


if __name__ == "__main__":
    uvicorn.run(create_gateway(), host="127.0.0.1", port=8082, access_log=False)
