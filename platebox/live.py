"""Live view: the camera's picture in the browser. An ADD-ON, kept apart from plate reading.

A browser cannot play the camera's stream (RTSP over TLS, H.265), so ffmpeg turns it into a
stream of JPEG pictures that any browser shows in an <img> tag.

What keeps this from disturbing the real job:

* It is OFF until someone gives the camera's login on the Live page.
* ffmpeg runs only while somebody is watching: started by the first viewer, shared by up to
  MAX_VIEWERS, and stopped IDLE_STOP_S seconds after the last one leaves.
* It uses the camera's small second stream (720p), not the large one the plate pictures come from.
* ffmpeg is a separate, low-priority process. Nothing in here raises into the intake or the reader.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
from typing import Callable, Iterator

from .config import Config
from .db import Db

log = logging.getLogger("platebox.live")

SOI, EOI = b"\xff\xd8", b"\xff\xd9"
MAX_VIEWERS = 3
IDLE_STOP_S = 8.0          # no viewers for this long: stop ffmpeg
NO_FRAME_S = 20.0          # a viewer gives up if the camera sends nothing for this long
RESTART_AFTER_S = 2.0


def rtsp_url(ip: str, user: str, password: str, subtype: int = 1) -> str:
    q = lambda s: urllib.parse.quote(s, safe="")  # noqa: E731
    return f"rtsps://{q(user)}:{q(password)}@{ip}:554/video/live?channel=1&subtype={subtype}"


def redact(text: str, *secrets_: str) -> str:
    for s in secrets_:
        if s:
            text = text.replace(s, "***").replace(urllib.parse.quote(s, safe=""), "***")
    return text


_TLS_CHECKS_BY_DEFAULT = re.compile(r"tls_verify.*\(default\s+(true|1)\)", re.I)
_tls_cache: dict[str, list[str]] = {}


def tls_flags(binary: str) -> list[str]:
    """The camera's certificate is self-signed. Newer ffmpeg builds (the Windows one) check certificates unless told
    not to; older ones (Debian's) do not check and reject the flag. Ask the binary which it is."""
    if binary in _tls_cache:
        return _tls_cache[binary]
    flags: list[str] = []
    try:
        kw = {"creationflags": 0x08000000} if os.name == "nt" else {}
        out = subprocess.run([binary, "-hide_banner", "-h", "protocol=tls"], capture_output=True, text=True,
                             timeout=15, **kw)
        if _TLS_CHECKS_BY_DEFAULT.search(out.stdout + out.stderr):
            flags = ["-tls_verify", "0"]
    except (OSError, subprocess.SubprocessError):
        pass
    _tls_cache[binary] = flags
    return flags


def ffmpeg_args(binary: str, url: str, fps: int, width: int, one_frame: bool = False,
                pre: list[str] | tuple = ()) -> list[str]:
    a = [binary, "-nostdin", "-hide_banner", "-loglevel", "error", *pre, "-rtsp_transport", "tcp", "-i", url, "-an",
         "-vf", f"fps={fps},scale={width}:-2", "-q:v", "6"]
    if one_frame:
        a += ["-frames:v", "1"]
    return a + ["-f", "image2pipe", "-c:v", "mjpeg", "pipe:1"]


class FrameSplitter:
    """Cuts a byte stream of concatenated JPEGs into pictures. Chunk boundaries can fall anywhere."""

    def __init__(self) -> None:
        self.buf = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self.buf += data
        out: list[bytes] = []
        while True:
            s = self.buf.find(SOI)
            if s < 0:
                # Nothing to keep, except a lone 0xFF at the end: it may be half of a start marker.
                keep = bytes(self.buf[-1:]) if self.buf.endswith(b"\xff") else b""
                self.buf = bytearray(keep)
                break
            if s > 0:
                del self.buf[:s]
            e = self.buf.find(EOI, 2)
            if e < 0:
                if len(self.buf) > 8 * 1024 * 1024:     # garbage: never grow without bound
                    self.buf.clear()
                break
            out.append(bytes(self.buf[: e + 2]))
            del self.buf[: e + 2]
        return out


def explain(stderr: str) -> str:
    """What ffmpeg's complaint means, for a person."""
    t = stderr.lower()
    if "401" in t or "unauthorized" in t or "authorization failed" in t:
        return "The camera did not accept that user name and password."
    if "refused" in t or "timed out" in t or "no route" in t or "unreachable" in t or "network is down" in t:
        return "The camera cannot be reached. Check that it is on and on the same network."
    if "404" in t or "not found" in t:
        return "The camera has no such stream."
    return "The camera's stream could not be opened." + (f" ({stderr.strip().splitlines()[-1][:120]})" if stderr.strip() else "")


class LiveView:
    def __init__(self, cfg: Config, db: Db, spawn: Callable[[str, bool], list[str]] | None = None) -> None:
        self.cfg, self.db = cfg, db
        self._spawn_args = spawn
        self._cond = threading.Condition()
        self._viewers = 0
        self._proc: subprocess.Popen | None = None
        self._frame: bytes | None = None
        self._seq = 0
        self._idle_since: float | None = None
        self._err = ""
        self.last_error = ""

    # -- what is set up ------------------------------------------------------------------
    def binary(self) -> str | None:
        if self.cfg.ffmpeg_path:
            return self.cfg.ffmpeg_path if os.path.exists(self.cfg.ffmpeg_path) else None
        return shutil.which("ffmpeg")

    def available(self) -> bool:
        return self._spawn_args is not None or self.binary() is not None

    def credentials(self) -> tuple[str, str] | None:
        u, p = self.db.get("live_user"), self.db.get("live_pass")
        return (u, p) if u and p else None

    def configured(self) -> bool:
        return bool(self.db.get("camera_ip")) and self.credentials() is not None

    def enabled(self) -> bool:
        return self.db.get("live_enabled", "0") == "1" and self.configured()

    def save(self, user: str, password: str, enabled: bool) -> None:
        if user:
            self.db.put("live_user", user.strip())
        if password:                          # blank = keep what is stored
            self.db.put("live_pass", password)
        self.db.put("live_enabled", "1" if enabled else "0")
        self.stop()

    def forget(self) -> None:
        for k in ("live_user", "live_pass", "live_enabled"):
            self.db.delete_setting(k)
        self.stop()

    def user(self) -> str:
        return self.db.get("live_user", "") or ""

    # -- one-shot check of the login ------------------------------------------------------
    def _args(self, one_frame: bool) -> list[str]:
        creds = self.credentials()
        url = rtsp_url(self.db.get("camera_ip") or "", creds[0], creds[1], self.cfg.live_subtype)
        if self._spawn_args:
            return self._spawn_args(url, one_frame)
        binary = self.binary() or "ffmpeg"
        return ffmpeg_args(binary, url, self.cfg.live_fps, self.cfg.live_width, one_frame, pre=tls_flags(binary))

    def _popen(self, args: list[str]) -> subprocess.Popen:
        kw: dict = {}
        if os.name == "nt":
            kw["creationflags"] = 0x00004000 | 0x08000000      # BELOW_NORMAL priority, no console window
        else:
            kw["preexec_fn"] = lambda: os.nice(10)             # never compete with the reader
        return subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, **kw)

    def test(self) -> tuple[bool, str]:
        if not self.available():
            return False, "ffmpeg is not installed on this box."
        if not self.configured():
            return False, "Enter the camera's user name and password first."
        creds = self.credentials()
        try:
            p = self._popen(self._args(True))
            out, err = p.communicate(timeout=25)
        except subprocess.TimeoutExpired:
            p.kill()
            return False, "The camera did not answer in time."
        except OSError as e:
            return False, f"Could not run ffmpeg: {e}"
        if out[:2] == SOI:
            return True, "Connected. The camera's picture is coming through."
        return False, explain(redact(err.decode("utf-8", "replace"), creds[1]))

    # -- the shared stream -----------------------------------------------------------------
    def subscribe(self) -> "Subscription | None":
        if not (self.enabled() and self.available()):
            return None
        with self._cond:
            if self._viewers >= MAX_VIEWERS:
                return None
            self._viewers += 1
            self._idle_since = None
            self._start_locked()
        return Subscription(self)

    def _release(self) -> None:
        with self._cond:
            self._viewers = max(0, self._viewers - 1)
            if self._viewers == 0:
                self._idle_since = time.time()
            self._cond.notify_all()

    def viewers(self) -> int:
        return self._viewers

    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _start_locked(self) -> None:
        if self.running():
            return
        try:
            proc = self._popen(self._args(False))
        except OSError as e:
            self.last_error = f"Could not run ffmpeg: {e}"
            log.warning(self.last_error)
            return
        self._proc, self._err = proc, ""
        threading.Thread(target=self._read, args=(proc,), name="live-read", daemon=True).start()
        threading.Thread(target=self._drain_err, args=(proc,), name="live-err", daemon=True).start()

    def _drain_err(self, proc: subprocess.Popen) -> None:
        try:
            data = proc.stderr.read()
            self._err = data.decode("utf-8", "replace")[-600:]
        except Exception:
            pass

    def _read(self, proc: subprocess.Popen) -> None:
        sp = FrameSplitter()
        try:
            while True:
                chunk = proc.stdout.read1(65536) if hasattr(proc.stdout, "read1") else proc.stdout.read(65536)
                if not chunk:
                    break
                for f in sp.feed(chunk):
                    with self._cond:
                        self._frame, self._seq = f, self._seq + 1
                        self._cond.notify_all()
                with self._cond:
                    if self._viewers == 0 and self._idle_since and time.time() - self._idle_since >= IDLE_STOP_S:
                        break
        except Exception:
            log.exception("live reader failed")
        finally:
            self._finish(proc)

    def _finish(self, proc: subprocess.Popen) -> None:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        with self._cond:
            if self._proc is proc:
                self._proc = None
            creds = self.credentials()
            if self._err:
                self.last_error = explain(redact(self._err, creds[1] if creds else ""))
            self._cond.notify_all()
            again = self._viewers > 0
        if again:                                   # the camera dropped us while people are watching
            time.sleep(RESTART_AFTER_S)
            with self._cond:
                if self._viewers > 0:
                    self._start_locked()

    def stop(self) -> None:
        with self._cond:
            p = self._proc
        if p is not None:
            try:
                p.kill()
            except OSError:
                pass

    def status(self) -> dict:
        return {"available": self.available(), "configured": self.configured(), "enabled": self.enabled(),
                "running": self.running(), "viewers": self._viewers, "max_viewers": MAX_VIEWERS,
                "error": self.last_error}


class Subscription:
    def __init__(self, lv: LiveView) -> None:
        self.lv, self._closed = lv, False
        with lv._cond:
            self._seen = lv._seq                 # only pictures taken from now on
        self._last = time.time()

    @property
    def expired(self) -> bool:
        """The camera has sent nothing for NO_FRAME_S: the page should say so rather than wait for ever."""
        return time.time() - self._last > NO_FRAME_S

    def poll(self, timeout: float = 1.0) -> bytes | None:
        """The next new picture, or None if none arrived within `timeout` (so callers can check on the client)."""
        lv = self.lv
        with lv._cond:
            lv._cond.wait_for(lambda: lv._seq != self._seen or self._closed, timeout=timeout)
            if lv._frame is not None and lv._seq != self._seen and not self._closed:
                self._seen, self._last = lv._seq, time.time()
                return lv._frame
        return None

    def frames(self) -> Iterator[bytes]:
        try:
            while not self._closed:
                f = self.poll(1.0)
                if f is not None:
                    yield f
                elif self.expired:
                    return
        finally:
            self.close()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self.lv._release()
