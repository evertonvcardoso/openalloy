# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""Start and drive the lab VM (QEMU) from the host.

Each run boots from a throw-away qcow2 overlay on top of the base image, so every
scenario starts from the same known state (the overlay is deleted afterwards).
KVM is used when /dev/kvm exists; otherwise QEMU's TCG emulation (slower, same
behaviour).
"""

from __future__ import annotations

import base64
import itertools
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional


class AgentError(RuntimeError):
    pass


class Agent:
    """Client of lab-agent.py over the virtio-serial chardev socket."""

    def __init__(self, path: Path):
        self.path = path
        self.sock: Optional[socket.socket] = None
        self.ids = itertools.count(1)
        self.waiting: Dict[int, Any] = {}
        self.cv = threading.Condition()
        self.reader: Optional[threading.Thread] = None

    def connect(self, timeout: float = 60) -> None:
        end = time.time() + timeout
        while True:
            try:
                s = socket.socket(socket.AF_UNIX)
                s.connect(str(self.path))
                self.sock = s
                break
            except OSError:
                if time.time() > end:
                    raise
                time.sleep(0.2)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        buf = b""
        while True:
            try:
                chunk = self.sock.recv(1 << 20)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                with self.cv:
                    self.waiting[msg.get("id")] = msg
                    self.cv.notify_all()

    def call(self, timeout: float = 120, **req) -> dict:
        rid = next(self.ids)
        req["id"] = rid
        data = (json.dumps(req) + "\n").encode()
        self.sock.sendall(data)
        end = time.time() + timeout + 5
        with self.cv:
            while rid not in self.waiting:
                left = end - time.time()
                if left <= 0:
                    raise AgentError(f"agent timeout: {req.get('op')} {req.get('cmd', '')[:80]}")
                self.cv.wait(left)
            return self.waiting.pop(rid)

    def run(self, cmd: str, user: str = "root", timeout: float = 120,
            env: Optional[dict] = None, check: bool = False) -> dict:
        res = self.call(timeout=timeout, op="run", cmd=cmd, user=user, env=env or {},
                        **{"timeout_s": timeout})
        if "error" in res:
            raise AgentError(res["error"])
        if check and res["rc"] != 0:
            raise AgentError(f"{cmd!r} -> rc {res['rc']}\n{res['out'][-2000:]}\n{res['err'][-2000:]}")
        return res

    def put(self, path: str, data: bytes, mode: int = 0o644, owner: Optional[str] = None) -> None:
        res = self.call(op="put", path=path, data=base64.b64encode(data).decode(), mode=mode,
                        owner=owner)
        if "error" in res:
            raise AgentError(res["error"])

    def get(self, path: str) -> bytes:
        res = self.call(op="get", path=path)
        if "error" in res:
            raise AgentError(res["error"])
        return base64.b64decode(res["data"])

    def wait_ready(self, timeout: float = 600) -> float:
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                self.call(timeout=5, op="ping")
                return time.time() - t0
            except AgentError:
                pass
        raise AgentError("guest agent did not answer")


class Qmp:
    def __init__(self, path: Path):
        self.path = path
        self.sock: Optional[socket.socket] = None
        self.file = None
        self.events: list = []
        self.lock = threading.Lock()

    def connect(self, timeout: float = 30) -> None:
        end = time.time() + timeout
        while True:
            try:
                s = socket.socket(socket.AF_UNIX)
                s.connect(str(self.path))
                break
            except OSError:
                if time.time() > end:
                    raise
                time.sleep(0.2)
        self.sock = s
        self.file = s.makefile("rwb", buffering=0)
        json.loads(self.file.readline())                     # greeting
        self.cmd("qmp_capabilities")

    def cmd(self, name: str, **args) -> Any:
        with self.lock:
            msg = {"execute": name}
            if args:
                msg["arguments"] = args
            self.file.write((json.dumps(msg) + "\r\n").encode())
            while True:
                reply = json.loads(self.file.readline())
                if "event" in reply:
                    self.events.append(reply)
                    continue
                if "error" in reply:
                    raise RuntimeError(reply["error"])
                return reply.get("return")

    def status(self) -> str:
        return self.cmd("query-status")["status"]


class Vm:
    def __init__(self, work: Path, name: str = "run", memory: int = 2048, cpus: int = 2):
        self.work = Path(work)
        self.name = name
        self.dir = self.work / f"vm-{name}"
        self.memory, self.cpus = memory, cpus
        self.proc: Optional[subprocess.Popen] = None
        self.agent = Agent(self.dir / "agent.sock")
        self.qmp = Qmp(self.dir / "qmp.sock")
        self.boot_seconds: Optional[float] = None

    @property
    def kvm(self) -> bool:
        return os.access("/dev/kvm", os.R_OK | os.W_OK)

    def start(self) -> None:
        if self.dir.exists():
            shutil.rmtree(self.dir)
        self.dir.mkdir(parents=True)
        overlay = self.dir / "disk.qcow2"
        subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", "-b",
                        str(self.work / "base.qcow2"), "-F", "qcow2", str(overlay)], check=True)
        accel = ["-accel", "kvm", "-cpu", "host"] if self.kvm else ["-accel", "tcg,thread=multi",
                                                                     "-cpu", "max"]
        cmd = ["qemu-system-x86_64", "-machine", "pc", *accel, "-smp", str(self.cpus),
               "-m", str(self.memory), "-nodefaults", "-vga", "std", "-display", "none",
               "-kernel", str(self.work / "vmlinuz"), "-initrd", str(self.work / "initrd.img"),
               "-append", "root=/dev/vda rw console=ttyS0 console=tty0 quiet "
                          "systemd.show_status=0 net.ifnames=1",
               "-drive", f"file={overlay},if=virtio,format=qcow2,cache=unsafe",
               "-netdev", "user,id=n0", "-device", "virtio-net-pci,netdev=n0",
               "-global", "PIIX4_PM.disable_s3=0",
               "-device", "virtio-serial",
               "-chardev", f"socket,id=agent,path={self.dir / 'agent.sock'},server=on,wait=off",
               "-device", "virtserialport,chardev=agent,name=lab.agent",
               "-qmp", f"unix:{self.dir / 'qmp.sock'},server=on,wait=off",
               "-serial", f"file:{self.dir / 'console.log'}",
               "-rtc", "base=utc,clock=host", "-no-reboot"]
        (self.dir / "qemu.cmd").write_text(" ".join(cmd))
        t0 = time.time()
        self.proc = subprocess.Popen(cmd, stdout=open(self.dir / "qemu.out", "w"),
                                     stderr=subprocess.STDOUT)
        self.qmp.connect()
        self.agent.connect()
        self.agent.wait_ready()
        self.boot_seconds = round(time.time() - t0, 1)

    # --- power -------------------------------------------------------------------------
    def wait_status(self, want: str, timeout: float = 120) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if self.qmp.status() == want:
                return True
            time.sleep(0.3)
        return False

    def wakeup(self) -> None:
        self.qmp.cmd("system_wakeup")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.qmp.cmd("quit")
            except Exception:
                pass
            try:
                self.proc.wait(20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)

    def discard(self) -> None:
        self.stop()
        overlay = self.dir / "disk.qcow2"
        if overlay.exists():
            overlay.unlink()
