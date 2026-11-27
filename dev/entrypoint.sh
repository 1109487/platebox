#!/bin/sh
# Development container: the same pieces the box runs (a dedicated sshd for the camera, and the app),
# started by hand because there is no systemd in a container. The app runs as root here.
set -eu
sh /opt/platebox/deploy/prepare-sftp.sh root
mkdir -p /etc/platebox
cp /opt/platebox/deploy/sshd_config /etc/platebox/sshd_config
# PLATEBOX_SFTP=builtin: the program receives the camera's pictures itself (the Windows way); no sshd.
if [ "${PLATEBOX_SFTP:-system}" != "builtin" ]; then
  /usr/sbin/sshd -D -e -f /etc/platebox/sshd_config &
fi
exec python -m platebox
