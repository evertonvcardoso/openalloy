# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""Tiny JSON-lines RPC over a Unix socket.

Replaces Linuity's "write preset file, then sudo systemctl restart" round
trip: the GUI and CLI talk to the running user daemon directly, so changes
apply instantly and no sudoers rule is needed.

Wire format, one JSON object per line:
    -> {"id": 1, "method": "apply", "params": {...}}
    <- {"id": 1, "progress": 0.4, "message": "..."}     (optional, repeated)
    <- {"id": 1, "result": ...}  or  {"id": 1, "error": "..."}
    -> {"id": 2, "method": "subscribe"}  then  <- {"event": "...", "data": {...}}
"""

from __future__ import annotations

import json
import logging
import os
import queue
import select
import socket
import stat
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from openalloy.i18n import N_, Message, _

log = logging.getLogger(__name__)

Handler = Callable[..., Any]

# Limits (idea from the parallel 1.4.1 "HP-Driver" branch): a runaway or
# malicious same-user client cannot exhaust memory or threads of the daemon.
MAX_REQUEST_BYTES = 256 * 1024
MAX_ACTIVE_CLIENTS = 24
MAX_ACTIVE_CALLS = 48


class RpcError(Exception):
    pass


class _Client:
    """One connection: a reader (in RpcServer._serve) and a single writer thread.

    Every outgoing line - call results, progress and broadcast events - goes through
    one queue and one writer, so lines can never interleave on the socket. Events are
    queued without blocking (the engine thread must never wait on a client).

    The queue is bounded by *bytes*, not by a number of lines: a burst of events to a
    client that is reading is only delayed, while a client that stopped reading is
    disconnected - when its backlog passes QUEUE_BYTES or when one write has made no
    progress for the send timeout.
    """

    QUEUE_BYTES = 4 * 1024 * 1024

    def __init__(self, conn: socket.socket, send_timeout: float):
        self.conn = conn
        self.subscribed = False
        self.pending = 0                   # calls still running for this connection
        self.lock = threading.Lock()
        self.alive = True
        self._q: queue.Queue[Optional[bytes]] = queue.Queue()
        self._queued = 0                   # bytes waiting in _q
        self._room = threading.Condition()
        conn.settimeout(send_timeout)      # bounds every sendall of the writer
        self._send_timeout = send_timeout
        self.writer = threading.Thread(target=self._write, name="openalloy-rpc-out",
                                       daemon=True)
        self.writer.start()

    def _write(self) -> None:
        try:
            while True:
                data = self._q.get()
                if data is None or not self.alive:
                    return                 # finally: close() after the queued lines
                try:
                    self.conn.sendall(data)
                finally:
                    with self._room:
                        self._queued -= len(data)
                        self._room.notify_all()
        except OSError:
            pass                           # peer gone or not draining: drop it
        finally:
            self.close()

    def _put(self, data: bytes, wait: float) -> bool:
        with self._room:
            if self._queued + len(data) > self.QUEUE_BYTES and wait > 0:
                self._room.wait_for(lambda: self._queued + len(data) <= self.QUEUE_BYTES
                                    or not self.alive, wait)
            if not self.alive or self._queued + len(data) > self.QUEUE_BYTES:
                return False
            self._queued += len(data)
        self._q.put(data)
        return True

    def send(self, obj: dict) -> None:
        """Result/progress: waits (bounded) for room in the queue."""
        if not self.alive:
            raise OSError("client closed")
        if not self._put((json.dumps(obj) + "\n").encode(), self._send_timeout):
            self.close()
            raise OSError("client not reading")

    def push_event(self, line: bytes) -> bool:
        """Never blocks. False = client dropped (dead or too slow)."""
        if not self.alive:
            return False
        if not self._put(line, 0):
            log.debug("slow subscriber dropped")
            self.close()
            return False
        return True

    def finish(self) -> None:
        """Graceful close: lines already queued (e.g. an error reply) go out first."""
        if not self.alive:
            return
        self._q.put(None)                           # writer closes after draining

    def close(self) -> None:
        """Immediate close (service stopping, client too slow)."""
        with self.lock:
            if not self.alive:
                return
            self.alive = False
        try:
            self.conn.shutdown(socket.SHUT_RDWR)    # wakes the reader's select/recv
        except OSError:
            pass
        self._q.put_nowait(None)                    # wakes the writer (unbounded queue)
        with self._room:
            self._room.notify_all()                 # wakes a send() waiting for room


class RpcServer:
    # A connection that sends nothing (or half a line) for this long, has no call
    # running and is not subscribed to events is closed, freeing its slot.
    IDLE_TIMEOUT = 30.0
    SEND_TIMEOUT = 10.0
    POLL = 0.25

    def __init__(self, path: Path, handlers: Dict[str, Handler]):
        self.path = Path(path)
        self.handlers = handlers
        self.idle_timeout = self.IDLE_TIMEOUT
        self._sock: Optional[socket.socket] = None
        self._clients: set = set()
        self._clients_lock = threading.Lock()
        self._threads: set = set()
        self._threads_lock = threading.Lock()
        self._stop = threading.Event()
        self._accept_thread: Optional[threading.Thread] = None
        self._client_slots = threading.BoundedSemaphore(MAX_ACTIVE_CLIENTS)
        self._call_slots = threading.BoundedSemaphore(MAX_ACTIVE_CALLS)

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _check_private_dir(self.path.parent)
        if self.path.exists() or self.path.is_symlink():
            if is_alive(self.path):
                raise RpcError(Message(N_("a service is already running at {path}"), path=self.path))
            self.path.unlink()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)
        try:
            sock.bind(str(self.path))
        finally:
            os.umask(old)
        os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        sock.listen(64)                  # > MAX_ACTIVE_CLIENTS: bursts are queued, not refused
        self._sock = sock
        self._stop.clear()
        self._accept_thread = threading.Thread(target=self._accept, name="openalloy-rpc",
                                               daemon=True)
        self._accept_thread.start()
        log.info("RPC listening on %s", self.path)

    def stop(self, timeout: float = 3.0) -> None:
        """Stop accepting, close every client, wait for the server's threads."""
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)   # wakes a blocked accept()
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
        with self._clients_lock:
            clients = list(self._clients)
        for c in clients:
            c.close()
        deadline = time.monotonic() + timeout
        with self._threads_lock:
            # a snapshot: the set changes while connections come and go
            workers = list(self._threads)
        threads = ([self._accept_thread] if self._accept_thread else []) + workers \
            + [c.writer for c in clients]
        for t in threads:
            # a thread registered a moment ago may not have started yet: it will find
            # the server stopped and end by itself, there is nothing to wait for
            if t is not threading.current_thread() and t.ident is not None:
                t.join(max(0.0, deadline - time.monotonic()))
        try:
            self.path.unlink()
        except OSError:
            pass

    def subscriber_count(self) -> int:
        with self._clients_lock:
            return sum(1 for c in self._clients if c.subscribed and c.alive)

    def broadcast(self, event: str, data: dict) -> None:
        """Never blocks: called from the engine thread. A subscriber that stopped
        reading (frozen GUI, suspended process) is dropped instead of stalling
        the lighting loop."""
        try:
            line = (json.dumps({"event": event, "data": data}) + "\n").encode()
        except (TypeError, ValueError):
            log.debug("event not serialisable: %s", event)
            return
        with self._clients_lock:
            targets = [c for c in self._clients if c.subscribed]
        for c in targets:
            c.push_event(line)

    def _spawn(self, target, *args, name: str) -> None:
        t = threading.Thread(target=self._tracked, args=(target,) + args, name=name, daemon=True)
        with self._threads_lock:
            self._threads.add(t)
        t.start()

    def _tracked(self, target, *args) -> None:
        try:
            target(*args)
        finally:
            with self._threads_lock:
                self._threads.discard(threading.current_thread())

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except OSError:
                return
            if self._stop.is_set():
                conn.close()
                return
            # Same-user check: the socket is 0600 in a 0700 runtime dir, and
            # SO_PEERCRED double-checks the caller's uid.
            try:
                import struct
                creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                        struct.calcsize("3i"))
                _pid, uid, _gid = struct.unpack("3i", creds)
                if uid not in (os.getuid(), 0):
                    conn.close()
                    continue
            except (OSError, AttributeError):
                pass
            if not self._client_slots.acquire(blocking=False):
                log.warning("RPC: too many clients connected; connection refused")
                conn.close()
                continue
            client = _Client(conn, self.SEND_TIMEOUT)
            with self._clients_lock:
                self._clients.add(client)
            if self._stop.is_set():          # stop() ran meanwhile and did not see it
                client.close()
            self._spawn(self._serve, client, name="openalloy-rpc-in")

    def _serve(self, client: _Client) -> None:
        conn = client.conn
        buf = bytearray()
        last = time.monotonic()
        try:
            while client.alive and not self._stop.is_set():
                ready, _w, _x = select.select([conn], [], [], self.POLL)
                if not ready:
                    idle = time.monotonic() - last > self.idle_timeout
                    if idle and not client.subscribed and client.pending == 0:
                        break                    # silent, half a line, or abandoned
                    continue
                chunk = conn.recv(65536)
                if not chunk:
                    break
                last = time.monotonic()
                buf += chunk
                while client.alive:
                    nl = buf.find(b"\n")
                    if nl < 0:
                        if len(buf) > MAX_REQUEST_BYTES:
                            client.send({"id": None, "error": _("request exceeds the size limit")})
                            return
                        break
                    raw = bytes(buf[:nl])
                    del buf[:nl + 1]
                    if len(raw) > MAX_REQUEST_BYTES:
                        client.send({"id": None, "error": _("request exceeds the size limit")})
                        return
                    self._handle_line(client, raw)
        except (OSError, ValueError):
            pass
        finally:
            with self._clients_lock:
                self._clients.discard(client)
            if client.pending == 0:
                client.finish()
            else:
                # let running calls deliver their results, then close
                threading.Thread(target=self._close_when_idle, args=(client,),
                                 daemon=True).start()
            self._client_slots.release()

    @staticmethod
    def _close_when_idle(client: _Client, limit: float = 600.0) -> None:
        end = time.monotonic() + limit
        while client.pending and time.monotonic() < end and client.alive:
            time.sleep(0.05)
        client.finish()

    def _handle_line(self, client: _Client, raw: bytes) -> None:
        try:
            line = raw.decode("utf-8")
        except UnicodeDecodeError:
            client.send({"id": None, "error": _("invalid encoding (UTF-8 expected)")})
            return
        if not line.strip():
            return
        try:
            req = json.loads(line)
            if not isinstance(req, dict):
                raise ValueError(Message(N_("the request must be an object")))
            rid = req.get("id")
            method = req.get("method", "")
            params = req.get("params") or {}
            if not isinstance(params, dict) or not isinstance(method, str):
                raise ValueError(Message(N_("invalid method/params")))
        except ValueError as exc:
            client.send({"id": None, "error": _("invalid request: {error}").format(error=exc)})
            return
        if method == "subscribe":
            client.subscribed = True
            client.send({"id": rid, "result": True})
            return
        handler = self.handlers.get(method)
        if handler is None:
            client.send({"id": rid, "error": _("unknown method: {method}").format(method=method)})
            return
        if not self._call_slots.acquire(blocking=False):
            client.send({"id": rid, "error": _("server busy; try again")})
            return
        with client.lock:
            client.pending += 1
        self._spawn(self._call_limited, client, handler, rid, params, name="openalloy-rpc-call")

    def _call_limited(self, client: _Client, handler, rid, params):
        try:
            self._call(handler, rid, params, client.send)
        finally:
            with client.lock:
                client.pending -= 1
            self._call_slots.release()

    @staticmethod
    def _call(handler, rid, params, send):
        def progress(fraction, message=""):
            try:
                send({"id": rid, "progress": round(float(fraction), 4), "message": message})
            except OSError:
                pass
        try:
            if getattr(handler, "wants_progress", False):
                params = dict(params, progress=progress)
            result = handler(**params)
            send({"id": rid, "result": result})
        except OSError:
            pass                                   # the client went away
        except TypeError as exc:
            try:
                send({"id": rid, "error": _("invalid parameters: {error}").format(error=exc)})
            except OSError:
                pass
        except Exception as exc:  # report every failure to the caller
            log.debug("rpc handler error", exc_info=True)
            try:
                send({"id": rid, "error": str(exc)})
            except OSError:
                pass


