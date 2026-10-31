#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
# Keeps the emulated keyboards listed in /etc/lab/attach attached over USB/IP, like a
# cable that stays plugged in: when the emulator "unplugs" one (closes the link) it
# is attached again as soon as the emulator accepts it.
HOST=10.0.2.2
modprobe vhci-hcd
while true; do
  if [ -r /etc/lab/attach ] && [ ! -e /run/lab-attach-paused ]; then
    # shellcheck disable=SC2013  # bus ids are words on one line
    for b in $(cat /etc/lab/attach); do
      if ! usbip port 2>/dev/null | grep -q "/$b\$"; then
        usbip attach -r "$HOST" -b "$b" >/dev/null 2>&1
      fi
    done
  fi
  sleep 0.5
done
