import logging
import sys

import uvicorn

from .app import create_app
from .config import Settings


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # HTTP request URLs contain the Telegram token. Do not log them.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        settings = Settings.from_env()
        settings.validate()
    except (ValueError, TypeError):
        print(
            "Перевірте .env: токен нового бота, секрет, HTTPS origin, провайдер та числові параметри.",
            file=sys.stderr,
        )
        return 1
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
