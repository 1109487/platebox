"""The worker that turns files the camera drops into plate sightings.

The SFTP server (a separate, locked-down sshd) only writes files into <intake>/<serial>/...
This module looks at that directory: a settled .jpg is read for plates, whether or not a plate was found.
A picture the camera took long ago (by the camera's own clock) is one it is sending again and is not read.

A picture that has been dealt with is NOT deleted at once. The camera asks the receiver whether a picture is
already there before it sends it, and sends it again if not, so deleting made the camera re-send the same
pictures at every reconnect. They stay (12 hours, at most 150 MB, in the locked data folder) and are listed in the
`delivered` table so they are never read twice. Nothing is shown from them: the pictures that appear are only the
motion log's small copies and the plates' crops.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .camclock import CameraClock, parse_camera_stamp
from .config import Config
from .db import Db, now_iso
from .reader import READ_LOCK, Reader
from .sightings import Sightings, camera_time_from_path

log = logging.getLogger("platebox.intake")

SERIAL_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
# Folders the camera makes that are not a camera: its "test" button writes into FTP_TEST.
NOT_CAMERAS = {"ftp_test"}


def is_camera_folder(name: str) -> bool:
    return bool(SERIAL_RE.match(name)) and name.lower() not in NOT_CAMERAS
JPEG_RE = re.compile(r"\.jpe?g$", re.I)
MAX_DEPTH = 6
PRUNE_EVERY_S = 60
PRUNE_MIN_AGE_S = 120


@dataclass
class Candidate:
    path: Path
    mtime: float
    size: int


def plan_backlog(ready: list[Candidate], max_backlog: int, batch: int) -> tuple[list[Candidate], list[Candidate]]:
    """(drop, take). When the reader falls behind, the newest pictures are the ones worth reading."""
    ordered = sorted(ready, key=lambda c: c.mtime)
    drop = ordered[: max(0, len(ordered) - max_backlog)]
    kept = ordered[len(drop):]
    return drop, kept[:batch]


class Intake:
    def __init__(self, cfg: Config, db: Db, reader: Reader, sightings: Sightings, motion=None) -> None:
        self.cfg, self.db, self.reader, self.sightings = cfg, db, reader, sightings
        self.motion = motion                  # the motion log, if there is one
        self.skip_uploads = lambda: False     # True while the stream watcher is working: the camera's own uploads are redundant
        self.clock = CameraClock(db, enabled=not cfg.fake_net)   # the camera's own clock: old picture or new?
        self._last_prune = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- file system ---------------------------------------------------------------------
    def _collect(self, d: Path, depth: int, out: list[Candidate]) -> None:
        try:
            entries = list(d.iterdir())
        except OSError:
            return
        for e in entries:
            try:
                if e.is_symlink():
                    continue          # never follow a link a camera user might have planted
                if e.is_dir():
                    if depth < MAX_DEPTH:
                        self._collect(e, depth + 1, out)
                elif e.is_file() and JPEG_RE.search(e.name):
                    st = e.stat()
                    out.append(Candidate(e, st.st_mtime, st.st_size))
            except OSError:
                continue

    @staticmethod
    def _unlink(p: Path) -> None:
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("could not delete %s: %s", p.name, e)

    # -- pictures already dealt with ------------------------------------------------------
    # The camera asks the receiver "is this picture there?" before it sends it, and sends it AGAIN if the answer is
    # no. A picture that was read and then deleted therefore comes back at every reconnect, for ever. So a picture
    # that has been dealt with stays where the camera put it (for delivered_keep_hours, within delivered_max_mb)
    # and is only remembered here as done.
    def _rel(self, p: Path) -> str:
        return p.relative_to(self.cfg.intake_dir).as_posix()

    def _kept(self) -> bool:
        return self.cfg.delivered_keep_hours > 0 and self.cfg.delivered_max_mb > 0

    def _done(self) -> dict[str, tuple[int, float]]:
        return {r["path"]: (r["size"], r["mtime"]) for r in self.db.all("SELECT path, size, mtime FROM delivered")}

    def _is_done(self, c: Candidate, done: dict[str, tuple[int, float]]) -> bool:
        hit = done.get(self._rel(c.path))
        return bool(hit and hit[0] == c.size and abs(hit[1] - c.mtime) < 2.0)       # rewritten since: not the same one

    def _deliver(self, c: Candidate) -> None:
        if not self._kept():
            self._unlink(c.path)
            return
        self.db.run("INSERT OR REPLACE INTO delivered (path, size, mtime, t) VALUES (?,?,?,?)",
                    (self._rel(c.path), c.size, c.mtime, time.time()))

    def prune_delivered(self, now: float) -> int:
        """Let go of the oldest kept pictures: past their time, or beyond the space they may take."""
        self.db.run("DELETE FROM seen_pictures WHERE t < ?", (now - 14 * 86400,))        # fingerprints: a fortnight
        rows = self.db.all("SELECT path, size, t FROM delivered ORDER BY t")
        ttl = self.cfg.delivered_keep_hours * 3600
        cap = self.cfg.delivered_max_mb * 1024 * 1024
        total = sum(r["size"] for r in rows)
        trimming = self._kept() and total > cap
        removed = 0
        for r in rows:
            if self._kept() and now - r["t"] <= ttl and not (trimming and total > cap * 0.9):
                continue
            self._unlink(self.cfg.intake_dir / r["path"])
            self.db.run("DELETE FROM delivered WHERE path = ?", (r["path"],))
            total -= r["size"]
            removed += 1
        return removed

    def _is_stale(self, c: Candidate) -> bool:
        """Did the camera take this picture long before its own clock's 'now'? Then it is one being sent again.
        If the camera's clock cannot be read, nothing is judged old."""
        minutes = int(self.db.get("stale_minutes", str(self.cfg.stale_minutes)) or 0)
        if minutes <= 0:
            return False
        stamp = parse_camera_stamp(camera_time_from_path(self._rel(c.path)))
        if stamp is None:
            return False
        cam_now = self.clock.now_local()
        return cam_now is not None and (cam_now - stamp).total_seconds() > minutes * 60

    def _seen_before(self, jpeg: bytes) -> bool:
        """True if this very picture (same bytes) was read before: the camera sent it again. Remembers it if not."""
        import hashlib

        h = hashlib.sha256(jpeg).hexdigest()
        if self.db.one("SELECT 1 FROM seen_pictures WHERE hash = ?", (h,)):
            self.db.put("repeat_ignored", str(int(self.db.get("repeat_ignored", "0") or 0) + 1))
            self.db.put("repeat_last", now_iso())
            log.info("ignoring a picture the camera has sent before")
            return True
        self.db.run("INSERT INTO seen_pictures (hash, t) VALUES (?, ?)", (h, time.time()))
        return False

    def _count_stale(self) -> None:
        self.db.put("stale_ignored", str(int(self.db.get("stale_ignored", "0") or 0) + 1))
        self.db.put("stale_last", now_iso())

    # -- cameras -------------------------------------------------------------------------
    def _camera(self, serial: str) -> int:
        row = self.db.one("SELECT id FROM cameras WHERE serial = ?", (serial,))
        now = now_iso()
        if row:
            self.db.run("UPDATE cameras SET last_seen = ? WHERE id = ?", (now, row["id"]))
            return row["id"]
        ip = self.db.get("camera_ip")
        cid = self.db.run(
            "INSERT INTO cameras (serial, ip, label, first_seen, last_seen) VALUES (?,?,?,?,?)",
            (serial, ip, self.db.get("camera_label"), now, now),
        )
        self.db.event("camera_connected", serial)
        log.info("camera %s connected", serial)
        return cid

    # -- one pass ------------------------------------------------------------------------
    def scan_once(self, now: float | None = None) -> dict:
        now = now if now is not None else time.time()
        stats = {"read": 0, "with_plate": 0, "dropped": 0, "stale": 0}
        root = self.cfg.intake_dir
        try:
            serials = [d for d in root.iterdir() if d.is_dir() and not d.is_symlink() and is_camera_folder(d.name)]
        except OSError:
            return stats

        done = self._done() if serials else {}
        for sd in serials:
            cid = self._camera(sd.name)          # any sign of the camera counts as "connected"
            found: list[Candidate] = []
            self._collect(sd, 0, found)
            ready = [c for c in found if now - c.mtime >= self.cfg.intake_settle_s and not self._is_done(c, done)]
            drop, take = plan_backlog(ready, self.cfg.intake_max_backlog, 25)
            for c in drop:
                self._deliver(c)
                stats["dropped"] += 1
            for c in take:
                if self.skip_uploads():             # PlateBox is watching the video itself: pictures the camera
                    self._deliver(c)                # pushes (often old ones it keeps re-sending) add nothing
                    stats["skipped"] = stats.get("skipped", 0) + 1
                    continue
                if self._is_stale(c):
                    self._deliver(c)
                    self._count_stale()
                    stats["stale"] += 1
                    continue
                had = self._process(cid, sd.name, c)
                stats["read"] += 1
                stats["with_plate"] += 1 if had else 0

        if now - self._last_prune >= PRUNE_EVERY_S:
            self._last_prune = now
            self.prune_delivered(now)
            self.prune_empty(root, now)
        return stats

    def _process(self, camera_id: int, serial: str, c: Candidate) -> bool:
        """Read one picture. Returns True if it contained a plate that was recorded."""
        recorded = False
        try:
            if c.size > self.cfg.intake_max_bytes:
                log.warning("dropping an oversize file (%d bytes)", c.size)
                return False
            import cv2
            import numpy as np

            jpeg = c.path.read_bytes()
            img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                log.warning("dropping a file that is not a picture")
                return False
            if self._seen_before(jpeg):
                return False
            first = self.db.one("SELECT uploads FROM cameras WHERE id = ?", (camera_id,))["uploads"] == 0
            self.db.run("UPDATE cameras SET uploads = uploads + 1, last_upload = ?, last_seen = ? WHERE id = ?",
                        (now_iso(), now_iso(), camera_id))
            if first:
                self.db.event("first_picture", serial)
            cam_time = camera_time_from_path(str(c.path.relative_to(self.cfg.intake_dir)))
            with READ_LOCK:
                reads = self.reader.read(img)
            kept: list[str] = []
            for read in reads:
                out = self.sightings.record(camera_id, read, jpeg, cam_time)
                recorded = recorded or out.kind in ("new", "updated", "merged")
                if out.kind != "ignored":
                    kept.append(read.text)
            if self.motion is not None:
                try:
                    self.motion.record(camera_id, img, cam_time, kept)
                except Exception:             # the log is a convenience: it must never lose a plate
                    log.exception("motion log failed")
        except Exception:
            log.exception("could not read a picture")
        finally:
            self._deliver(c)
        return recorded

    def prune_empty(self, root: Path, now: float) -> int:
        removed = 0

        def walk(d: Path, depth: int) -> bool:
            nonlocal removed
            try:
                kids = list(d.iterdir())
            except OSError:
                return False
            empty = True
            for k in kids:
                if k.is_dir() and not k.is_symlink() and depth < MAX_DEPTH:
                    if not walk(k, depth + 1):
                        empty = False
                else:
                    empty = False
            if empty and depth > 0:
                try:
                    if now - d.stat().st_mtime >= PRUNE_MIN_AGE_S:
                        d.rmdir()
                        removed += 1
                        return True
                except OSError:
                    pass
            return False

        try:
            for sd in root.iterdir():
                if sd.is_dir() and not sd.is_symlink() and SERIAL_RE.match(sd.name):
                    walk(sd, 0)
        except OSError:
            pass
        return removed

    # -- thread --------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._run, name="intake", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        last_sweep = 0.0
        while not self._stop.is_set():
            try:
                self.scan_once()
                if time.time() - last_sweep > 3600:
                    last_sweep = time.time()
                    self.sightings.sweep()
            except Exception:
                log.exception("intake pass failed")
            self._stop.wait(self.cfg.intake_poll_s)
