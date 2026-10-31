# Security policy

## Supported versions

Security fixes are made for the latest release only. Please upgrade before reporting:
`openalloy --version` shows the version you run, and the
[latest release](https://github.com/evertonvcardoso/openalloy/releases/latest) is always
listed on GitHub.

## Reporting a vulnerability

**Please do not open a public issue for a security problem.** Report it privately through
GitHub: [Report a vulnerability](https://github.com/evertonvcardoso/openalloy/security/advisories/new)
(the *Security* tab › *Report a vulnerability*). Only the maintainer can see the report.

Include, if you can:

- the OpenAlloy version and your distribution;
- what an attacker can do and under which conditions (local user, another user on the
  same machine, the network);
- steps to reproduce, or a proof of concept;
- the output of `openalloy doctor`, with anything you consider private removed.

This is a project maintained by one person in his spare time. Reports are answered on a
best-effort basis; you will be told whether the report is accepted, and credited in the
release notes unless you prefer otherwise. Please allow a fix to be released before
disclosing the details publicly.

## Scope

OpenAlloy runs as the logged-in user, with no root daemon. The parts most relevant to
security are:

| Component | What it does | Runs as |
|---|---|---|
| `60-openalloy.rules` | grants the active local session access to the keyboard's **lighting** interfaces (USB interfaces 2 and 3) only | udev |
| `61-openalloy-reactive.rules` (optional, `--reactive`) | grants read access to **this keyboard's** key events, for the key-reactive effects | udev |
| `openalloy-sleep` | resets the keyboard's USB device around system sleep | root (systemd-sleep hook) |
| `openalloyd` and its socket | the user service; `$XDG_RUNTIME_DIR/openalloy.sock`, mode 0600 | the user |
| onboard writes | write the keyboard's flash memory, gated by a live RGB test | the user |

Examples of what is in scope: a rule that exposes the keystroke interfaces to other users,
a way to make the sleep hook do anything other than reset this keyboard, another local user
reaching the service socket, or a way to send arbitrary HID commands to the keyboard.

Out of scope: the test lab in `lab/` (a development tool, never installed), and anything
that requires the user's own account already being compromised.
