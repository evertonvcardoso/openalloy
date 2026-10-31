#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
# Removes everything install.sh added. Your presets and settings in
# ~/.config/openalloy are kept unless you pass --purge.
# Onboard profiles already written to the keyboard stay in the keyboard.
set -uo pipefail

PURGE=0
OA_LANG=""
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    --lang=*) OA_LANG="${arg#--lang=}" ;;
  esac
done

# t MSG - translated message (falls back to English)
t() {
  local out
  if command -v python3 >/dev/null 2>&1 && \
     out="$(cd "$SRC" && python3 -m openalloy.i18n tr ${OA_LANG:+--lang "$OA_LANG"} "$1" 2>/dev/null)"; then
    printf '%s' "$out"
  else
    printf '%s' "$1"
  fi
}
SUDO=sudo; command -v sudo >/dev/null || SUDO=doas

systemctl --user disable --now openalloy.service 2>/dev/null
rm -f "$HOME/.config/systemd/user/openalloy.service"
systemctl --user daemon-reload 2>/dev/null
pipx uninstall openalloy 2>/dev/null
rm -f "$HOME/.local/share/applications/openalloy.desktop" \
      "$HOME/.local/share/icons/hicolor/scalable/apps/openalloy.svg" \
      "${XDG_DATA_HOME:-$HOME/.local/share}/man/man1/openalloy.1" \
      "${XDG_DATA_HOME:-$HOME/.local/share}/man/man1/openalloyd.1"
if [[ -f /etc/udev/rules.d/60-openalloy.rules || -f /etc/udev/rules.d/61-openalloy-reactive.rules ]]; then
  $SUDO rm -f /etc/udev/rules.d/60-openalloy.rules /etc/udev/rules.d/61-openalloy-reactive.rules
  $SUDO udevadm control --reload-rules
fi
for hook in /usr/lib/systemd/system-sleep/openalloy /etc/systemd/system-sleep/openalloy; do
  [[ -e "$hook" ]] && $SUDO rm -f "$hook"
done
if [[ $PURGE -eq 1 ]]; then
  rm -rf "$HOME/.config/openalloy"
  t "Settings and presets removed."; echo
fi
t "OpenAlloy removed. Profiles written to the keyboard memory stay in it."; echo
