import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

SOURCE = Path(__file__).resolve().parents[1] / "app/guard.py"
spec = importlib.util.spec_from_file_location("guard", SOURCE)
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)


def device(wan="primary"):
    backup = wan == "backup"
    return {"meta": {"rc": "ok"}, "data": [{
        "type": "udm", "last_seen": time.time(),
        "last_wan_status": {"WAN": "online", "WAN2": "online"},
        "last_wan_interfaces": {"WAN": {"alive": True}, "WAN2": {"alive": True}},
        "uplink": {"comment": "WAN2" if backup else "WAN", "name": "eth9" if backup else "eth8",
                   "type": "wire", "up": True},
        "wan1": {"is_uplink": not backup, "ifname": "eth8", "up": True},
        "wan2": {"is_uplink": backup, "ifname": "eth9", "up": True}}]}


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, body, status=200, login=False):
        self.send_response(status)
        if login:
            self.send_header("Set-Cookie", "TOKEN=fixture; HttpOnly; Path=/")
            self.send_header("X-CSRF-Token", "fixture-csrf")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert body == {"username": "fixture", "password": "fixture", "rememberMe": False}
        self.reply({}, login=True)

    def do_GET(self):
        state = self.server.fixture
        if self.path.startswith("/proxy/"):
            if self.headers.get("Cookie") != "TOKEN=fixture" or self.headers.get("X-CSRF-Token") != "fixture-csrf":
                self.reply({}, 401)
            else:
                self.reply(state["wan"], state["wan_status"])
            return
        query = parse_qs(urlsplit(self.path).query)
        assert query["apikey"] == ["fixture-key"]
        mode = query["mode"][0]
        if mode == "queue":
            self.reply({"queue": {"paused": state["paused"], "paused_all": False,
                                 "pause_int": "0", "status": "Paused" if state["paused"] else "Idle"}})
        else:
            state["actions"].append(mode)
            if state["action_status"] == 200 or state["apply_error"]:
                state["paused"] = mode == "pause"
            self.reply({"status": True}, state["action_status"])


