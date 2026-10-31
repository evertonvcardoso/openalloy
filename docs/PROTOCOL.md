# HyperX Alloy Origins Core protocol

What OpenAlloy knows about the keyboard and how it talks to it. Implemented in
`openalloy/infra/protocol.py` (pure data, no I/O), `openalloy/infra/drivers.py` (vendor
channel) and `openalloy/infra/lamparray.py` (HID LampArray). Sources: OpenRGB (GPL-2.0),
hxoctkl `docs/PROTOCOL.md` (GPL-2.0-or-later) and the HID Usage Tables; see
[THANKS](../THANKS).

## USB interfaces

| Interface | Content | Opened by OpenAlloy |
|---|---|---|
| 0 | boot keyboard (keystrokes) | only for reactive effects, read-only, through evdev, with `--reactive` |
| 1 | other input reports (keys) | never |
| 2 | vendor channel, usage page `0xFF13` | yes |
| 3 | HID LampArray, usage page `0x59` (HP firmware 21.16) | yes |

The udev rule grants the logged-in user access to interfaces 2 and 3 only.

## Vendor channel (interface 2)

### Transport

- USB `0951:16e6` (HyperX) or `03f0:098f` (HP), interface 2, usage page `0xFF13`.
- 64-byte reports without a report ID; the write buffer is 65 bytes (byte 0 = 0).
- Request: `[1]=command [2]=index [3]=block [4]=length [5..]=data`.
- Reply: `[0]=command [1]=index [2]=block [3]=length echo [4..]=data`.
- Status: `ec 00` unsupported, `ec 01` wrong parameters, `ec ac` success.

The HP `03f0:098f` with firmware 21.16 refuses every write on this interface with
`EPROTO`, already on the `0x10` query; OpenAlloy then uses HID LampArray.

### Commands

| Command | Use |
|---|---|
| `0x10` | info: firmware in bytes 4–5, name in 8–32, **layout in byte 56** (`09` ANSI, `10` ABNT2) |
| `0x15` / `0x95` | read / select the active profile (1–3) |
| `0x27` / `0xA7` | read / set the brightness (0–255, 5 effective levels) |
| `0xA2` | live stream: 7 blocks (6 × 60 + 1 × 20 bytes), each write `data + 5` bytes |
| `0x22` / `0xA3` | read / write the key-press layer (index `profile<<4 \| block`) |
| `0x30` / `0xB0` | read / write the 780-byte profile storage (13 blocks). **Never send `0x30` without parameters** — it resets the firmware |
| `0x93` | effect GUID (metadata; OpenAlloy writes zeros) |
| `0x94` | 5-byte profile storage |
| `0xA4` | start (`55 N`) / end (`aa N`) of an animation upload |
| `0xA5` | animation frame: index `profile<<4 \| block`, byte 3 = frame number |
| `0xA0` | profile configuration block (byte 10 = number of frames) |

Unsolicited notifications: `0x70 [profile]` (Fn + 1/2/3), `0x71`, `0x73 [usage, 01/02]`.

### Color buffer (380 bytes, 94 LEDs, G/R/B planes)

```
pos = (idx // 12) * 48 + ((idx // 6) & 1) * 8 + (idx % 6)
green at pos, red at pos + 16, blue at pos + 32
```

LEDs without a key on ANSI: 34, 44, 46, 47, 77, 83, 93. On ABNT2, 34 (`\`), 77 (`]`) and
83 (`/`) exist and 69 (ANSI `\`) does not.

### Onboard write sequence (same as NGENUITY)

```
0x30 x13      read the 780-byte storage (refuse the write if this fails)
0x95          select the profile
0x94          5-byte storage
0xB0 x13      write the storage back, unchanged
0x93          GUID (zeros)
0xA4 55 N     start
0xA5 x7xN     frames
0xA4 aa N     end
0xA0          configuration (N frames, peak color)
0x95          select the profile
-- key-press layer --
0xA2 x7 + 0xA3   send to the buffer and commit to the profile bank; verify with 0x22
```

The base animation cannot be read back (the firmware has no getter for `0xA5`); only the
key-press layer can. The onboard animation speed is set by the firmware and controlled
through the number of frames (up to 255).

### Live mode

The keyboard returns to its onboard profile a few seconds after the stream stops, so the
service keeps sending frames (30 fps when animated; a static frame is refreshed every
0.25 s).

## HID LampArray (interface 3)

Used when the firmware refuses the vendor channel. Observed on the HP `03f0:098f`,
firmware 21.16: interface 3 declares usage page 0x59 (*Lighting and Illumination*). All
reports are *feature* reports (SET/GET_REPORT on the control endpoint, `HIDIOCSFEATURE` /
`HIDIOCGFEATURE` ioctls), little-endian, first byte = report ID:

| ID | Dir | Report | Fields |
|---|---|---|---|
| 1 | GET | LampArrayAttributes | LampCount u16, BoundingBox W/H/D u32 (µm), Kind u32, MinUpdateInterval u32 (µs) |
| 2 | SET | LampAttributesRequest | LampId u16 |
| 3 | GET | LampAttributesResponse | LampId u16, Position X/Y/Z u32 (µm), UpdateLatency u32, Purposes u32, R/G/B/I levels u8, IsProgrammable u8, **InputBinding u8** (HID usage of the key) |
| 4 | SET | LampMultiUpdate | LampCount u8, Flags u8, LampId u16 × 8, RGBI u8 × 8 |
| 5 | SET | LampRangeUpdate | Flags u8, LampIdStart u16, LampIdEnd u16, R, G, B, I u8 |
| 6 | SET | LampArrayControl | AutonomousMode u8 (1 = the firmware drives the LEDs) |

Flags bit 0 = *LampUpdateComplete* (apply the pending colors).

Sequence: read 1; for each LED send 2 and read 3 (a missing or duplicated reply is asked
again); send 6 with 0; update with 4 (changed LEDs only) or 5 (one color for all); on exit,
or to hand the lighting back, send 6 with 1. The minimum update interval reported by the
keyboard is respected. The key-to-LED map and the layout come from the InputBinding of
each LED. The HP reports 89 LEDs.

There is no brightness, profile or onboard memory in this standard: brightness is applied
in software, and a color sent once without AutonomousMode stays on the LEDs until unplug,
reboot or suspend (the *fixed* mode).
