#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
# Install -> update -> rollback check, with the real install.sh, in a throw-away HOME.
#
#   tools/test_install.sh OLD.zip NEW.zip
#
# Uses only user-level steps (pipx; --no-deps --no-udev --no-service --no-desktop), so it
# needs no sudo and does not touch the running system. Checks after each step:
# installed version, saved settings (language) and a user preset survive; the manual
# pages of versions that ship them are installed, and the new uninstall.sh removes them.
set -euo pipefail

OLD_ZIP="$(realpath "$1")"
NEW_ZIP="$(realpath "$2")"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
export HOME="$WORK/home" XDG_CONFIG_HOME="$WORK/home/.config" XDG_RUNTIME_DIR="$WORK/run"
export PIPX_HOME="$WORK/pipx" PIPX_BIN_DIR="$WORK/bin" PATH="$WORK/bin:$PATH"
mkdir -p "$HOME" "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"

ok()   { printf '[ OK ] %s\n' "$*"; }
fail() { printf '[FAIL] %s\n' "$*" >&2; exit 1; }

install_zip() {   # $1 = zip, $2 = label
  local dir="$WORK/src-$2"
  mkdir -p "$dir"
  (cd "$dir" && python3 -m zipfile -e "$1" .)
  (cd "$dir/openalloy" && bash ./install.sh --no-deps --no-udev --no-service --no-desktop \
      >"$WORK/install-$2.log" 2>&1) || { cat "$WORK/install-$2.log"; fail "install $2"; }
}

version_of() { python3 -c "import sys,zipfile; z=zipfile.ZipFile(sys.argv[1]); \
import re; print(re.search(r'__version__ = \"(.*)\"', z.read('openalloy/openalloy/__init__.py').decode()).group(1))" "$1"; }

OLD_V="$(version_of "$OLD_ZIP")"
NEW_V="$(version_of "$NEW_ZIP")"

install_zip "$OLD_ZIP" old
[[ "$(openalloy --version)" == "OpenAlloy $OLD_V" ]] || fail "old version not installed"
ok "installed $OLD_V"
openalloy language pt_BR >/dev/null
mkdir -p "$XDG_CONFIG_HOME/openalloy/presets"
printf '{"name": "Mine", "lighting": {"effect": "static", "params": {"color": "#123456"}}}\n' \
  >"$XDG_CONFIG_HOME/openalloy/presets/mine.json"

install_zip "$NEW_ZIP" new
[[ "$(openalloy --version)" == "OpenAlloy $NEW_V" ]] || fail "update did not install $NEW_V"
openalloy language | grep -q '\* pt_BR' || fail "language preference lost on update"
openalloy --simulate-hp preset list | grep -q '^  mine ' || fail "user preset lost on update"
openalloy --simulate-hp status | grep -q 'Teclado' || fail "new version does not start"
ok "updated to $NEW_V (settings and preset kept)"
MAN1="$HOME/.local/share/man/man1"
if [[ -d "$WORK/src-new/openalloy/man" ]]; then
  for page in openalloy.1 openalloyd.1; do
    grep -q "\"OpenAlloy $NEW_V\"" "$MAN1/$page" 2>/dev/null || fail "manual page $page not installed"
  done
  ok "manual pages installed in $MAN1"
fi

install_zip "$OLD_ZIP" rollback
[[ "$(openalloy --version)" == "OpenAlloy $OLD_V" ]] || fail "rollback did not install $OLD_V"
openalloy language | grep -q '\* pt_BR' || fail "language preference lost on rollback"
openalloy --simulate-hp preset list | grep -q '^  mine ' || fail "user preset lost on rollback"
ok "rolled back to $OLD_V (settings and preset kept)"

if [[ -f "$WORK/src-new/openalloy/uninstall.sh" && -d "$WORK/src-new/openalloy/man" ]]; then
  install_zip "$NEW_ZIP" final
  (cd "$WORK/src-final/openalloy" && bash ./uninstall.sh >"$WORK/uninstall.log" 2>&1) || true
  hash -r
  if command -v openalloy >/dev/null || [[ -e "$PIPX_BIN_DIR/openalloy" ]]; then
    cat "$WORK/uninstall.log"; fail "uninstall left the openalloy command"
  fi
  [[ -e "$MAN1/openalloy.1" || -e "$MAN1/openalloyd.1" ]] && fail "uninstall left the manual pages"
  [[ -f "$XDG_CONFIG_HOME/openalloy/presets/mine.json" ]] || fail "uninstall removed user presets"
  ok "uninstalled $NEW_V (manual pages removed, presets kept)"
fi
