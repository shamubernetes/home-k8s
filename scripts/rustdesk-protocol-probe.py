#!/usr/bin/env python3
"""RustDesk 1.1.16 protocol probe. No client/server secrets required.

Wire formats from rustdesk-server libs/hbb_common rendezvous.proto/bytes_codec.rs.
Tests NAT reflection, UDP public-key registration/heartbeat, key rejection,
relay advertisement, and raw bidirectional relay forwarding.
"""
import argparse
import base64
import json
import ipaddress
import urllib.request
import os
import socket
import time
import uuid


def varint(n):
    result = bytearray()
    while n > 127:
        result.append((n & 127) | 128)
        n >>= 7
    result.append(n)
    return bytes(result)


def number(field, value):
    return varint(field << 3) + varint(value)


def blob(field, value):
    if isinstance(value, str):
        value = value.encode()
    return varint((field << 3) | 2) + varint(len(value)) + value


def decode(data):
    def read_int(offset):
        n = shift = 0
        while True:
            b = data[offset]
            offset += 1
            n |= (b & 127) << shift
            if not b & 128:
                return n, offset
            shift += 7
    result = {}
    i = 0
    while i < len(data):
        tag, i = read_int(i)
        if tag & 7 == 0:
            value, i = read_int(i)
        elif tag & 7 == 2:
            size, i = read_int(i)
            value = data[i:i + size]
            i += size
        else:
            raise ValueError('unsupported wire type')
        result[tag >> 3] = value
    return result


def frame(data):
    width = next(w for w in range(1, 5) if len(data) < 1 << (8 * w - 2))
    return ((len(data) << 2) | (width - 1)).to_bytes(width, 'little') + data


def exact(sock, n):
    result = b''
    while len(result) < n:
        part = sock.recv(n - len(result))
        if not part:
            raise EOFError('connection closed')
        result += part
    return result


def read_frame(sock):
    first = exact(sock, 1)
    width = (first[0] & 3) + 1
    size = int.from_bytes(first + exact(sock, width - 1), 'little') >> 2
    assert size < 1024 * 1024
    return exact(sock, size)


def tcp(host, port, request):
    with socket.create_connection((host, port), timeout=8) as sock:
        sock.sendall(frame(request))
        return decode(read_frame(sock))


def run(host, key, require_public=False, check_closed=False):
    assert len(base64.b64decode(key, validate=True)) == 32, 'invalid public key'
    resolved = socket.gethostbyname(host)
    if require_public:
        assert ipaddress.ip_address(resolved).is_global, resolved
    result = {'target': host, 'resolved_ipv4': resolved, 'tests': {}}
    if require_public:
        with urllib.request.urlopen('https://api.ipify.org', timeout=10) as response:
            origin = response.read(64).decode().strip()
        assert ipaddress.ip_address(origin).is_global and origin != resolved, origin
        result['source_public_ipv4'] = origin
    tests = result['tests']
    if check_closed:
        for port in (21114, 21118, 21119):
            try:
                connection = socket.create_connection((host, port), timeout=2)
            except OSError:
                tests[f'optional_tcp_{port}_unreachable'] = {'pass': True}
            else:
                connection.close()
                raise AssertionError(f'optional TCP port {port} unexpectedly reachable')
    for port in (21115, 21116):
        response = tcp(host, port, blob(20, b''))
        reflected = decode(response[21])[1]
        assert 0 < reflected <= 65535
        tests[f'tcp_{port}_nat_response'] = {'pass': True, 'reflected_port': reflected}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        udp.settimeout(8)
        udp.connect((host, 21116))
        peer_id = 'k8s106-' + uuid.uuid4().hex[:12]
        request = blob(15, blob(1, peer_id) + blob(2, uuid.uuid4().bytes) + blob(3, os.urandom(32)))
        udp.send(request)
        response = decode(udp.recv(65535))
        registered = decode(response[16])
        assert registered.get(1, 0) == 0, registered
        tests['udp_register_pk'] = {'pass': True, 'peer_id': peer_id, 'keep_alive': registered.get(2, 0)}
        udp.send(blob(6, blob(1, peer_id)))
        response = decode(udp.recv(65535))
        assert 7 in response and decode(response[7]).get(2, 0) == 0
        tests['udp_registered_heartbeat'] = {'pass': True, 'request_pk': False}
        bad = tcp(host, 21116, blob(8, blob(1, peer_id) + blob(3, 'wrong-key')))
        assert decode(bad[11]).get(3) == 3
        tests['hbbs_rejects_wrong_key'] = {'pass': True, 'failure': 'LICENSE_MISMATCH'}
        # A symmetric-NAT peer request reaches the registered UDP peer.
        with socket.create_connection((host, 21116), timeout=8) as requester:
            requester.sendall(frame(blob(8, blob(1, peer_id) + number(2, 2) + blob(3, key) + blob(6, '1.4.4'))))
            incoming = decode(udp.recv(65535))
            assert 9 in incoming or 12 in incoming, incoming
            # Same-source IP tests request local addresses instead of a punch.
            punch = decode(incoming.get(9, incoming.get(12)))
            advertised = punch[2].decode()
            assert advertised == 'rustdesk.thezoo.house:21117', advertised
            tests['hbbs_advertises_relay'] = {'pass': True, 'relay': advertised}
    token = str(uuid.uuid4())
    relay_request = blob(18, blob(1, peer_id) + blob(2, token) + blob(6, key))
    with socket.create_connection((host, 21117), timeout=8) as a:
        a.sendall(frame(relay_request))
        time.sleep(0.2)
        with socket.create_connection((host, 21117), timeout=8) as b:
            b.sendall(frame(relay_request))
            time.sleep(0.2)
            payload = os.urandom(4096)
            a.sendall(payload)
            assert exact(b, len(payload)) == payload
            reverse = os.urandom(4096)
            b.sendall(reverse)
            assert exact(a, len(reverse)) == reverse
            tests['hbbr_bidirectional_raw_relay'] = {'pass': True, 'bytes_each_direction': 4096}
    with socket.create_connection((host, 21117), timeout=8) as bad:
        bad.sendall(frame(blob(18, blob(2, str(uuid.uuid4())) + blob(6, 'wrong-key'))))
        assert bad.recv(1) == b''
        tests['hbbr_rejects_wrong_key'] = {'pass': True}
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('host')
    parser.add_argument('public_key')
    parser.add_argument('--require-public', action='store_true')
    parser.add_argument('--check-closed', action='store_true')
    args = parser.parse_args()
    print(json.dumps(run(args.host, args.public_key, args.require_public, args.check_closed), indent=2))
