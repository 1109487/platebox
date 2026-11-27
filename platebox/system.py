"""The box itself: its SFTP account, its setup hotspot, and keeping it reachable."""
from __future__ import annotations

import logging
import os
import re
import secrets
import string
import subprocess
import threading
import time
from pathlib import Path

from .config import Config
from .db import Db
from .netmgr import FakeBackend, NmcliBackend, WindowsBackend, local_ip_towards

log = logging.getLogger("platebox.system")

_ALNUM = string.ascii_letters + string.digits


# No 0/O, 1/l/I: this is read off a screen and typed into a camera that shows only dots, and a lookalike
# letter is the commonest reason a camera's Test fails.
_CLEAR = "ABCDEFGHJKMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789"
CUSTOM_PASSWORD = re.compile(r"^[A-Za-z0-9]{8,32}$")


def random_password(n: int = 12) -> str:
    # Letters and digits only: this is typed by hand into a camera's web form, and a
    # symbol that the form or the SFTP client treats specially is a support call.
    return "".join(secrets.choice(_CLEAR) for _ in range(n))


class Sftp:
    """The one SFTP account the camera uses. Fixed for the life of the box: it does not
    change when the network does, so the camera never needs the password re-entered."""

    def __init__(self, cfg: Config, db: Db) -> None:
        self.cfg, self.db = cfg, db

    def password(self) -> str:
        pw = self.db.get("sftp_password")
        if not pw:
            pw = self._chosen_at_install() or random_password()
            self.db.put("sftp_password", pw)
            self.apply(pw)
        else:                                            # already set up: a leftover choice must not stay lying around
            try:
                (self.cfg.data_dir / "initial-camera-password.txt").unlink()
            except OSError:
                pass
        return pw

    def _chosen_at_install(self) -> str:
        """The installer lets the person choose the camera's password. It leaves it in a file that is read once, here,
        and then deleted: the password is kept in the database only, like any other."""
        f = self.cfg.data_dir / "initial-camera-password.txt"
        try:
            pw = f.read_text(encoding="utf-8-sig").strip()
        except OSError:
            return ""
        try:
            f.unlink()
        except OSError:
            log.warning("could not delete %s", f.name)
        if pw and not CUSTOM_PASSWORD.match(pw):
            log.warning("the password chosen at install was not 8 to 32 letters and digits: a made-up one is used")
            return ""
        return pw

    def apply(self, password: str) -> bool:
        """Set the OS account's password through the privileged helper. False if there isn't one."""
        helper = self.cfg.sftp_password_helper
        if not helper:
            return False
        try:
            r = subprocess.run(helper.split(), input=f"{self.cfg.sftp_user}:{password}\n",
                               capture_output=True, text=True, timeout=15)
            if r.returncode != 0:
                log.error("sftp password helper failed: %s", r.stderr.strip()[:200])
            return r.returncode == 0
        except (OSError, subprocess.SubprocessError) as e:
            log.error("sftp password helper could not run: %s", e)
            return False

    def reset(self, chosen: str = "") -> str:
        """A new camera password: the one given (letters and digits, 8 to 32), or a made-up one."""
        if chosen and not CUSTOM_PASSWORD.match(chosen):
            raise ValueError("Use 8 to 32 letters and digits only (no spaces or symbols).")
        pw = chosen or random_password()
        self.db.put("sftp_password", pw)
        self.apply(pw)
        return pw

    def camera_settings(self, server_ip: str) -> dict:
        return {
            "protocol": "SFTP",
            "server": server_ip,
            "port": self.cfg.sftp_port,
            "username": self.cfg.sftp_user,
            "password": self.password(),
            "directory": self.cfg.sftp_remote_dir,
        }


def hotspot_credentials(cfg: Config) -> tuple[str, str]:
    """(ssid, password) of the setup Wi-Fi the box raises when it has no network."""
    f = cfg.data_dir / "hotspot.txt"
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    if f.exists():
        parts = f.read_text().split()
        if len(parts) >= 2:
            return parts[0], parts[1]
    ssid = "PlateBox-" + "".join(secrets.choice("0123456789ABCDEF") for _ in range(4))
    pw = "".join(secrets.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(10))
    f.write_text(f"{ssid} {pw}\n")
    try:
        f.chmod(0o600)
    except OSError:
        pass
    return ssid, pw


def make_net(cfg: Config):
    if cfg.fake_net:
        return FakeBackend(cfg.advertise_ip or None)
    if os.name == "nt":
        return WindowsBackend()
    ssid, pw = hotspot_credentials(cfg)
    return NmcliBackend(ssid, pw)


def address_for_camera(net, camera_ip: str | None, override: str = "") -> str | None:
    """The box's address as the camera sees it: this is what goes in the camera's server field."""
    if override:
        return override
    if camera_ip:
        ip = local_ip_towards(camera_ip)
        if ip:
            return ip
    return net.status().ip


class NetWatch:
    """With no network at all, raise the setup hotspot so somebody can reach the box and give it one.
    When a real network appears, take the hotspot down again."""

    GRACE_S = 45

    def __init__(self, net, interval_s: float = 15.0) -> None:
        self.net, self.interval_s = net, interval_s
        self._since: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self, now: float | None = None) -> str:
        now = now if now is not None else time.time()
        st = self.net.status()
        if st.kind in ("wifi", "ethernet"):
            self._since = None
            return "online"
        if st.kind == "hotspot":
            self._since = None
            return "hotspot"
        if not st.has_wifi:
            return "no-wifi"
        if self._since is None:
            self._since = now
            return "waiting"
        if now - self._since >= self.GRACE_S:
            if self.net.start_hotspot():
                log.info("no network for %ds: raised the setup hotspot", self.GRACE_S)
            else:                                     # a Windows PC has no setup hotspot: it has its own network settings
                log.info("no network for %ds", self.GRACE_S)
            self._since = None
            return "hotspot-started"
        return "waiting"

    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._run, name="netwatch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("network watch failed")
            self._stop.wait(self.interval_s)
