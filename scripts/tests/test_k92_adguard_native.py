"""Credential-free unit checks, not native original recovery acceptance."""
import importlib.util
from pathlib import Path
import socket
import struct
import tempfile
import threading
import unittest

SPEC = importlib.util.spec_from_file_location(
    "adguard_native", Path(__file__).parents[2] / "kubernetes/apps/adguard-home/adguard-home/backup-candidate/native-proof.py")
assert SPEC and SPEC.loader
PROOF = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROOF)


class OriginalSafety(unittest.TestCase):
    def test_roster_tracks_bytes_and_permissions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "conf").mkdir()
            (root / "work").mkdir()
            config = root / "conf/AdGuardHome.yaml"
            config.write_text("synthetic-unit-fixture")
            config.chmod(0o600)
            first = PROOF.roster(root)
            self.assertEqual(first["conf/AdGuardHome.yaml"]["mode"], 0o600)
            config.chmod(0o640)
            self.assertNotEqual(first, PROOF.roster(root))
            config.write_text("changed-unit-fixture")
            self.assertNotEqual(first["conf/AdGuardHome.yaml"]["sha256"],
                                PROOF.roster(root)["conf/AdGuardHome.yaml"]["sha256"])

    def test_roster_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "conf").mkdir()
            (root / "work").mkdir()
            (root / "conf/AdGuardHome.yaml").symlink_to("/etc/hosts")
            with self.assertRaises(ValueError):
                PROOF.roster(root)

    def dns_fixture(self, wrong_id=False, wrong_address=False):
        server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        server.settimeout(5)
        server.bind(("127.0.0.1", 15353))

        def respond():
            try:
                request, peer = server.recvfrom(4096)
                ident = int.from_bytes(request[:2], "big") ^ int(wrong_id)
                address = "192.0.2.2" if wrong_address else "192.0.2.1"
                reply = struct.pack("!HHHHHH", ident, 0x8180, 1, 1, 0, 0)
                reply += request[12:]
                reply += b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 30, 4) + socket.inet_aton(address)
                server.sendto(reply, peer)
            finally:
                server.close()

        worker = threading.Thread(target=respond)
        worker.start()
        try:
            PROOF.dns_probe("synthetic.example.invalid", "192.0.2.1")
        finally:
            worker.join(timeout=6)
            self.assertFalse(worker.is_alive())

    def test_dns_accepts_matching_wire_answer(self):
        self.dns_fixture()

    def test_dns_rejects_wrong_transaction(self):
        with self.assertRaises(ValueError):
            self.dns_fixture(wrong_id=True)

    def test_dns_rejects_wrong_rewrite(self):
        with self.assertRaises(ValueError):
            self.dns_fixture(wrong_address=True)


if __name__ == "__main__":
    unittest.main()
