# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""RPC lifecycle and concurrency (external 1.5.1 review, items P1, P2, P3, P5)."""
import json
import socket
import threading
import time

import pytest

from openalloy.infra.rpc import RpcClient, RpcServer


def rpc_threads():
    return [t for t in threading.enumerate() if t.name.startswith("openalloy-rpc")
            or t.name.startswith("openalloy-subscribe") or "(_serve)" in t.name]


def wait_until(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


@pytest.fixture
def server(tmp_path):
    d = tmp_path / "sock"
    d.mkdir(mode=0o700)
    srv = RpcServer(d / "s.sock", {
        "ping": lambda: "pong",
        "slow": lambda seconds=0.8: (time.sleep(seconds), "done")[1],
        "big": lambda size=300_000: "x" * size,
    })
    srv.idle_timeout = 0.4            # seconds; production default is much longer
    srv.start()
    yield srv
    srv.stop()


def connect(path, timeout=3.0):
    c = socket.socket(socket.AF_UNIX)
    c.settimeout(timeout)
    c.connect(str(path))
    return c


def read_lines(c, until):
    buf, lines = b"", []
    end = time.time() + 5
    while time.time() < end and not until(lines):
        try:
            chunk = c.recv(65536)
        except TimeoutError:
            continue
        if not chunk:
            break
        buf += chunk
        *done, buf = buf.split(b"\n")
        lines += done
    return lines


# --- P1: idle connections are closed, busy ones are not ---------------------------------------

def test_silent_client_is_disconnected(server):
    c = connect(server.path)
    assert wait_until(lambda: c.recv(1) == b"", 3)          # server closed it


def test_incomplete_request_is_disconnected(server):
    c = connect(server.path)
    c.sendall(b'{"id": 1, "method": "pi')                     # never finished
    assert wait_until(lambda: c.recv(1) == b"", 3)


def test_slot_is_released_after_idle_close(server):
    clients = [connect(server.path) for _ in range(24)]       # every slot taken by idlers
    time.sleep(1.0)                                           # > idle_timeout
    assert RpcClient(server.path, timeout=3).call("ping") == "pong"
    for c in clients:
        c.close()


def test_long_call_is_not_cut_by_the_idle_timeout(server):
    assert RpcClient(server.path, timeout=5).call("slow", seconds=1.2) == "done"


def test_subscriber_is_kept_while_idle(server):
    c = connect(server.path)
    c.sendall(b'{"id": 0, "method": "subscribe"}\n')
    read_lines(c, lambda ls: len(ls) >= 1)
    time.sleep(1.0)                                           # > idle_timeout
    server.broadcast("status", {"ok": True})
    lines = read_lines(c, lambda ls: len(ls) >= 1)
    assert json.loads(lines[0])["event"] == "status"
    c.close()


# --- P2: responses and events never interleave on one socket -------------------------------------

def test_responses_and_events_do_not_interleave(server):
    c = connect(server.path, timeout=5)
    c.sendall(b'{"id": 0, "method": "subscribe"}\n')
    read_lines(c, lambda ls: len(ls) >= 1)
    stop = threading.Event()

    def spam():
        while not stop.is_set():
            server.broadcast("status", {"blob": "y" * 50_000})
            time.sleep(0.002)
    t = threading.Thread(target=spam, daemon=True)
    t.start()
    for i in range(1, 6):
        c.sendall(json.dumps({"id": i, "method": "big"}).encode() + b"\n")
    lines = read_lines(c, lambda ls: sum(b'"result"' in x for x in ls) >= 5)
    stop.set()
    t.join(2)
    c.close()
    results = 0
    for raw in lines:
        msg = json.loads(raw)                                  # a torn line fails here
        results += "result" in msg
    assert results == 5


# --- P3: a subscription stops promptly ---------------------------------------------------------

def test_subscription_stops_promptly(server):
    stop = threading.Event()
    t = RpcClient(server.path).subscribe(lambda k, d: None, stop)
    time.sleep(0.3)
    stop.set()
    t.join(2.0)
    assert not t.is_alive()


def test_subscription_survives_a_service_restart(tmp_path):
    d = tmp_path / "s2"
    d.mkdir(mode=0o700)
    path = d / "s.sock"
    srv = RpcServer(path, {"ping": lambda: "pong"})
    srv.start()
    got, stop = [], threading.Event()
    t = RpcClient(path).subscribe(lambda k, data: got.append(k), stop)
    try:
        assert wait_until(lambda: srv.subscriber_count() == 1)
        srv.stop()
        srv = RpcServer(path, {"ping": lambda: "pong"})
        srv.start()
        assert wait_until(lambda: srv.subscriber_count() == 1, 5)   # reconnected by itself
        srv.broadcast("status", {})
        assert wait_until(lambda: "status" in got)
    finally:
        stop.set()
        srv.stop()
        t.join(3)


# --- P5: stop is coordinated --------------------------------------------------------------------

def test_stop_closes_clients_and_ends_every_thread(tmp_path):
    d = tmp_path / "s3"
    d.mkdir(mode=0o700)
    before = set(rpc_threads())
    srv = RpcServer(d / "s.sock", {"slow": lambda: (time.sleep(0.5), 1)[1]})
    srv.start()
    sub = connect(srv.path)
    sub.sendall(b'{"id": 0, "method": "subscribe"}\n')
    idle = connect(srv.path)
    time.sleep(0.2)
    srv.stop()
    assert sub.recv(4096).endswith(b"\n") or True               # may hold the subscribe ack
    assert wait_until(lambda: sub.recv(1) == b"" and idle.recv(1) == b"", 3)
    assert wait_until(lambda: set(rpc_threads()) <= before, 3), [t.name for t in rpc_threads()]
    assert not srv.path.exists()


def test_repeated_start_stop_does_not_leak(tmp_path):
    d = tmp_path / "s4"
    d.mkdir(mode=0o700)
    before = len(rpc_threads())
    for _attempt in range(15):
        srv = RpcServer(d / "s.sock", {"ping": lambda: "pong"})
        srv.start()
        assert RpcClient(srv.path, timeout=2).call("ping") == "pong"
        srv.stop()
    assert wait_until(lambda: len(rpc_threads()) <= before, 3)
