#!/usr/bin/env python3
"""Offline round-trip checks for RustDesk protobuf and length-prefix formats."""
import importlib.util
from pathlib import Path
import socket
import threading
import unittest

path = Path(__file__).resolve().parents[1] / 'rustdesk-protocol-probe.py'
spec = importlib.util.spec_from_file_location('probe', path)
assert spec is not None and spec.loader is not None
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class WireCodecTests(unittest.TestCase):
    def test_protobuf_integers(self):
        for value in [0, 127, 128, 16383, 16384, 65535]:
            self.assertEqual(probe.decode(probe.number(3, value)), {3: value})

    def test_nested_messages(self):
        encoded = probe.blob(18, probe.blob(1, 'peer') + probe.blob(6, 'public-key'))
        self.assertEqual(probe.decode(probe.decode(encoded)[18]), {1: b'peer', 6: b'public-key'})

    def test_tcp_header_boundaries(self):
        for size in [0, 63, 64, 16383, 16384]:
            a, b = socket.socketpair()
            with a, b:
                payload = b'x' * size
                a.settimeout(5)
                b.settimeout(5)
                writer = threading.Thread(target=a.sendall, args=(probe.frame(payload),))
                writer.start()
                try:
                    self.assertEqual(probe.read_frame(b), payload)
                finally:
                    writer.join(timeout=5)
                self.assertFalse(writer.is_alive())

    def test_closed_socket_is_not_success(self):
        a, b = socket.socketpair()
        a.close()
        with b:
            with self.assertRaises(EOFError):
                probe.read_frame(b)


if __name__ == '__main__':
    unittest.main()