def with_progress(fn: Handler) -> Handler:
    fn.wants_progress = True  # type: ignore[attr-defined]
    return fn


class RpcClient:
    SUBSCRIBE_POLL = 0.5
    RECONNECT_DELAY = 1.0
    MAX_EVENT_BYTES = 8 * 1024 * 1024

    def __init__(self, path: Path, timeout: float = 10.0):
        self.path = Path(path)
        self.timeout = timeout
        self._id = 0
        self._lock = threading.Lock()

    def call(self, method: str, progress: Optional[Callable[[float, str], None]] = None,
             timeout: Optional[float] = None, **params) -> Any:
        with self._lock:
            self._id += 1
            rid = self._id
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout or self.timeout)
        try:
            sock.connect(str(self.path))
            sock.sendall((json.dumps({"id": rid, "method": method, "params": params}) + "\n")
                         .encode())
            for line in sock.makefile("r", encoding="utf-8"):
                msg = json.loads(line)
                if msg.get("id") != rid:
                    continue
                if "progress" in msg:
                    if progress:
                        progress(msg["progress"], msg.get("message", ""))
                    continue
                if "error" in msg:
                    raise RpcError(msg["error"])
                return msg.get("result")
            raise RpcError(Message(N_("connection closed by the service")))
        except TimeoutError as exc:
            raise RpcError(Message(N_("timed out waiting for '{method}'"), method=method)) from exc
        finally:
            sock.close()

    def subscribe(self, on_event: Callable[[str, dict], None],
                  stop: threading.Event) -> threading.Thread:
        """Deliver service events until ``stop`` is set; reconnects by itself (e.g.
        after a service restart). Reads with a short timeout, so setting ``stop``
        ends the thread within about a second even when no event arrives."""
        def run():
            while not stop.is_set():
                sock = None
                try:
                    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    sock.settimeout(self.SUBSCRIBE_POLL)
                    sock.connect(str(self.path))
                    sock.sendall(b'{"id": 0, "method": "subscribe"}\n')
                    buf = b""
                    while not stop.is_set():
                        try:
                            chunk = sock.recv(65536)
                        except TimeoutError:
                            continue
                        if not chunk:
                            break                         # service stopped
                        buf += chunk
                        if len(buf) > self.MAX_EVENT_BYTES and b"\n" not in buf:
                            break
                        *lines, buf = buf.split(b"\n")
                        for raw in lines:
                            if stop.is_set():
                                break
                            try:
                                msg = json.loads(raw)
                            except ValueError:
                                continue
                            if isinstance(msg, dict) and "event" in msg:
                                try:
                                    on_event(msg["event"], msg.get("data") or {})
                                except Exception:
                                    log.exception("event handler failed for %s", msg.get("event"))
                except OSError:
                    pass
                finally:
                    if sock is not None:
                        try:
                            sock.close()
                        except OSError:
                            pass
                stop.wait(self.RECONNECT_DELAY)
        t = threading.Thread(target=run, name="openalloy-subscribe", daemon=True)
        t.start()
        return t


def _check_private_dir(directory: Path) -> None:
    """The socket's directory must be ours and closed to others (the /tmp
    fallback could otherwise be pre-created by another user)."""
    st = os.lstat(directory)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise RpcError(Message(N_("{path} does not belong to this user; refusing to create the "
                         "socket"), path=directory))
    if st.st_mode & 0o022:
        os.chmod(directory, st.st_mode & ~0o022 & 0o7777)


def is_alive(path: Path) -> bool:
    try:
        return RpcClient(path, timeout=1.0).call("ping") == "pong"
    except (RpcError, OSError, ValueError):
        return False
