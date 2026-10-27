"""Outbound-only SAB WAN guard. No third-party dependencies."""
import hashlib
import http.client
import json
import logging
import os
from pathlib import Path
import ssl
import time
from urllib.parse import urlencode, urlsplit


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, pin, **kwargs):
        super().__init__(host, context=ssl._create_unverified_context(), **kwargs)
        self.pin = pin

    def connect(self):
        super().connect()
        # Verify on the very connection that will carry credentials, before HTTP.
        certificate = self.sock.getpeercert(binary_form=True)
        if not certificate or hashlib.sha256(certificate).hexdigest() != self.pin:
            self.close()
            raise ssl.SSLError("gateway certificate pin mismatch")


class API:
    def __init__(self, base, pin=None):
        self.url = urlsplit(base)
        self.pin = pin
        self.cookie = ""
        self.csrf = ""

    def request(self, path, payload=None):
        if self.url.scheme == "https":
            conn = PinnedHTTPSConnection(self.url.netloc, self.pin, timeout=8)
        else:
            conn = http.client.HTTPConnection(self.url.netloc, timeout=8)
        headers = {"Cookie": self.cookie, "X-CSRF-Token": self.csrf}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload)
        try:
            conn.request("POST" if body is not None else "GET", self.url.path + path,
                         body=body, headers=headers)
            response = conn.getresponse()
            if response.status != 200:
                raise ValueError("API HTTP status " + str(response.status))
            from http.cookies import SimpleCookie
            cookies = SimpleCookie()
            for key, value in response.getheaders():
                if key.lower() == "set-cookie":
                    cookies.load(value)
            if cookies:
                self.cookie = "; ".join(key + "=" + value.value for key, value in cookies.items())
            self.csrf = response.getheader("X-CSRF-Token") or self.csrf
            return json.loads(response.read(2_000_000))
        finally:
            conn.close()


class UniFi:
    def __init__(self, api, username, password):
        self.api, self.username, self.password = api, username, password

    def wan(self):
        if not self.api.cookie:
            self.api.request("/api/auth/login", {"username": self.username,
                             "password": self.password, "rememberMe": False})
        try:
            data = self.api.request("/proxy/network/api/s/default/stat/device")
            return classify_wan(data, time.time())
        except Exception:
            self.api.cookie = ""
            raise


def classify_wan(response, now):
    if response.get("meta", {}).get("rc") != "ok":
        return "unknown"
    devices = [d for d in response.get("data", []) if d.get("type") == "udm"]
    if len(devices) != 1:
        return "unknown"
    device = devices[0]
    age = now - float(device.get("last_seen", 0))
    if not 0 <= age <= 180:
        return "unknown"
    uplink = device.get("uplink", {})
    if uplink.get("up") is not True or uplink.get("type") != "wire":
        return "unknown"
    active = []
    for key, label in (("wan1", "WAN"), ("wan2", "WAN2")):
        link = device.get(key, {})
        if link.get("is_uplink") is True:
            if (link.get("up") is not True or link.get("ifname") != uplink.get("name")
                    or uplink.get("comment") != label
                    or device.get("last_wan_status", {}).get(label) != "online"
                    or device.get("last_wan_interfaces", {}).get(label, {}).get("alive") is not True):
                return "unknown"
            active.append(label)
    return {"WAN": "primary", "WAN2": "backup"}.get(active[0], "unknown") if len(active) == 1 else "unknown"


class SAB:
    def __init__(self, api, key):
        self.api, self.key = api, key

    def call(self, mode):
        return self.api.request("?" + urlencode({"mode": mode, "output": "json", "apikey": self.key}))

    def paused(self):
        queue = self.call("queue")["queue"]
        if type(queue.get("paused")) is not bool:
            raise ValueError("missing queue pause state")
        if queue["paused"]:
            return True
        # Reject timed, post-processing, and inconsistent pause states as ambiguous.
        if (queue.get("pause_int") not in (0, "0") or queue.get("paused_all") is not False
                or str(queue.get("status", "")).lower() not in ("idle", "downloading")):
            raise ValueError("ambiguous queue pause state")
        return False

    def change(self, mode):
        if self.call(mode).get("status") is not True:
            raise ValueError("SAB rejected action")


class Journal:
    STATES = {"idle", "pause_pending", "owned", "resume_pending"}

    def __init__(self, path):
        self.path = Path(path)
        if self.path.exists():
            data = json.loads(self.path.read_text())
            if data.get("version") != 1 or data.get("state") not in self.STATES:
                raise ValueError("invalid journal; operator intervention required")
            self.state = data["state"]
        else:
            self.state = "idle"
            self.save("idle")

    def save(self, state):
        try:
            self._save(state)
        except OSError:
            # A failed directory fsync can leave disk and memory disagreeing.
            # Exit, then reload the durable journal; never retry with stale ownership.
            raise SystemExit("journal persistence failed") from None

    def _save(self, state):
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w") as output:
            json.dump({"version": 1, "state": state}, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, self.path)
        descriptor = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self.state = state
        logging.info("journal state=%s", state)


class Guard:
    def __init__(self, journal, sab, unifi):
        self.journal, self.sab, self.unifi = journal, sab, unifi
        self.primary_since = None
        self.last_poll = None

    def tick(self, now):
        try:
            wan = self.unifi.wan()
            paused = self.sab.paused()
            if self.last_poll is None or not 0 <= now - self.last_poll <= 60:
                self.primary_since = None
            self.last_poll = now
            if wan != "primary":
                self.primary_since = None
            elif self.primary_since is None:
                self.primary_since = now
            state = self.journal.state
            if state in ("pause_pending", "resume_pending"):
                logging.warning("ambiguous action journal=%s; operator intervention required", state)
                return
            if state == "owned" and not paused:
                # A caller resumed the queue. Relinquish before possibly pausing anew.
                self.journal.save("idle")
                state = "idle"
            if wan == "backup" and not paused and state == "idle":
                self.journal.save("pause_pending")
                self.sab.change("pause")
                if not self.sab.paused():
                    raise ValueError("pause not confirmed")
                self.journal.save("owned")
            elif (wan == "primary" and paused and state == "owned"
                  and now - self.primary_since >= 120):
                # Consume ownership durably before sending resume. Never replay it.
                self.journal.save("resume_pending")
                self.sab.change("resume")
                if self.sab.paused():
                    raise ValueError("resume not confirmed")
                self.journal.save("idle")
        except Exception as error:
            self.primary_since = None
            self.last_poll = None
            # API errors may contain URLs/credentials. Log only the exception class.
            logging.warning("poll/action failed kind=%s", type(error).__name__)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    gateway = os.environ["UNIFI_URL"]
    pin = os.environ["UNIFI_CERT_SHA256"]
    if urlsplit(gateway).scheme != "https" or len(pin) != 64:
        raise ValueError("HTTPS gateway and certificate SHA256 required")
    guard = Guard(Journal("/state/journal.json"),
                  SAB(API(os.environ["SAB_URL"]), os.environ["SAB_API_KEY"]),
                  UniFi(API(gateway, pin), os.environ["UNIFI_USER"], os.environ["UNIFI_PASS"]))
    while True:
        started = time.monotonic()
        guard.tick(started)
        time.sleep(max(0, 25 - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
