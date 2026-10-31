# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""1.5.3: findings from the journal of the real HP keyboard (03f0:098f) and the 1.5.2 review."""
import io
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from openalloy import i18n
from openalloy.config.settings import PresetStore, Settings
from openalloy.i18n import N_, Message, english

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "openalloy" / "resources" / "openalloy-sleep"


def wait_until(cond, timeout=4.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


# --- 1. the journal stays in English (one line was half Portuguese) ---------------------------

@pytest.fixture
def journal():
    from openalloy.infra.logging_config import setup_logging
    setup_logging(daemon=True)
    buf = io.StringIO()
    logging.getLogger().handlers[0].stream = buf
    return buf


def _failing_write():
    from openalloy.infra.transport import HidrawTransport, TransportError

    class Closed(HidrawTransport):
        def open(self):
            self._fd = 2 ** 20                 # never a valid descriptor: EBADF

    try:
        Closed("/dev/hidraw6").write(b"x")
    except TransportError as exc:
        return exc
    raise AssertionError("write did not fail")


def test_vendor_channel_line_is_english_in_pt_br(journal):
    i18n.set_language("pt_BR")
    exc = _failing_write()
    logging.getLogger("openalloy.infra.drivers").info(
        "Vendor channel (interface 2) refused (%s); using HID LampArray", exc)
    line = journal.getvalue()
    assert "write to /dev/hidraw6 failed" in line
    assert "falha" not in line
    assert line.count("[Errno") == 1                 # was "[Errno 71] ...: [Errno 71] ..."
    assert "falha de escrita em /dev/hidraw6" in str(exc)    # the window still translates it


def test_engine_error_logged_in_english_and_shown_in_the_current_language(tmp_path, journal):
    from openalloy.application.engine import Engine
    from openalloy.infra.transport import DeviceNotFound

    def opener():
        raise DeviceNotFound(19, Message(N_("HyperX Alloy Origins Core not found")))
    i18n.set_language("pt_BR")
    e = Engine(opener, Settings(tmp_path / "c.json"), PresetStore(tmp_path / "p"),
               enable_input=False, presence=lambda: ())
    e._connect()
    assert "Keyboard not detected: [Errno 19] HyperX Alloy Origins Core not found" in journal.getvalue()
    pt = e.error_text()
    i18n.set_language("en")
    assert e.error_text() != pt                       # follows a language switch
    assert e.error_text().startswith("Keyboard not detected")


def test_every_core_module_raises_lazy_messages():
    """No _("...") left inside a raise in the core: those reached the journal translated."""
    import ast
    core = [p for p in (ROOT / "openalloy").rglob("*.py") if "presentation" not in p.parts]
    offenders = []
    for path in core:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Raise) and node.exc is not None:
                for sub in ast.walk(node.exc):
                    if isinstance(sub, ast.Call) and getattr(sub.func, "id", None) == "_":
                        offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert offenders == []


def test_message_keeps_format_semantics_and_holds_no_traceback():
    try:
        raise OSError(71, "Protocol error")
    except OSError as exc:
        m = Message(N_("{name}: invalid value {value!r} ({reason})"), name="fps", value=5, reason=exc)
    assert m.english() == "fps: invalid value 5 ([Errno 71] Protocol error)"
    assert not any(isinstance(v, BaseException) for v in m.params.values())
    assert m == Message(N_("{name}: invalid value {value!r} ({reason})"), name="fps", value=5,
                        reason=OSError(71, "Protocol error"))
    assert english(ValueError(Message(N_("text is expected")))) == "text is expected"
    assert english(KeyError("x")) == str(KeyError("x"))
    reasons = Message(N_("the keyboard was found but refused the commands ({details})"),
                      details=(Message(N_("vendor channel refused by the firmware")), "LampArray: x"))
    assert reasons.english().endswith("(vendor channel refused by the firmware; LampArray: x)")


def test_broken_translation_falls_back_to_english(monkeypatch):
    m = Message(N_("{path} does not exist"), path="/dev/x")
    monkeypatch.setattr(i18n, "_", lambda s: "{caminho} nao existe")
    assert str(m) == "/dev/x does not exist"


# --- 2. sleep: systemd 256+ freezes user services while the hook runs ------------------------

class Gap:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


@pytest.fixture
def fixed_hp(tmp_path):
    from openalloy.application.engine import Engine
    from openalloy.infra.drivers import open_driver
    from openalloy.infra.simulator import SimulatedLampArray
    arr = SimulatedLampArray()
    gap = Gap()
    e = Engine(lambda: open_driver("hp", arr), Settings(tmp_path / "c.json"),
               PresetStore(tmp_path / "p"), enable_input=False, sleep_gap=gap)
    e.arr, e.gap = arr, gap
    e.start()
    e.fix({"effect": "static", "params": {"color": "#00ff00"}})
    assert wait_until(lambda: e.status()["fixed_applied"])
    yield e
    e.stop()


