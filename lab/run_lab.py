#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""OpenAlloy lab: runs the full scenario set against one version, in a fresh VM.

    python3 lab/run_lab.py --work ./lab-work --zip openalloy-1.5.3.zip --out results/1.5.3

What runs where:
  * the program under test: inside a Debian forky VM (QEMU), installed with its own
    install.sh, as a desktop user with a local login on seat0;
  * the keyboards: hidemu on the host, attached to the VM over USB/IP, so the guest
    kernel's usbhid/hidraw/evdev/udev handle them as real USB devices;
  * the checks: here, from what the emulated devices *received* and from the guest's
    journal, files and processes.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from hidemu import keytable  # noqa: E402
from labkit import HP, KINGSTON, Lab, key_colors_close  # noqa: E402

RED, WHITE, BLACK = (255, 0, 0), (255, 255, 255), (0, 0, 0)


def openalloy_tables():
    from openalloy.domain.layout import HID_USAGE, get_layout
    return HID_USAGE, get_layout


# --- scenarios ----------------------------------------------------------------------------

def sc_environment(lab: Lab) -> None:
    lab.begin("environment")
    lab.wait(lambda: "seat0" in lab.run("loginctl list-sessions --no-legend")["out"], 90)
    r = lab.run("uname -r; python3 --version; systemctl --version | head -1; "
                "cat /etc/os-release | grep PRETTY; loginctl list-sessions --no-legend")
    lab.note("guest", r["out"].strip())
    lab.check("desktop-like login on seat0", "seat0" in r["out"] and "tty1" in r["out"],
              r["out"].strip().splitlines()[-1] if r["out"].strip() else "")
    lab.metrics["guest"] = r["out"].strip()


def sc_install(lab: Lab, zip_path: Path) -> None:
    lab.begin("install")
    lab.put_file(zip_path, f"/home/tester/{zip_path.name}")
    r = lab.run(f"cd ~ && rm -rf oa-src && mkdir oa-src && cd oa-src && "
                f"python3 -m zipfile -e ../{zip_path.name} . && cd openalloy && "
                f"bash ./install.sh --lang pt_BR --reactive", user="tester", timeout=900)
    (lab.out / "install.log").write_text(r["out"] + "\n--- stderr ---\n" + r["err"])
    lab.check("install.sh finishes", r["rc"] == 0, f"rc={r['rc']}",
              tail=(r["out"] + r["err"])[-1500:])
    v = lab.oa("--version")
    lab.check("openalloy on PATH, right version", v["rc"] == 0 and lab.label in v["out"], v["out"].strip())
    files = lab.run("ls /etc/udev/rules.d/ | grep -i openalloy; "
                    "ls /usr/lib/systemd/system-sleep/ /etc/systemd/system-sleep/ 2>/dev/null | grep openalloy; "
                    "ls ~tester/.config/systemd/user/ 2>/dev/null; "
                    "ls ~tester/.local/share/applications/ 2>/dev/null")
    lab.check("udev rules installed", "60-openalloy.rules" in files["out"], files["out"])
    lab.check("reactive udev rule installed (--reactive)", "61-openalloy-reactive" in files["out"])
    lab.check("systemd-sleep hook installed", "\nopenalloy" in "\n" + files["out"])
    act = lab.wait(lambda: lab.run("systemctl --user is-active openalloy",
                                   user="tester")["out"].strip() == "active", 30)
    lab.check("user service active", bool(act))
    lang = lab.oa("language")
    lab.check("language saved by the installer (pt_BR)", "* pt_BR" in lang["out"], lang["out"].strip())


def sc_permissions(lab: Lab) -> None:
    lab.begin("permissions")
    lab.attach([HP, KINGSTON])
    time.sleep(3)
    r = lab.run("for d in /sys/class/hidraw/*; do n=/dev/$(basename $d); "
                "i=$(cat $(dirname $(readlink -f $d/device))/bInterfaceNumber); "
                "id=$(grep HID_ID $d/device/uevent | cut -d= -f2); "
                "echo \"$n $id if=$i $(getfacl -p $n 2>/dev/null | grep '^user:tester' || echo no-acl)\"; done")
    lines = [ln for ln in r["out"].splitlines() if "if=" in ln]
    lab.note("hidraw ACLs", "\n".join(lines))
    ok_rgb = all("user:tester:rw" in ln for ln in lines if "if=02" in ln or "if=03" in ln)
    no_keys = all("no-acl" in ln for ln in lines if "if=00" in ln or "if=01" in ln)
    lab.check("tester can use interfaces 2 and 3 (lighting)", ok_rgb and len(lines) == 8)
    lab.check("keystroke interfaces 0 and 1 NOT exposed (security)", no_keys)
    tags = lab.run("for d in /sys/class/hidraw/*; do udevadm info -q property /dev/$(basename $d) "
                   "| grep '^CURRENT_TAGS='; done")["out"]
    lab.note("udev tags", tags.strip())
    if not ok_rgb:
        # LAB WORKAROUND ONLY, so the other scenarios can run: the shipped rule does
        # not match (see the report). Same interfaces, matched through usb_id.
        lab.note("lab workaround", "shipped 60-openalloy.rules grants nothing; installing the "
                 "usb_id-based equivalent in the guest to continue")
        lab.vm.agent.put("/etc/udev/rules.d/60-openalloy.rules", LAB_RULE.encode())
        lab.run("udevadm control --reload; udevadm trigger --subsystem-match=hidraw "
                "--action=change; udevadm settle")
        lab.run("systemctl --user restart openalloy", user="tester")
        time.sleep(3)
    ev = lab.run("for e in /dev/input/event*; do echo $e $(getfacl -p $e 2>/dev/null | "
                 "grep '^user:tester' || echo no-acl) $(udevadm info -q property $e | "
                 "grep -E '^ID_VENDOR_ID=' ); done")
    lab.note("evdev ACLs (reactive rule)", ev["out"].strip())
    rr = lab.run("python3 - <<'EOF'\nimport os, glob\nres = {}\n"
                 "for d in sorted(glob.glob('/sys/class/hidraw/*')):\n"
                 "    n = '/dev/' + os.path.basename(d)\n"
                 "    try:\n        os.close(os.open(n, os.O_RDWR)); res[n] = 'open'\n"
                 "    except OSError as e:\n        res[n] = e.strerror\nprint(res)\nEOF",
                 user="tester")
    lab.note("open() as tester", rr["out"].strip())


