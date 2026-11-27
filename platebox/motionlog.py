"""The motion log: a record, with pictures, of every time something moved in front of the camera.

The camera sends a picture about once a second while anything moves. Keeping every one at full size would
fill a small box in a day, so:

* each logged picture is a REDUCED copy (800 px wide, JPEG 70: roughly 40-60 KB);
* at most one picture per `interval` seconds per camera is logged (a picture with a plate in it is always
  logged: it is the one that matters);
* the total size of the logged pictures is capped (default 50 MB, set in Settings). When a new picture
  would go over the cap, the OLDEST pictures are deleted first, until it fits again.

The plates themselves are kept separately (sightings.py) with their own, full-size pictures. This log is
about movement, so it shows the plain things: when, a small picture, and a plate if one was read.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .db import Db

log = logging.getLogger("platebox.motionlog")

MB = 1024 * 1024
MIN_MB, MAX_MB = 5, 5000
THUMB_WIDTH, THUMB_QUALITY = 800, 70
# When over the cap, trim down to this share of it, so one new picture does not trigger a delete every time.
TRIM_TO = 0.9


class MotionLog:
    def __init__(self, cfg: Config, db: Db) -> None:
        self.cfg, self.db = cfg, db
        self._last_saved: dict[int | None, float] = {}

    # -- settings, read each time so a change in the UI applies at once ----------------------
    def enabled(self) -> bool:
        return self.db.get("motion_enabled", "1") == "1"

    def max_mb(self) -> int:
        return int(self.db.get("motion_max_mb", str(self.cfg.motion_max_mb)))

    def interval_s(self) -> int:
        return int(self.db.get("motion_interval_s", str(self.cfg.motion_interval_s)))

    def configure(self, enabled: bool, max_mb: int, interval_s: int) -> None:
        if not (MIN_MB <= max_mb <= MAX_MB):
            raise ValueError(f"The size limit must be between {MIN_MB} MB and {MAX_MB} MB.")
        if not (0 <= interval_s <= 600):
            raise ValueError("Seconds between pictures must be between 0 and 600.")
        self.db.put("motion_enabled", "1" if enabled else "0")
        self.db.put("motion_max_mb", str(max_mb))
        self.db.put("motion_interval_s", str(interval_s))
        self.enforce()                          # a lower limit takes effect now, not at the next picture

    # -- recording -------------------------------------------------------------------------
    def record(self, camera_id: int | None, img, cam_time: str | None, plates: list[str],
               now: datetime | None = None, monotonic: float | None = None) -> int | None:
        """Log one picture (a decoded BGR image). Returns the new row's id, or None if it was not logged."""
        if not self.enabled():
            return None
        import time

        if cam_time and self.db.one("SELECT 1 FROM motion_events WHERE camera_id IS ? AND cam_time = ?",
                                    (camera_id, cam_time)):
            return None                         # the camera sent this very picture again
        t = monotonic if monotonic is not None else time.monotonic()
        last = self._last_saved.get(camera_id)
        if not plates and last is not None and t - last < self.interval_s():
            return None                         # the same movement a moment ago
        now = now or datetime.now(timezone.utc)
        try:
            data, w, h = self._encode(img)
        except Exception:
            log.exception("could not make the motion picture")
            return None
        rel_dir = Path("motion") / now.strftime("%Y/%m/%d")
        (self.cfg.media_dir / rel_dir).mkdir(parents=True, exist_ok=True)
        rel = str(rel_dir / f"{uuid.uuid4().hex}.jpg")
        (self.cfg.media_dir / rel).write_bytes(data)
        rid = self.db.run(
            "INSERT INTO motion_events (camera_id, seen_at, cam_time, image, bytes, width, height, plates) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (camera_id, now.strftime("%Y-%m-%dT%H:%M:%SZ"), cam_time, rel, len(data), w, h, " ".join(plates)),
        )
        self._last_saved[camera_id] = t
        self.enforce()
        return rid

    @staticmethod
    def _encode(img) -> tuple[bytes, int, int]:
        import cv2

        h, w = img.shape[:2]
        if w > THUMB_WIDTH:
            img = cv2.resize(img, (THUMB_WIDTH, max(1, int(h * THUMB_WIDTH / w))), interpolation=cv2.INTER_AREA)
            h, w = img.shape[:2]
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, THUMB_QUALITY])
        if not ok:
            raise ValueError("encode failed")
        return buf.tobytes(), w, h

    # -- the size cap ---------------------------------------------------------------------------
    def used_bytes(self) -> int:
        return int(self.db.one("SELECT COALESCE(SUM(bytes), 0) AS n FROM motion_events")["n"])

    def enforce(self) -> int:
        """Delete the oldest pictures while the total is over the cap. Returns how many were removed."""
        cap = self.max_mb() * MB
        used = self.used_bytes()
        if used <= cap:
            return 0
        goal, removed = int(cap * TRIM_TO), 0
        while used > goal:
            batch = self.db.all("SELECT id, image, bytes FROM motion_events ORDER BY seen_at, id LIMIT 200")
            if not batch:
                break
            for r in batch:
                if used <= goal:
                    break
                self._remove_file(r["image"])
                self.db.run("DELETE FROM motion_events WHERE id = ?", (r["id"],))
                used -= r["bytes"]
                removed += 1
        if removed:
            log.info("motion log over its %d MB limit: removed %d oldest picture(s)", self.max_mb(), removed)
        return removed

    def _remove_file(self, rel: str | None) -> None:
        if rel:
            try:
                (self.cfg.media_dir / rel).unlink()
            except OSError:
                pass

    # -- reading ---------------------------------------------------------------------------
    def stats(self) -> dict:
        r = self.db.one("SELECT COUNT(*) AS n, MIN(seen_at) AS first, MAX(seen_at) AS last FROM motion_events")
        used, cap = self.used_bytes(), self.max_mb() * MB
        return {"count": r["n"], "first": r["first"], "last": r["last"], "used_bytes": used, "cap_bytes": cap,
                "used_mb": round(used / MB, 1), "cap_mb": self.max_mb(), "percent": min(100, round(100 * used / cap)),
                "enabled": self.enabled(), "interval_s": self.interval_s()}

    def list(self, limit: int = 60, offset: int = 0, plates_only: bool = False) -> list[dict]:
        where = "WHERE plates <> ''" if plates_only else ""
        rows = self.db.all(
            f"SELECT id, seen_at, cam_time, width, height, bytes, plates, image FROM motion_events {where} "
            "ORDER BY seen_at DESC, id DESC LIMIT ? OFFSET ?", (limit, offset))
        return [dict(r) | {"plates": r["plates"].split() if r["plates"] else [],
                           "token": Path(r["image"]).stem} for r in rows]       # the file name is unique to this picture

    def count(self, plates_only: bool = False) -> int:
        where = "WHERE plates <> ''" if plates_only else ""
        return self.db.one(f"SELECT COUNT(*) AS n FROM motion_events {where}")["n"]

    def file_for(self, event_id: int) -> Path | None:
        r = self.db.one("SELECT image FROM motion_events WHERE id = ?", (event_id,))
        if not r:
            return None
        p = (self.cfg.media_dir / r["image"]).resolve()
        return p if p.is_file() and self.cfg.media_dir.resolve() in p.parents else None

    def clear(self) -> int:
        rows = self.db.all("SELECT image FROM motion_events")
        for r in rows:
            self._remove_file(r["image"])
        self.db.run("DELETE FROM motion_events")
        self._last_saved.clear()
        return len(rows)
