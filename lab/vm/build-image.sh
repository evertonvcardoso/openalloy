#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
# Builds the lab guest: Debian testing ("forky": Python 3.14, systemd 262, kernel 7.x,
# the base of SparkyLinux rolling) as a qcow2 base image for QEMU.
#
#   sudo lab/vm/build-image.sh [WORKDIR]        (default: ./lab-work)
#
# Needs: debootstrap, debian-archive-keyring, qemu-utils, e2fsprogs (mkfs.ext4 -d).
# Optional: LAB_PROXY=http://host:port and LAB_CA=/path/ca.pem when the guest must
# reach PyPI through a proxy (the guest sees the host as 10.0.2.2).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="$(realpath "${1:-./lab-work}")"
R="$WORK/rootfs-forky"
mkdir -p "$WORK"

if [ ! -x "$R/usr/bin/python3" ]; then
  BASE=systemd,systemd-sysv,udev,dbus,dbus-user-session,libpam-systemd,sudo,python3,kmod,iproute2,ca-certificates,procps,apt
  debootstrap --variant=minbase --include="$BASE" --keyring=/usr/share/keyrings/debian-archive-keyring.gpg \
    forky "$R" http://deb.debian.org/debian /usr/share/debootstrap/scripts/sid
fi

mount --bind /proc "$R/proc"; mount --bind /sys "$R/sys"; mount --bind /dev "$R/dev"
trap 'umount "$R/proc" "$R/sys" "$R/dev" 2>/dev/null || true' EXIT
cp /etc/resolv.conf "$R/etc/resolv.conf"
echo "deb http://deb.debian.org/debian forky main" > "$R/etc/apt/sources.list"
in_root() { chroot "$R" env DEBIAN_FRONTEND=noninteractive "$@"; }
in_root apt-get update -q
in_root apt-get install -y -q --no-install-recommends \
  initramfs-tools linux-image-amd64 systemd-resolved python3-venv pipx python3-gi gir1.2-gtk-4.0 \
  xvfb xauth x11-apps usbip psmisc less file fonts-dejavu-core adwaita-icon-theme python3-pip acl

# --- identity, disk, network --------------------------------------------------------
echo lab-forky > "$R/etc/hostname"
printf '127.0.0.1 localhost\n127.0.1.1 lab-forky\n' > "$R/etc/hosts"
echo '/dev/vda / ext4 defaults,errors=remount-ro 0 1' > "$R/etc/fstab"
mkdir -p "$R/etc/systemd/network"
printf '[Match]\nName=en*\n\n[Network]\nDHCP=yes\n' > "$R/etc/systemd/network/20-wired.network"
in_root systemctl enable systemd-networkd systemd-resolved
echo vhci-hcd > "$R/etc/modules-load.d/lab.conf"

# --- the test user: a desktop-like local login on seat0 (uaccess ACLs apply) ---------
in_root id tester >/dev/null 2>&1 || in_root useradd -m -s /bin/bash -G sudo tester
echo 'tester ALL=(ALL) NOPASSWD:ALL' > "$R/etc/sudoers.d/lab"; chmod 440 "$R/etc/sudoers.d/lab"
mkdir -p "$R/etc/systemd/system/getty@tty1.service.d" "$R/var/lib/systemd/linger"
# shellcheck disable=SC2016  # $TERM and %I are for the unit file, not this shell
printf '[Service]\nExecStart=\nExecStart=-/sbin/agetty --autologin tester --noclear %%I $TERM\n' \
  > "$R/etc/systemd/system/getty@tty1.service.d/autologin.conf"
touch "$R/var/lib/systemd/linger/tester"

# --- lab services: agent (virtio-serial) and USB/IP attach loop ---------------------
install -D -m 0755 "$HERE/guest/lab-agent.py" "$R/usr/local/lib/lab/lab-agent.py"
install -D -m 0755 "$HERE/guest/lab-attach.sh" "$R/usr/local/lib/lab/lab-attach.sh"
install -D -m 0755 "$HERE/guest/zz-lab-usbip" "$R/usr/lib/systemd/system-sleep/zz-lab-usbip"
cat > "$R/etc/systemd/system/lab-agent.service" <<'EOF'
[Unit]
Description=OpenAlloy lab agent (virtio-serial)
[Service]
ExecStart=/usr/local/lib/lab/lab-agent.py
Restart=always
[Install]
WantedBy=multi-user.target
EOF
cat > "$R/etc/systemd/system/lab-attach.service" <<'EOF'
[Unit]
Description=OpenAlloy lab: keep the emulated keyboards attached (USB/IP)
After=network-online.target
[Service]
ExecStart=/usr/local/lib/lab/lab-attach.sh
Restart=always
[Install]
WantedBy=multi-user.target
EOF
mkdir -p "$R/etc/lab"; : > "$R/etc/lab/attach"
in_root systemctl enable lab-agent lab-attach
in_root systemctl mask apt-daily.timer apt-daily-upgrade.timer e2scrub_all.timer fstrim.timer 2>/dev/null || true

# --- optional proxy for pip/pipx inside the guest -----------------------------------
if [ -n "${LAB_PROXY:-}" ]; then
  # HTTPS only (pip/pipx); apt uses plain HTTP and goes direct
  sed -i '/_proxy=/Id' "$R/etc/environment"
  printf 'https_proxy=%s\nHTTPS_PROXY=%s\nno_proxy=localhost,127.0.0.1,10.0.2.2\n' \
    "$LAB_PROXY" "$LAB_PROXY" >> "$R/etc/environment"
fi
if [ -n "${LAB_CA:-}" ]; then
  install -m 0644 "$LAB_CA" "$R/usr/local/share/ca-certificates/lab-proxy.crt"
  in_root update-ca-certificates >/dev/null
  printf '[global]\ncert = /etc/ssl/certs/ca-certificates.crt\n' > "$R/etc/pip.conf"
  sed -i '/^REQUESTS_CA_BUNDLE=/d; /^SSL_CERT_FILE=/d' "$R/etc/environment"
  echo 'REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt' >> "$R/etc/environment"
  echo 'SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt' >> "$R/etc/environment"
fi

umount "$R/proc" "$R/sys" "$R/dev"; trap - EXIT

# --- disk image -----------------------------------------------------------------------
cp "$R"/boot/vmlinuz-* "$WORK/vmlinuz"; cp "$R"/boot/initrd.img-* "$WORK/initrd.img"
rm -f "$WORK/base.raw" "$WORK/base.qcow2"
truncate -s 8G "$WORK/base.raw"
mkfs.ext4 -q -F -L labroot -d "$R" "$WORK/base.raw"
qemu-img convert -f raw -O qcow2 "$WORK/base.raw" "$WORK/base.qcow2"
rm -f "$WORK/base.raw"
echo "base image: $WORK/base.qcow2"
