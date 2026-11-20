import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from dotenv import dotenv_values

from bot_control.manager import (
    Manager,
    StartError,
    gateway_upstream,
    public_origin,
    write_environment,
)
from bot_control.runtime import Processes, operation_lock


class FakeProcesses:
    def __init__(self):
        self.running = {}
        self.spawned = []
        self.stopped = []
        self.unknown_port = None

    def record(self, pid):
        return {"pid": pid, "created": float(pid), "exe": "fake", "command_hash": str(pid)}

    def alive(self, record):
        return record["pid"] in self.running.values()

    def listener(self, port):
        if port == self.unknown_port:
            return 999
        return self.running.get(port)

    def matches(self, pid, **kwargs):
        return pid != 999

    def spawn(self, args, cwd, env, log):
        if "run_libretranslate.py" in " ".join(args):
            port = 5000
        elif "translator.gateway" in args:
            port = 8082
        elif "translator" in args:
            port = 8081
        elif "http" in args:
            port = 4040
        else:
            port = 8080
        pid = 100 + len(self.spawned)
        self.running[port] = pid
        self.spawned.append((port, args, dict(env)))
        return self.record(pid)

    def stop(self, record):
        self.stopped.append(record["pid"])
        for port, pid in list(self.running.items()):
            if pid == record["pid"]:
                del self.running[port]


class PanelManagerTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name) / "translator"
        self.planner = self.root.parent / "telegram-task-planner"
        self.root.mkdir()
        self.planner.mkdir()
        self.translator_text = (
            "# Keep this comment\nTELEGRAM_BOT_TOKEN=123:translator_test\n"
            "WEBHOOK_SECRET=" + "t" * 32 + "\nPORT=8081\n"
            "PUBLIC_BASE_URL=https://old.ngrok.test\nTRANSLATION_PROVIDER=libretranslate\n"
            "TRANSLATION_API_URL=http://127.0.0.1:5000/translate\n"
        )
        self.planner_text = (
            "TELEGRAM_BOT_TOKEN=456:planner_test\nWEBHOOK_SECRET="
            + "p" * 32
            + "\nPORT=8080\nPUBLIC_BASE_URL=https://old.ngrok.test\n"
        )
        (self.root / ".env").write_text(self.translator_text)
        (self.planner / ".env").write_text(self.planner_text)
        self.processes = FakeProcesses()
        self.calls = []
        self.webhooks = {}
        self.external_tunnel = False
        self.reject_new_tunnel = False
        self.bad_webhook = False
        self.client = httpx.Client(transport=httpx.MockTransport(self.handle))
        self.events = []
        self.manager = Manager(
            self.root,
            client=self.client,
            processes=self.processes,
            notify=self.events.append,
            wait_timeout=0.1,
        )
        self.manager.discover_tools = lambda: {
            "python": "python.exe",
            "libre_python": "libre.exe",
            "node": "node.exe",
            "ngrok": "ngrok.exe",
            "ngrok_config": "ngrok.yml",
        }

    def tearDown(self):
        self.client.close()
        self.folder.cleanup()

    def handle(self, request):
        self.calls.append((request.method, request.url.host, request.url.path))
        port, path = request.url.port, request.url.path
        if request.url.host == "api.telegram.org":
            if path.endswith("/setWebhook"):
                body = json.loads(request.content)
                self.assertFalse(body["drop_pending_updates"])
                self.webhooks[path.rsplit("/", 1)[0]] = body["url"]
                return httpx.Response(200, json={"ok": True, "result": True})
            if path.endswith("/getWebhookInfo"):
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "result": {
                            "url": "https://wrong.test"
                            if self.bad_webhook
                            else self.webhooks.get(path.rsplit("/", 1)[0], "")
                        },
                    },
                )
            self.fail("Unexpected Telegram method")
        if port == 4040:
            if self.external_tunnel:
                if request.method == "POST":
                    return httpx.Response(
                        429 if self.reject_new_tunnel else 201,
                        json={"public_url": "https://new.ngrok.test"},
                    )
                return httpx.Response(
                    200,
                    json={
                        "tunnels": [
                            {
                                "proto": "https",
                                "config": {"addr": "http://localhost:8080"},
                                "public_url": "https://old.ngrok.test",
                            }
                        ]
                    },
                )
            if port in self.processes.running:
                return httpx.Response(
                    200,
                    json={
                        "tunnels": [
                            {
                                "proto": "https",
                                "config": {"addr": "http://localhost:8082"},
                                "public_url": "https://new.ngrok.test",
                            }
                        ]
                    },
                )
            raise httpx.ConnectError("not started", request=request)
        if request.url.host == "new.ngrok.test":
            return httpx.Response(200, json={"ok": True})
        if port not in self.processes.running:
            raise httpx.ConnectError("not started", request=request)
        if port == 5000:
            return httpx.Response(200, json=[{"code": "en"}, {"code": "ru"}, {"code": "uk"}])
        if port == 8082:
            return httpx.Response(200, json={"ok": True, "service": "telegram-shared-gateway"})
        if port == 8080:
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, json={"ok": True, "translation_configured": True})

    def test_cold_start_updates_both_envs_and_webhooks(self):
        self.assertEqual(self.manager.start(), "https://new.ngrok.test")
        self.assertEqual([p[0] for p in self.processes.spawned], [5000, 8082, 4040, 8080, 8081])
        translator = dotenv_values(self.root / ".env")
        planner = dotenv_values(self.planner / ".env")
        self.assertEqual(translator["PUBLIC_BASE_URL"], planner["PUBLIC_BASE_URL"])
        self.assertEqual(translator["TELEGRAM_BOT_TOKEN"], "123:translator_test")
        self.assertEqual(planner["TELEGRAM_BOT_TOKEN"], "456:planner_test")
        self.assertEqual(translator["PUBLIC_WEBHOOK_PATH"], "/translator/telegram/webhook")
        self.assertIn("# Keep this comment", (self.root / ".env").read_text())
        self.assertEqual(
            set(self.webhooks.values()),
            {
                "https://new.ngrok.test/telegram/webhook",
                "https://new.ngrok.test/translator/telegram/webhook",
            },
        )
        metadata = self.manager.metadata_path.read_text()
        self.assertNotIn("translator_test", metadata)
        self.assertNotIn("t" * 32, metadata)

    def test_repeated_start_does_not_duplicate_services(self):
        self.manager.start()
        self.manager.start()
        self.assertEqual(len(self.processes.spawned), 5)

    def test_rate_limited_registration_retries_without_changing_bot_messages(self):
        original = self.handle
        rejected = False

        def handle(request):
            nonlocal rejected
            if request.url.path.endswith("/setWebhook") and not rejected:
                rejected = True
                return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 0}})
            return original(request)

        self.manager.http = httpx.Client(transport=httpx.MockTransport(handle))
        try:
            self.manager.start()
            self.assertTrue(rejected)
        finally:
            self.manager.http.close()

    def test_reopened_panel_reuses_previous_services(self):
        self.manager.start()
        reopened = Manager(self.root, client=self.client, processes=self.processes)
        reopened.discover_tools = self.manager.discover_tools
        reopened.start()
        self.assertEqual(len(self.processes.spawned), 5)

    def test_panel_opened_before_start_can_stop_newly_recorded_services(self):
        other_panel = Manager(self.root, client=self.client, processes=self.processes)
        self.manager.start()
        other_panel.stop()
        self.assertEqual(len(self.processes.stopped), 5)
        self.assertFalse(self.processes.running)

    def test_simultaneous_start_is_rejected_before_launching_processes(self):
        with operation_lock(self.manager.data / "operation.lock"):
            with self.assertRaises(StartError):
                self.manager.start()
        self.assertFalse(self.processes.spawned)

    def test_unknown_listener_is_not_stopped(self):
        self.processes.unknown_port = 8082
        with self.assertRaises(StartError):
            self.manager.start()
        self.assertEqual(self.processes.stopped, [])
        self.assertEqual(self.processes.unknown_port, 8082)

    def test_ngrok_quota_failure_leaves_old_tunnel_untouched(self):
        self.external_tunnel = self.reject_new_tunnel = True
        with self.assertRaises(StartError):
            self.manager.start()
        self.assertFalse(any(method in {"DELETE", "PUT"} for method, _, _ in self.calls))
        self.assertEqual(
            dotenv_values(self.planner / ".env")["PUBLIC_BASE_URL"], "https://old.ngrok.test"
        )

    def test_inherited_credentials_are_not_passed_between_bots(self):
        with patch.dict(
            os.environ, {"TELEGRAM_BOT_TOKEN": "wrong_token", "DATABASE_PATH": "wrong.sqlite"}
        ):
            self.manager.start()
        for _, _, environment in self.processes.spawned:
            self.assertFalse("TELEGRAM_BOT_TOKEN" in environment)
            self.assertFalse("DATABASE_PATH" in environment)

    def test_invalid_credentials_fail_before_starting_anything(self):
        (self.planner / ".env").write_text("TELEGRAM_BOT_TOKEN=\n")
        with self.assertRaises(StartError):
            self.manager.start()
        self.assertEqual(self.processes.spawned, [])
        self.assertEqual(self.calls, [])

    def test_webhook_mismatch_is_reported_without_killing_services(self):
        self.bad_webhook = True
        with self.assertRaises(StartError):
            self.manager.start()
        self.assertEqual(len(self.processes.running), 5)
        self.assertEqual(self.processes.stopped, [])

    def test_stop_backs_up_sqlite_and_only_stops_recorded_processes(self):
        self.manager.start()
        database = self.planner / "data/planner.sqlite"
        database.parent.mkdir()
        connection = sqlite3.connect(database)
        connection.execute("CREATE TABLE tasks(description TEXT)")
        connection.execute("INSERT INTO tasks VALUES ('important task')")
        connection.commit()
        connection.close()
        self.manager.stop()
        self.assertEqual(len(self.processes.stopped), 5)
        self.assertEqual(self.processes.running, {})
        backups = list((self.manager.data / "backups").glob("planner-*.sqlite"))
        self.assertEqual(len(backups), 1)
        backup = sqlite3.connect(backups[0])
        self.assertEqual(
            backup.execute("SELECT description FROM tasks").fetchone()[0], "important task"
        )
        backup.close()


