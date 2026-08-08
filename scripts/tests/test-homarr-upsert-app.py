#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


class FakeHomarrHandler(BaseHTTPRequestHandler):
    apps: list[dict[str, Any]] = []
    board: dict[str, Any] = {"id": "board-1", "name": "dashboard", "items": []}
    fail_next_tile = False

    def log_message(self, format: str, *args: object) -> None:
        del format, args
        return

    def send_json(self, value: Any, *, cookie: bool = False) -> None:
        body = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if cookie:
            self.send_header("Set-Cookie", "session=fake; Path=/; HttpOnly")
        self.end_headers()
        self.wfile.write(body)

    def trpc(self, value: Any) -> None:
        self.send_json({"result": {"data": {"json": value}}})

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/health/live":
            self.send_json({"status": "ok"})
        elif parsed.path == "/api/auth/csrf":
            self.send_json({"csrfToken": "csrf"})
        elif parsed.path == "/api/auth/session":
            self.send_json({"user": {"name": "admin"}} if "session=fake" in self.headers.get("Cookie", "") else {})
        elif parsed.path == "/api/trpc/app.all":
            self.trpc(type(self).apps)
        elif parsed.path == "/api/trpc/board.getBoardByName":
            self.trpc(type(self).board)
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        if self.path == "/api/auth/callback/credentials":
            form = urllib.parse.parse_qs(raw.decode())
            if form.get("name") != ["admin"] or form.get("password") != ["very-secret"]:
                self.send_error(401)
                return
            self.send_json({"url": "/"}, cookie=True)
            return
        document = json.loads(raw)
        value = document["json"]
        if self.path == "/api/trpc/app.create":
            app = {"id": f"app-{len(type(self).apps) + 1}", **value}
            type(self).apps.append(app)
            self.trpc(app)
        elif self.path == "/api/trpc/app.update":
            app_id = value.pop("id")
            app = next(item for item in type(self).apps if item["id"] == app_id)
            app.update(value)
            self.trpc(None)
        elif self.path == "/api/trpc/app.delete":
            type(self).apps = [item for item in type(self).apps if item["id"] != value["id"]]
            self.trpc(None)
        elif self.path == "/api/trpc/board.addItem":
            if type(self).fail_next_tile:
                type(self).fail_next_tile = False
                self.send_error(400, "simulated board failure")
                return
            item_id = f"item-{len(type(self).board['items']) + 1}"
            item = {"id": item_id, **value}
            type(self).board["items"].append(item)
            self.trpc({"itemId": item_id})
        else:
            self.send_error(404)


class HomarrUpsertTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo = Path(__file__).resolve().parents[2]
        FakeHomarrHandler.apps = []
        FakeHomarrHandler.board = {"id": "board-1", "name": "dashboard", "items": []}
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeHomarrHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.thread.join(timeout=5)
        cls.server.server_close()

    def invoke(
        self, description: str = "Manage ebook and audiobook downloads"
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(self.repo / "scripts/homarr-upsert-app"),
                "--base-url",
                self.base_url,
                "--name",
                "Chaptarr",
                "--href",
                "https://chaptarr.thezoo.house/",
                "--description",
                description,
                "--icon-url",
                "https://example.test/chaptarr.svg",
                "--board",
                "dashboard",
            ],
            cwd=self.repo,
            input=json.dumps({"username": "admin", "password": "very-secret"}),
            text=True,
            capture_output=True,
        )

    def test_upsert_is_verified_and_idempotent_without_leaking_credentials(self) -> None:
        first = self.invoke()
        self.assertEqual(first.returncode, 0, first.stderr)
        first_result = json.loads(first.stdout)
        self.assertTrue(first_result["createdApp"])
        self.assertTrue(first_result["createdTile"])
        self.assertTrue(first_result["verified"])

        second = self.invoke()
        self.assertEqual(second.returncode, 0, second.stderr)
        second_result = json.loads(second.stdout)
        self.assertFalse(second_result["createdApp"])
        self.assertFalse(second_result["createdTile"])
        self.assertFalse(second_result["updatedApp"])
        self.assertTrue(second_result["verified"])

        third = self.invoke("Manage books and audiobooks")
        self.assertEqual(third.returncode, 0, third.stderr)
        third_result = json.loads(third.stdout)
        self.assertTrue(third_result["updatedApp"])
        self.assertFalse(third_result["createdTile"])
        self.assertTrue(third_result["verified"])
        self.assertEqual(len(FakeHomarrHandler.apps), 1)
        self.assertEqual(len(FakeHomarrHandler.board["items"]), 1)
        self.assertNotIn(
            "very-secret",
            first.stdout + first.stderr + second.stdout + second.stderr + third.stdout + third.stderr,
        )

        FakeHomarrHandler.apps = []
        FakeHomarrHandler.board["items"] = []
        FakeHomarrHandler.fail_next_tile = True
        failed = self.invoke()
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(FakeHomarrHandler.apps, [])
        self.assertEqual(FakeHomarrHandler.board["items"], [])


if __name__ == "__main__":
    unittest.main()
