import json
import os
import re
import secrets
import shutil
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from dotenv import dotenv_values, set_key

from .runtime import Processes, operation_lock

LABELS = {
    "libre": "Перекладач",
    "gateway": "Спільний шлюз",
    "ngrok": "Інтернет-з’єднання",
    "planner": "Бот-планнер",
    "translator": "Бот-українізатор",
}
PROBES = {
    "libre": (5000, "/languages"),
    "gateway": (8082, "/gateway/health"),
    "planner": (8080, "/health"),
    "translator": (8081, "/health"),
}
SHARED_PATH = "/translator/telegram/webhook"


class StartError(Exception):
    """Only fixed, credential-free messages may reach the UI."""


def exclusive_operation(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        try:
            with operation_lock(self.data / "operation.lock"):
                self.reload()
                return method(self, *args, **kwargs)
        except BlockingIOError:
            raise StartError("Інший запуск або зупинка ще триває. Зачекайте й повторіть.") from None

    return wrapped


def gateway_upstream(value):
    try:
        url = urlsplit(value if "://" in value else "http://" + value)
        return (
            url.scheme == "http"
            and url.hostname in {"localhost", "127.0.0.1", "::1"}
            and url.port == 8082
        )
    except (ValueError, TypeError):
        return False


def public_origin(value):
    url = urlsplit(value)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.path not in ("", "/")
        or url.query
        or url.fragment
    ):
        raise StartError("ngrok повернув некоректну адресу. Спробуйте запуск ще раз.")
    return value.rstrip("/")


def write_environment(translator_path, planner_path, url):
    """Preserve secrets/comments and roll back both files if a write fails."""
    url = public_origin(url)
    originals = {
        path: path.read_text(encoding="utf-8-sig") for path in (translator_path, planner_path)
    }
    try:
        for path in originals:
            set_key(path, "PUBLIC_BASE_URL", url)
        set_key(translator_path, "PUBLIC_WEBHOOK_PATH", SHARED_PATH)
    except Exception:
        for path, original in originals.items():
            temp = path.with_name(path.name + ".restore")
            temp.write_text(original, encoding="utf-8")
            temp.replace(path)
        raise StartError(
            "Не вдалося зберегти налаштування. Перевірте доступ до файлів .env."
        ) from None


