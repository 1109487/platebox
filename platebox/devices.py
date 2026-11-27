"""Gateways: the 4G boxes that sit between a camera and this server (firmware/gateway).

A gateway signs in with its own token, says how it is (firmware version, signal, position), is told whether a newer
firmware is waiting, and downloads it from here. In bus mode it also reports where it is every few seconds, which is
how a picture gets a place: by the time it was taken.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import secrets
import struct
import time
from pathlib import Path

from .config import Config
from .db import Db, now_iso

log = logging.getLogger("platebox.devices")

MAX_FIRMWARE_BYTES = 0x1C0000          # one OTA slot of the gateway's 4 MB flash (partitions.csv)
APP_DESC_OFFSET = 32                   # image header (24) + first segment header (8)
APP_DESC_MAGIC = 0xABCD5432
SAFE = re.compile(r"^[A-Za-z0-9._+-]{1,31}$")
POSITIONS_KEEP_DAYS = 35


class FirmwareError(ValueError):
    pass


def read_app_description(image: bytes) -> tuple[str, str]:
    """(project name, version) from an ESP-IDF application image, as the build wrote them into it."""
    if len(image) < APP_DESC_OFFSET + 256 or image[0] != 0xE9:
        raise FirmwareError("That is not an ESP32 firmware file (it should be the project's .bin from the build folder).")
    magic, = struct.unpack_from("<I", image, APP_DESC_OFFSET)
    if magic != APP_DESC_MAGIC:
        raise FirmwareError("That file is not an application image (the bootloader or partition table was chosen by mistake?).")

    def text(offset: int, size: int) -> str:
        return image[offset:offset + size].split(b"\0", 1)[0].decode("ascii", "replace").strip()

    version, project = text(APP_DESC_OFFSET + 16, 32), text(APP_DESC_OFFSET + 48, 32)
    if not SAFE.match(version) or not SAFE.match(project):
        raise FirmwareError("The firmware's name or version has characters that are not allowed.")
    return project, version


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two positions."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371.0088 * 2 * math.asin(min(1.0, math.sqrt(a)))


def valid_position(lat, lon) -> bool:
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    return -90 <= lat <= 90 and -180 <= lon <= 180 and not (lat == 0 and lon == 0)


class Devices:
    def __init__(self, cfg: Config, db: Db) -> None:
        self.cfg, self.db = cfg, db
        self.dir = cfg.data_dir / "firmware"
        self.dir.mkdir(parents=True, exist_ok=True)

    # -- gateways and their tokens -------------------------------------------------------------
    @staticmethod
    def _hash(token: str) -> str:
        # The token is 32 random bytes: nothing to guess, so a plain hash is enough to keep it out of the database.
        return hashlib.sha256(token.encode()).hexdigest()

    def create(self, name: str) -> tuple[int, str]:
        name = " ".join(name.split())[:60] or "Gateway"
        token = secrets.token_urlsafe(32)
        dev_id = self.db.run("INSERT INTO devices (name, token_hash, created) VALUES (?, ?, ?)",
                             (name, self._hash(token), now_iso()))
        self.db.event("gateway_added", name)
        return dev_id, token

    def new_token(self, dev_id: int) -> str | None:
        if not self.db.one("SELECT id FROM devices WHERE id = ?", (dev_id,)):
            return None
        token = secrets.token_urlsafe(32)
        self.db.run("UPDATE devices SET token_hash = ? WHERE id = ?", (self._hash(token), dev_id))
        return token

    def remove(self, dev_id: int) -> None:
        self.db.run("DELETE FROM positions WHERE device_id = ?", (dev_id,))
        self.db.run("DELETE FROM devices WHERE id = ?", (dev_id,))

    def auth(self, token: str):
        if not token or len(token) > 200:
            return None
        return self.db.one("SELECT * FROM devices WHERE token_hash = ?", (self._hash(token),))

    def list(self) -> list[dict]:
        out = []
        for r in self.db.all("SELECT * FROM devices ORDER BY id"):
            d = dict(r)
            try:
                d["info"] = json.loads(r["info"] or "{}")
            except ValueError:
                d["info"] = {}
            d["waiting"] = self.update_for(r["model"], r["version"])
            out.append(d)
        return out

    def checkin(self, dev, info: dict, ip: str) -> dict:
        model = str(info.get("model", ""))[:31]
        version = str(info.get("version", ""))[:31]
        keep = {k: info[k] for k in ("uptime_s", "signal_dbm", "carrier", "data_up", "link", "lan_up", "sats", "fix",
                                     "lat", "lon", "reset_reason", "ota_state", "free_heap") if k in info}
        self.db.run("UPDATE devices SET model = ?, version = ?, last_seen = ?, last_ip = ?, info = ? WHERE id = ?",
                    (model, version, now_iso(), ip[:45], json.dumps(keep)[:2000], dev["id"]))
        if dev["version"] and version and dev["version"] != version:
            self.db.event("gateway_firmware", f"{dev['name']}: {dev['version']} -> {version}")
        return {"ok": True, "time": int(time.time()), "mode": self.mode(), "firmware": self.update_for(model, version)}

    def mode(self) -> str:
        """What this installation records: 'parking' (number plates) or 'bus' (passengers and where they rode)."""
        return "bus" if self.db.get("mode", "parking") == "bus" else "parking"

    # -- firmware ----------------------------------------------------------------------------
    def add_firmware(self, image: bytes, note: str = "") -> dict:
        if len(image) > MAX_FIRMWARE_BYTES:
            raise FirmwareError(f"That file is too big for the gateway ({len(image)} bytes; the limit is {MAX_FIRMWARE_BYTES}).")
        model, version = read_app_description(image)
        if self.db.one("SELECT id FROM firmware WHERE model = ? AND version = ?", (model, version)):
            raise FirmwareError(f"Version {version} of {model} is already here. Raise the version number and build again.")
        sha = hashlib.sha256(image).hexdigest()
        name = f"{model}-{version}-{sha[:12]}.bin"
        (self.dir / name).write_bytes(image)
        fw_id = self.db.run("INSERT INTO firmware (model, version, file, size, sha256, uploaded, note) VALUES (?,?,?,?,?,?,?)",
                            (model, version, name, len(image), sha, now_iso(), " ".join(note.split())[:200]))
        self.db.event("firmware_added", f"{model} {version}")
        return {"id": fw_id, "model": model, "version": version, "size": len(image), "sha256": sha}

    def firmware_list(self) -> list[dict]:
        return [dict(r) for r in self.db.all("SELECT * FROM firmware ORDER BY id DESC")]

    def firmware_file(self, fw_id: int) -> Path | None:
        r = self.db.one("SELECT file FROM firmware WHERE id = ?", (fw_id,))
        if not r:
            return None
        p = self.dir / r["file"]
        return p if p.is_file() else None

    def remove_firmware(self, fw_id: int) -> None:
        p = self.firmware_file(fw_id)
        self.db.run("DELETE FROM firmware WHERE id = ?", (fw_id,))
        if p:
            try:
                p.unlink()
            except OSError:
                pass

    def update_for(self, model: str | None, version: str | None) -> dict | None:
        """The newest firmware uploaded for this kind of gateway, if it is not the one it runs. The newest upload is
        the one wanted: to go back to an older version, build it under a new version number."""
        if not model:
            return None
        r = self.db.one("SELECT * FROM firmware WHERE model = ? ORDER BY id DESC LIMIT 1", (model,))
        if not r or r["version"] == (version or "") or not (self.dir / r["file"]).is_file():
            return None
        return {"id": r["id"], "version": r["version"], "size": r["size"], "sha256": r["sha256"],
                "path": f"/device/v1/firmware/{r['id']}"}

    # -- positions (bus mode) ------------------------------------------------------------------
    def add_positions(self, dev_id: int, points: list) -> int:
        """Points are {t (epoch seconds, from the satellites), lat, lon, speed_kmh?, sats?, hdop?}. Bad ones are skipped."""
        now, n = time.time(), 0
        for p in points[:500]:
            if not isinstance(p, dict) or not valid_position(p.get("lat"), p.get("lon")):
                continue
            try:
                t = float(p.get("t") or 0)
            except (TypeError, ValueError):
                continue
            if not (now - 7 * 86400 <= t <= now + 300):      # no clock, or a wrong one: the server's time is the best there is
                t = now
            self.db.run("INSERT OR IGNORE INTO positions (device_id, t, lat, lon, speed_kmh, sats, hdop) VALUES (?,?,?,?,?,?,?)",
                        (dev_id, round(t, 1), float(p["lat"]), float(p["lon"]), _num(p.get("speed_kmh")), _num(p.get("sats")),
                         _num(p.get("hdop"))))
            n += 1
        return n

    def position_at(self, t: float, within_s: float = 120.0, dev_id: int | None = None) -> dict | None:
        """Where the gateway was at time t: between the two reports around it, or the nearest one if only one is close."""
        dev = "AND device_id = ?" if dev_id else ""
        args = (dev_id,) if dev_id else ()
        before = self.db.one(f"SELECT * FROM positions WHERE t <= ? {dev} ORDER BY t DESC LIMIT 1", (t, *args))
        after = self.db.one(f"SELECT * FROM positions WHERE t > ? {dev} ORDER BY t LIMIT 1", (t, *args))
        b = before if before and t - before["t"] <= within_s else None
        a = after if after and after["t"] - t <= within_s else None
        if b and a and a["t"] > b["t"]:
            f = (t - b["t"]) / (a["t"] - b["t"])
            return {"lat": b["lat"] + (a["lat"] - b["lat"]) * f, "lon": b["lon"] + (a["lon"] - b["lon"]) * f,
                    "t": t, "age_s": round(min(t - b["t"], a["t"] - t), 1)}
        one = b or a
        return {"lat": one["lat"], "lon": one["lon"], "t": one["t"], "age_s": round(abs(t - one["t"]), 1)} if one else None

    def track_km(self, t0: float, t1: float, dev_id: int | None = None) -> float | None:
        """How far the gateway travelled between two times: along its reported positions, not in a straight line.
        A position jumping about while the bus stands still must not add distance, so steps under 15 m are skipped."""
        if t1 <= t0:
            return 0.0
        start, end = self.position_at(t0, dev_id=dev_id), self.position_at(t1, dev_id=dev_id)
        if not start or not end:
            return None
        dev = "AND device_id = ?" if dev_id else ""
        rows = self.db.all(f"SELECT lat, lon FROM positions WHERE t > ? AND t < ? {dev} ORDER BY t",
                           (t0, t1, *((dev_id,) if dev_id else ())))
        pts = [(start["lat"], start["lon"])] + [(r["lat"], r["lon"]) for r in rows] + [(end["lat"], end["lon"])]
        total, last = 0.0, pts[0]
        for p in pts[1:]:
            step = distance_km(last[0], last[1], p[0], p[1])
            if step >= 0.015:
                total += step
                last = p
        return round(total, 3)

    def latest_position(self) -> dict | None:
        r = self.db.one("SELECT * FROM positions ORDER BY t DESC LIMIT 1")
        return dict(r) if r else None

    def prune(self, now: float | None = None) -> None:
        self.db.run("DELETE FROM positions WHERE t < ?", ((now or time.time()) - POSITIONS_KEEP_DAYS * 86400,))


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
