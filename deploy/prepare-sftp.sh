#!/bin/sh
# Creates the SFTP account, its folders and its host keys. Safe to run again.
# Usage: prepare-sftp.sh <app-user>      (the user the web application runs as)
set -eu
APP_USER=${1:-platebox}
ROOT=/srv/platebox-sftp

getent group "$APP_USER" >/dev/null || groupadd --system "$APP_USER"
id ftpcam >/dev/null 2>&1 || useradd --system --gid "$APP_USER" --home-dir "$ROOT" --no-create-home --shell /usr/sbin/nologin ftpcam
usermod -g "$APP_USER" ftpcam

# The chroot and everything above it: root-owned, nobody else may write (OpenSSH checks).
mkdir -p "$ROOT/upload"
chown root:root /srv "$ROOT"
chmod 0755 "$ROOT"
# Where the camera writes. Group-writable and setgid, so the app can delete what it ingests.
chown ftpcam:"$APP_USER" "$ROOT/upload"
chmod 2770 "$ROOT/upload"

mkdir -p /etc/platebox/ssh
[ -f /etc/platebox/ssh/ssh_host_ed25519_key ] || ssh-keygen -q -t ed25519 -N '' -f /etc/platebox/ssh/ssh_host_ed25519_key
[ -f /etc/platebox/ssh/ssh_host_rsa_key ]     || ssh-keygen -q -t rsa -b 3072 -N '' -f /etc/platebox/ssh/ssh_host_rsa_key
[ -f /etc/platebox/ssh/ssh_host_ecdsa_key ]   || ssh-keygen -q -t ecdsa -b 256 -N '' -f /etc/platebox/ssh/ssh_host_ecdsa_key
mkdir -p /run/sshd