LAB_RULE = """SUBSYSTEM=="hidraw", IMPORT{builtin}="usb_id"
SUBSYSTEM=="hidraw", ENV{ID_VENDOR_ID}=="0951", ENV{ID_MODEL_ID}=="16e6", ENV{ID_USB_INTERFACE_NUM}=="02|03", TAG+="uaccess", TAG+="openalloy"
SUBSYSTEM=="hidraw", ENV{ID_VENDOR_ID}=="03f0", ENV{ID_MODEL_ID}=="098f", ENV{ID_USB_INTERFACE_NUM}=="02|03", TAG+="uaccess", TAG+="openalloy"
"""


def sc_hp_detect(lab: Lab) -> None:
    lab.begin("hp-detection")
    lab.run("echo > /etc/lab/attach")
    for b in (HP, KINGSTON):
        lab.emu("unplug", busid=b)
    time.sleep(3)
    # a fresh service: it remembers per process that this firmware refuses the vendor
    # channel and would not try it again
    lab.run("systemctl --user restart openalloy", user="tester")
    seq = lab.log_seq(HP)
    since = lab.epoch()
    lab.attach([HP])
    st = lab.wait(lambda: (lambda s: s if s.get("connected") and (s.get("device") or {}).get(
        "vendor_id") == 0x03F0 else None)(lab.status()), 40)
    dev = (st or {}).get("device") or {}
    lab.check("HP detected", bool(st), json.dumps(dev)[:300])
    lab.check("driver hid_lamparray", dev.get("driver") == "hid_lamparray", dev.get("driver"))
    lab.check("firmware 21.16 (from bcdDevice)", dev.get("firmware") == "21.16", dev.get("firmware"))
    lab.check("layout detected abnt2 (from InputBinding)", dev.get("variant") == "abnt2",
              dev.get("variant"))
    log = lab.log_since(HP, seq)
    refused = [e for e in log if e["kind"] == "OUT" and e["status"] == -71]
    attrs_reads = [e for e in log if e["kind"] == "GET_FEATURE" and e["hex"].startswith("01")]
    lamp_reads = [e for e in log if e["kind"] == "GET_FEATURE" and e["hex"].startswith("03")]
    lab.check("vendor channel tried and refused with EPROTO (as the real keyboard)",
              len(refused) >= 1, f"{len(refused)} refused write(s)")
    lab.check("LampArray enumerated: one attributes read, 89 lamp reads",
              len(attrs_reads) >= 1 and len(lamp_reads) == 89,
              f"attrs={len(attrs_reads)} lamps={len(lamp_reads)}")
    j = lab.journal(since)
    lab.note("journal", j[-1500:])
    lab.check("journal: 89 LEDs, 89 mapped", "89 LEDs, 89 mapped to keys, layout abnt2" in j)
    refused_line = [ln for ln in j.splitlines() if "Vendor channel" in ln]
    lab.check("journal line in English only (1.5.3 fix)",
              bool(refused_line) and "falha" not in refused_line[0], refused_line[:1])
    lab.check("Errno shown once in that line", bool(refused_line) and refused_line[0].count("[Errno") == 1,
              refused_line[:1])


def sc_hp_colors(lab: Lab) -> None:
    lab.begin("hp-colors")
    HID_USAGE, get_layout = openalloy_tables()
    lab.oa("settings --set led_gamma=1.0")
    r = lab.oa("apply static -c '#ff0000'")
    st = lab.wait(lambda: (lambda s: s if s["mode"] == "host" and s["colors"] and
                           all(c == list(RED) for c in s["colors"].values()) else None)(
        lab.emu_state(HP)), 10)
    lab.check("static red reaches every lamp", bool(st), r["out"].strip()[-200:])
    lab.snap("hp-static-red", HP)
    # per-key sweep: every key gets its own colour, checked by HID usage on the device
    layout = get_layout("abnt2")
    want = {}
    args = []
    for n, k in enumerate(sorted(layout.keys, key=lambda k: k.index)):
        col = (10 + 2 * n, 250 - 2 * n, (n * 53) % 200 + 20)
        want[k.index] = col
        args.append(f"-k {k.index}=%02x%02x%02x" % col)
    lab.oa("apply static -c '#000000' " + " ".join(args))
    by_usage = keytable.by_usage("abnt2")

    def sweep_ok():
        s = lab.emu_state(HP)
        if s["mode"] != "host":
            return None
        bad = []
        for idx, col in want.items():
            usage = HID_USAGE.get(idx)
            # Fn: the program's placeholder usage 0xE8 has no lamp binding (placed by position)
            name = by_usage[usage].name if usage in by_usage else "fn"
            got = s["colors"].get(name) if name else None
            if not key_colors_close(got, col, 0):
                bad.append((idx, usage, name, got, col))
        return ("ok", bad) if not bad else (None, bad)
    res = lab.wait(lambda: (lambda x: x if x and x[0] else None)(sweep_ok()), 15)
    bad = (sweep_ok() or (None, []))[1] if not res else []
    lab.check("per-key sweep: 89 keys, each colour on the lamp of its own HID usage",
              bool(res), f"{len(bad)} wrong" + (f": {bad[:5]}" if bad else ""))
    lab.snap("hp-sweep", HP)
    # ABNT2-specific keys by name
    lab.oa("apply static -c '#000000' -k ro=#ff0000 -k 102nd=#00ff00 -k nonushash=#0000ff "
           "-k semicolon=#ffff00 -k fn=#ffffff")
    exp = {"slash": RED, "backslash": (0, 255, 0), "rbracket": (0, 0, 255),
           "cedilla": (255, 255, 0), "fn": WHITE}
    ok = lab.wait(lambda: all(lab.emu_state(HP)["colors"].get(k) == list(v) for k, v in exp.items()), 10)
    lab.check("ABNT2 keys (/?, \\|, ], Ç) and Fn (no usage: matched by position)", bool(ok),
              {k: lab.emu_state(HP)["colors"].get(k) for k in exp})
    # brightness: LampArray has none in hardware -> colour scaled by the program
    lab.oa("apply static -c '#ff0000'")
    lab.oa("brightness 50%")
    ok = lab.wait(lambda: all(c == [128, 0, 0] for c in lab.emu_state(HP)["colors"].values()), 10)
    lab.check("brightness 50% -> every lamp (128,0,0)", bool(ok),
              list(lab.emu_state(HP)["colors"].values())[:2])
    lab.oa("brightness 100%")
    lab.oa("settings --set led_gamma=2.2")
    lab.oa("apply static -c '#e01b24'")
    got = lab.wait(lambda: lab.emu_state(HP)["colors"].get("a"), 5)
    lab.check("LED colour correction (gamma 2.2) applied to mixed colours",
              bool(got) and got[0] > 150 and got[1] <= 10 and got[2] <= 12, got)
    lab.oa("settings --set led_gamma=1.0")


