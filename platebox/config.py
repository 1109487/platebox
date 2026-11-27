"""Where things live and how the box is told to behave. Everything has a default that is right on the Pi."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _flag(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    data_dir: Path = Path("/var/lib/platebox")
    # The SFTP server is chrooted here. OpenSSH insists the chroot and every folder above it
    # belong to root and are not writable by anyone else, so it cannot live inside data_dir
    # (which the app owns). The camera writes into <sftp_root>/<sftp_remote_dir>.
    sftp_root: Path = Path("/srv/platebox-sftp")
    intake_dir: Path | None = None
    http_host: str = "0.0.0.0"
    http_port: int = 80
    sftp_port: int = 2222
    sftp_host: str = "0.0.0.0"
    # "system": a separate sshd does the receiving (Linux box). "builtin": the program does (Windows).
    sftp_mode: str = "system"
    sftp_user: str = "ftpcam"
    sftp_remote_dir: str = "upload"
    # Name the box answers to on the local network (mDNS), shown to the user.
    hostname: str = "platebox"
    # Development only: the address to tell the camera, when the box is inside a container
    # whose own address is not the one the camera can reach.
    advertise_ip: str = ""
    # No NetworkManager here (development, tests): canned Wi-Fi list, no hotspot.
    fake_net: bool = False
    # Tell the root helper to (re)set the SFTP account password. Empty in tests.
    sftp_password_helper: str = ""
    # Sessions end after this long.
    session_hours: int = 12
    # Reading and recording.
    min_confidence: float = 0.6
    dedupe_seconds: int = 30
    retention_days: int = 90
    detector: str = "yolo-v9-t-384-license-plate-end2end"
    ocr: str = "cct-s-v2-global-model"
    # Intake worker.
    intake_poll_s: float = 1.0
    intake_settle_s: float = 1.5
    intake_max_backlog: int = 200
    intake_max_bytes: int = 25 * 1024 * 1024
    # The camera asks "is this picture already there?" before sending it, and sends it again if not. So a picture
    # that has been dealt with is kept, as it was received, for this long (and at most this many MB, oldest first).
    delivered_keep_hours: float = 12.0
    delivered_max_mb: int = 150
    stale_minutes: int = 10          # a picture the camera stamped this much before its own clock now is "old": ignored
    # Answers the camera's name and time lookups itself (offline_helper.py), so it never waits for the internet.
    offline_helper: bool = True
    dns_port: int = 53
    ntp_port: int = 123
    # Motion log (platebox/motionlog.py): total size of the kept pictures, and the gap between two of them.
    motion_max_mb: int = 50
    motion_interval_s: int = 3
    # Live view add-on (needs ffmpeg).
    ffmpeg_path: str = ""
    live_fps: int = 8
    live_width: int = 960
    watch_fps: int = 3             # pictures a second looked at when watching the camera's stream directly
    live_subtype: int = 1          # the camera's second, smaller stream
    # Cookie needs the Secure flag only when served over HTTPS.
    cookie_secure: bool = False
    # Run the worker threads (off in tests that drive them by hand).
    background: bool = True
    setup_code_file: Path | None = None
    # Server mode: PlateBox runs on a server on the internet instead of at the site. The camera sends its pictures
    # there (for example through a 4G gateway), so there is no local network to manage, no camera to search for and
    # no way to pull the camera's video: the wizard is password -> camera settings -> done.
    server_mode: bool = False
    # The name or address the camera must send to (what goes in its "Server address" field), e.g. plates.example.com.
    public_host: str = ""

    def __post_init__(self) -> None:
        if self.server_mode:
            self.fake_net = True              # the server's network is not ours to manage
            self.offline_helper = False       # no DNS/NTP answering on the internet
            self.sftp_mode = "builtin"        # the program receives the pictures itself: no second sshd to keep safe
            if self.public_host:
                self.advertise_ip = self.public_host
        self.data_dir = Path(self.data_dir)
        self.sftp_root = Path(self.sftp_root)
        if self.intake_dir is None:
            self.intake_dir = self.sftp_root / self.sftp_remote_dir
        self.intake_dir = Path(self.intake_dir)
        if self.setup_code_file is None:
            self.setup_code_file = self.data_dir / "setup-code.txt"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "platebox.db"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @classmethod
    def from_env(cls) -> "Config":
        e = os.environ.get
        win = os.name == "nt"
        default_data = Path(e("PROGRAMDATA", "C:/ProgramData")) / "PlateBox" if win else Path("/var/lib/platebox")
        data = Path(e("PLATEBOX_DATA", str(default_data)))
        return cls(
            data_dir=data,
            sftp_root=Path(e("PLATEBOX_SFTP_ROOT", str(data / "sftp") if win else "/srv/platebox-sftp")),
            sftp_mode=e("PLATEBOX_SFTP", "builtin" if win else "system"),
            offline_helper=_flag("PLATEBOX_OFFLINE_HELPER", True),
            dns_port=int(e("PLATEBOX_DNS_PORT", "53")),
            ntp_port=int(e("PLATEBOX_NTP_PORT", "123")),
            intake_dir=Path(e("PLATEBOX_INTAKE")) if e("PLATEBOX_INTAKE") else None,
            http_host=e("PLATEBOX_HOST", "0.0.0.0"),
            http_port=int(e("PLATEBOX_PORT", "80")),
            sftp_port=int(e("PLATEBOX_SFTP_PORT", "2222")),
            hostname=e("PLATEBOX_HOSTNAME", "platebox"),
            fake_net=_flag("PLATEBOX_FAKE_NET"),
            advertise_ip=e("PLATEBOX_ADVERTISE_IP", ""),
            sftp_password_helper=e("PLATEBOX_SFTP_PASSWORD_HELPER", ""),
            cookie_secure=_flag("PLATEBOX_COOKIE_SECURE"),
            ffmpeg_path=e("PLATEBOX_FFMPEG", ""),
            detector=e("PB_DETECTOR", "yolo-v9-t-384-license-plate-end2end"),
            ocr=e("PB_OCR", "cct-s-v2-global-model"),
            server_mode=_flag("PLATEBOX_SERVER"),
            public_host=e("PLATEBOX_PUBLIC_HOST", "").strip(),
        )
