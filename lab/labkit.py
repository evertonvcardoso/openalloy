# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""Shared machinery of the lab: emulator process, VM, checks and evidence."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from hidemu.control import ControlClient  # noqa: E402
from vm.vmctl import Vm  # noqa: E402

HP, KINGSTON = "1-2", "1-1"


class Emulator:
    def __init__(self, out: Path, variant: str = "abnt2"):
        self.sock = out / "hidemu.sock"
        self.out = out
        if self.sock.exists():
            self.sock.unlink()                    # stale, from an earlier run
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "hidemu", "--control", str(self.sock), "serve",
             "--kingston", KINGSTON, "--hp", HP, "--variant", variant],
            cwd=str(HERE), stdout=open(out / "hidemu.log", "w"), stderr=subprocess.STDOUT)
        for _ in range(200):
            try:
                self.c = ControlClient(self.sock)
                break
            except OSError:
                if self.proc.poll() is not None:
                    raise RuntimeError("emulator exited: "
                                       + (out / "hidemu.log").read_text()[-500:]) from None
                time.sleep(0.05)
        else:
            self.proc.kill()
            raise RuntimeError("emulator did not start")

    def __call__(self, op: str, **kw) -> Any:
        return self.c(op, **kw)

    def stop(self) -> None:
        try:
            self.c.close()
        except Exception:
            pass
        self.proc.terminate()
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