def sc_hp_effects(lab: Lab) -> None:
    lab.begin("hp-effects")
    out = lab.oa("effects")["out"]
    ids = [ln.split()[0] for ln in out.splitlines() if ln[:1].isalpha() and not ln.startswith(" ")]
    lab.note("effects offered", ids)
    static = {"static", "gradient", "off"}
    needs_input = {"reactive", "ripple"}
    for eff in ids:
        too_fast0 = lab.emu_state(HP)["too_fast_updates"]
        r = lab.oa(f"apply {eff}")
        time.sleep(1.5)
        a = lab.emu_state(HP)
        pictures = []
        for _ in range(6):                    # slow effects (blink) need a longer look
            time.sleep(0.5)
            pictures.append(json.dumps(lab.emu_state(HP)["colors"], sort_keys=True))
        b = lab.emu_state(HP)
        changed = len(set(pictures + [json.dumps(a["colors"], sort_keys=True)])) > 1
        if eff == "off":
            ok = b["mode"] == "host" and all(c == [0, 0, 0] for c in b["colors"].values())
            lab.check(f"effect {eff}: all lamps dark", ok)
        elif eff in static:
            lab.check(f"effect {eff}: steady picture", b["mode"] == "host" and not changed, "")
        elif eff in needs_input:
            lab.check(f"effect {eff}: accepted", r["rc"] == 0, r["err"][-200:])
        else:
            sound = "sound" in eff or eff in ("equalizer",)
            if sound:
                st = lab.status()
                lab.check(f"effect {eff}: runs without audio capture, reason shown",
                          r["rc"] == 0 and b["mode"] == "host", str(st.get("audio"))[:120])
            else:
                lab.check(f"effect {eff}: animates on the device", changed and b["mode"] == "host",
                          f"updates/s={b['updates_per_s']}")
        lab.check(f"effect {eff}: device minimum update interval respected",
                  b["too_fast_updates"] == too_fast0, f"too_fast={b['too_fast_updates'] - too_fast0}")
        if eff in ("wave", "fire", "starlight"):
            lab.snap(f"hp-effect-{eff}", HP)
    bad = lab.oa("apply wave -p speed=999")
    lab.check("out-of-range parameter: rejected, or clamped and the applied value shown",
              (bad["rc"] != 0 and bad["err"].strip()) or '"speed": 100' in bad["out"],
              (bad["out"] + bad["err"]).strip()[-200:])
    bad3 = lab.oa("apply wave -p speed=abc")
    lab.check("non-numeric parameter rejected", bad3["rc"] != 0, (bad3["err"] or bad3["out"]).strip()[-200:])
    bad2 = lab.oa("apply nosuchfx")
    lab.check("unknown effect rejected", bad2["rc"] != 0, bad2["err"].strip()[-160:])


def sc_hp_reactive(lab: Lab) -> None:
    lab.begin("hp-reactive")
    lab.oa("brightness 100%")
    lab.oa("apply reactive -c '#00ff00' -p background=#000000")    # -c = colour on key press
    time.sleep(2)
    st = lab.status()
    lab.note("input reader", st.get("input"))
    hit = None
    for _ in range(4):
        lab.emu("press_key", busid=HP, usage=0x04)          # 'a' on the emulated if0
        hit = lab.wait(lambda: (lab.emu_state(HP)["colors"] or {}).get("a", [0, 0, 0])[1] > 40, 2,
                       step=0.05)
        if hit:
            break
    lab.check("key press on the keyboard (evdev) lights the key (reactive)", bool(hit),
              f"input={st.get('input')}")
    lab.oa("apply static -c '#ff0000'")