def test_resume_is_noticed_without_the_hook(fixed_hp, journal):
    e = fixed_hp
    sent = e._frames_sent
    e.gap.value += 60                                  # the PC slept for a minute
    assert wait_until(lambda: e._frames_sent > sent and e.status()["fixed_applied"], 5)
    assert e.arr.key_colors()[30] == (0, 255, 0)
    assert "System resumed (60 s asleep)" in journal.getvalue()


def test_late_sleep_notice_is_ignored(fixed_hp):
    """Frozen service: "suspend" and "wake" both arrive after the resume, in any order."""
    from openalloy.application.engine import boottime
    e = fixed_hp
    sent_at = boottime() - 30                          # queued while the PC slept
    e.wake(sent_at=sent_at)
    e.suspend(sent_at=sent_at)
    assert e._asleep is False
    assert wait_until(lambda: e.status()["fixed_applied"])


def test_notice_lost_after_suspend_still_wakes(fixed_hp):
    e = fixed_hp
    e.suspend()                                        # delivered before the sleep
    assert e._asleep
    e.gap.value += 30                                  # ...but "wake" never comes
    assert wait_until(lambda: not e._asleep and e.status()["fixed_applied"], 5)


def test_wake_and_clock_do_not_take_the_keyboard_back_twice(fixed_hp, journal):
    e = fixed_hp
    e.suspend()
    e.gap.value += 30
    e.wake()                                           # hook first: the clock check is a no-op
    time.sleep(0.6)
    e.wake()                                           # and a second wake changes nothing
    log = journal.getvalue()
    assert log.count("System resumed") == 1


def test_rpc_accepts_the_hook_parameter(fixed_hp, tmp_path):
    """The real hook request goes through the RPC layer to the engine."""
    from openalloy.application.backend import make_handlers
    from openalloy.application.engine import boottime
    from openalloy.infra.rpc import RpcClient, RpcServer
    d = tmp_path / "r"
    d.mkdir(mode=0o700)
    srv = RpcServer(d / "s.sock", make_handlers(fixed_hp))
    srv.start()
    try:
        RpcClient(srv.path).call("suspend", sent_at=boottime())
        assert fixed_hp._asleep
        RpcClient(srv.path).call("wake", sent_at=boottime())
        assert not fixed_hp._asleep
    finally:
        srv.stop()


def _load_hook():
    ns = {"__name__": "openalloy_sleep_hook"}
    exec(compile(HOOK.read_text(), str(HOOK), "exec"), ns)       # no __pycache__ in the package
    return ns


class FakeDaemon:
    """Answers like the service; records what it got. ``old`` = 1.5.2 (no parameters)."""

    def __init__(self, path, old=False):
        self.got = []
        self.old = old
        self.srv = socket.socket(socket.AF_UNIX)
        self.srv.bind(str(path))
        self.srv.listen(8)
        self.srv.settimeout(0.2)
        self.stop = threading.Event()
        self.t = threading.Thread(target=self.run, daemon=True)
        self.t.start()

    def run(self):
        while not self.stop.is_set():
            try:
                conn, _addr = self.srv.accept()
            except OSError:
                continue
            with conn:
                req = json.loads(conn.makefile().readline())
                self.got.append(req)
                if self.old and req.get("params"):
                    reply = {"id": req["id"], "error": "invalid parameters"}
                else:
                    reply = {"id": req["id"], "result": None}
                conn.sendall((json.dumps(reply) + "\n").encode())

    def close(self):
        self.stop.set()
        self.t.join(2)
        self.srv.close()


@pytest.fixture
def hook(tmp_path, monkeypatch):
    ns = _load_hook()
    cg = tmp_path / "user.slice"
    cg.mkdir()
    (cg / "cgroup.freeze").write_text("0\n")
    (cg / "cgroup.events").write_text("populated 1\nfrozen 0\n")
    sock = tmp_path / "openalloy.sock"
    ns["CGROUP"] = str(cg)
    monkeypatch.setattr(ns["glob"], "glob", lambda pattern: [str(sock)] if sock.exists() else [])
    ns["cg"], ns["sock"], ns["monkeypatch"] = cg, sock, monkeypatch
    return ns


def test_hook_does_not_wait_on_frozen_user_services(hook):
    d = FakeDaemon(hook["sock"])
    try:
        (hook["cg"] / "cgroup.events").write_text("populated 1\nfrozen 1\n")
        t0 = time.time()
        hook["notify"]("suspend")
        assert time.time() - t0 < 0.2 and d.got == []
    finally:
        d.close()