class Manager:
    def __init__(self, root=None, *, client=None, processes=None, notify=None, wait_timeout=120):
        self.root = Path(root or Path(__file__).resolve().parents[1]).resolve()
        self.planner = self.root.parent / "telegram-task-planner"
        self.data = self.root / "data" / "control-panel"
        self.data.mkdir(parents=True, exist_ok=True)
        self.http = client or httpx.Client(timeout=15, trust_env=False, follow_redirects=False)
        self.processes = processes or Processes()
        self.notify = notify or (lambda event: None)
        self.wait_timeout = wait_timeout
        self.metadata_path = self.data / "runtime.json"
        self.state = {"processes": {}}
        self.reload()

    def reload(self):
        try:
            state = json.loads(self.metadata_path.read_text(encoding="utf-8"))
            if not isinstance(state.get("processes"), dict):
                raise ValueError
            self.state = state
        except (OSError, ValueError, AttributeError):
            pass

    def emit(self, name, state, detail=""):
        self.notify({"service": name, "state": state, "detail": detail})

    def save(self):
        temp = self.metadata_path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        temp.replace(self.metadata_path)

    def json_get(self, url):
        try:
            response = self.http.get(url, timeout=2)
            return response.json() if response.is_success else None
        except (httpx.HTTPError, ValueError):
            return None

    def healthy(self, name):
        port, path = PROBES[name]
        data = self.json_get(f"http://127.0.0.1:{port}{path}")
        if name == "libre":
            return isinstance(data, list) and {"en", "ru", "uk"} <= {
                v.get("code") for v in data if isinstance(v, dict)
            }
        if not isinstance(data, dict):
            return False
        if name == "gateway":
            return data.get("service") == "telegram-shared-gateway" and data.get("ok") is True
        if name == "planner":
            return data.get("status") == "ok"
        return data.get("ok") is True and data.get("translation_configured") is True

    def configs(self):
        paths = (self.root / ".env", self.planner / ".env")
        if not all(path.is_file() for path in paths):
            raise StartError(
                "Потрібні налаштовані .env перекладача та сусіднього telegram-task-planner."
            )
        translator, planner = (dotenv_values(path) for path in paths)
        for values, label in ((translator, "українізатора"), (planner, "планнера")):
            if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", values.get("TELEGRAM_BOT_TOKEN") or ""):
                raise StartError(f"Вкажіть токен бота-{label} у його .env.")
            if not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", values.get("WEBHOOK_SECRET") or ""):
                raise StartError(f"Перевірте WEBHOOK_SECRET бота-{label} у його .env.")
        if translator["TELEGRAM_BOT_TOKEN"] == planner["TELEGRAM_BOT_TOKEN"]:
            raise StartError("Для двох ботів потрібні різні токени Telegram.")
        if translator.get("PORT", "8081") != "8081" or planner.get("PORT", "8080") != "8080":
            raise StartError("Панель очікує PORT=8081 для українізатора та PORT=8080 для планнера.")
        if (
            translator.get("TRANSLATION_PROVIDER") != "libretranslate"
            or translator.get("TRANSLATION_API_URL", "").rstrip("/")
            != "http://127.0.0.1:5000/translate"
        ):
            raise StartError(
                "Для панелі виберіть локальний LibreTranslate: http://127.0.0.1:5000/translate."
            )
        return translator, planner

    def discover_tools(self):
        python = self.root / ".venv" / "Scripts" / "python.exe"
        libre = self.root / ".tools" / "libretranslate" / ".venv" / "Scripts" / "python.exe"
        node = shutil.which("node")
        ngrok = next(
            (
                str(p)
                for p in (
                    self.root / ".tools/ngrok/ngrok.exe",
                    self.planner / ".tools/ngrok/ngrok.exe",
                )
                if p.is_file()
            ),
            None,
        ) or shutil.which("ngrok")
        if not python.is_file() or not libre.is_file() or not node or not ngrok:
            raise StartError(
                "Не знайдені Python-середовища, Node.js або ngrok. Перевірте встановлення за README."
            )
        candidates = [self.root / ".tools/ngrok/ngrok.yml", self.planner / ".tools/ngrok/ngrok.yml"]
        if os.getenv("LOCALAPPDATA"):
            candidates.append(Path(os.environ["LOCALAPPDATA"]) / "ngrok/ngrok.yml")
        config = next((str(path) for path in candidates if path.is_file()), None)
        if not config and not os.getenv("NGROK_AUTHTOKEN"):
            raise StartError(
                "Немає авторизації ngrok. Один раз налаштуйте його authtoken за README."
            )
        return {
            "python": str(python),
            "libre_python": str(libre),
            "node": node,
            "ngrok": ngrok,
            "ngrok_config": config,
        }

    def environment(self, public_url=None):
        env = dict(os.environ)
        # A GUI may have inherited another project's dotenv settings.
        for key in (
            "TELEGRAM_BOT_TOKEN",
            "WEBHOOK_SECRET",
            "PUBLIC_BASE_URL",
            "PUBLIC_WEBHOOK_PATH",
            "HOST",
            "PORT",
            "DATABASE_PATH",
            "OWNER_USER_ID",
            "ALLOWED_CHAT_IDS",
            "TIMEZONE",
        ):
            env.pop(key, None)
        for key in list(env):
            if key.startswith(("CODEX_", "BROWSER_USE_", "NODE_REPL_")):
                env.pop(key, None)
        for key in dotenv_values(self.root / ".env"):
            env.pop(key, None)
        for key in dotenv_values(self.planner / ".env"):
            env.pop(key, None)
        env["PYTHONUTF8"] = "1"
        if public_url:
            env["PUBLIC_BASE_URL"] = public_url
        return env

    def ensure(self, name, args, cwd, *, module=None, script=None, executable=None, url=None):
        port = PROBES[name][0]
        record = self.state["processes"].get(name)
        listener = self.processes.listener(port)
        if listener:
            if not self.processes.matches(
                listener, cwd=cwd, module=module, script=script, executable=executable
            ):
                raise StartError(
                    f"Порт {port} зайнятий іншою програмою. Звільніть його й повторіть запуск."
                )
            self.state["processes"][name] = self.processes.record(listener)
            self.save()
        elif not record or not self.processes.alive(record):
            self.emit(name, "starting", "Запускаємо…")
            record = self.processes.spawn(
                args, cwd, self.environment(url), self.data / f"{name}.log"
            )
            self.state["processes"][name] = record
            self.save()
        deadline = time.monotonic() + self.wait_timeout
        while not self.healthy(name):
            record = self.state["processes"][name]
            if not self.processes.alive(record):
                raise StartError(
                    f"{LABELS[name]} завершив роботу. Деталі — у локальному журналі {name}.log."
                )
            if time.monotonic() >= deadline:
                raise StartError(
                    f"{LABELS[name]} ще не готовий. Перевірте журнал і натисніть «Запустити все» повторно."
                )
            time.sleep(0.3)
        self.emit(name, "ready", "Працює")

    def tunnel(self, tools):
        self.emit("ngrok", "starting", "Отримуємо адресу…")
        data = self.json_get("http://127.0.0.1:4040/api/tunnels")
        if not isinstance(data, dict) or not isinstance(data.get("tunnels"), list):
            if self.processes.listener(4040):
                raise StartError(
                    "Порт 4040 зайнятий. Закрийте зайвий ngrok або програму, яка його використовує."
                )
            previous = self.state["processes"].get("ngrok")
            if not previous or not self.processes.alive(previous):
                args = [
                    tools["ngrok"],
                    "http",
                    "http://127.0.0.1:8082",
                    "--log-format=json",
                    "--log=stdout",
                ]
                if tools["ngrok_config"]:
                    args.append("--config=" + tools["ngrok_config"])
                self.state["processes"]["ngrok"] = self.processes.spawn(
                    args, self.root, self.environment(), self.data / "ngrok.log"
                )
                self.save()
            deadline = time.monotonic() + 40
            while not isinstance(data, dict) or not isinstance(data.get("tunnels"), list):
                if time.monotonic() >= deadline or not self.processes.alive(
                    self.state["processes"]["ngrok"]
                ):
                    raise StartError(
                        "ngrok не підключився. Перевірте інтернет і журнал ngrok.log, потім повторіть запуск."
                    )
                time.sleep(0.3)
                data = self.json_get("http://127.0.0.1:4040/api/tunnels")
        for tunnel in data["tunnels"]:
            if tunnel.get("proto") == "https" and gateway_upstream(
                tunnel.get("config", {}).get("addr", "")
            ):
                url = public_origin(tunnel["public_url"])
                self.state["public_url"] = url
                self.save()
                self.emit("ngrok", "ready", "Підключено")
                return url
        # Reuse an existing agent without replacing/deleting its other tunnels.
        name = "bot_panel_" + secrets.token_hex(4)
        try:
            response = self.http.post(
                "http://127.0.0.1:4040/api/tunnels",
                json={
                    "name": name,
                    "proto": "http",
                    "addr": "http://127.0.0.1:8082",
                    "schemes": ["https"],
                },
            )
            response.raise_for_status()
            url = public_origin(response.json()["public_url"])
        except (httpx.HTTPError, ValueError, KeyError):
            raise StartError(
                "ngrok не дозволив додатковий тунель. Закрийте попередній ручний ngrok і натисніть запуск повторно."
            ) from None
        self.state.update(public_url=url, created_tunnel=name)
        self.save()
        self.emit("ngrok", "ready", "Підключено")
        return url

    def telegram_call(self, token, method, payload=None):
        status = None
        for attempt in range(3):
            delay = 1 + attempt
            retryable = True
            try:
                response = self.http.post(
                    f"https://api.telegram.org/bot{token}/{method}", json=payload or {}, timeout=25
                )
                status = response.status_code
                data = response.json()
                if response.is_success and isinstance(data, dict) and data.get("ok"):
                    return data["result"]
                retryable = status == 429 or status >= 500
                if status == 429 and isinstance(data, dict):
                    requested = data.get("parameters", {}).get("retry_after", delay)
                    if isinstance(requested, (int, float)) and 0 <= requested <= 30:
                        delay = requested
                    else:
                        retryable = False
            except (httpx.HTTPError, ValueError, KeyError):
                pass
            if not retryable or attempt == 2:
                break
            time.sleep(delay)
        raise StartError(
            f"Telegram не підтвердив підключення ({status or 'мережа'}). Перевірте інтернет і повторіть запуск."
        )

    def register(self, url, translator, planner):
        for name, config, path, updates in [
            (
                "planner",
                planner,
                "/telegram/webhook",
                ["message", "callback_query", "my_chat_member", "chat_member"],
            ),
            ("translator", translator, SHARED_PATH, ["message", "channel_post"]),
        ]:
            self.emit(name, "starting", "Підключаємо Telegram…")
            expected = url + path
            info = self.telegram_call(config["TELEGRAM_BOT_TOKEN"], "getWebhookInfo")
            if info.get("url") != expected:
                self.telegram_call(
                    config["TELEGRAM_BOT_TOKEN"],
                    "setWebhook",
                    {
                        "url": expected,
                        "secret_token": config["WEBHOOK_SECRET"],
                        "allowed_updates": updates,
                        "drop_pending_updates": False,
                    },
                )
                info = self.telegram_call(config["TELEGRAM_BOT_TOKEN"], "getWebhookInfo")
            if info.get("url") != expected:
                raise StartError("Адреса webhook не підтвердилася. Повторіть запуск.")
            self.emit(name, "ready", "Telegram підключено")

    @exclusive_operation
    def start(self):
        translator, planner = self.configs()
        tools = self.discover_tools()
        self.emit("overall", "starting", "Запускаємо служби…")
        libre_script = self.root / "scripts/run_libretranslate.py"
        self.ensure(
            "libre",
            [
                tools["libre_python"],
                "-u",
                "-X",
                "utf8",
                str(libre_script),
                "--host",
                "127.0.0.1",
                "--port",
                "5000",
                "--load-only",
                "en,uk,ru,pl",
                "--disable-web-ui",
                "--disable-files-translation",
            ],
            self.root,
            script=libre_script,
        )
        self.ensure(
            "gateway",
            [tools["python"], "-u", "-X", "utf8", "-m", "translator.gateway"],
            self.root,
            module="translator.gateway",
        )
        url = self.tunnel(tools)
        self.emit("overall", "starting", "Зберігаємо нову адресу…")
        write_environment(self.root / ".env", self.planner / ".env", url)
        self.ensure(
            "planner",
            [tools["node"], "--env-file-if-exists=.env", "src/index.js"],
            self.planner,
            script=self.planner / "src/index.js",
            executable=tools["node"],
            url=url,
        )
        self.ensure(
            "translator",
            [tools["python"], "-u", "-X", "utf8", "-m", "translator"],
            self.root,
            module="translator",
            url=url,
        )
        self.register(url, translator, planner)
        for path in ("/health", "/translator/health"):
            try:
                self.http.get(
                    url + path, headers={"ngrok-skip-browser-warning": "1"}, timeout=20
                ).raise_for_status()
            except httpx.HTTPError:
                raise StartError(
                    "Локальні служби працюють, але тунель ще недоступний з інтернету. Повторіть запуск."
                ) from None
        self.emit("url", "ready", url)
        self.emit("overall", "ready", "Усе працює. Можна користуватися ботами.")
        return url

    def status(self):
        self.reload()
        result = {}
        for name in PROBES:
            result[name] = self.healthy(name)
            self.emit(
                name,
                "ready" if result[name] else "stopped",
                "Працює" if result[name] else "Зупинено",
            )
        data = self.json_get("http://127.0.0.1:4040/api/tunnels")
        result["ngrok"] = isinstance(data, dict) and any(
            t.get("proto") == "https" and gateway_upstream(t.get("config", {}).get("addr", ""))
            for t in data.get("tunnels", [])
        )
        self.emit(
            "ngrok",
            "ready" if result["ngrok"] else "stopped",
            "Підключено" if result["ngrok"] else "Зупинено",
        )
        if self.state.get("public_url"):
            self.emit("url", "ready", self.state["public_url"])
        self.emit(
            "overall",
            "ready" if all(result.values()) else "stopped",
            "Усе працює. Можна користуватися ботами."
            if all(result.values())
            else "Не всі служби працюють. Натисніть «Запустити все».",
        )
        return result

    def backup_databases(self):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        for name, folder, default in [
            ("translator", self.root, "data/translator.sqlite"),
            ("planner", self.planner, "data/planner.sqlite"),
        ]:
            values = dotenv_values(folder / ".env")
            path = Path(values.get("DATABASE_PATH") or default)
            if not path.is_absolute():
                path = folder / path
            if path.is_file():
                target = self.data / "backups" / f"{name}-{stamp}.sqlite"
                target.parent.mkdir(parents=True, exist_ok=True)
                with (
                    closing(sqlite3.connect(path)) as source,
                    closing(sqlite3.connect(target)) as destination,
                ):
                    source.backup(destination)

    @exclusive_operation
    def stop(self):
        self.emit("overall", "starting", "Зберігаємо бази та зупиняємо…")
        self.backup_databases()
        for name in ("translator", "planner", "gateway", "libre", "ngrok"):
            record = self.state["processes"].get(name)
            if record:
                self.processes.stop(record)
                self.state["processes"].pop(name, None)
                self.save()
            self.emit(name, "stopped", "Зупинено")
        if self.state.get("created_tunnel"):
            try:
                self.http.delete(
                    "http://127.0.0.1:4040/api/tunnels/"
                    + quote(self.state["created_tunnel"], safe="")
                )
            except httpx.HTTPError:
                pass
            self.state.pop("created_tunnel", None)
            self.save()
        self.emit("overall", "stopped", "Запущені панеллю служби зупинено.")