class UtilityTests(unittest.TestCase):
    def test_gateway_addresses(self):
        for address in ["http://localhost:8082", "http://127.0.0.1:8082", "localhost:8082"]:
            self.assertTrue(gateway_upstream(address))
        for address in [
            "http://localhost:8080",
            "http://remote.example:8082",
            "https://127.0.0.1:8082",
            "",
        ]:
            self.assertFalse(gateway_upstream(address))

    def test_public_origin_does_not_accept_embedded_secrets_or_paths(self):
        for url in [
            "http://x.test",
            "https://user:password@x.test",
            "https://x.test/path",
            "https://x.test/?token=private",
        ]:
            with self.assertRaises(StartError):
                public_origin(url)

    def test_env_pair_is_restored_if_second_write_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = [Path(folder) / "translator.env", Path(folder) / "planner.env"]
            for path in paths:
                path.write_text("PUBLIC_BASE_URL=https://old.test\nTOKEN=keep\n")
            from dotenv import set_key

            count = 0

            def fail_second(*args):
                nonlocal count
                count += 1
                if count == 2:
                    raise OSError("write failed")
                return set_key(*args)

            with (
                patch("bot_control.manager.set_key", side_effect=fail_second),
                self.assertRaises(StartError),
            ):
                write_environment(*paths, "https://new.test")
            for path in paths:
                self.assertEqual(path.read_text(), "PUBLIC_BASE_URL=https://old.test\nTOKEN=keep\n")

    def test_reused_pid_is_never_terminated(self):
        runtime = Processes()
        saved = {"pid": 123, "created": 1, "exe": "python.exe", "command_hash": "old"}
        with (
            patch.object(runtime, "record", return_value={**saved, "created": 2}),
            patch("bot_control.runtime.psutil.Process") as process,
        ):
            self.assertFalse(runtime.stop(saved))
            process.assert_not_called()
