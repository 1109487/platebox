"""Watching the camera's own video stream: movement, plates, and the motion log, with nothing pushed by the camera.

Why this exists. The camera can also PUSH a picture to PlateBox when its own motion detection fires, and that
is how PlateBox first worked. In practice the push is the fragile part: it needs the camera's upload settings, its
clock, its name and time lookups, and a good network, and some cameras keep re-sending an old queue of pictures
and never get to the new ones. LIVE VIEW never had those problems: PlateBox opens the camera's stream itself.

So here PlateBox does that all the time. ffmpeg turns the stream into pictures (a few per second); each is
compared with the one before; when something moves, plates are read and the picture goes in the motion log.
Nothing depends on the camera's upload, its clock, DNS or internet: only that PlateBox can reach the camera.

It uses the camera login given on the Live page. It is off until that is given.
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from datetime import datetime
from typing import Callable

from .config import Config
from .db import Db, now_iso
from .live import SOI, FrameSplitter, explain, redact, rtsp_url, tls_flags
from .reader import READ_LOCK

log = logging.getLogger("platebox.watch")

HOLD_S = 4.0               # keep reading plates this long after the last movement (a vehicle is not one frame)
STALE_FRAME_S = 20.0       # no picture for this long: not healthy
RESTART_AFTER_S = 4.0
MAX_RESTART_S = 30.0
GLOBAL_CHANGE_PCT = 55.0   # more of the picture than this changing at once is the lights or the IR filter, not movement


def watch_args(binary: str, url: str, fps: int, pre: list[str] | tuple = ()) -> list[str]:
    """ffmpeg: the camera's stream as full-size JPEGs, `fps` a second."""
    return [binary, "-nostdin", "-hide_banner", "-loglevel", "error", *pre, "-fflags", "nobuffer", "-flags", "low_delay",
            "-rtsp_transport", "tcp", "-i", url, "-an",
            "-vf", f"fps={fps}", "-q:v", "3", "-f", "image2pipe", "-c:v", "mjpeg", "pipe:1"]


class MotionDetector:
    """Percentage of a small grey copy of the picture that changed against a slowly updated background."""

    def __init__(self) -> None:
        self.bg = None

    def changed_percent(self, img) -> float:
        import cv2
        import numpy as np

        small = cv2.GaussianBlur(cv2.cvtColor(cv2.resize(img, (160, 90), interpolation=cv2.INTER_AREA),
                                              cv2.COLOR_BGR2GRAY), (5, 5), 0)
        if self.bg is None:
            self.bg = small.astype(np.float32)
            return 0.0
        diff = cv2.absdiff(small, cv2.convertScaleAbs(self.bg))
        pct = 100.0 * float((diff > 22).sum()) / diff.size
        if pct >= GLOBAL_CHANGE_PCT:                       # the whole scene changed: start again from here
            self.bg = small.astype(np.float32)
            return 0.0
        cv2.accumulateWeighted(small, self.bg, 0.15)       # the background follows slow changes (daylight, shadows)
        return pct


