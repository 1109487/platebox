"""Bus mode: who got on where, who got off where, and how far that was.

A camera at the door sends a picture whenever somebody moves in front of it. Each face in it is compared with the
people who are on board:

  * nobody like it on board            -> this person is boarding: a ride starts, at the bus's position at that moment
  * like somebody who boarded a moment ago -> still the same boarding (several pictures of one person): nothing new
  * like somebody who has been on board for a while -> this person is leaving: the ride ends here

The bus's position comes from its gateway, which reports it every few seconds (devices.py); the distance is measured
along those reports, and the fare is a starting amount plus an amount per kilometre.

The numbers that describe a face are kept only while the person is on board, to recognise them at the door on the way
out. They are deleted when the ride ends. The pictures are kept for the retention time set in Settings.
"""
from __future__ import annotations

import csv
import io
import logging
import struct
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config
from .db import Db
from .devices import Devices, distance_km
from .faces import Face, similarity

log = logging.getLogger("platebox.passengers")

DEFAULTS = {
    "face_threshold": "0.42",       # how alike two faces must be to be one person (the model's own line is 0.363)
    "board_window_s": "45",         # pictures of one person within this long are one boarding (or one leaving)
    "min_ride_s": "60",             # seen again sooner than this after boarding: still boarding, not leaving
    "max_ride_h": "4",              # nobody rides longer: after this a ride with no exit is closed as "exit not seen"
    "fare_base": "10",
    "fare_per_km": "2",
    "fare_currency": "Rs",
    "tz_minutes": "330",            # the camera's clock: minutes ahead of UTC (India: +5:30)
    "rides_retention_days": "30",
}


def pack(vec) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def _unit(vec: list[float]) -> list[float]:
    n = sum(v * v for v in vec) ** 0.5
    return [v / n for v in vec] if n > 0 else vec


