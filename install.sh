#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
# OpenAlloy installer - HyperX Alloy Origins Core configurator for Linux.
#
# What it does (each step can be skipped, see --help):
#   1. installs system packages: Python 3, pipx, PyGObject + GTK4 + gi-cairo
#   2. installs OpenAlloy with pipx (isolated, but with system site-packages so
#      the distro's PyGObject is visible)
#   3. installs a udev rule granting YOUR seat access to the keyboard's lighting
#      interfaces only (needs sudo once)
#   4. installs a systemd *user* service (no root daemon, no sudoers rule)
#   5. installs the desktop launcher and icon
#   6. installs a systemd-sleep hook that releases the keyboard around suspend
#   7. installs the manual pages, openalloy(1) and openalloyd(1), for your user
#
# Differences from Linuity's installer: no root service, no sudoers file, no
# device picker (the keyboard is found automatically by USB id + interface).
#
# Messages are English and translated with the project's catalog (--lang,
# $OPENALLOY_LANG or the saved preference); without Python they stay in English.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RES="$SRC/openalloy/resources"
DO_DEPS=1 DO_UDEV=1 DO_SERVICE=1 DO_DESKTOP=1 REACTIVE=0 DEV=0 OA_LANG=""

# t MSG [ARG...] - translated message (printf-style %s arguments)
t() {
  local msg="$1" out
  shift
  if command -v python3 >/dev/null 2>&1 && \
     out="$(cd "$SRC" && python3 -m openalloy.i18n tr ${OA_LANG:+--lang "$OA_LANG"} "$msg" "$@" \
            2>/dev/null)"; then
    printf '%s' "$out"
    return
  fi
  # shellcheck disable=SC2059
  printf "$msg" "$@"
}

usage() {
  t "Usage: ./install.sh [options]"; echo
  echo "  --lang CODE    $(t "installer and interface language (en, pt_BR)")"
  echo "  --reactive     $(t "also allow reading the keyboard's keys (Reactive/Ripple effects)")"
  echo "  --no-deps      $(t "do not install system packages")"
  echo "  --no-udev      $(t "do not install the udev rule")"
  echo "  --no-service   $(t "do not install/enable the systemd user service")"
  echo "  --no-desktop   $(t "do not add a menu shortcut")"
  echo "  --dev          $(t "editable install (pipx install -e)")"
  echo "  -h, --help     $(t "this help")"
}

ARGS=("$@")
for ((i = 0; i < ${#ARGS[@]}; i++)); do
  arg="${ARGS[$i]}"
  case "$arg" in
    --lang) i=$((i + 1)); OA_LANG="${ARGS[$i]:-}" ;;
    --lang=*) OA_LANG="${arg#--lang=}" ;;
    --reactive) REACTIVE=1 ;;
    --no-deps) DO_DEPS=0 ;;
    --no-udev) DO_UDEV=0 ;;
    --no-service) DO_SERVICE=0 ;;
    --no-desktop) DO_DESKTOP=0 ;;
    --dev) DEV=1 ;;
    -h|--help) usage; exit 0 ;;
    *) t "unknown option: %s" "$arg"; echo; usage; exit 1 ;;
  esac
done

c_ok()   { printf '\033[32m[ + ]\033[0m %s\n' "$*"; }
c_info() { printf '\033[36m[ * ]\033[0m %s\n' "$*"; }
c_warn() { printf '\033[33m[ ! ]\033[0m %s\n' "$*"; }
c_err()  { printf '\033[31m[ x ]\033[0m %s\n' "$*" >&2; }

if [[ $EUID -eq 0 ]]; then
  c_err "$(t "Run as your normal user (the script asks for sudo only when needed).")"
  exit 1
fi

SUDO=""
need_sudo() {
  if [[ -z "$SUDO" ]]; then
    if command -v sudo >/dev/null; then SUDO=sudo
    elif command -v doas >/dev/null; then SUDO=doas
    else c_err "$(t "sudo/doas not found")"; exit 1; fi
  fi
}

# --- 1. system packages ------------------------------------------------------
if [[ $DO_DEPS -eq 1 ]]; then
  need_sudo
  c_info "$(t "Installing system dependencies...")"
  if command -v apt-get >/dev/null; then
    $SUDO apt-get update -qq
    $SUDO apt-get install -y python3 python3-venv pipx python3-gi python3-gi-cairo \
      gir1.2-gtk-4.0 librsvg2-common pulseaudio-utils
  elif command -v pacman >/dev/null; then
    $SUDO pacman -S --needed --noconfirm python python-pipx python-gobject python-cairo gtk4 librsvg libpulse
  elif command -v dnf >/dev/null; then
    $SUDO dnf install -y python3 pipx python3-gobject python3-cairo gtk4 librsvg2 pulseaudio-utils
  elif command -v zypper >/dev/null; then
    $SUDO zypper --non-interactive install python3 python3-pipx python3-gobject \
      python3-gobject-cairo typelib-1_0-Gtk-4_0 librsvg pulseaudio-utils
  else
    c_warn "$(t "Unknown package manager. Install manually: Python >= 3.10, pipx, PyGObject, pycairo (gi-cairo) and GTK 4.")"
  fi
  c_ok "$(t "Dependencies installed")"
fi

# --- 2. the application ------------------------------------------------------
if ! command -v pipx >/dev/null; then
  c_err "$(t "pipx not found. Install it (or run without --no-deps).")"
  exit 1