def sc_hp_fixed(lab: Lab) -> None:
    lab.begin("hp-fixed-hold")
    r = lab.oa("hold blue -b 50%", lang="pt_BR")
    lab.note("hold output", r["out"].strip()[-300:])
    ok = lab.wait(lambda: all(c == [0, 0, 128] for c in (lab.emu_state(HP)["colors"] or {"x": 0}).values()), 10)
    lab.check("hold blue 50%: lamps (0,0,128)", bool(ok))
    time.sleep(1.0)
    seq = lab.log_seq(HP)
    time.sleep(6)
    traffic = [e for e in lab.log_since(HP, seq) if e["kind"] in ("SET_FEATURE", "GET_FEATURE")]
    lab.check("fixed mode: nothing streams afterwards (6 s, 0 reports)", len(traffic) == 0,
              f"{len(traffic)} reports")
    lab.check("fixed mode: colour stays on the device", lab.emu_state(HP)["mode"] == "host")
    lab.oa("release")
    ok = lab.wait(lambda: lab.emu_state(HP)["autonomous"], 10)
    lab.check("release hands the LEDs back (AutonomousMode=1)", bool(ok))
    lab.oa("resume")
    lab.oa("apply static -c '#ff0000'")


def sc_presets(lab: Lab) -> None:
    lab.begin("presets")
    lab.oa("resume")
    lab.oa("brightness 100%")
    lab.oa("apply static -c '#00ff00'")
    s = lab.oa("preset save lab-green")
    lab.oa("apply static -c '#ff0000'")
    lst = lab.oa("preset list")
    lab.check("preset saved and listed", s["rc"] == 0 and "lab-green" in lst["out"], lst["out"][-300:])
    lab.oa("preset apply lab-green")
    ok = lab.wait(lambda: lab.emu_state(HP)["colors"].get("a") == [0, 255, 0], 10)
    lab.check("preset applied reaches the device", bool(ok))
    d = lab.oa("preset delete lab-green")
    d2 = lab.oa("preset delete lab-green")
    lab.check("preset deleted", d["rc"] == 0, d["out"].strip()[-120:])
    lab.check("deleting a missing preset reports failure (exit code != 0)", d2["rc"] != 0,
              f"rc={d2['rc']} {(d2['out'] + d2['err']).strip()[-120:]}")
    files = lab.run("ls ~tester/.config/openalloy/presets/ 2>/dev/null", user="tester")
    lab.check("no preset file left", "lab-green" not in files["out"])


def sc_kingston(lab: Lab) -> None:
    lab.begin("kingston-vendor")
    HID_USAGE, get_layout = openalloy_tables()
    lab.attach([KINGSTON])
    st = lab.wait(lambda: (lambda s: s if s.get("connected") and (s.get("device") or {}).get(
        "vendor_id") == 0x0951 else None)(lab.status()), 40)
    dev = (st or {}).get("device") or {}
    lab.check("Kingston detected on the vendor channel",
              dev.get("driver") == "hyperx_alloy_origins_core", json.dumps(dev)[:250])
    lab.check("firmware 21.08 and layout abnt2 from INFO", dev.get("firmware") == "21.08"
              and dev.get("variant") == "abnt2", f"{dev.get('firmware')} {dev.get('variant')}")
    lab.oa("settings --set led_gamma=1.0")
    layout = get_layout("abnt2")
    want, args = {}, []
    for n, k in enumerate(sorted(layout.keys, key=lambda k: k.index)):
        col = (10 + 2 * n, 250 - 2 * n, (n * 53) % 200 + 20)
        want[k.index] = col
        args.append(f"-k {k.index}=%02x%02x%02x" % col)
    lab.oa("brightness 100%")
    lab.oa("apply static -c '#000000' " + " ".join(args))

    def ok_buf():
        s = lab.emu_state(KINGSTON)
        if s["mode"] != "direct":
            return False
        return all(key_colors_close(s["colors"][i], c, 0) for i, c in want.items())
    lab.check("per-key sweep in the 380-byte GRB buffer (protocol.py layout)", bool(lab.wait(ok_buf, 15)))
    lab.snap("kingston-sweep", KINGSTON)
    lab.oa("apply static -c '#ff0000'")
    time.sleep(2)
    fps = lab.emu_state(KINGSTON)["fps"]
    lab.check("static colour kept alive (~4 frames/s, KEEPALIVE 0.25 s)", 2.5 <= fps <= 6, f"fps={fps}")
    lab.oa("brightness 50%")
    b = lab.wait(lambda: lab.emu_state(KINGSTON)["brightness"] in (127, 128), 8)
    lab.check("brightness uses the hardware command (SET_BRIGHT)", bool(b),
              lab.emu_state(KINGSTON)["brightness"])
    lab.oa("brightness 100%")
    lab.oa("profile 2")
    lab.check("profile 2 selected on the device", lab.wait(lambda: lab.emu_state(KINGSTON)["profile"] == 2, 8))
    since = lab.epoch()
    lab.emu("profile_key", busid=KINGSTON, profile=3)
    time.sleep(3)
    j = lab.journal(since)
    lab.check("Fn+profile key notification seen by the service", "Profile key 3" in j, j[-300:])
    lab.oa("resume")
    lab.oa("apply static -c '#ff0000'")
    lab.check("no undocumented opcode sent", not lab.emu_state(KINGSTON)["unknown_commands"],
              lab.emu_state(KINGSTON)["unknown_commands"])


