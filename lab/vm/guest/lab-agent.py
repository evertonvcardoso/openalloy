#!/usr/bin/python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""Lab guest agent: runs commands for the host over a virtio-serial port.

Protocol: one JSON object per line, both ways.
  {"id": 1, "op": "run", "cmd": "...", "user": "tester"|"root", "timeout": 60, "env": {}}
     -> {"id": 1, "rc": 0, "out": "...", "err": "...", "dt": 0.12}
  {"id": 2, "op": "put", "path": "/x", "data": "<base64>", "mode": 420}
  {"id": 3, "op": "get", "path": "/x"}  -> {"id": 3, "data": "<base64>"}
  {"id": 4, "op": "ping"}
Commands run in threads, so a long one never blocks the channel. Lab use only.
"""
import base64
import json
import os
import pwd
import subprocess
import threading
import time

PORT = "/dev/virtio-ports/lab.agent"


def user_env(name):
    pw = pwd.getpwnam(name)
    run = f"/run/user/{pw.pw_uid}"
    return pw, {"HOME": pw.pw_dir, "USER": name, "LOGNAME": name, "SHELL": "/bin/bash",
                "XDG_RUNTIME_DIR": run, "DBUS_SESSION_BUS_ADDRESS": f"unix:path={run}/bus",
                "PATH": f"{pw.pw_dir}/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"}


def handle(req):
    op = req.get("op")
    if op == "ping":
        return {"pong": time.time()}
    if op == "put":
        path = req["path"]
        os.makedirs(os.path.dirname(path) or "/", exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(base64.b64decode(req["data"]))
        os.chmod(path, req.get("mode", 0o644))
        if req.get("owner"):
            pw = pwd.getpwnam(req["owner"])
            os.chown(path, pw.pw_uid, pw.pw_gid)
        return {"ok": True}
    if op == "get":
        with open(req["path"], "rb") as fh:
            return {"data": base64.b64encode(fh.read()).decode()}
    if op == "run":
        env = dict(os.environ)
        user = req.get("user", "root")
        preexec = None
        cwd = "/root"
        if user != "root":
            pw, uenv = user_env(user)
            env = uenv
            cwd = pw.pw_dir

            def preexec():
                os.initgroups(user, pw.pw_gid)
                os.setgid(pw.pw_gid)
                os.setuid(pw.pw_uid)
        env.update(req.get("env") or {})
        t0 = time.monotonic()
        try:
            p = subprocess.run(["/bin/bash", "-lc", req["cmd"]], capture_output=True,
                               timeout=req.get("timeout_s", 120), env=env, cwd=cwd,
                               preexec_fn=preexec, stdin=subprocess.DEVNULL)
            rc, out, err = p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired as exc:
            rc, out, err = -999, exc.stdout or b"", (exc.stderr or b"") + b"\n[lab-agent] TIMEOUT"
        return {"rc": rc, "out": out.decode("utf-8", "replace"),
                "err": err.decode("utf-8", "replace"), "dt": round(time.monotonic() - t0, 3)}
    return {"error": f"unknown op {op!r}"}


def main():
    while not os.path.exists(PORT):
        time.sleep(0.2)
    fd = os.open(PORT, os.O_RDWR)
    rf = os.fdopen(fd, "rb", buffering=0)
    wlock = threading.Lock()

    def reply(obj):
        data = (json.dumps(obj) + "\n").encode()
        with wlock:
            view = memoryview(data)
            while view:
                n = os.write(fd, view)
                view = view[n:]

    def work(req):
        try:
            res = handle(req)
        except Exception as exc:          # report, never die
            res = {"error": repr(exc)}
        res["id"] = req.get("id")
        reply(res)

    buf = b""
    while True:
        chunk = rf.read(65536)
        if not chunk:
            time.sleep(0.05)
            continue
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if line.strip():
                try:
                    req = json.loads(line)
                except ValueError:
                    continue
                threading.Thread(target=work, args=(req,), daemon=True).start()


if __name__ == "__main__":
    main()