def test_hook_sends_the_time_and_talks_to_an_old_daemon(hook):
    d = FakeDaemon(hook["sock"])
    try:
        hook["notify"]("suspend")
        assert d.got[0]["params"]["sent_at"] > 0
        assert len(d.got) == 1
    finally:
        d.close()
    os.unlink(hook["sock"])
    old = FakeDaemon(hook["sock"], old=True)
    try:
        hook["notify"]("wake")
        assert [bool(r.get("params")) for r in old.got] == [True, False]   # retried plain
    finally:
        old.close()


def test_hook_is_bounded_even_if_the_freeze_is_not_seen(hook, tmp_path):
    """cgroup v1 or another freezer: a stopped service costs at most REPLY_TIMEOUT."""
    env = dict(os.environ, PYTHONPATH=str(ROOT), XDG_RUNTIME_DIR=str(tmp_path / "run"),
               XDG_CONFIG_HOME=str(tmp_path / "cfg"))
    (tmp_path / "run").mkdir(mode=0o700, exist_ok=True)
    sock = tmp_path / "run" / "openalloy.sock"
    proc = subprocess.Popen([sys.executable, "-m", "openalloy.presentation.daemon.main",
                             "--simulate-hp"], env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    try:
        assert wait_until(sock.exists, 10)
        hook["monkeypatch"].setattr(hook["glob"], "glob", lambda pattern: [str(sock)])
        os.kill(proc.pid, signal.SIGSTOP)
        t0 = time.time()
        hook["notify"]("suspend")
        assert time.time() - t0 < hook["REPLY_TIMEOUT"] + 0.5
    finally:
        os.kill(proc.pid, signal.SIGCONT)
        proc.terminate()
        proc.wait(8)


# --- 3. RPC: a burst no longer drops a client that is reading -----------------------------------

def test_burst_reaches_a_reading_subscriber_in_order(tmp_path):
    from openalloy.infra.rpc import RpcClient, RpcServer
    d = tmp_path / "r"
    d.mkdir(mode=0o700)
    srv = RpcServer(d / "s.sock", {})
    srv.start()
    got, stop = [], threading.Event()
    sub = RpcClient(srv.path).subscribe(lambda k, data: got.append(data["n"]), stop)
    try:
        assert wait_until(lambda: srv.subscriber_count() == 1)
        status = {"pad": "x" * 1500}
        for n in range(2000):                           # ~3 MB at once (1.5.2: dropped at 256)
            srv.broadcast("status", dict(status, n=n))
        assert wait_until(lambda: len(got) == 2000, 10)
        assert got == list(range(2000))
    finally:
        stop.set()
        sub.join(3)
        srv.stop()


def test_backlog_over_the_byte_limit_drops_only_that_client(tmp_path, monkeypatch):
    from openalloy.infra import rpc
    monkeypatch.setattr(rpc._Client, "QUEUE_BYTES", 64 * 1024)
    d = tmp_path / "r"
    d.mkdir(mode=0o700)
    srv = rpc.RpcServer(d / "s.sock", {"ping": lambda: "pong"})
    srv.start()
    stuck = socket.socket(socket.AF_UNIX)
    try:
        stuck.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        stuck.connect(str(srv.path))
        stuck.sendall(b'{"id": 0, "method": "subscribe"}\n')
        assert wait_until(lambda: srv.subscriber_count() == 1)
        for n in range(400):
            srv.broadcast("status", {"pad": "x" * 2000, "n": n})
        assert wait_until(lambda: srv.subscriber_count() == 0, 5)
        assert rpc.RpcClient(srv.path).call("ping") == "pong"
    finally:
        stuck.close()
        srv.stop()


# --- 4. CLI: a closed pipe is not an error -----------------------------------------------------

def test_cli_output_into_head_is_quiet(tmp_path):
    env = dict(os.environ, PYTHONPATH=str(ROOT), XDG_CONFIG_HOME=str(tmp_path / "cfg"),
               XDG_RUNTIME_DIR=str(tmp_path / "run"))
    (tmp_path / "run").mkdir(mode=0o700, exist_ok=True)
    p = subprocess.run(f"{sys.executable} -m openalloy.presentation.cli.main --simulate-hp "
                       "status | head -1", shell=True, env=env, capture_output=True, text=True,
                       timeout=30)
    assert p.stdout.startswith("OpenAlloy")
    assert "Broken pipe" not in p.stderr and "Error" not in p.stderr


# --- 5. the service always stops after a freeze/thaw --------------------------------------------

def test_service_stops_after_being_frozen_and_thawed(tmp_path):
    """1.5.2: after SIGSTOP/SIGCONT the kernel could give SIGTERM to a worker thread and
    the main thread, blocked in an endless wait(), never stopped (systemd: until SIGKILL)."""
    env = dict(os.environ, PYTHONPATH=str(ROOT), XDG_RUNTIME_DIR=str(tmp_path / "run"),
               XDG_CONFIG_HOME=str(tmp_path / "cfg"))
    (tmp_path / "run").mkdir(mode=0o700, exist_ok=True)
    sock = tmp_path / "run" / "openalloy.sock"
    for _attempt in range(3):
        proc = subprocess.Popen([sys.executable, "-m", "openalloy.presentation.daemon.main",
                                 "--simulate-hp"], env=env, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        try:
            assert wait_until(sock.exists, 10)
            time.sleep(0.3)
            os.kill(proc.pid, signal.SIGSTOP)
            os.kill(proc.pid, signal.SIGCONT)
            proc.terminate()
            assert proc.wait(timeout=3) == 0
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(3)


def _start_daemon(tmp_path):
    env = dict(os.environ, PYTHONPATH=str(ROOT), XDG_RUNTIME_DIR=str(tmp_path / "run"),
               XDG_CONFIG_HOME=str(tmp_path / "cfg"))
    (tmp_path / "run").mkdir(mode=0o700, exist_ok=True)
    sock = tmp_path / "run" / "openalloy.sock"
    log = open(tmp_path / "daemon.log", "a")
    proc = subprocess.Popen([sys.executable, "-m", "openalloy.presentation.daemon.main",
                             "--simulate-hp"], env=env, stdout=log, stderr=subprocess.STDOUT)
    log.close()
    from openalloy.infra.rpc import is_alive
    assert wait_until(lambda: is_alive(sock), 10)          # fully started, answering
    return proc, sock


def test_stop_never_deadlocks_in_the_signal_handler(tmp_path, hook):
    """1.5.2: SIGTERM's handler called Event.set() and could deadlock against the
    main thread's Event.wait() (seen 2 times in 6 with a hook request pending)."""
    hook["CGROUP"] = str(tmp_path / "no-cgroup")
    for _attempt in range(5):
        proc, sock = _start_daemon(tmp_path)
        try:
            hook["monkeypatch"].setattr(hook["glob"], "glob", lambda pattern, s=sock: [str(s)])
            os.kill(proc.pid, signal.SIGSTOP)
            hook["notify"]("suspend")
            os.kill(proc.pid, signal.SIGCONT)
            proc.terminate()
            rc = proc.wait(timeout=4)
            assert rc == 0, (tmp_path / "daemon.log").read_text()[-3000:]
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(3)


def test_reload_signal_keeps_the_service_answering(tmp_path):
    from openalloy.infra.rpc import RpcClient
    proc, sock = _start_daemon(tmp_path)
    try:
        cfg = tmp_path / "cfg" / "openalloy" / "config.json"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps({"language": "pt_BR"}))
        for _i in range(5):
            proc.send_signal(signal.SIGHUP)
        assert wait_until(lambda: RpcClient(sock).call("ping") == "pong", 5)
        i18n.set_language("pt_BR")
        off_pt = i18n._("off")
        i18n.set_language("en")
        assert off_pt != "off"
        assert wait_until(lambda: RpcClient(sock).call("status")["audio"] == off_pt, 5)
        proc.terminate()
        assert proc.wait(timeout=4) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(3)


def test_rpc_stop_while_connections_arrive(tmp_path):
    """1.5.2: stop() could join a just-registered thread that had not started yet
    ("cannot join thread before it is started" -> the service exited with code 1)."""
    from openalloy.infra.rpc import RpcServer
    d = tmp_path / "r"
    d.mkdir(mode=0o700)
    srv = RpcServer(d / "s.sock", {"ping": lambda: "pong"})
    srv.start()
    srv._threads.add(threading.Thread(target=lambda: None))   # registered, not started yet
    srv.stop()                                                 # 1.5.2: RuntimeError here
    for _round in range(20):
        srv = RpcServer(d / "s.sock", {"ping": lambda: "pong"})
        srv.start()
        go = threading.Event()

        def knock(go=go, srv=srv):
            go.wait()
            for _i in range(5):
                try:
                    c = socket.socket(socket.AF_UNIX)
                    c.settimeout(0.5)
                    c.connect(str(srv.path))
                    c.sendall(b'{"id": 1, "method": "ping"}\n')
                    c.close()
                except OSError:
                    pass
        knockers = [threading.Thread(target=knock) for _k in range(6)]
        for k in knockers:
            k.start()
        go.set()
        srv.stop()                                  # must not raise
        for k in knockers:
            k.join(3)
    assert wait_until(lambda: not [t for t in threading.enumerate()
                                   if t.name.startswith("openalloy-rpc")], 5)


@pytest.mark.parametrize("value", ["abc", "nan", "inf", "1e400", True, None, [1]])
def test_number_settings_reject_with_our_own_message(tmp_path, value):
    from openalloy.config.settings import validate
    i18n.set_language("pt_BR")
    with pytest.raises(ValueError) as err:
        validate("fps", value)
    assert "esperado um número" in str(err.value)
    assert "number is expected" in english(err.value)
