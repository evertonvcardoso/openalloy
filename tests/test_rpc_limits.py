# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""RPC limits (from the parallel 1.4.1 HP-Driver branch, originally 1.3.3)."""
import json
import socket
import time

from openalloy.infra.rpc import MAX_REQUEST_BYTES, RpcServer


def _connect(path):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(3)
    s.connect(str(path))
    return s


def test_rpc_rejects_oversize_without_calling_handler(tmp_path):
    calls = []
    server = RpcServer(tmp_path / 'rpc.sock', {'ping': lambda: calls.append(1)})
    server.start()
    try:
        with _connect(server.path) as sock:
            sock.sendall(b'{"id":1,"method":"ping","params":{"stuff":"' + b'x' * MAX_REQUEST_BYTES + b'"}}\n')
            reply = sock.recv(4096)
            assert b'limit' in reply
            assert calls == []
    finally:
        server.stop()


def test_rpc_rejects_malformed_utf8_and_continues(tmp_path):
    server = RpcServer(tmp_path / 'rpc.sock', {'ping': lambda: 'pong'})
    server.start()
    try:
        with _connect(server.path) as sock:
            sock.sendall(b'\xff\n')
            assert b'encoding' in sock.recv(4096)
            sock.sendall(b'{"id":2,"method":"ping"}\n')
            assert json.loads(sock.recv(4096))['result'] == 'pong'
    finally:
        server.stop()


def test_rpc_client_slots_released_after_disconnect(tmp_path):
    server = RpcServer(tmp_path / 'rpc.sock', {'ping': lambda: 'pong'})
    server.start()
    try:
        for _ in range(30):
            with _connect(server.path) as sock:
                sock.sendall(b'{"id":1,"method":"ping"}\n')
                assert b'pong' in sock.recv(4096)
            time.sleep(.005)
    finally:
        server.stop()