class Watcher:
    def __init__(self, cfg: Config, db: Db, live, reader, sightings, motion=None,
                 spawn: Callable[[str], list[str]] | None = None) -> None:
        self.cfg, self.db, self.live, self.reader, self.sightings, self.motion = cfg, db, live, reader, sightings, motion
        self._spawn_args = spawn
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cond = threading.Condition()
        self._latest: bytes | None = None
        self._proc: subprocess.Popen | None = None
        self.detector = MotionDetector()
        self.error = ""
        self.frames = 0
        self.motion_frames = 0
        self.last_frame = 0.0
        self.last_motion = 0.0
        self._active_until = 0.0
        self._camera_row: int | None = None

    # -- what is set up -----------------------------------------------------------------------
    def enabled(self) -> bool:
        return (self.db.get("watch_enabled", "1") != "0" and bool(self.db.get("camera_ip"))
                and self.live.credentials() is not None and (self._spawn_args is not None or self.live.available()))

    def sensitivity(self) -> float:
        try:
            return max(0.05, float(self.db.get("watch_sensitivity", "0.5")))
        except ValueError:
            return 0.5

    def healthy(self) -> bool:
        return self.enabled() and time.time() - self.last_frame <= STALE_FRAME_S

    def status(self) -> dict:
        now = time.time()
        return {"enabled": self.enabled(), "needs_login": self.live.credentials() is None,
                "running": self._proc is not None and self._proc.poll() is None,
                "healthy": self.healthy(), "error": self.error, "frames": self.frames,
                "motion_frames": self.motion_frames, "sensitivity": self.sensitivity(),
                "last_frame_ago": int(now - self.last_frame) if self.last_frame else None,
                "last_motion_ago": int(now - self.last_motion) if self.last_motion else None}

    def describe(self) -> tuple[str, str]:
        """(level, a sentence for the Settings page)."""
        st = self.status()
        if self.db.get("watch_enabled", "1") == "0":
            return "off", "Switched off. Pictures come only from what the camera uploads."
        if st["needs_login"]:
            return "wait", "Give PlateBox the camera's user name and password below, and it starts watching at once."
        if not self.db.get("camera_ip"):
            return "wait", "Choose the camera first (Settings, Find or change the camera)."
        if st["healthy"]:
            m = ("No movement yet." if st["last_motion_ago"] is None else
                 f"Last movement {st['last_motion_ago']} s ago.")
            return "ok", f"Watching the camera directly. {m} ({st['frames']} pictures looked at.)"
        return "bad", st["error"] or "Connecting to the camera's video…"

    # -- the supervisor -------------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._run, name="watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        p = self._proc
        if p is not None:
            try:
                p.kill()
            except OSError:
                pass
        with self._cond:
            self._cond.notify_all()

    def restart(self) -> None:
        """The login or a setting changed: drop the current connection so the next one uses it."""
        p = self._proc
        if p is not None:
            try:
                p.kill()
            except OSError:
                pass

    def _run(self) -> None:
        delay = RESTART_AFTER_S
        while not self._stop.is_set():
            if not self.enabled():
                self._stop.wait(3.0)
                continue
            try:
                got = self._session()
            except Exception:
                log.exception("watching the camera failed")
                got = False
            delay = RESTART_AFTER_S if got else min(MAX_RESTART_S, delay * 1.7)
            self._stop.wait(delay)

    def _args(self) -> list[str]:
        creds = self.live.credentials()
        url = rtsp_url(self.db.get("camera_ip") or "", creds[0], creds[1], int(self.db.get("watch_subtype", "0") or 0))
        if self._spawn_args:
            return self._spawn_args(url)
        binary = self.live.binary() or "ffmpeg"
        return watch_args(binary, url, self.cfg.watch_fps, pre=tls_flags(binary))

    def _session(self) -> bool:
        """One connection to the camera, until it ends. True if pictures came through."""
        creds = self.live.credentials()
        try:
            proc = self.live._popen(self._args())
        except OSError as e:
            self.error = f"Could not run ffmpeg: {e}"
            return False
        self._proc, self.error, err = proc, "", []
        threading.Thread(target=lambda: err.append(proc.stderr.read().decode("utf-8", "replace")[-600:]),
                         name="watch-err", daemon=True).start()
        worker = threading.Thread(target=self._work, name="watch-work", daemon=True)
        worker.start()
        sp, got = FrameSplitter(), False
        try:
            while not self._stop.is_set():
                chunk = proc.stdout.read1(262144) if hasattr(proc.stdout, "read1") else proc.stdout.read(262144)
                if not chunk:
                    break
                for f in sp.feed(chunk):
                    got = True
                    self.last_frame = time.time()
                    with self._cond:
                        self._latest = f                 # only the newest picture matters: never fall behind
                        self._cond.notify_all()
        finally:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            self._proc = None
            with self._cond:
                self._cond.notify_all()
            worker.join(timeout=10)
        if err and err[0].strip() and not got:
            self.error = explain(redact(err[0], creds[1] if creds else ""))
            log.warning("camera stream: %s", self.error)
        elif not got:
            self.error = "The camera's video did not come through."
        else:
            self.error = "The camera's video stopped. PlateBox is reconnecting."
        return got

    # -- looking at pictures ----------------------------------------------------------------------------
    def _work(self) -> None:
        while not self._stop.is_set() and self._proc is not None:
            with self._cond:
                if self._latest is None:
                    self._cond.wait(1.0)
                jpeg, self._latest = self._latest, None
            if jpeg is None:
                continue
            try:
                self.handle(jpeg)
            except Exception:
                log.exception("could not look at a picture")

    def _camera_id(self) -> int:
        if self._camera_row is not None:
            return self._camera_row
        row = self.db.one("SELECT id FROM cameras ORDER BY last_seen DESC, id LIMIT 1")
        if row:
            self._camera_row = row["id"]
        else:
            now = now_iso()
            self._camera_row = self.db.run(
                "INSERT INTO cameras (serial, ip, label, first_seen, last_seen) VALUES (?,?,?,?,?)",
                ("VIDEO", self.db.get("camera_ip"), self.db.get("camera_label"), now, now))
            self.db.event("camera_connected", "VIDEO")
        return self._camera_row

    def handle(self, jpeg: bytes, now: float | None = None) -> str:
        """One picture from the stream. Returns 'still', 'moving' or 'bad'. (Public so a test can feed pictures.)"""
        import cv2
        import numpy as np

        now = now if now is not None else time.time()
        img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return "bad"
        self.frames += 1
        pct = self.detector.changed_percent(img)
        if pct >= self.sensitivity():
            self._active_until = now + HOLD_S
            self.last_motion = now
        if now > self._active_until:
            return "still"
        self.motion_frames += 1
        cam_time = datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S")      # this computer's clock: always right
        cid = self._camera_id()
        with READ_LOCK:
            reads = self.reader.read(img)
        kept: list[str] = []
        for read in reads:
            out = self.sightings.record(cid, read, jpeg, cam_time)
            if out.kind != "ignored":
                kept.append(read.text)
        if self.motion is not None:
            try:
                self.motion.record(cid, img, cam_time, kept)
            except Exception:
                log.exception("motion log failed")
        first = self.db.one("SELECT uploads FROM cameras WHERE id = ?", (cid,))["uploads"] == 0
        self.db.run("UPDATE cameras SET uploads = uploads + 1, last_upload = ?, last_seen = ? WHERE id = ?",
                    (now_iso(), now_iso(), cid))
        if first:
            self.db.event("first_picture", "VIDEO")
        return "moving"
