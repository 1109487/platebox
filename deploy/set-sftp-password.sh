#!/bin/sh
# Reads "ftpcam:<password>" on stdin and sets that account's password. The ONLY thing the
# web application may do as root (see /etc/sudoers.d/platebox). It refuses any other user.
set -eu
IFS= read -r line
user=${line%%:*}
pass=${line#*:}
[ "$user" = "ftpcam" ] || { echo "refusing user $user" >&2; exit 2; }
[ -n "$pass" ] || { echo "empty password" >&2; exit 2; }
printf '%s:%s\n' "$user" "$pass" | chpasswd