class Lab:
    def __init__(self, work: Path, out: Path, label: str):
        self.work, self.out, self.label = Path(work), Path(out), label
        self.out.mkdir(parents=True, exist_ok=True)
        self.results: List[dict] = []
        self.metrics: Dict[str, Any] = {}
        self.vm = Vm(self.work, label)
        self.emu = Emulator(self.out)
        self.scenario = "?"
        self.t0 = time.time()

    # --- lifecycle ------------------------------------------------------------------
    def boot(self) -> None:
        self.vm.start()
        self.metrics["boot_seconds"] = self.vm.boot_seconds

    def close(self) -> None:
        try:
            self.save()
        finally:
            try:
                self.vm.discard()
            finally:
                self.emu.stop()

    def save(self) -> None:
        (self.out / "results.json").write_text(json.dumps(
            {"label": self.label, "results": self.results, "metrics": self.metrics,
             "seconds": round(time.time() - self.t0, 1)}, indent=1, default=str))

    # --- checks -----------------------------------------------------------------------
    def check(self, name: str, ok: bool, detail: Any = "", **evidence) -> bool:
        entry = {"scenario": self.scenario, "check": name, "ok": bool(ok),
                 "detail": detail if isinstance(detail, str) else json.dumps(detail, default=str),
                 "t": round(time.time() - self.t0, 1)}
        if evidence:
            entry["evidence"] = evidence
        self.results.append(entry)
        print(("PASS " if ok else "FAIL ") + f"[{self.scenario}] {name}"
              + (f" - {entry['detail'][:300]}" if entry["detail"] else ""), flush=True)
        self.save()
        return bool(ok)

    def note(self, name: str, detail: Any) -> None:
        self.results.append({"scenario": self.scenario, "check": name, "ok": None,
                             "detail": detail if isinstance(detail, str) else
                             json.dumps(detail, default=str), "t": round(time.time() - self.t0, 1)})
        print(f"INFO [{self.scenario}] {name} - {str(detail)[:300]}", flush=True)
        self.save()

    def begin(self, scenario: str) -> None:
        self.scenario = scenario
        print(f"\n=== {scenario} ===", flush=True)

    # --- guest helpers -------------------------------------------------------------------
    def run(self, cmd: str, user: str = "root", timeout: float = 120, check: bool = False,
            env: Optional[dict] = None) -> dict:
        return self.vm.agent.run(cmd, user=user, timeout=timeout, check=check, env=env)

    def oa(self, args: str, timeout: float = 90, lang: str = "en") -> dict:
        """openalloy as the desktop user. English output by default (parsable)."""
        return self.run(f"openalloy --lang {lang} {args}", user="tester", timeout=timeout)

    def status(self) -> dict:
        r = self.oa("--json status", timeout=60)
        try:
            return json.loads(r["out"])
        except ValueError:
            return {"_raw": r["out"] + r["err"], "_rc": r["rc"]}

    def epoch(self) -> float:
        return float(self.run("date +%s.%N")["out"].strip())

    def journal(self, since: Optional[float] = None, unit: str = "openalloy.service",
                user_unit: bool = True) -> str:
        sel = f"_SYSTEMD_USER_UNIT={unit}" if user_unit else f"-u {unit}"
        since_arg = f"--since @{since:.3f}" if since else ""
        return self.run(f"journalctl {sel} {since_arg} -o short-precise --no-pager")["out"]

    def svc_pid(self) -> int:
        r = self.run("systemctl --user show -p MainPID --value openalloy", user="tester")
        try:
            return int(r["out"].strip())
        except ValueError:
            return 0

    def proc_metrics(self, pid: int) -> dict:
        if not pid:
            return {}
        r = self.run(f"cat /proc/{pid}/stat; grep -E 'VmRSS|Threads' /proc/{pid}/status; "
                     f"ls /proc/{pid}/fd | wc -l; getconf CLK_TCK")
        lines = r["out"].split("\n")
        try:
            stat = lines[0].rsplit(")", 1)[1].split()
            ticks = int(stat[11]) + int(stat[12])
            rss = int(lines[1].split()[1])
            threads = int(lines[2].split()[1])
            fds = int(lines[3])
            hz = int(lines[4])
            return {"cpu_ticks": ticks, "rss_kb": rss, "threads": threads, "fds": fds, "hz": hz}
        except (IndexError, ValueError):
            return {"raw": r["out"]}

    def attach(self, busids: List[str], timeout: float = 30) -> bool:
        """Exactly these keyboards plugged in. The others stay *unplugged* at the
        emulator (plugging them straight back raced with the guest's attach loop,
        which could still be working from the previous list)."""
        others = [b for b in (HP, KINGSTON) if b not in busids]
        for b in others:
            self.emu("unplug", busid=b)
        self.run(f"echo {' '.join(busids)} > /etc/lab/attach")
        for b in busids:
            self.emu("plug", busid=b)
        return self.wait(lambda: all(self.emu("attached", busid=b) for b in busids)
                         and all(not self.emu("attached", busid=b) for b in others), timeout)

    def wait(self, cond: Callable[[], Any], timeout: float = 15, step: float = 0.25) -> Any:
        end = time.time() + timeout
        last = None
        while time.time() < end:
            try:
                last = cond()
            except Exception as exc:          # a probe failing just means "not yet"
                last = None
                _ = exc
            if last:
                return last
            time.sleep(step)
        return last

    def emu_state(self, busid: str) -> dict:
        return self.emu("state", busid=busid)

    def snap(self, name: str, busid: str, title: str = "") -> str:
        svg = self.emu("render", busid=busid, title=title or name)
        path = self.out / f"{name}.svg"
        path.write_text(svg)
        return path.name

    def log_seq(self, busid: str) -> int:
        return int(self.emu_state(busid)["log_seq"])

    def log_since(self, busid: str, seq: int) -> List[dict]:
        return self.emu("log", busid=busid, since=seq, limit=100000)

    def put_file(self, local: Path, remote: str, owner: str = "tester", mode: int = 0o644) -> None:
        self.vm.agent.put(remote, Path(local).read_bytes(), mode=mode, owner=owner)

    def screenshot(self, name: str, display: str = ":99") -> Optional[str]:
        remote = f"/tmp/{name}.xwd"
        r = self.run(f"xwd -root -silent -display {display} -out {remote}", user="tester")
        if r["rc"] != 0:
            return None
        data = self.vm.agent.get(remote)
        xwd = self.out / f"{name}.xwd"
        xwd.write_bytes(data)
        png = self.out / f"{name}.png"
        subprocess.run(["convert", str(xwd), str(png)], check=False)
        xwd.unlink()
        return png.name if png.exists() else None


def q(s: str) -> str:
    return shlex.quote(s)


def key_colors_close(a, b, tol: int = 2) -> bool:
    return a is not None and b is not None and all(abs(int(x) - int(y)) <= tol for x, y in zip(a, b))


def env_flag(name: str) -> bool:
    return os.environ.get(name, "") not in ("", "0")
