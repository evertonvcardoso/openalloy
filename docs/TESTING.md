# Testing OpenAlloy

How OpenAlloy is tested, what the tests prove and what they cannot prove, the results of
the test lab, and the defects found by each audit. How to build and run the lab is in
[lab/README.md](../lab/README.md).

## Layers

OpenAlloy reaches the keyboard through `/dev/hidraw*` (no hidapi, no libusb), finds the
interfaces through the USB parent (`bInterfaceNumber`, `bcdDevice`), and depends on udev
and logind (`uaccess`) for permission and on systemd (user service, sleep hook). A virtual
machine alone emulates no RGB keyboard, and a mock of the HID layer would test none of
that. So testing is done in layers, **without changing the program's code**:

| Layer | Implementation | What is real | What is emulated |
|---|---|---|---|
| A. Logical simulation | `--simulate`, `--simulate-hp` and the pytest suite | the program's code | everything else, in-process |
| B. Protocol emulation | `lab/hidemu`: USB and HID descriptors, vendor channel (if2), LampArray (if3), boot keyboard (if0), faults, hex log | — | the keyboard firmware |
| B'. Fast conformance | the program's real drivers against `hidemu` through a hidraw-like adapter (`lab/tests`) | the program's code | the kernel |
| C. Operating system | Debian testing "forky" under QEMU (TCG, or KVM when available): Python 3.14, systemd 262, kernel 7.2 — the base of SparkyLinux rolling | kernel (usbhid, hidraw, evdev, vhci-hcd), udev with the program's rules, logind with a local seat0 session, the systemd user service, `install.sh`/`uninstall.sh`, S3 suspend with a frozen `user.slice`, GTK 4 | the keyboard, over USB/IP |
| D. Real hardware | `lab/tools/capture_descriptors.py` (read-only) and a manual checklist | everything | — |

**Why USB/IP** (and not UHID, a USB gadget or a custom QEMU device): the guest kernel sees a
complete USB device — four interfaces, `bcdDevice=21.16`, "HP, Inc HyperX Alloy Origins
Core" — and every transfer, including feature `GET/SET_REPORT` and the `EPROTO` of the HP's
vendor channel, is answered by the emulator. UHID has no USB parent (the program and the
udev rule need it); a gadget would need `dummy_hcd` and feature GET_REPORT in `f_hid`; a
custom QEMU device would need a QEMU build.

**Independence.** The lab's key table (`hidemu/keytable.py`) is its own: the program's
key-to-LED map is *checked* against the InputBinding reported by the emulated keyboard, not
reused. LampArray follows the HID standard (page 0x59). The vendor channel follows the
protocol documented by OpenRGB and hxoctkl, which cannot be verified without the keyboard.

## The pytest suite

`python3 -m pytest` runs 357 tests in about a minute, all on simulated keyboards:
protocol encoding, both drivers, LampArray enumeration and limits, every effect, the
engine and its modes, RPC limits and lifecycle, settings and presets, the flash gate, the
udev rules, layouts, LED color correction, suspend and resume handling, i18n catalogs,
and the GUI's thread hand-off. A watchdog interrupts a test that hangs for 120 s and
writes every thread's stack to `/tmp/openalloy-test-hang-<uid>.log`; a test that leaves
OpenAlloy threads running fails.

`python3 -m pytest lab/tests` (20 tests, seconds) runs the real drivers against the
emulator. `tools/test_install.sh OLD.zip NEW.zip` installs, upgrades and rolls back with
the real `install.sh` in a temporary HOME.

## Emulator fidelity and limits

| Aspect | Source | Fidelity |
|---|---|---|
| USB IDs, interfaces 0–3, `bcdDevice`, name | protocol module, udev rules, journal of a real HP | same as observed (`HP, Inc HyperX Alloy Origins Core`, 21.16, 89 LEDs) |
| LampArray report descriptor | HID standard + the layout documented in the driver | **reconstructed**, not captured; replace with `capture_descriptors.py` output |
| Vendor channel descriptor | protocol module (0xFF13, 64 bytes) | reconstructed |
| HP refusing the vendor channel | real journal (`[Errno 71] Protocol error`) | the guest kernel returns the same `EPROTO` |
| LED id order and positions | unknown | by row; positions from the TKL geometry |
| Kingston firmware: direct-mode timeout | "a few seconds" | assumed 3 s |
| Kingston interface 3 | undocumented | generic 0xFF00 collection |
| Suspend | `vhci-hcd` refuses S3 with an active USB/IP link, by design (below) | a lab hook detaches after the program's hook and re-attaches on resume = the keyboard is re-enumerated |
| GUI screenshots | GTK 4 under Xvfb without a GPU | cairo renderer (the GL renderer draws no text without a GPU) |

### Suspend over USB/IP