class Integration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "journal.json"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        self.state = {"wan": device("backup"), "paused": False, "actions": [],
                      "wan_status": 200, "action_status": 200, "apply_error": False}
        self.server.fixture = self.state
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port)
        self.restart()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def restart(self):
        self.guard = g.Guard(g.Journal(self.path), g.SAB(g.API(self.base + "/api"), "fixture-key"),
                             g.UniFi(g.API(self.base), "fixture", "fixture"))

    def healthy(self, start=25):
        self.state["wan"] = device()
        for now in range(start, start + 126, 25):
            self.guard.tick(now)

    def test_pause_then_stable_resume(self):
        self.guard.tick(0)
        self.assertEqual(self.guard.journal.state, "owned")
        self.healthy()
        self.assertEqual(self.state["actions"], ["pause", "resume"])
        self.assertFalse(self.state["paused"])

    def test_preexisting_pause_never_owned_or_resumed(self):
        self.state["paused"] = True
        self.guard.tick(0)
        self.restart()
        self.healthy()
        self.assertEqual(self.state["actions"], [])
        self.assertEqual(self.guard.journal.state, "idle")

    def test_owned_restart_requires_full_new_stability(self):
        self.guard.tick(0)
        self.state["wan"] = device()
        self.guard.tick(25)
        self.guard.tick(50)
        self.restart()
        self.guard.tick(100)
        for now in (125, 150, 175, 200):
            self.guard.tick(now)
        self.assertEqual(self.state["actions"], ["pause"])
        self.guard.tick(225)
        self.assertEqual(self.state["actions"], ["pause", "resume"])

    def test_unknown_error_and_flap_reset_stability(self):
        for interruption in ("unknown", "error", "backup"):
            with self.subTest(interruption=interruption):
                self.state["wan_status"] = 200
                self.state["wan"] = device("backup")
                self.state["paused"] = False
                self.guard.journal.save("idle")
                self.guard.tick(0)
                self.state["wan"] = device()
                self.guard.tick(25)
                self.guard.tick(50)
                self.state["wan"] = {} if interruption == "unknown" else device(interruption)
                self.state["wan_status"] = 503 if interruption == "error" else 200
                self.guard.tick(75)
                self.state["wan_status"] = 200
                self.state["wan"] = device()
                for now in (100, 125, 150, 175, 200):
                    self.guard.tick(now)
                self.assertTrue(self.state["paused"])
                self.guard.tick(225)
                self.assertFalse(self.state["paused"])

    def test_unknown_never_resumes(self):
        self.guard.tick(0)
        self.state["wan"] = {}
        for now in range(25, 501, 25):
            self.guard.tick(now)
        self.assertEqual(self.state["actions"], ["pause"])

    def test_gap_resets_stability(self):
        self.guard.tick(0)
        self.state["wan"] = device()
        self.guard.tick(25)
        self.guard.tick(200)
        self.assertTrue(self.state["paused"])

    def test_pause_error_before_or_after_application_never_owned(self):
        for applied in (False, True):
            with self.subTest(applied=applied):
                self.state.update(paused=False, action_status=503, apply_error=applied, wan=device("backup"))
                self.guard.journal.save("idle")
                self.guard.tick(0)
                self.assertEqual(self.guard.journal.state, "pause_pending")
                self.restart()
                self.state["action_status"] = 200
                self.healthy()
                self.assertNotIn("resume", self.state["actions"])

    def test_resume_ambiguous_never_replayed_after_restart(self):
        self.guard.tick(0)
        self.state.update(action_status=503, apply_error=True)
        self.healthy()
        self.assertEqual(self.guard.journal.state, "resume_pending")
        self.restart()
        self.state.update(action_status=200, paused=True)
        self.healthy(200)
        self.assertEqual(self.state["actions"], ["pause", "resume"])

    def test_crash_pending_before_side_effect(self):
        for state in ("pause_pending", "resume_pending"):
            self.guard.journal.save(state)
            self.restart()
            self.healthy()
        self.assertEqual(self.state["actions"], [])

    def test_journal_write_failure_prevents_side_effect(self):
        with patch.object(self.guard.journal, "_save", side_effect=OSError):
            with self.assertRaises(SystemExit):
                self.guard.tick(0)
        self.assertEqual(self.state["actions"], [])

    def test_directory_fsync_failure_after_replace_reloads_pending(self):
        original = g.os.fsync
        calls = []

        def fail_directory(descriptor):
            calls.append(descriptor)
            if len(calls) == 2:
                raise OSError("fixture directory sync error")
            return original(descriptor)

        with patch.object(g.os, "fsync", side_effect=fail_directory):
            with self.assertRaises(SystemExit):
                self.guard.tick(0)
        self.restart()
        self.assertEqual(self.guard.journal.state, "pause_pending")
        self.healthy()
        self.assertEqual(self.state["actions"], [])

    def test_manual_resume_relinquishes_ownership(self):
        self.guard.tick(0)
        self.state.update(paused=False, wan=device())
        self.guard.tick(25)
        self.state["paused"] = True
        self.healthy(50)
        self.assertEqual(self.state["actions"], ["pause"])
        self.assertEqual(self.guard.journal.state, "idle")

    def test_tls_pin_before_credentials(self):
        import hashlib
        import ssl
        import subprocess
        certificate = Path(self.temp.name) / "cert.pem"
        key = Path(self.temp.name) / "key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", str(key), "-out", str(certificate), "-days", "1",
                        "-subj", "/CN=fixture.invalid"], check=True, capture_output=True)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate, key)
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        base = self.base.replace("http:", "https:")
        api = g.API(base, "0" * 64)
        with self.assertRaises(ssl.SSLError):
            g.UniFi(api, "fixture", "fixture").wan()
        self.assertEqual(api.cookie, "")
        pin = hashlib.sha256(ssl.PEM_cert_to_DER_cert(certificate.read_text())).hexdigest()
        self.assertEqual(g.UniFi(g.API(base, pin), "fixture", "fixture").wan(), "backup")

    def test_corrupt_journal_fails_closed(self):
        self.path.write_text("broken")
        with self.assertRaises(ValueError):
            self.restart()


class Classification(unittest.TestCase):
    def test_active_not_physical_link(self):
        for wan in ("primary", "backup"):
            self.assertEqual(g.classify_wan(device(wan), time.time()), wan)

    def test_stale_future_ambiguous_or_inconsistent_is_unknown(self):
        baseline = device()
        mutations = [lambda d: d.update(last_seen=time.time() - 181),
                     lambda d: d.update(last_seen=time.time() + 10),
                     lambda d: d["wan2"].update(is_uplink=True),
                     lambda d: d["wan1"].update(is_uplink=False),
                     lambda d: d["uplink"].update(name="eth9"),
                     lambda d: d["uplink"].update(up=False),
                     lambda d: d["last_wan_status"].update(WAN="offline"),
                     lambda d: d["last_wan_interfaces"]["WAN"].update(alive=False)]
        for mutate in mutations:
            data = copy.deepcopy(baseline)
            mutate(data["data"][0])
            self.assertEqual(g.classify_wan(data, time.time()), "unknown")


if __name__ == "__main__":
    unittest.main()