class Passengers:
    def __init__(self, cfg: Config, db: Db, devices: Devices) -> None:
        self.cfg, self.db, self.devices = cfg, db, devices
        self.dir = cfg.media_dir / "rides"
        self._lock = threading.Lock()
        self._last_sweep = 0.0

    # -- settings ------------------------------------------------------------------------------
    def setting(self, key: str) -> float:
        try:
            return float(self.db.get(key, DEFAULTS[key]))
        except (TypeError, ValueError):
            return float(DEFAULTS[key])

    def settings(self) -> dict:
        d = {k: self.setting(k) for k in DEFAULTS if k != "fare_currency"}
        d["fare_currency"] = self.db.get("fare_currency", DEFAULTS["fare_currency"])
        return d

    def configure(self, **values) -> None:
        limits = {"face_threshold": (0.2, 0.9), "board_window_s": (5, 600), "min_ride_s": (10, 3600), "max_ride_h": (0.5, 24),
                  "fare_base": (0, 100000), "fare_per_km": (0, 100000), "tz_minutes": (-720, 840),
                  "rides_retention_days": (1, 3650)}
        for key, (lo, hi) in limits.items():
            if key in values and values[key] not in (None, ""):
                v = float(values[key])
                if not lo <= v <= hi:
                    raise ValueError(f"{key.replace('_', ' ')} must be between {lo} and {hi}.")
                self.db.put(key, repr(v) if v != int(v) else str(int(v)))
        cur = str(values.get("fare_currency") or "").strip()[:6]
        if cur:
            self.db.put("fare_currency", cur)

    def camera_role(self, serial: str) -> str:
        """'both' (one door for getting on and off), 'entry' or 'exit' (a bus with a door for each)."""
        role = self.db.get(f"cam_role:{serial}", "both")
        return role if role in ("both", "entry", "exit") else "both"

    def set_camera_role(self, serial: str, role: str) -> None:
        if role not in ("both", "entry", "exit"):
            raise ValueError("The role is one of: both, entry, exit.")
        self.db.put(f"cam_role:{serial}", role)

    # -- time ------------------------------------------------------------------------------------
    def picture_time(self, cam_time: str | None, received: float) -> float:
        """When the picture was taken, as epoch seconds. The camera's own clock (from the file name) is used, because a
        picture can arrive long after it was taken when the bus has no coverage. If that clock is plainly wrong (more
        than a day old, or in the future), the time the picture arrived is the best there is."""
        if cam_time:
            try:
                local = datetime.strptime(cam_time, "%Y-%m-%d %H:%M:%S")
                t = (local - timedelta(minutes=self.setting("tz_minutes"))).replace(tzinfo=timezone.utc).timestamp()
                if received - 86400 <= t <= received + 300:
                    return t
            except ValueError:
                pass
        return received

    # -- the heart of it ---------------------------------------------------------------------------
    def process(self, camera_id: int | None, serial: str, faces: list[Face], jpeg: bytes, cam_time: str | None,
                received: float | None = None) -> list[dict]:
        """Deal with the faces of one picture. Returns what happened to each: boarded / still / left / ignored."""
        received = received if received is not None else time.time()
        t = self.picture_time(cam_time, received)
        role = self.camera_role(serial)
        out = []
        with self._lock:
            for face in faces:
                out.append(self._one(camera_id, role, face, jpeg, t))
        if received - self._last_sweep > 300:
            self._last_sweep = received
            self.sweep(received)
        return out

    def _best(self, rows, face: Face):
        best, best_sim = None, -1.0
        for r in rows:
            s = similarity(unpack(r["embedding"]), face.embedding)
            if s > best_sim:
                best, best_sim = r, s
        return best, best_sim

    def _one(self, camera_id: int | None, role: str, face: Face, jpeg: bytes, t: float) -> dict:
        threshold, window = self.setting("face_threshold"), self.setting("board_window_s")
        onboard = self.db.all("SELECT * FROM rides WHERE state = 'onboard' AND embedding IS NOT NULL AND entry_at <= ?", (t + window,))
        ride, sim = self._best(onboard, face)
        if ride is not None and sim >= threshold:
            if role != "exit" and (t - ride["last_seen"] <= window or t - ride["entry_at"] < self.setting("min_ride_s")):
                self._refresh(ride, face, t)                    # still getting on: the same boarding
                return {"what": "still", "ride": ride["id"], "similarity": round(sim, 3)}
            if role == "entry":                                 # the entry door again, much later: a second look, not an exit
                self._refresh(ride, face, t)
                return {"what": "still", "ride": ride["id"], "similarity": round(sim, 3)}
            return self._leave(ride, face, jpeg, t, sim)

        # Somebody who left a moment ago and is still in the doorway is not a new passenger.
        just_left = self.db.all("SELECT * FROM rides WHERE state = 'done' AND embedding IS NOT NULL AND exit_at >= ?", (t - window,))
        gone, gone_sim = self._best(just_left, face)
        if gone is not None and gone_sim >= threshold:
            return {"what": "still", "ride": gone["id"], "similarity": round(gone_sim, 3)}

        if role == "exit":                                      # leaving, but never seen getting on
            self.db.put("exits_unmatched", str(int(self.db.get("exits_unmatched", "0") or 0) + 1))
            return {"what": "ignored", "why": "leaving, but not seen boarding", "similarity": round(max(sim, 0.0), 3)}
        return self._board(camera_id, face, jpeg, t)

    def _board(self, camera_id: int | None, face: Face, jpeg: bytes, t: float) -> dict:
        pos = self.devices.position_at(t)
        ride_id = self.db.run(
            "INSERT INTO rides (state, camera_id, entry_at, entry_lat, entry_lon, last_seen, embedding, shots, face_px) "
            "VALUES ('onboard', ?, ?, ?, ?, ?, ?, 1, ?)",
            (camera_id, t, pos["lat"] if pos else None, pos["lon"] if pos else None, t, pack(face.embedding), face.width_px))
        image, crop = self._save(ride_id, "entry", jpeg, face)
        self.db.run("UPDATE rides SET entry_image = ?, entry_crop = ? WHERE id = ?", (image, crop, ride_id))
        return {"what": "boarded", "ride": ride_id, "position": bool(pos)}

    def _refresh(self, ride, face: Face, t: float) -> None:
        """Another picture of the same boarding: the description of the face becomes the average of them, which is
        steadier than any single picture, and the clearest picture is the one kept."""
        n = ride["shots"]
        old = unpack(ride["embedding"])
        mean = _unit([(o * n + f) / (n + 1) for o, f in zip(old, face.embedding)])
        self.db.run("UPDATE rides SET embedding = ?, shots = ?, last_seen = ? WHERE id = ?",
                    (pack(mean), n + 1, max(t, ride["last_seen"]), ride["id"]))
        if face.width_px > (ride["face_px"] or 0) and ride["state"] == "onboard":
            _, crop = self._save(ride["id"], "entry", None, face)
            self.db.run("UPDATE rides SET entry_crop = ?, face_px = ? WHERE id = ?", (crop, face.width_px, ride["id"]))

    def _leave(self, ride, face: Face, jpeg: bytes, t: float, sim: float) -> dict:
        pos = self.devices.position_at(t)
        km = None
        if pos and ride["entry_lat"] is not None:
            km = self.devices.track_km(ride["entry_at"], t)
            if km is None:                                      # no reports in between: the straight line is the least it was
                km = round(distance_km(ride["entry_lat"], ride["entry_lon"], pos["lat"], pos["lon"]), 3)
        fare = self.fare(km)
        image, crop = self._save(ride["id"], "exit", jpeg, face)
        # The description of the face stays for one more boarding window (so the same person lingering in the doorway
        # is not taken for a new passenger); the sweep removes it after that.
        self.db.run(
            "UPDATE rides SET state = 'done', exit_at = ?, exit_lat = ?, exit_lon = ?, exit_image = ?, exit_crop = ?, "
            "distance_km = ?, fare = ?, similarity = ?, last_seen = ? WHERE id = ?",
            (t, pos["lat"] if pos else None, pos["lon"] if pos else None, image, crop, km, fare, round(sim, 3), t, ride["id"]))
        return {"what": "left", "ride": ride["id"], "similarity": round(sim, 3), "distance_km": km, "fare": fare}

    def fare(self, km: float | None) -> float | None:
        if km is None:
            return None
        return float(round(self.setting("fare_base") + self.setting("fare_per_km") * km))

    # -- pictures --------------------------------------------------------------------------------
    def _save(self, ride_id: int, which: str, jpeg: bytes | None, face: Face) -> tuple[str | None, str | None]:
        import cv2

        day = self.dir / time.strftime("%Y-%m-%d", time.gmtime())
        day.mkdir(parents=True, exist_ok=True)
        image = crop = None
        if jpeg is not None:
            p = day / f"{ride_id}-{which}.jpg"
            p.write_bytes(jpeg)
            image = str(p.relative_to(self.cfg.media_dir)).replace("\\", "/")
        if face.crop is not None:
            ok, buf = cv2.imencode(".jpg", face.crop, [cv2.IMWRITE_JPEG_QUALITY, 88])
            if ok:
                p = day / f"{ride_id}-{which}-face.jpg"
                p.write_bytes(buf.tobytes())
                crop = str(p.relative_to(self.cfg.media_dir)).replace("\\", "/")
        return image, crop

    def file_for(self, ride_id: int, kind: str) -> Path | None:
        column = {"entry": "entry_image", "entry-face": "entry_crop", "exit": "exit_image", "exit-face": "exit_crop"}.get(kind)
        if not column:
            return None
        r = self.db.one(f"SELECT {column} AS f FROM rides WHERE id = ?", (ride_id,))
        if not r or not r["f"]:
            return None
        p = (self.cfg.media_dir / r["f"]).resolve()
        return p if p.is_file() and self.cfg.media_dir.resolve() in p.parents else None

    def _remove_files(self, row) -> None:
        for col in ("entry_image", "entry_crop", "exit_image", "exit_crop"):
            if row[col]:
                try:
                    (self.cfg.media_dir / row[col]).unlink()
                except OSError:
                    pass

    # -- housekeeping ----------------------------------------------------------------------------
    def sweep(self, now: float | None = None) -> dict:
        now = now if now is not None else time.time()
        window = self.setting("board_window_s")
        # Rides that ended: the face's description has done its job.
        self.db.run("UPDATE rides SET embedding = NULL WHERE state != 'onboard' AND embedding IS NOT NULL AND last_seen < ?",
                    (now - window,))
        # Nobody rides this long: the exit was not seen (looked away, door crowded, got off by another door).
        limit = now - self.setting("max_ride_h") * 3600
        for r in self.db.all("SELECT id FROM rides WHERE state = 'onboard' AND entry_at < ?", (limit,)):
            self.db.run("UPDATE rides SET state = 'unseen', embedding = NULL WHERE id = ?", (r["id"],))
        old = now - self.setting("rides_retention_days") * 86400
        removed = 0
        for r in self.db.all("SELECT * FROM rides WHERE entry_at < ?", (old,)):
            self._remove_files(r)
            self.db.run("DELETE FROM rides WHERE id = ?", (r["id"],))
            removed += 1
        self.devices.prune(now)
        return {"removed": removed}

    def close_all(self, now: float | None = None) -> int:
        """End of the route: everybody still on board gets off here."""
        now = now if now is not None else time.time()
        pos = self.devices.position_at(now, within_s=600)
        n = 0
        for r in self.db.all("SELECT * FROM rides WHERE state = 'onboard'"):
            km = None
            if pos and r["entry_lat"] is not None:
                km = self.devices.track_km(r["entry_at"], now)
                if km is None:
                    km = round(distance_km(r["entry_lat"], r["entry_lon"], pos["lat"], pos["lon"]), 3)
            self.db.run("UPDATE rides SET state = 'done', exit_at = ?, exit_lat = ?, exit_lon = ?, distance_km = ?, fare = ?, "
                        "embedding = NULL, last_seen = ?, note = 'closed at the end of the route' WHERE id = ?",
                        (now, pos["lat"] if pos else None, pos["lon"] if pos else None, km, self.fare(km), now, r["id"]))
            n += 1
        return n

    def delete(self, ride_id: int) -> None:
        r = self.db.one("SELECT * FROM rides WHERE id = ?", (ride_id,))
        if r:
            self._remove_files(r)
            self.db.run("DELETE FROM rides WHERE id = ?", (ride_id,))

    def delete_all(self) -> int:
        rows = self.db.all("SELECT * FROM rides")
        for r in rows:
            self._remove_files(r)
        self.db.run("DELETE FROM rides")
        return len(rows)

    # -- reading it back -------------------------------------------------------------------------
    def list(self, limit: int = 100, offset: int = 0, state: str = "") -> list[dict]:
        where, args = ("WHERE state = ?", (state,)) if state in ("onboard", "done", "unseen") else ("", ())
        rows = self.db.all(f"SELECT * FROM rides {where} ORDER BY entry_at DESC, id DESC LIMIT ? OFFSET ?", (*args, limit, offset))
        out = []
        for r in rows:
            d = {k: r[k] for k in r.keys() if k != "embedding"}
            d["entry_iso"] = _iso(r["entry_at"])
            d["exit_iso"] = _iso(r["exit_at"])
            d["minutes"] = round((r["exit_at"] - r["entry_at"]) / 60, 1) if r["exit_at"] else None
            out.append(d)
        return out

    def summary(self, now: float | None = None) -> dict:
        now = now if now is not None else time.time()
        day = now - 86400
        one = lambda sql, *a: self.db.one(sql, a)[0] or 0      # noqa: E731
        return {
            "onboard": one("SELECT COUNT(*) FROM rides WHERE state = 'onboard'"),
            "boarded_24h": one("SELECT COUNT(*) FROM rides WHERE entry_at >= ?", day),
            "finished_24h": one("SELECT COUNT(*) FROM rides WHERE state = 'done' AND exit_at >= ?", day),
            "unseen_24h": one("SELECT COUNT(*) FROM rides WHERE state = 'unseen' AND entry_at >= ?", day),
            "km_24h": round(one("SELECT SUM(distance_km) FROM rides WHERE state = 'done' AND exit_at >= ?", day), 1),
            "fare_24h": round(one("SELECT SUM(fare) FROM rides WHERE state = 'done' AND exit_at >= ?", day)),
            "no_position_24h": one("SELECT COUNT(*) FROM rides WHERE entry_at >= ? AND entry_lat IS NULL", day),
            "exits_unmatched": int(self.db.get("exits_unmatched", "0") or 0),
            "total": one("SELECT COUNT(*) FROM rides"),
        }

    def export_csv(self) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["ride", "state", "boarded_utc", "boarded_lat", "boarded_lon", "left_utc", "left_lat", "left_lon",
                    "minutes", "distance_km", "fare", "match", "note"])
        for r in self.list(limit=100000):
            w.writerow([r["id"], r["state"], r["entry_iso"], r["entry_lat"], r["entry_lon"], r["exit_iso"] or "", r["exit_lat"],
                        r["exit_lon"], r["minutes"], r["distance_km"], r["fare"], r["similarity"], r["note"] or ""])
        return buf.getvalue()


def _iso(t: float | None) -> str | None:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if t else None
