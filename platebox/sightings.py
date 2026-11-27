"""Turning plate reads into sightings: one row per vehicle pass, with all the times it came by.

A vehicle in front of the camera produces several pictures a second apart, and the reader
may guess a slightly different number on each. They are the same pass, so they are merged
and the BEST reading of the pass is kept (a valid Indian format beats an invalid one, then
the higher confidence). A different pass, later, is a new sighting of the same plate: that
is what "seen again at ..." is made of.
"""
from __future__ import annotations

import csv
import io
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config
from .db import Db, now_iso
from .reader import PlateRead, looks_valid


def edit_distance(a: str, b: str) -> int:
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def same_vehicle(a: str, b: str) -> bool:
    """Are these two readings plausibly the same plate?

    Two DIFFERENT valid registrations are two vehicles however alike they look (a fleet
    often has consecutive numbers). A reading that is not a valid registration is most
    likely a misread of a nearby valid one.
    """
    if a == b:
        return True
    if looks_valid(a) and looks_valid(b):
        return False
    if min(len(a), len(b)) < 5:
        return False
    return edit_distance(a, b) <= 2 and abs(len(a) - len(b)) <= 2


def _rank(valid: bool, confidence: float) -> tuple[int, float]:
    return (1 if valid else 0, confidence)


_CAM_TIME = re.compile(r"(\d{4}-\d{2}-\d{2})[/\\].*?[/\\](\d{2})\.(\d{2})\.(\d{2})\[")


def camera_time_from_path(path: str) -> str | None:
    """The camera names each upload <date>/.../<HH.MM.SS>[M]...jpg. Its clock is the site's."""
    m = _CAM_TIME.search(path)
    return f"{m.group(1)} {m.group(2)}:{m.group(3)}:{m.group(4)}" if m else None


@dataclass
class Outcome:
    kind: str          # "new" | "updated" | "merged" | "ignored"
    sighting_id: int | None = None