- `vhci-hcd` refuses to suspend while a USB/IP device is attached ("We have 1 active
  connection. Do not suspend", `-EBUSY`): the TCP connection does not survive the client
  sleeping. The kernel patch "usb: vhci-hcd: Prevent suspending virtually attached
  devices" (Collabora, July 2025, also in the stable series) *reinforces* that refusal. So
  there is no supported way to suspend with the keyboard attached over USB/IP, and the
  lab's detach/re-attach is necessary, not a shortcut.
- Detaching and re-attaching reproduces one of the two real paths of a USB keyboard
  through S3: **re-enumeration** (the controller was reset or lost power; OpenAlloy's own
  hook also resets the device before sleeping).
- The other real path is **USB persist** (kernel documentation): the device comes back
  with the same descriptors and the kernel keeps the same structures — same `hidraw` node,
  same sysfs identity — although the keyboard has lost its colors. It cannot be exercised
  over USB/IP; in the program it is covered by the clock-based resume detection (1.5.3+)
  and by `test_resume_is_noticed_without_the_hook` (same identity, clock jump → color
  restored). On a real computer the journal shows which path the controller takes:
  `Keyboard reconnected` means re-enumeration; only `System resumed (N s asleep)` means
  persist.

**What the lab does not prove:** light on real LEDs, descriptors byte for byte, the real
firmware's timing, remote wake-up through the real USB port, other desktop environments.

## Lab scenarios (127 checks per version)

Environment · installation (`install.sh --lang pt_BR --reactive`) · permissions (ACL per
interface; interfaces 0 and 1 stay closed) · HP detection (`EPROTO` on if2, 89 LED
attribute reads, journal) · colors (red on every LED; a sweep of 89 keys with a unique
color each, checked by the HID usage of the LED; ABNT2 keys and Fn; brightness; gamma
correction) · 20 effects (animate or not; the LampArray minimum update interval is
respected) · invalid parameters · reactive (a key pressed on if0 → real evdev → the LED of
that key) · fixed color (`hold`: nothing is sent afterwards; `release`) · presets ·
Kingston (sweep in the 380-byte GRB buffer, keepalive, hardware brightness, profile, Fn +
profile key) · **onboard write** (locked before the RGB test; storage read in 13 blocks and
written back byte for byte; never the `0x30` that crashes the firmware) · HP faults (STALL,
duplicated LED reply, zero LEDs, slow device, five reconnections, fd/thread leaks) ·
Kingston faults (unexpected reply, mute keyboard, unplug during a write, total refusal) ·
no udev rule · **real S3 suspend** · service lifecycle (restart, SIGHUP, stop) ·
performance · graphical interface (real service, *Fix on keyboard* checked on the device,
language switch) · uninstall.

## Lab results — same scenarios, same base VM, three versions

| Version | Passed | Failed | Failed checks |
|---|---|---|---|
| 1.5.2 | 120 | 7 | udev rule; mixed-language log line (2 checks); `release` after `hold`; `preset delete` of a missing preset exits 0; onboard mode misses a replug; language switch in the window |
| 1.5.3 | 122 | 5 | udev rule; `release` after `hold`; `preset delete`; onboard mode; language switch in the window |
| 1.5.4 | **127** | **0** | — |

From 1.5.3 to 1.5.4 there were only changes from failed to passed: **no regression**.
1.6.0 adds documentation, packaging and manual pages, and fixes one defect the lab showed
(a non-numeric effect parameter answered with Python's untranslated text).

**Real suspend (systemd 262 freezes `user.slice` during the hooks)**

| Version | Hooks before sleep | Resume to thaw | Sleep notice delivered after the resume |
|---|---|---|---|
| 1.5.2 | 5.1 s | 4.2 s | yes — in 1 of 2 runs it arrived after the "wake" and the service stayed **asleep** (the fixed color did not come back) |
| 1.5.3 | 3.8 s | 1.2 s | no; `System resumed (N s asleep)` |
| 1.5.4 | 2.7 s | 1.5 s | no; `System resumed (11 s asleep)`, fixed color back in 4.6 s |

The times before sleep include the lab's own hook, which waits for the program's hook.

**Metrics (1.5.4, TCG without KVM — relative values, for comparing versions)**

| Metric | Value | Note |
|---|---|---|
| VM boot | 38 s | TCG, 2 vCPUs |
| CLI command → color on the device | 1.5 s (3 runs) | includes starting the CLI's Python under emulation |
| Agent round trip (empty command) | 0.24 s | subtract from the above |
| Reconnection after replug (5 cycles) | 3.8–3.9 s | from USB plug to the first color sent |
| *wave* effect: service CPU | ~21 % of one emulated vCPU | much lower on real hardware (1.5.2: 20.9 %, 1.5.3: 20.3 %) |
| *wave* effect: memory (RSS) over 90 s | 26.2 MB, constant | no growth |
| Open descriptors / threads | 6 / 3, constant | no leak, also after the fault scenarios |
| LampArray updates | 17–21 per second | within the device limit (≤ 33/s for 30 ms); 0 violations in 20 effects |
| 89-key sweep | 0 wrong keys | HP (by HID usage) and Kingston (GRB buffer) |

## Audit history

Each audit reproduced every defect before fixing it and added a test that fails on the
previous version. User-visible effects are in [NEWS](../NEWS).

**1.4.1 — package clean-up.** Dead code removed (unused color helpers, an unused opener
class, an unused brightness quantizer, orphan imports); repeated numbers replaced by
protocol constants; eight outdated screenshots (1.16 MB) replaced by two current ones
(61 KB); the `gui` extra declares pycairo. No behavior change.

**1.5.1 — audit of 1.5.0.**

| Severity | Defect | Fix |
|---|---|---|
| High | a local variable named `_` hid the translation function: a vendor-channel read error became `UnboundLocalError`/`TypeError` and could block the LampArray fallback | renamed; a static test forbids `_` as a variable where `_()` is called |
| Medium | the reactive key reader always ended with an exception and stayed "active" | same cause |
| Medium | a translation with a wrong placeholder broke the message | the compiler drops it; English is used |
| Low | `--lang` without a value or with an unknown code; an `assert` used as a runtime check | fixed |
| Optimization | 48 LampArray reports per second for an unchanged color | per-driver refresh: about 4 per second |

**1.5.2 — external review of 1.5.1.** Real problems found: idle RPC connections held a slot
forever; replies and events could interleave on one socket; event subscriptions were slow
to stop; service shutdown left threads; a deeply nested `config.json` crashed the service
and the CLI; a corrupted `flash_validated` record crashed `status()`; concurrent preset
deletion raised `FileNotFoundError`; a duplicated LampArray reply left a LED unidentified;
late GUI replies were shown after a switch. Items of the review that were checked and are
**not** defects were documented as such (for example, preset names that differ only in case
or punctuation are the same preset by design).

**1.5.3 — journal of the real HP keyboard and review of 1.5.2.**

| # | Defect | Evidence | Severity |
|---|---|---|---|
| 1 | the sleep hook talked to the **frozen** service (systemd 256+), delaying sleep and resume by about 3 s each | journal: "going to sleep" and "resumed" in the same second | Medium |
| 2 | late notices in reverse order left the service "asleep" | reproduced: `wake()` before `suspend()` | High (rare) |
| 3 | service stop hung (SIGTERM handler vs `Event.wait()`; signal delivered to another thread after a freeze) | 4 of 5 attempts hung under SIGSTOP/SIGCONT/SIGTERM | Medium |
| 4 | stopping the RPC while a connection arrived: `cannot join thread before it is started`, exit status 1 | stack captured under load | Medium |
| 5 | mixed-language journal and a doubled error number | real journal | Low |
| 6 | the RPC output queue was limited to 256 messages | 500 events at once → client dropped | Low |
| 7 | `openalloy status \| head -1` → "Broken pipe", status 1 | reproduced | Low |
| 8 | an invalid number showed Python's untranslated text | `fps=abc` | Low |
| 9 | tests with fixed sleeps before checks | review | — |

**1.5.4 — found by the test lab.**

| # | Defect | Severity | Test |
|---|---|---|---|
| 1 | the udev rule **never matched** (`ATTRS{idVendor}` and `ATTRS{bInterfaceNumber}` belong to different parents; udev requires one parent per rule): no access on a clean system | High | `tests/test_udev_rules.py`; lab: ACL on if2/if3 only, if0/if1 closed |
| 2 | `release` / `hold --release` after `hold` did not hand the LEDs back | Medium | `test_release_after_a_fixed_colour_hands_the_leds_back` |
| 3 | GUI replies could starve at idle priority (language switch saved but not applied) | Medium on a slow machine | `tests/test_gui_priority.py` |
| 4 | onboard mode did not notice a replug | Low | `test_onboard_mode_notices_a_replug` |
| 5 | a lighting event before the initial data → `IndexError` in the window | Low | lab GUI scenario |
| 6 | `preset delete` of a missing preset exited 0 | Low | `test_deleting_a_missing_preset_fails` |
| 7 | a keyboard refusing the vendor channel without LampArray logged a misleading line every 2 s | Low | `test_vendor_refusal_logged_once_and_worded_for_the_case` |

Checked in the lab and **not** defects: an out-of-range parameter (`speed=999`) is clamped
to the maximum and the applied value is shown; the vendor channel is not retried within the
same run (cache per VID, PID and firmware, by design); a feature-report STALL is logged and
the keyboard reconnects in about 5 s; switching keyboards (Kingston ↔ HP) works. Known
limitation: with `--lang en` on the CLI and the service in `pt_BR`, messages produced by the
service come in the service's language.

## Pending items that only real hardware can settle

1. Run `python3 lab/tools/capture_descriptors.py > descriptors.json` on the computer with
   the keyboard (read-only) so the lab can use the real descriptors.
2. Confirm on the HP keyboard that `openalloy hold blue` followed by `openalloy hold
   --release` returns the keyboard to its firmware effect.
3. Run the udev permission check in [INSTALL](../INSTALL) (section 4) when upgrading from
   1.5.3 or earlier.

## Suggested next steps

KVM on the host (the lab runs about ten times faster); the real descriptors; `lab/tests` in
CI on every commit and the full lab before each release; an in-VM upgrade scenario (today
covered by `tools/test_install.sh`).
