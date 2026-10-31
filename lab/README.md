# OpenAlloy test lab

A laboratory to test OpenAlloy end to end **without the physical keyboard**: the
program runs, installed by its own `install.sh`, in a real Linux guest (QEMU), and
talks through the guest kernel (usbhid, hidraw, evdev, udev, logind, systemd-sleep)
to emulated keyboards that the kernel sees as real USB devices.

Not part of the program: nothing here is imported by `openalloy` and it is not in
the wheel.

## Layers

| Layer | What | Where | Fidelity |
|---|---|---|---|
| A. logical simulation | `--simulate`, `--simulate-hp` (in the program) | `openalloy/infra/simulator.py` | in-process, no kernel |
| B. protocol emulation | `hidemu`: USB descriptors, HID report descriptors, vendor protocol (if2), HID LampArray (if3), boot keyboard (if0), faults, hex log | `lab/hidemu/` | device side written from the documented protocol and the HID LampArray standard, **independently** of the driver code |
| B'. fast conformance | the program's real drivers against `hidemu` through a hidraw-like adapter | `lab/tests/` | seconds, no VM |
| C. OS virtualisation | Debian testing "forky" (Python 3.14, systemd 262, kernel 7.x - base of SparkyLinux rolling) in QEMU; the keyboards attached over **USB/IP** (vhci-hcd) | `lab/vm/` | real kernel USB/HID stack, real udev rules and uaccess ACLs (autologin on seat0), real systemd user service, real S3 suspend |
| D. real hardware | read-only descriptor capture; manual checklist | `lab/tools/capture_descriptors.py` | the only proof of physical behaviour |

Why USB/IP and not UHID: the program identifies interfaces by the USB parent
(`bInterfaceNumber`, `bcdDevice`) and the udev rule matches the USB vendor, product
and interface number (through udev's `usb_id` builtin). A UHID device has no USB
parent; a USB/IP device has all of it, and every URB (including feature reports and the EPROTO of the HP's
vendor channel) is answered by the emulator.

## Requirements (host)

Linux, root (for debootstrap), `qemu-system-x86`, `qemu-utils`, `debootstrap`,
`debian-archive-keyring`, `e2fsprogs`, Python 3.10+, `imagemagick` (screenshots).
KVM is used when `/dev/kvm` exists; without it QEMU's TCG emulation works (slower).

## Use

```bash
# 1. build the guest image once (~10 min)
sudo lab/vm/build-image.sh ./lab-work
#    behind a proxy: LAB_PROXY=http://10.0.2.2:PORT LAB_CA=/path/ca.pem sudo -E ...

# 2. fast layer (seconds)
python3 -m pytest lab/tests

# 3. full run against one version (fresh VM from the base snapshot each time)
python3 lab/run_lab.py --work ./lab-work --zip openalloy-1.5.3.zip --out results/1.5.3
python3 lab/run_lab.py --work ./lab-work --zip openalloy-1.5.2.zip --out results/1.5.2
python3 lab/compare.py results/1.5.2 results/1.5.3 > comparison.md

# emulator alone (e.g. against another Linux VM or machine with vhci-hcd). It listens on
# the loopback by default; --host 0.0.0.0 exposes it to the network (trusted networks only):
cd lab && python3 -m hidemu serve --hp 1-2 --kingston 1-1 --host 0.0.0.0
#   guest: sudo modprobe vhci-hcd; sudo usbip attach -r HOST -b 1-2
python3 -m hidemu state 1-2 | log 1-2 | render 1-2 > kb.svg | fault 1-2 stall_feature true
```

Each run boots from a throw-away overlay of `base.qcow2`: every scenario set starts
from the same state; nothing persists between runs.

## Faults that can be injected (`hidemu fault BUSID NAME VALUE`)

| Device | Fault | Effect |
|---|---|---|
| both | `unplug`/`plug` (commands) | cable pulled / plugged back |
| HP | `stall_feature` | every feature report STALLs (EPIPE) |
| HP | `dup_lamp_reply` N | the next lamp reply repeats the previous lamp |
| HP | `wrong_lamp_id` N | a lamp reply for another lamp |
| HP | `bad_lamp_count` N | attributes report N lamps |
| HP | `feature_delay_ms` N | slow device |
| HP | `short_feature` N | truncated feature report |
| HP | `unplug_after_feature` N | unplugged during a transfer |
| Kingston | `refuse_out` | every write fails with EPROTO |
| Kingston | `mute` / `drop_reply` N | no reply |
| Kingston | `stale_reply` N | an unrelated reply arrives first |
| Kingston | `truncate_reply` N, `bad_status`, `delay_ms` N | malformed / error / slow |
| Kingston | `unplug_after_out` N | unplugged during a transfer |
| Kingston | bare `0x30` | firmware "crash" (device drops off) - recorded as dangerous |

## What the lab cannot prove

Light on real LEDs, the real descriptors byte for byte (until captured with
`tools/capture_descriptors.py`), the real firmware's timing (direct-mode timeout,
LampArray latency), remote wake-up of the real USB port, and anything about other
desktop environments than the one in the guest.