fi
c_info "$(t "Installing OpenAlloy with pipx...")"
PIPX_ARGS=(install --force --system-site-packages)
if [[ $DEV -eq 1 ]]; then PIPX_ARGS+=(-e); fi
pipx "${PIPX_ARGS[@]}" "$SRC"
pipx ensurepath >/dev/null 2>&1 || true
BIN_DIR="$(pipx environment --value PIPX_BIN_DIR 2>/dev/null || echo "$HOME/.local/bin")"
DAEMON_BIN="$BIN_DIR/openalloyd"
c_ok "$(t "Installed in %s" "$BIN_DIR")"

# --- language preference (only when --lang was given) ---------------------------
if [[ -n "$OA_LANG" ]]; then
  if (cd "$SRC" && python3 -c 'import sys
from openalloy.config.settings import Settings
Settings().update(language=sys.argv[1])' "$OA_LANG") 2>/dev/null; then
    c_ok "$(t "Language saved: %s" "$OA_LANG")"
  else
    c_warn "$(t "Unknown language %s; keeping the current one" "$OA_LANG")"
  fi
fi

# --- 3. udev ------------------------------------------------------------------
if [[ $DO_UDEV -eq 1 ]]; then
  need_sudo
  c_info "$(t "Installing the udev rule (access to the keyboard lighting interfaces only)...")"
  $SUDO install -m 0644 "$RES/60-openalloy.rules" /etc/udev/rules.d/60-openalloy.rules
  if [[ $REACTIVE -eq 1 ]]; then
    $SUDO install -m 0644 "$RES/61-openalloy-reactive.rules" /etc/udev/rules.d/61-openalloy-reactive.rules
    c_ok "$(t "Rule for reactive effects installed")"
  fi
  # Linuity/PlasmaNGenuity/hxoctkl rules do not conflict, but an old hxoctkl
  # rule grants access to ALL interfaces (including your keystrokes).
  if [[ -f /etc/udev/rules.d/60-hxoctkl.rules ]]; then
    c_warn "$(t "Found /etc/udev/rules.d/60-hxoctkl.rules (it opens every interface); the OpenAlloy rule is stricter. Remove the old one if you do not use hxoctkl.")"
  fi
  # Release the keyboard around system sleep (prevents a PC that won't wake).
  SLEEP_DIR=/usr/lib/systemd/system-sleep
  if findmnt -n -o OPTIONS --target /usr/lib 2>/dev/null | tr , "\n" | grep -qx ro; then
    SLEEP_DIR=/etc/systemd/system-sleep
  fi
  $SUDO install -d "$SLEEP_DIR"
  $SUDO install -m 0755 "$RES/openalloy-sleep" "$SLEEP_DIR/openalloy" && \
    c_ok "$(t "Sleep hook installed in %s" "$SLEEP_DIR/openalloy")"
  $SUDO udevadm control --reload-rules
  $SUDO udevadm trigger --subsystem-match=hidraw --subsystem-match=input || true
  c_ok "$(t "udev rule active (if the keyboard was already plugged in and is not detected, replug it)")"
fi

# --- 4. systemd user service -------------------------------------------------
if [[ $DO_SERVICE -eq 1 ]]; then
  if command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1; then
    UNIT_DIR="$HOME/.config/systemd/user"
    mkdir -p "$UNIT_DIR"
    sed "s|^ExecStart=.*|ExecStart=$DAEMON_BIN|" "$RES/openalloy.service" > "$UNIT_DIR/openalloy.service"
    systemctl --user daemon-reload
    systemctl --user enable openalloy.service
    # restart (not just start): after an update the old version must not keep running
    systemctl --user restart openalloy.service
    c_ok "$(t "User service running (new version loaded): systemctl --user status openalloy")"
  else
    c_warn "$(t "systemd user session unavailable; run 'openalloyd' by hand or from autostart.")"
  fi
fi

# --- 5. desktop entry ------------------------------------------------------------
if [[ $DO_DESKTOP -eq 1 ]]; then
  APPS="$HOME/.local/share/applications"
  ICONS="$HOME/.local/share/icons/hicolor/scalable/apps"
  mkdir -p "$APPS" "$ICONS"
  sed "s|^Exec=.*|Exec=$BIN_DIR/openalloy gui|" "$RES/openalloy.desktop" > "$APPS/openalloy.desktop"
  install -m 0644 "$RES/openalloy.svg" "$ICONS/openalloy.svg"
  command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" >/dev/null 2>&1 || true
  command -v gtk-update-icon-cache >/dev/null && \
    gtk-update-icon-cache -q "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true
  c_ok "$(t "'OpenAlloy' shortcut added to the application menu")"
fi

# --- 6. manual pages (per user; man-db finds them next to ~/.local/bin) ---------
MAN_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/man/man1"
if [[ -d "$SRC/man" ]]; then
  mkdir -p "$MAN_DIR"
  install -m 0644 "$SRC/man/openalloy.1" "$SRC/man/openalloyd.1" "$MAN_DIR/"
  c_ok "$(t "Manual pages installed: man openalloy, man openalloyd")"
fi

# --- conflicts ------------------------------------------------------------------
for p in openrgb OpenRGB; do
  if pgrep -x "$p" >/dev/null 2>&1; then
    c_warn "$(t "OpenRGB is running and may fight over the keyboard. Close it or disable the 'HyperX Alloy Origins Core' device in it.")"
  fi
done
if systemctl list-unit-files linuity.service >/dev/null 2>&1 && \
   systemctl is-active --quiet linuity.service 2>/dev/null; then
  c_info "$(t "Linuity detected (microphone) - it can keep running, there is no conflict.")"
fi

echo
c_ok "$(t "Done! Open 'OpenAlloy' from the menu or run: openalloy gui")"
c_info "$(t "Diagnostics: openalloy doctor     Uninstall: ./uninstall.sh")"