class Sightings:
    def __init__(self, cfg: Config, db: Db) -> None:
        self.cfg, self.db = cfg, db

    # -- settings, read each time so a change in the UI applies at once ----------------------
    def min_confidence(self) -> float:
        return float(self.db.get("min_confidence", str(self.cfg.min_confidence)))

    def dedupe_seconds(self) -> int:
        return int(self.db.get("dedupe_seconds", str(self.cfg.dedupe_seconds)))

    def retention_days(self) -> int:
        return int(self.db.get("retention_days", str(self.cfg.retention_days)))

    def strict_format(self) -> bool:
        """Only numbers that look like an Indian registration are recorded as plates.

        The detector also finds plate-shaped TEXT that is not a plate: above all the camera's own on-screen
        title and clock, which are on every picture. Those never look like a registration, so this one rule
        removes them. Switch it off (Settings) for foreign or unusual plates.
        """
        return self.db.get("strict_format", "1") == "1"

    # -- recording ---------------------------------------------------------------------
    def record(self, camera_id: int | None, read: PlateRead, jpeg: bytes, cam_time: str | None,
               now: datetime | None = None) -> Outcome:
        if read.confidence < self.min_confidence():
            return Outcome("ignored")
        if self.strict_format() and not read.valid:
            return Outcome("ignored")
        now = now or datetime.now(timezone.utc)
        since = (now - timedelta(seconds=self.dedupe_seconds())).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = self.db.all(
            "SELECT * FROM sightings WHERE camera_id IS ? AND seen_at >= ? ORDER BY id DESC",
            (camera_id, since),
        )
        match = next((r for r in rows if same_vehicle(r["plate"], read.text)), None)

        if match is not None:
            if _rank(read.valid, read.confidence) <= _rank(bool(match["valid_format"]), match["confidence"] or 0.0):
                return Outcome("merged", match["id"])
            old_image, old_crop = match["image"], match["crop"]
            image, crop = self._save_images(jpeg, read.box, now)
            self.db.run(
                "UPDATE sightings SET plate=?, confidence=?, det_confidence=?, box=?, plate_px=?, "
                "valid_format=?, image=?, crop=? WHERE id=?",
                (read.text, read.confidence, read.det_confidence, json.dumps(read.box), read.width_px,
                 int(read.valid), image, crop, match["id"]),
            )
            self._remove_files(old_image, old_crop)
            return Outcome("updated", match["id"])

        image, crop = self._save_images(jpeg, read.box, now)
        sid = self.db.run(
            "INSERT INTO sightings (plate, camera_id, seen_at, cam_time, confidence, det_confidence, box, "
            "plate_px, valid_format, image, crop) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (read.text, camera_id, now.strftime("%Y-%m-%dT%H:%M:%SZ"), cam_time, read.confidence,
             read.det_confidence, json.dumps(read.box), read.width_px, int(read.valid), image, crop),
        )
        return Outcome("new", sid)

    def _save_images(self, jpeg: bytes, box: tuple[int, int, int, int], now: datetime) -> tuple[str, str | None]:
        import cv2
        import numpy as np

        rel_dir = Path(now.strftime("%Y/%m/%d"))
        (self.cfg.media_dir / rel_dir).mkdir(parents=True, exist_ok=True)
        name = uuid.uuid4().hex
        image = str(rel_dir / f"{name}.jpg")
        (self.cfg.media_dir / image).write_bytes(jpeg)

        crop_rel: str | None = None
        try:
            img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
            if img is not None:
                x, y, w, h = box
                px, py = int(w * 0.15), int(h * 0.3)       # a little context around the plate
                H, W = img.shape[:2]
                c = img[max(0, y - py):min(H, y + h + py), max(0, x - px):min(W, x + w + px)]
                if c.size:
                    if c.shape[1] > 480:
                        s = 480 / c.shape[1]
                        c = cv2.resize(c, (480, max(1, int(c.shape[0] * s))), interpolation=cv2.INTER_AREA)
                    ok, buf = cv2.imencode(".jpg", c, [cv2.IMWRITE_JPEG_QUALITY, 88])
                    if ok:
                        crop_rel = str(rel_dir / f"{name}_c.jpg")
                        (self.cfg.media_dir / crop_rel).write_bytes(buf.tobytes())
        except Exception:      # a missing thumbnail must never lose the sighting
            crop_rel = None
        return image, crop_rel

    def _remove_files(self, *rels: str | None) -> None:
        for rel in rels:
            if rel:
                try:
                    (self.cfg.media_dir / rel).unlink()
                except OSError:
                    pass

    # -- reading ---------------------------------------------------------------------------
    def grouped(self, query: str = "", limit: int = 60, offset: int = 0) -> list[dict]:
        q = re.sub(r"[^A-Z0-9]", "", query.upper())
        like = f"%{q}%"
        plates = self.db.all(
            "SELECT plate, COUNT(*) AS n, MAX(seen_at) AS last_at FROM sightings "
            "WHERE plate LIKE ? GROUP BY plate ORDER BY last_at DESC, MAX(id) DESC LIMIT ? OFFSET ?",
            (like, limit, offset),
        )
        out = []
        for p in plates:
            rows = self.db.all(
                "SELECT id, seen_at, cam_time, confidence, valid_format, plate_px, crop, image "
                "FROM sightings WHERE plate = ? ORDER BY seen_at DESC, id DESC",
                (p["plate"],),
            )
            best = max(rows, key=lambda r: (r["confidence"] or 0.0))
            latest = rows[0]
            out.append({
                "plate": p["plate"],
                "count": p["n"],
                "valid": bool(best["valid_format"]),
                "confidence": best["confidence"],
                "plate_px": latest["plate_px"],
                "latest_id": latest["id"],
                "token": Path(latest["crop"] or latest["image"] or "").stem,      # unique to this picture (see web.py)
                "image_id": latest["id"],
                "has_crop": bool(latest["crop"]),
                "times": [
                    {"id": r["id"], "seen_at": r["seen_at"], "cam_time": r["cam_time"]} for r in rows
                ],
            })
        return out

    def count_plates(self, query: str = "") -> int:
        q = re.sub(r"[^A-Z0-9]", "", query.upper())
        return self.db.one("SELECT COUNT(DISTINCT plate) AS n FROM sightings WHERE plate LIKE ?", (f"%{q}%",))["n"]

    def totals(self) -> dict:
        r = self.db.one("SELECT COUNT(*) AS s, COUNT(DISTINCT plate) AS p, MAX(seen_at) AS last FROM sightings")
        return {"sightings": r["s"], "plates": r["p"], "last": r["last"]}

    def file_for(self, sighting_id: int, kind: str) -> Path | None:
        r = self.db.one("SELECT image, crop FROM sightings WHERE id = ?", (sighting_id,))
        if not r:
            return None
        rel = r["crop"] if kind == "crop" else r["image"]
        if not rel:
            return None
        p = (self.cfg.media_dir / rel).resolve()
        return p if p.is_file() and self.cfg.media_dir.resolve() in p.parents else None

    def delete_plate(self, plate: str) -> int:
        rows = self.db.all("SELECT id, image, crop FROM sightings WHERE plate = ?", (plate,))
        for r in rows:
            self._remove_files(r["image"], r["crop"])
        self.db.run("DELETE FROM sightings WHERE plate = ?", (plate,))
        return len(rows)

    def delete_invalid(self) -> int:
        """Remove sightings whose number does not look like a registration (camera labels, misreads)."""
        rows = self.db.all("SELECT id, image, crop FROM sightings WHERE valid_format = 0")
        for r in rows:
            self._remove_files(r["image"], r["crop"])
        self.db.run("DELETE FROM sightings WHERE valid_format = 0")
        return len(rows)

    def count_invalid(self) -> int:
        return self.db.one("SELECT COUNT(*) AS n FROM sightings WHERE valid_format = 0")["n"]

    def delete_all(self) -> int:
        rows = self.db.all("SELECT image, crop FROM sightings")
        for r in rows:
            self._remove_files(r["image"], r["crop"])
        self.db.run("DELETE FROM sightings")
        return len(rows)

    def sweep(self, now: datetime | None = None) -> int:
        days = self.retention_days()
        if days <= 0:
            return 0
        cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = self.db.all("SELECT id, image, crop FROM sightings WHERE seen_at < ?", (cutoff,))
        for r in rows:
            self._remove_files(r["image"], r["crop"])
        self.db.run("DELETE FROM sightings WHERE seen_at < ?", (cutoff,))
        return len(rows)

    def export_csv(self) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["plate", "seen_at_utc", "camera_time", "confidence", "valid_format", "plate_width_px"])
        for r in self.db.all("SELECT plate, seen_at, cam_time, confidence, valid_format, plate_px "
                             "FROM sightings ORDER BY seen_at DESC, id DESC"):
            w.writerow([r["plate"], r["seen_at"], r["cam_time"] or "",
                        f"{r['confidence']:.2f}" if r["confidence"] is not None else "",
                        "yes" if r["valid_format"] else "no", r["plate_px"] or ""])
        return buf.getvalue()