def sc_kingston_flash(lab: Lab) -> None:
    lab.begin("kingston-flash")
    s0 = lab.emu_state(KINGSTON)
    pattern = bytes((i * 7 + 3) & 0xFF for i in range(13 * 60))
    lab.emu("seed_store", busid=KINGSTON, profile=1, hex=pattern.hex())
    w0 = s0["flash_writes"]
    r = lab.oa("onboard write 1 static -c '#00ff00' -y", timeout=180)
    s1 = lab.emu_state(KINGSTON)
    lab.check("memory write refused before the live RGB test (gate)",
              r["rc"] != 0 and s1["flash_writes"] == w0, (r["err"] or r["out"]).strip()[-200:])
    t = lab.oa("test-rgb --yes", timeout=120)
    lab.note("test-rgb", (t["out"] + t["err"]).strip()[-300:])
    seq = lab.log_seq(KINGSTON)
    r = lab.oa("onboard write 1 wave --frames 10 -y", timeout=600)
    s2 = lab.emu_state(KINGSTON)
    lab.check("memory write after the test", r["rc"] == 0, (r["out"] + r["err"]).strip()[-300:])
    lab.check("10 animation frames stored in profile 1", s2["animation_frames"]["1"] == 10,
              s2["animation_frames"])
    log = lab.log_since(KINGSTON, seq)
    order = [e["note"].split()[0] for e in log if e["kind"] == "OUT"]
    first_write = next((i for i, n in enumerate(order) if n == "WRITE_STORE"), None)
    reads_before = order[:first_write].count("READ_STORE") if first_write is not None else 0
    lab.check("profile store read (13 chunks) before it is rewritten", reads_before == 13,
              f"reads before first write: {reads_before}")
    lab.check("store written back byte for byte (nothing invented)",
              s2["stores"]["1"] == pattern.hex())
    cfg = bytes.fromhex(s2["configs"]["1"])
    lab.check("config block: frame count 10", len(cfg) >= 11 and cfg[10] == 10, cfg.hex())
    lab.check("never the bare 0x30 that crashes the firmware", not s2["dangerous"], s2["dangerous"])


def sc_faults(lab: Lab) -> None:
    lab.begin("faults-hp")
    lab.oa("resume")
    lab.oa("brightness 100%")
    since = lab.epoch()
    lab.attach([HP])
    switched = lab.wait(lambda: (lab.status().get("device") or {}).get("driver") == "hid_lamparray", 40)
    lab.check("service moves from the Kingston to the HP when the cable changes", bool(switched),
              "" if switched else json.dumps(lab.status())[:300] + " | " + lab.journal(since)[-800:])
    lab.oa("apply wave")
    pid = lab.svc_pid()
    m0 = lab.proc_metrics(pid)
    since = lab.epoch()
    lab.emu("fault", busid=HP, name="stall_feature", value=True)
    time.sleep(4)
    lab.emu("fault", busid=HP, name="stall_feature", value=None)
    j = lab.journal(since)
    lab.check("STALL on feature reports -> logged and retried", "Communication error" in j
              or "communication failed" in j, j[-400:])
    upd = lab.wait(lambda: lab.emu_state(HP)["updates_per_s"] > 1, 20)
    lab.check("recovers after the device answers again", bool(upd))
    # duplicated lamp reply during enumeration
    since = lab.epoch()
    lab.emu("fault", busid=HP, name="dup_lamp_reply", value=1)
    lab.emu("unplug", busid=HP)
    time.sleep(2)
    lab.emu("plug", busid=HP)
    lab.wait(lambda: lab.emu("attached", busid=HP), 20)
    j = lab.wait(lambda: (lambda x: x if "LEDs" in x else None)(lab.journal(since)), 30) or ""
    lab.check("duplicated lamp reply: warned, LED asked again, 89 mapped",
              "asked for LED" in j and "89 mapped" in j, [ln for ln in j.splitlines() if "LED" in ln][-3:])
    # impossible lamp count
    since = lab.epoch()
    lab.emu("fault", busid=HP, name="bad_lamp_count", value=0)
    lab.emu("unplug", busid=HP)
    time.sleep(2)
    lab.emu("plug", busid=HP)
    time.sleep(8)
    st = lab.status()
    lab.check("lamp count 0: clear error, service alive", not st.get("connected") and st.get("error"),
              str(st.get("error"))[:200])
    lab.emu("fault", busid=HP, name="bad_lamp_count", value=None)
    lab.emu("unplug", busid=HP)
    time.sleep(1)
    lab.emu("plug", busid=HP)
    ok = lab.wait(lambda: lab.status().get("connected"), 40)
    lab.check("recovers when the device is sane again", bool(ok))
    # slow device
    lab.emu("fault", busid=HP, name="feature_delay_ms", value=150)
    t0 = time.time()
    r = lab.oa("apply static -c '#00ff00'")
    ok = lab.wait(lambda: lab.emu_state(HP)["colors"].get("a") == [0, 255, 0], 30)
    lab.check("slow device (150 ms per report): command returns, colour arrives", r["rc"] == 0 and bool(ok),
              f"{time.time() - t0:.1f}s")
    lab.emu("fault", busid=HP, name="feature_delay_ms", value=None)
    # replug cycles: reconnection and leaks
    lab.oa("apply static -c '#ff0000'")
    times = []
    for _ in range(5):
        lab.emu("unplug", busid=HP)
        time.sleep(1.5)
        seq = lab.log_seq(HP)
        t0 = time.time()
        lab.emu("plug", busid=HP)
        got = lab.wait(lambda: any(e["kind"] == "SET_FEATURE" and e["hex"].startswith(("04", "05"))
                                   for e in lab.log_since(HP, seq)), 30, step=0.1)  # noqa: B023
        times.append(round(time.time() - t0, 2) if got else None)
    lab.check("5 unplug/replug cycles: colour back every time", all(times), f"seconds: {times}")
    lab.metrics["replug_seconds"] = times
    time.sleep(2)
    m1 = lab.proc_metrics(lab.svc_pid())
    lab.check("no fd/thread growth over the fault scenarios",
              m1.get("fds", 999) <= m0.get("fds", 0) + 3 and m1.get("threads", 999) <= m0.get("threads", 0) + 2,
              f"before {m0} after {m1}")
    lab.check("same service process throughout (no crash/restart)", lab.svc_pid() == pid, f"{pid}")
    lab.begin("faults-kingston")
    lab.attach([KINGSTON])
    lab.wait(lambda: (lab.status().get("device") or {}).get("vendor_id") == 0x0951, 40)
    since = lab.epoch()
    lab.emu("fault", busid=KINGSTON, name="stale_reply", value=1)
    r = lab.oa("profile 1")
    j = lab.journal(since)
    lab.check("unexpected reply ignored (never taken as the answer)", r["rc"] == 0,
              [ln for ln in j.splitlines() if "Unexpected" in ln][-1:])
    lab.emu("fault", busid=KINGSTON, name="mute", value=True)
    r = lab.oa("profile 2", timeout=60)
    lab.check("keyboard that stops answering -> clean error, no hang", r["rc"] != 0,
              (r["err"] or r["out"]).strip()[-200:])
    lab.emu("fault", busid=KINGSTON, name="mute", value=None)
    lab.emu("fault", busid=KINGSTON, name="unplug_after_out", value=40)
    r = lab.oa("onboard write 2 static -c '#0000ff' -y", timeout=300)
    lab.check("unplug in the middle of a memory write -> error reported", r["rc"] != 0,
              (r["err"] or r["out"]).strip()[-200:])
    lab.emu("fault", busid=KINGSTON, name="unplug_after_out", value=None)
    lab.emu("plug", busid=KINGSTON)
    ok = lab.wait(lambda: lab.status().get("connected"), 40)
    lab.check("service reconnects after that", bool(ok))
    lab.oa("resume")
    lab.emu("fault", busid=KINGSTON, name="refuse_out", value=True)
    lab.emu("unplug", busid=KINGSTON)
    time.sleep(1)
    lab.emu("plug", busid=KINGSTON)
    time.sleep(8)
    st = lab.status()
    lab.check("Kingston refusing every write (no LampArray): clear error", not st.get("connected")
              and st.get("error"), str(st.get("error"))[:200])
    lab.emu("fault", busid=KINGSTON, name="refuse_out", value=None)
    lab.emu("unplug", busid=KINGSTON)
    time.sleep(1)
    lab.emu("plug", busid=KINGSTON)
    lab.wait(lambda: lab.status().get("connected"), 40)
    lab.check("never the bare 0x30", not lab.emu_state(KINGSTON)["dangerous"])


