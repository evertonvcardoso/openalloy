# Developing OpenAlloy

Architecture, the decisions behind it, and how to maintain the translations.
Testing is described in [TESTING.md](TESTING.md) and the keyboard protocol in
[PROTOCOL.md](PROTOCOL.md).

## Origin

OpenAlloy started from a question: can **Linuity** (a LED controller for the HyperX
QuadCast II and DuoCast microphones) and **PlasmaNGenuity** (an NGENUITY replacement for
the Pulsefire Dart mouse) be combined into a useful configurator for the Alloy Origins
Core? On their own, no — neither talks to a keyboard. But their architectures complement
each other, and together with the keyboard protocol documented by **OpenRGB** and
**hxoctkl** they make a solid base. What was taken from each project is listed in
[THANKS](../THANKS).

| Topic | Linuity | PlasmaNGenuity | OpenAlloy |
|---|---|---|---|
| Device model | 2 intensity zones | mouse (DPI, buttons) | 94 RGB LEDs + profiles + brightness |
| Drivers | factory with `if` per PID | registry with `match()` | registry with exact `match(vid, pid, interface)` |
| Transport | hidapi + pyusb | hidapi | `/dev/hidraw` with `os.read`/`os.write` and ioctls — no dependencies |
| Effects | stateful objects, one step per tick | — | pure periodic functions of time |
| Applying a change | write a file + `sudo systemctl restart` | direct write | RPC over a Unix socket, instant |
| Daemon | root, system unit + sudoers | none (tray app) | `systemd --user`, no root, no sudoers |
| Permissions | broad udev `MODE` | udev `MODE=0666` | udev `uaccess` on the lighting interfaces only |
| Configuration | `key=value` | JSON + deep merge | JSON + deep merge + atomic writes + validation |
| Interface | GTK 4 | Qt 5 + QML | GTK 4 (Linuity's base) with an interactive keyboard drawing |
| Tests | pytest + mocks | none | pytest + protocol simulator + emulated USB lab |

## Layout of the code

```
openalloy/
├── domain/          pure data: colors, layouts and key maps, sound analysis, types
├── application/     engine (modes live/fixed/onboard), effects, ports, backend, flash gate
├── infra/           hidraw transport, vendor and LampArray drivers, protocol, simulator,
│                    RPC server/client, audio capture, key input (evdev), logging
├── config/          settings and presets (XDG, JSON, validation, atomic writes)
├── i18n/            gettext wrapper, lazy messages, regional formats
├── presentation/    cli/ (openalloy), daemon/ (openalloyd), gui/ (GTK 4)
├── resources/       udev rules, systemd unit, sleep hook, desktop entry, icon, presets
└── locales/         openalloy.pot and the en and pt_BR catalogs
man/                 openalloy(1), openalloyd(1)
tools/               i18n.py, hid_diagnostics.py, test_install.sh
tests/               pytest suite (simulated keyboards)
lab/                 test lab: USB/HID emulator, QEMU guest, scenarios (not installed)
```

Dependencies point inwards: `presentation` → `application` → `domain`; `infra` implements
the ports of `application`. Only `presentation` may produce translated text for the user;
the core raises lazily translated `i18n.Message` objects (see below).

## Design decisions

**Pure effects.** An effect is `frame(t) -> 94 colors`, always periodic. This gives a GUI
preview identical to the keyboard, lets the service skip ticks without drifting the speed,
and lets **any** effect be written to onboard memory: one period is sampled into N frames
and the firmware loops it seamlessly. Random-looking effects (starlight, candle, rain) use
deterministic pseudo-random phases and integer harmonics so that they loop too.

**Two drivers, one engine.** The engine drives a `Keyboard` port; the vendor driver and the
LampArray driver declare their `Capabilities` (onboard memory, hardware brightness, fixed
mode) and the interface adapts. The vendor channel is tried first; if it is refused
(`EPROTO`), the refusal is cached per (VID, PID, firmware) and LampArray is used.

**Three modes.** *Live* streams frames (identical frames are only refreshed: LampArray every
2 s, vendor channel every 0.25 s). *Fixed* sends one frame and closes the device without
handing the LEDs back; the service watches the device identity every second and resends
after a replug or a resume. *Onboard* hands the LEDs to the firmware.

**Suspend.** systemd 256+ freezes `user.slice` while sleep hooks run, so the hook never
waits for a frozen service. It resets the USB device (the firmware restarts on its onboard
profile) and, only when services are not frozen, notifies them with the send time. The
service detects a resume itself from the gap between `CLOCK_BOOTTIME` and
`CLOCK_MONOTONIC`, and ignores a sleep notice that arrives after the resume.

**Simulator.** `openalloy/infra/simulator.py` implements the protocol semantics (`ec ac`
replies, header echo, color bank, bracketed uploads, the `0x30` trap, notifications, the
HP refusal plus LampArray). The tests check, for example, that the write sequence is
exactly NGENUITY's and that the 780-byte storage is written back unchanged.

**Safety.** Onboard writes are gated per keyboard and firmware by the live RGB test
(`application/flashgate.py`). Only the two supported USB IDs are accepted, the reset
command is never sent, and the RPC API exposes no raw HID access.

**RPC.** One JSON object per line over `$XDG_RUNTIME_DIR/openalloy.sock` (mode 0600, the
directory's owner and permissions are checked). Limits: 256 KiB per request, 24 clients,
48 concurrent calls, 4 MiB of pending output per client, 30 s for an idle connection.
Events are pushed to subscribers without ever blocking the engine.

**Logs.** The journal is always in English, whatever the interface language: a log record
factory (`infra/logging_config.py`) renders every `Message`, and every exception carrying
one, in English.

## Translations (GNU gettext)

The standard library `gettext` reads the compiled `.mo` catalogs; `tools/i18n.py` does in
pure Python what xgettext, msgmerge and msgfmt would do, so no GNU gettext installation
is needed.

```
openalloy/i18n/__init__.py         current language, _(), ngettext, pgettext, N_, Message, formats
openalloy/i18n/__main__.py         "python3 -m openalloy.i18n tr MSG" (used by install.sh)
openalloy/i18n/argparse_messages.py argparse's user-visible strings (for extraction only)
openalloy/locales/openalloy.pot     template extracted from the code
openalloy/locales/<lang>/LC_MESSAGES/openalloy.po|.mo
openalloy/resources/openalloy.desktop.in   the .desktop file is generated with [pt_BR] keys
```

- **Languages:** `en` (source, default and fallback) and `pt_BR`. The language is chosen by
  `--lang` › `$OPENALLOY_LANG` › the saved `language` setting › English. The system locale
  does not change the default. `pt-BR`, `pt_br`, `pt_BR.UTF-8` and `pt` normalize to
  `pt_BR`; anything unknown falls back to English.
- **Switching:** the window rebuilds itself in the new language without closing; the
  service switches at once; an error already on screen follows, because errors are stored
  as a template plus parameters.
- A missing or broken translation (wrong placeholder) falls back to the English text,
  never to an internal key or an empty string; the `.mo` contains only complete,
  non-fuzzy entries.
- **Not translated on purpose:** service logs, simulator messages and programming
  assertions, key legends drawn on the keyboard, proper and technical names (OpenAlloy,
  LampArray, effect IDs, commands), the language names in the selector (each in its own
  language), RPC identifiers, setting keys and preset IDs. Inputs are accepted in both
  languages where it matters (`yes`/`sim`, `red`/`vermelho`, `rainbow`/`arco-iris`).

### Rules for code

- Write user-facing text in English inside `_()`, or `N_()` where it is defined at import
  time; named placeholders only (`_("Profile {profile} written").format(profile=p)`); never
  concatenate sentence fragments; plurals with `ngettext`; ambiguous words with `pgettext`.
- Core code (`domain`, `application`, `infra`, `config`) raises
  `Message(N_("..."), name=value)` instead of a translated string; a test enforces it.
- Never use `_` as a variable name in a module that calls `_()` (a test enforces it).

### Workflow

```bash
python3 tools/i18n.py all       # extract, update, compile, check
python3 tools/i18n.py stats     # coverage per language
python3 tools/i18n.py check     # exit status 1 on any problem (use in CI)
```

`test_catalogs_are_complete_and_in_sync` fails while a message is untranslated or fuzzy, or
a `.mo` is out of date.

### Adding a language

Create `openalloy/locales/<code>/LC_MESSAGES/`, copy `openalloy.pot` there as
`openalloy.po`, set `Language` and `Plural-Forms` (and `PLURAL_FORMS` in `tools/i18n.py`),
translate, run `python3 tools/i18n.py all`, and add the code to `openalloy.i18n.LANGUAGES`
and to the regional table `_REGION`.

## Code quality and releases

```bash
python3 -m pytest                       # full suite
ruff check openalloy tests tools lab
shellcheck install.sh uninstall.sh tools/test_install.sh
bandit -q -r openalloy
python3 tools/i18n.py check
python3 -m build                        # sdist and wheel
tools/test_install.sh OLD.zip NEW.zip   # install, upgrade, roll back
```

Versions follow Semantic Versioning. Every release updates `openalloy/__init__.py`,
`pyproject.toml`, the manual pages' header, [NEWS](../NEWS) and [ChangeLog](../ChangeLog).
Every source file starts with an SPDX license identifier and a copyright line.
