#!/bin/sh
# Installs PlateBox on Raspberry Pi OS (Bookworm, 64-bit) or Debian 12 / Ubuntu 22.04+.
#
#     sudo sh install.sh
#
# Needs internet once, to fetch packages and the plate-reading models. Afterwards the box
# works with no internet at all. Safe to run again (it upgrades in place; data is kept).
#
# PB_NO_SYSTEMD=1 skips starting services (used to test this script inside a container).
set -eu

[ "$(id -u)" = 0 ] || { echo "Run this as root:  sudo sh install.sh" >&2; exit 1; }
SRC=$(cd "$(dirname "$0")/.." && pwd)
PREFIX=/opt/platebox
DATA=/var/lib/platebox
DETECTOR=${PB_DETECTOR:-yolo-v9-t-384-license-plate-end2end}
OCR=${PB_OCR:-cct-s-v2-global-model}

say() { printf '\n== %s\n' "$*"; }

say "Packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends python3 python3-venv python3-pip openssh-server \
    network-manager avahi-daemon libglib2.0-0 libgl1 sudo ca-certificates iproute2 ffmpeg
if ! command -v nmcli >/dev/null 2>&1; then
  echo "NetworkManager (nmcli) is required for the Wi-Fi setup screen." >&2; exit 1
fi

say "Service account and folders"
id platebox >/dev/null 2>&1 || useradd --system --create-home --home-dir "$DATA" --shell /usr/sbin/nologin platebox
getent group netdev >/dev/null && usermod -aG netdev platebox   # may change Wi-Fi without root
install -d -o platebox -g platebox -m 0750 "$DATA"

say "Application"
install -d "$PREFIX"
rm -rf "$PREFIX/platebox" "$PREFIX/deploy"
cp -r "$SRC/platebox" "$SRC/deploy" "$SRC/requirements.txt" "$PREFIX/"
find "$PREFIX" -name '__pycache__' -type d -prune -exec rm -rf {} +
[ -d "$PREFIX/venv" ] || python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --no-cache-dir --upgrade pip
"$PREFIX/venv/bin/pip" install --no-cache-dir -r "$PREFIX/requirements.txt"
chown -R root:root "$PREFIX"

say "Plate-reading models (downloaded once)"
su -s /bin/sh platebox -c "HOME=$DATA PB_D='$DETECTOR' PB_O='$OCR' $PREFIX/venv/bin/python -c \"
import os
from fast_alpr import ALPR
ALPR(detector_model=os.environ['PB_D'], ocr_model=os.environ['PB_O'])
print('models ready')\""

say "SFTP receiver for the camera"
sh "$PREFIX/deploy/prepare-sftp.sh" platebox
install -m 0644 "$PREFIX/deploy/sshd_config" /etc/platebox/sshd_config
install -m 0755 "$PREFIX/deploy/set-sftp-password.sh" /usr/local/sbin/platebox-set-sftp-password
install -m 0440 "$PREFIX/deploy/sudoers-platebox" /etc/sudoers.d/platebox
visudo -cf /etc/sudoers.d/platebox >/dev/null

say "Name on the network"
if command -v hostnamectl >/dev/null 2>&1 && [ -z "${PB_NO_SYSTEMD:-}" ]; then
  hostnamectl set-hostname platebox || true
fi

say "Setup code and setup Wi-Fi (for the label on the box)"
CODE_FILE="$DATA/setup-code.txt"
if [ ! -s "$CODE_FILE" ]; then
  python3 -c "import secrets; print(''.join(secrets.choice('0123456789') for _ in range(6)))" > "$CODE_FILE"
fi
chown platebox:platebox "$CODE_FILE"; chmod 0600 "$CODE_FILE"
su -s /bin/sh platebox -c "PLATEBOX_DATA=$DATA PYTHONPATH=$PREFIX $PREFIX/venv/bin/python -c \"
from platebox.config import Config
from platebox.system import hotspot_credentials
hotspot_credentials(Config.from_env())\""
BOOT=/boot/firmware; [ -d "$BOOT" ] || BOOT=/boot
if [ -d "$BOOT" ]; then
  {
    echo "PlateBox - keep this with the box"
    echo "Setup code:      $(cat "$CODE_FILE")"
    echo "Setup Wi-Fi:     $(cut -d' ' -f1 "$DATA/hotspot.txt")   password: $(cut -d' ' -f2 "$DATA/hotspot.txt")"
    echo "Then open:       http://platebox.local   (or http://10.42.0.1 on the setup Wi-Fi)"
  } > "$BOOT/platebox-label.txt"
fi

if [ -z "${PB_NO_SYSTEMD:-}" ]; then
  say "Starting"
  install -m 0644 "$PREFIX/deploy/platebox.service" /etc/systemd/system/platebox.service
  install -m 0644 "$PREFIX/deploy/platebox-sftp.service" /etc/systemd/system/platebox-sftp.service
  systemctl daemon-reload
  systemctl enable --now avahi-daemon NetworkManager
  systemctl enable --now platebox-sftp.service platebox.service
fi

say "Done"
echo "Setup code: $(cat "$CODE_FILE")"
echo "Open http://platebox.local  (or the box's address) from a phone or laptop on the same network."
echo "No network? The box raises its own Wi-Fi: $(cut -d' ' -f1 "$DATA/hotspot.txt")"