def sc_permission_denied(lab: Lab) -> None:
    lab.begin("permission-denied")
    lab.attach([HP])
    lab.oa("resume")
    lab.wait(lambda: lab.status().get("connected"), 40)
    lab.run("mv /etc/udev/rules.d/60-openalloy.rules /root/ ; udevadm control --reload")
    lab.emu("unplug", busid=HP)
    time.sleep(1)
    lab.emu("plug", busid=HP)
    time.sleep(10)
    st = lab.status()
    lab.check("no udev rule -> status explains (udev rule / permission)",
              not st.get("connected") and ("udev" in str(st.get("error")) or "permission" in str(st.get("error")).lower()),
              str(st.get("error"))[:200])
    lab.run("mv /root/60-openalloy.rules /etc/udev/rules.d/ ; udevadm control --reload")
    lab.emu("unplug", busid=HP)
    time.sleep(1)
    lab.emu("plug", busid=HP)
    ok = lab.wait(lambda: lab.status().get("connected"), 40)
    lab.check("rule back + replug -> works", bool(ok))
    # the same in "onboard" mode (keyboard's own lighting)
    lab.oa("release")
    time.sleep(2)
    since = lab.epoch()
    lab.emu("unplug", busid=HP)
    time.sleep(1)
    lab.emu("plug", busid=HP)
    time.sleep(10)
    j = lab.journal(since)
    lab.check("onboard mode notices an unplug/replug (no stale 'connected' handle)",
              any(w in j for w in ("Connection lost", "Communication error", "Connected:",
                                   "Keyboard reconnected")), j.strip()[-300:] or "nothing logged")
    lab.oa("resume")


def sc_suspend(lab: Lab) -> None:
    """Real S3 of the guest. systemd freezes user.slice while the sleep hooks run.
    The lab's own hook (zz-lab-usbip) detaches USB/IP after the program's hook, as
    vhci-hcd cannot suspend with a live link: the keyboard re-enumerates on resume."""
    lab.begin("suspend-resume")
    lab.attach([HP])
    lab.oa("resume")
    lab.wait(lambda: lab.status().get("connected"), 40)
    lab.oa("brightness 100%")
    lab.oa("hold red")
    lab.wait(lambda: lab.emu_state(HP)["colors"].get("a") == list(RED), 10)
    resets0 = lab.emu_state(HP)["resets"]
    since = lab.epoch()
    t0 = time.time()
    lab.run("systemd-run --no-block systemctl suspend")
    went = lab.vm.wait_status("suspended", 90)
    t_sleep = round(time.time() - t0, 1)
    lab.check("guest entered S3 (systemctl suspend)", went, lab.vm.qmp.status())
    resets_sleep = lab.emu_state(HP)["resets"]
    lab.check("the program's hook reset the USB device before sleeping",
              resets_sleep > resets0, f"resets {resets0}->{resets_sleep}")
    lab.metrics["seconds_command_to_s3"] = t_sleep
    time.sleep(10)
    t1 = time.time()
    lab.vm.wakeup()
    lab.vm.wait_status("running", 60)
    lab.vm.agent.wait_ready(120)
    ok = lab.wait(lambda: lab.emu_state(HP)["mode"] == "host" and
                  lab.emu_state(HP)["colors"].get("a") == list(RED), 60, step=0.2)
    lab.metrics["seconds_wake_to_colour"] = round(time.time() - t1, 1) if ok else None
    lab.check("fixed colour back after resume, without user action", bool(ok),
              f"{lab.metrics['seconds_wake_to_colour']} s after wake-up")
    st = lab.status()
    lab.check("service not left 'asleep'", st.get("asleep") is False, st.get("asleep"))
    j = lab.journal(since)
    sysj = lab.run(f"journalctl --since @{since:.3f} -o short-precise --no-pager | "
                   "grep -E 'systemd-sleep|PM: suspend|Freezing|froze|thawed|openalloy' ")["out"]
    (lab.out / "suspend-journal.txt").write_text(j + "\n----\n" + sysj)
    lines = [ln for ln in j.splitlines() if any(w in ln.lower() for w in ("sleep", "resumed", "notice",
                                                                         "reconnected", "fixed color"))]
    lab.metrics["suspend_lines"] = lines
    lab.note("service journal around the suspend", "\n".join(lines))
    lab.note("sleep timeline (system journal)", sysj.strip()[-2500:])
    lab.note("seconds from 'systemctl suspend' to S3", t_sleep)


def sc_lifecycle(lab: Lab) -> None:
    lab.begin("service-lifecycle")
    t0 = time.time()
    r = lab.run("time -p systemctl --user restart openalloy", user="tester", timeout=120)
    lab.check("restart completes quickly", r["rc"] == 0 and time.time() - t0 < 20,
              r["err"].strip()[-120:])
    ok = lab.wait(lambda: lab.status().get("connected"), 40)
    lab.check("connected again after restart", bool(ok))
    pid = lab.svc_pid()
    for _ in range(3):
        lab.run("systemctl --user kill -s HUP openalloy", user="tester")
    time.sleep(2)
    lab.check("SIGHUP (reload) x3: same process, still answering",
              lab.svc_pid() == pid and lab.status().get("connected"))
    lab.run("systemctl --user stop openalloy", user="tester")
    st = lab.run("systemctl --user is-active openalloy", user="tester")["out"].strip()
    lab.check("stop", st in ("inactive", "failed") and st == "inactive", st)
    lab.run("systemctl --user start openalloy", user="tester")
    lab.wait(lambda: lab.status().get("connected"), 40)


def sc_performance(lab: Lab, seconds: int = 90) -> None:
    lab.begin("performance")
    lab.attach([HP])
    lab.wait(lambda: lab.status().get("connected"), 40)
    noop = []
    for _ in range(3):
        t0 = time.time()
        lab.run("true")
        noop.append(time.time() - t0)
    lat = []
    for col, rgb in (("#0000ff", [0, 0, 255]), ("#00ff00", [0, 255, 0]), ("#ff0000", [255, 0, 0])):
        t0 = time.time()
        lab.oa(f"apply static -c '{col}'")
        got = lab.wait(lambda: lab.emu_state(HP)["colors"].get("a") == rgb, 20, step=0.02)  # noqa: B023
        lat.append(round(time.time() - t0, 2) if got else None)
    lab.metrics["cli_to_device_seconds"] = lat
    lab.metrics["agent_roundtrip_seconds"] = round(sum(noop) / len(noop), 3)
    lab.note("CLI command -> colour on the device (includes starting the CLI under TCG)", lat)
    lab.oa("apply wave")
    time.sleep(3)
    pid = lab.svc_pid()
    samples = []
    for _ in range(max(2, seconds // 15)):
        m = lab.proc_metrics(pid)
        m["t"] = time.time()
        m["updates_per_s"] = lab.emu_state(HP)["updates_per_s"]
        samples.append(m)
        time.sleep(15)
    a, b = samples[0], samples[-1]
    dt = b["t"] - a["t"]
    cpu = round(100 * (b["cpu_ticks"] - a["cpu_ticks"]) / a["hz"] / dt, 1) if dt else None
    lab.metrics["wave"] = {"cpu_percent": cpu, "rss_kb": [s["rss_kb"] for s in samples],
                           "fds": [s["fds"] for s in samples], "threads": [s["threads"] for s in samples],
                           "updates_per_s": [s["updates_per_s"] for s in samples]}
    lab.note("wave effect over time", lab.metrics["wave"])
    lab.check("RSS stable during the run (<= +2 MiB)", b["rss_kb"] - a["rss_kb"] <= 2048,
              f"{a['rss_kb']} -> {b['rss_kb']} kB")
    lab.check("fds and threads constant", a["fds"] == b["fds"] and a["threads"] == b["threads"],
              f"fds {a['fds']}->{b['fds']} threads {a['threads']}->{b['threads']}")
    lab.check("update rate within the device limit (<= 1/30 ms)",
              max(s["updates_per_s"] for s in samples) <= 34, lab.metrics["wave"]["updates_per_s"])
    lab.oa("apply static -c '#ff0000'")


GUI_SCRIPT = r'''
import json, os, sys, time
import gi; gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk
from openalloy import i18n
i18n.set_language(i18n.resolve())
from openalloy.application.backend import RemoteBackend
from openalloy.presentation.gui import app as A
from openalloy.presentation.gui.widgets import rgba_from_hex
be = A.BackendHolder(RemoteBackend(), simulate=None)
application = A._build_app(be, None)
res = {"lang0": i18n.current()}
def find_button(w, label):
    if isinstance(w, Gtk.Button) and w.get_label() == label:
        return w
    c = w.get_first_child()
    while c is not None:
        r = find_button(c, label)
        if r: return r
        c = c.get_next_sibling()
    return None
def mark(name):
    print("MARK", name, flush=True); time.sleep(0.2)
def steps():
    win = application.get_active_window()
    t0 = time.time()
    while not win.effects and time.time() - t0 < 120:
        yield 500
    res["loaded_after_s"] = round(time.time() - t0, 1)
    yield 1500
    mark("start")
    res["conn"] = win.conn_label.get_text()
    win.lighting = {"effect": "static", "params": {"color": "#ff0000"}, "keys": {}, "reaction": {}}
    win._show_lighting()
    win.view.select([win.view.layout.resolve(k) for k in "wasd"])
    win.brush_color.set_rgba(rgba_from_hex("#ffffff"))
    win._paint_selection()
    b = find_button(win, i18n._("Fix on keyboard"))
    res["fix_button"] = b is not None
    b.emit("clicked")
    yield 8000
    res["status"] = be.call("status")["mode"]
    mark("fixed")
    yield 3000
    win.lang_dd.set_selected(win.lang_codes.index("en"))
    t1 = time.time()
    while i18n.current() != "en" and time.time() - t1 < 60:
        yield 500
    res["switch_s"] = round(time.time() - t1, 1)
    yield 4000
    win = application.get_active_window()
    res["lang1"] = i18n.current()
    mark("english")
    yield 3000
    application.quit()
it = steps()
def tick():
    try:
        GLib.timeout_add(next(it), tick)
    except StopIteration:
        pass
    return False
GLib.timeout_add(100, tick)
application.run(None)
print("RESULT", json.dumps(res), flush=True)
'''


def sc_gui(lab: Lab) -> None:
    lab.begin("gui")
    lab.attach([HP])
    lab.wait(lambda: lab.status().get("connected"), 40)
    lab.vm.agent.put("/home/tester/lab_gui.py", GUI_SCRIPT.encode(), owner="tester")
    py = "~/.local/share/pipx/venvs/openalloy/bin/python"
    lab.run("rm -f /tmp/gui.out; (Xvfb :99 -screen 0 1280x900x24 >/tmp/xvfb.log 2>&1 &) ; sleep 2; "
            # cairo renderer: GTK 4's GL renderer draws no text under Xvfb (no GPU)
            f"(DISPLAY=:99 GDK_BACKEND=x11 GSK_RENDERER=cairo {py} ~/lab_gui.py > /tmp/gui.out 2>&1 &)",
            user="tester")
    shots = {}
    for mark in ("start", "fixed", "english"):
        got = lab.wait(lambda: f"MARK {mark}" in lab.run("cat /tmp/gui.out",  # noqa: B023
                                                         user="tester")["out"], 120)
        if got:
            shots[mark] = lab.screenshot(f"gui-{mark}")
            if mark == "fixed":
                s = lab.emu_state(HP)
                lab.check("GUI 'Fix on keyboard': WASD white over red on the device",
                          s["colors"].get("w") == list(WHITE) and s["colors"].get("q") == list(RED),
                          {k: s["colors"].get(k) for k in ("w", "a", "q")})
                lab.snap("gui-fixed-device", HP)
    out = lab.wait(lambda: (lambda o: o if "RESULT" in o else None)(
        lab.run("cat /tmp/gui.out", user="tester")["out"]), 120) or ""
    res = {}
    for ln in out.splitlines():
        if ln.startswith("RESULT"):
            res = json.loads(ln[7:])
    lab.note("GUI result", res)
    lab.check("GUI talks to the running service", "LampArray" in str(res.get("conn", "")) or
              "HyperX" in str(res.get("conn", "")), res.get("conn"))
    lab.check("GUI starts in the saved language (pt_BR) and switches to English",
              res.get("lang0") == "pt_BR" and res.get("lang1") == "en", res)
    errs = [ln for ln in out.splitlines() if "Traceback" in ln or ln.startswith(("Error", "ERROR"))
            or ("Error:" in ln and "Gtk-WARNING" not in ln and "a11y" not in ln)]
    lab.check("no exceptions in the GUI output", not errs, errs[:3])
    lab.metrics["screenshots"] = shots
    lab.run("pkill -f lab_gui.py; pkill Xvfb", user="tester")


def sc_uninstall(lab: Lab) -> None:
    lab.begin("uninstall")
    r = lab.run("cd ~/oa-src/openalloy && bash ./uninstall.sh --purge 2>&1 || bash ./uninstall.sh 2>&1",
                user="tester", timeout=300)
    (lab.out / "uninstall.log").write_text(r["out"] + r["err"])
    left = lab.run("ls /etc/udev/rules.d/ /usr/lib/systemd/system-sleep/ /etc/systemd/system-sleep/ "
                   "~tester/.config/systemd/user/ 2>/dev/null | grep -i openalloy; "
                   "ls ~tester/.local/bin/openalloy 2>/dev/null")
    lab.check("uninstall removes rules, hook, service and command", r["rc"] == 0 and not left["out"].strip(),
              left["out"].strip())


SCENARIOS = ["environment", "install", "permissions", "hp_detect", "hp_colors", "hp_effects",
             "hp_reactive", "hp_fixed", "presets", "kingston", "kingston_flash", "faults",
             "permission_denied", "suspend", "lifecycle", "performance", "gui", "uninstall"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", default=str(HERE.parent.parent / "lab-work"))
    ap.add_argument("--zip", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--label", help="version label (default: from the zip name)")
    ap.add_argument("--only", nargs="*", help="run only these scenarios (install is implied)")
    ap.add_argument("--perf-seconds", type=int, default=90)
    a = ap.parse_args()
    zip_path = Path(a.zip).resolve()
    label = a.label or zip_path.stem.replace("openalloy-", "")
    lab = Lab(Path(a.work), Path(a.out), label)
    wanted = SCENARIOS if not a.only else ["environment", "install"] + a.only
    try:
        lab.boot()
        for name in SCENARIOS:
            if name not in wanted:
                continue
            fn = globals()["sc_" + name]
            try:
                if name == "install":
                    fn(lab, zip_path)
                elif name == "performance":
                    fn(lab, a.perf_seconds)
                else:
                    fn(lab)
            except Exception as exc:          # a broken scenario is a result, not a stop
                import traceback
                lab.check(f"scenario {name} ran to the end", False, repr(exc),
                          trace=traceback.format_exc()[-2000:])
    finally:
        lab.close()
    failed = [r for r in lab.results if r["ok"] is False]
    print(f"\n{len([r for r in lab.results if r['ok']])} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
