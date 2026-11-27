"""The camera's SFTP receiver, built into the program (for Windows, where there is no sshd to lean on).

On Linux the receiver is a separate, locked-down `sshd` (deploy/sshd_config). Here the same promises are kept
in-process with asyncssh:

* ONE account (`ftpcam`), PASSWORD only; the password is the one the setup screen shows (stored in the database,
  so it is not an operating-system account and needs no administrator rights);
* confined to one folder: the client sees only `/upload`, nothing else on the computer;
* SFTP and nothing else: no shell, no commands, no port forwarding;
* guessing is made slow (each wrong password waits a moment, a little longer each time), but the RIGHT password is
  never refused, however many wrong ones came before it. (An earlier version locked an address out after five
  wrong tries, which silently turned "I fixed the typo" into "still failing for five minutes".)

It also keeps a short diary of what the camera did (connected, wrong password, logged in, SFTP started) so the
screens can say in plain words why a camera's "Test" failed. The camera itself only says "Right verification
failure" for several different reasons.

It runs on its own thread and event loop. If it cannot start (the port is taken) the rest of the program carries
on, and `error` says why so the screens can tell the person.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import threading
import time
from collections import deque
from pathlib import Path

from .config import Config
from .db import Db

log = logging.getLogger("platebox.sftp")

MAX_CONNECTIONS = 10
FAIL_DELAY_BASE_S = 1.0          # a wrong password waits this long, plus half a second per recent failure
FAIL_DELAY_MAX_S = 5.0
RECENT_S = 600                   # what counts as "just now" when explaining a failure


def ensure_host_keys(directory: Path) -> list[str]:
    """Create the server's identity once; the same files are reused ever after, so the camera never sees it change."""
    import asyncssh

    directory.mkdir(parents=True, exist_ok=True)
    out: list[str] = []
    for name, alg, kw in (("ssh_host_ed25519_key", "ssh-ed25519", {}),
                          ("ssh_host_ecdsa_key", "ecdsa-sha2-nistp256", {}),
                          ("ssh_host_rsa_key", "ssh-rsa", {"key_size": 3072})):
        p = directory / name
        if not p.exists():
            key = asyncssh.generate_private_key(alg, **kw)
            key.write_private_key(str(p))
            try:
                p.chmod(0o600)
            except OSError:
                pass
        out.append(str(p))
    return out


def camera_sftp_server(asyncssh_module):
    """The SFTP session class: the library's, with one correction a camera depends on.

    The library hands out a folder handle without looking, so asking to open a folder that does not exist
    "succeeds" and the failure only shows on the first read. A camera (the CP Plus does this in its Test) opens
    the folder first and, if that fails, creates it. Told "opened", it never creates it and then cannot write.
    Here a missing folder is reported missing at once, as an ordinary SFTP server does.
    """
    try:                                         # the library's own mapping to a real path (differs on Windows)
        from asyncssh.sftp import _to_local_path as to_local
    except ImportError:                          # pragma: no cover - a future library without it
        to_local = lambda p: p                   # noqa: E731

    class CameraSFTP(asyncssh_module.SFTPServer):
        def scandir(self, path):
            if not os.path.isdir(os.fsdecode(to_local(self.map_path(path)))):
                raise asyncssh_module.SFTPNoSuchFile("No such directory")
            return super().scandir(path)

        def statvfs(self, path):
            """'How much room is there?' The CP Plus asks this before each upload. Windows has no statvfs, so the
            library answered 'unsupported'; answer from the disk's real free space instead, as an ordinary server does."""
            import shutil

            try:
                du = shutil.disk_usage(os.fsdecode(to_local(self.map_path(path))))
            except OSError:
                raise asyncssh_module.SFTPNoSuchFile("No such file") from None
            bs = 4096
            return asyncssh_module.sftp.SFTPVFSAttrs(
                bsize=bs, frsize=bs, blocks=du.total // bs, bfree=du.free // bs, bavail=du.free // bs,
                files=1_000_000, ffree=900_000, favail=900_000, fsid=0, flags=0, namemax=255)

    return CameraSFTP


def explain(events: list[dict], listening: bool, error: str, server: str, port: int, user: str,
            now: float | None = None) -> dict:
    """What the camera did, in plain words, and what to do about it. `events` are newest first."""
    now = now if now is not None else time.time()
    if not listening:
        return {"level": "bad", "kind": "not_running",
                "text": error or "The camera receiver is not running on this computer."}
    recent = [e for e in events if now - e["t"] <= RECENT_S]
    if not recent:
        return {"level": "wait", "kind": "silence",
                "text": ("Nothing from the camera has reached this computer yet. On the camera check: Server address "
                         f"{server or '(this computer)'}, Port {port}, and that you pressed Save before pressing test. "
                         "If those are right, Windows Firewall may be blocking PlateBox (Windows Security, Firewall, "
                         "Allow an app, PlateBox), or the camera and this computer are on different networks.")}
    # A "disconnected" (and a bare "connected") follows every attempt; what matters is the newest thing that
    # said whether the login worked.
    told = [e for e in recent if e["kind"] in ("login_password", "login_user", "login_ok", "sftp")]
    if not told:
        return {"level": "wait", "kind": "connecting",
                "text": f"The camera ({recent[0].get('ip', '?')}) reached this computer but did not get as far as logging in."}
    last = told[0]
    ip = last.get("ip", "the camera")
    if last["kind"] == "login_password":
        return {"level": "bad", "kind": "password", "text":
                (f"The camera ({ip}) reached this computer, but the password it sent is wrong. Click the password on this "
                 "page to copy it, paste it into the camera's Password box, press Save, then press test. Passwords are "
                 "case-sensitive, and the camera shows only dots, so retype it completely.")}
    if last["kind"] == "login_user":
        return {"level": "bad", "kind": "user", "text":
                f"The camera ({ip}) reached this computer, but its user name is wrong. It must be exactly {user}."}
    if last["kind"] in ("login_ok", "sftp"):
        worked = any(e["kind"] == "sftp" for e in recent)
        if worked:
            return {"level": "ok", "kind": "ok", "text": f"The camera ({ip}) connected and its SFTP session worked."}
        return {"level": "wait", "kind": "partial", "text":
                f"The camera ({ip}) logged in but did not go on to send anything. Press test on the camera again."}
    return {"level": "wait", "kind": "connecting", "text": f"The camera ({ip}) is connecting."}


class BuiltinSftp:
    def __init__(self, cfg: Config, db: Db) -> None:
        self.cfg, self.db = cfg, db
        self.error = ""
        self.listening = False
        self.last_login: str | None = None
        self.events: deque[dict] = deque(maxlen=60)          # newest first
        self.targets: dict[str, tuple[str, float]] = {}      # camera address -> (our address it connected to, when)
        self._fails: dict[str, deque[float]] = {}
        self._connections = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    # -- the diary ----------------------------------------------------------------------------
    def note(self, kind: str, ip: str, detail: str = "") -> None:
        self.events.appendleft({"t": time.time(), "kind": kind, "ip": ip, "detail": detail})

    def target_of(self, ip: str, within_s: float = 1800) -> str | None:
        """The address of THIS computer that `ip` last connected to: what its server-address field really holds."""
        hit = self.targets.get(ip)
        return hit[0] if hit and time.time() - hit[1] <= within_s else None

    def recent_login(self, within_s: float = 900) -> bool:
        return any(e["kind"] == "login_ok" and time.time() - e["t"] <= within_s for e in self.events)

    def recent_events(self, n: int = 8) -> list[dict]:
        names = {"connect": "connected to the receiver", "login_ok": "logged in", "login_password": "wrong password",
                 "login_user": "wrong user name", "sftp": "started its SFTP session", "closed": "disconnected"}
        return [{"ago_s": int(time.time() - e["t"]), "ip": e["ip"], "what": names.get(e["kind"], e["kind"]),
                 "kind": e["kind"]} for e in list(self.events)[:n]]

    def diagnosis(self, server: str = "") -> dict:
        return explain(list(self.events), self.listening, self.error, server, self.cfg.sftp_port, self.cfg.sftp_user)

    def trace(self, seconds: float = 120.0) -> float:
        """Record every request the camera makes and every answer, in the log, for a while. For finding out what a
        camera does that is not what is expected. Returns when it will switch itself off (epoch seconds)."""
        import asyncssh

        if time.time() >= getattr(self, "_trace_until", 0):                       # not already tracing: remember the levels
            self._trace_was = (logging.getLogger("asyncssh").level, logging.getLogger("asyncssh.sftp").level)
        asyncssh.set_log_level(logging.DEBUG)
        asyncssh.set_sftp_log_level(logging.DEBUG)
        self._trace_until = time.time() + seconds
        log.info("tracing the camera's requests for %d seconds", seconds)
        end = self._trace_until

        def off() -> None:
            if self._trace_until == end:
                logging.getLogger("asyncssh").setLevel(self._trace_was[0])
                logging.getLogger("asyncssh.sftp").setLevel(self._trace_was[1])
                log.info("tracing the camera's requests: finished")

        t = threading.Timer(seconds, off)
        t.daemon = True
        t.start()
        return self._trace_until

    def tracing(self) -> float:
        """Seconds of tracing left (0 if none)."""
        return max(0.0, getattr(self, "_trace_until", 0) - time.time())

    def _fail_delay(self, ip: str) -> float:
        now = time.time()
        q = self._fails.setdefault(ip, deque())
        while q and now - q[0] > 300:
            q.popleft()
        q.append(now)
        return min(FAIL_DELAY_MAX_S, FAIL_DELAY_BASE_S + 0.5 * (len(q) - 1))

    # -- control ------------------------------------------------------------------------------
    def start(self, wait: float = 5.0) -> bool:
        if self._thread:
            return self.listening
        self._thread = threading.Thread(target=lambda: asyncio.run(self._main()), name="sftp", daemon=True)
        self._thread.start()
        self._ready.wait(wait)
        return self.listening

    def stop(self) -> None:
        """Safe to call more than once (the program's shutdown and a test may both ask)."""
        if self._loop and self._stop and self._thread and self._thread.is_alive():
            try:
                self._loop.call_soon_threadsafe(self._stop.set)
            except RuntimeError:                       # the loop has already closed: nothing left to stop
                pass
        if self._thread:
            self._thread.join(timeout=5)
        self._thread = None
        self.listening = False

    # -- checking it from the inside ----------------------------------------------------------------
    def self_test(self, host: str) -> tuple[bool, str]:
        """Log in to our own receiver, as the camera would, using the stored password. Proves the receiver and the
        password work. It cannot prove the camera can REACH us (a firewall treats traffic to ourselves differently)."""
        import asyncssh

        if not self.listening:
            return False, self.error or "The receiver is not running."
        pw = self.db.get("sftp_password") or ""

        async def go() -> str:
            async with asyncssh.connect(host, self.cfg.sftp_port, username=self.cfg.sftp_user, password=pw,
                                        known_hosts=None, client_keys=None, agent_path=None, login_timeout=10) as c:
                async with c.start_sftp_client() as s:
                    names = await s.listdir("/")
                    return "upload" if "upload" in names else "no upload folder"

        try:
            r = asyncio.run(go())
        except Exception as e:                                   # connection refused, timed out, refused login...
            return False, f"Could not log in to {host}:{self.cfg.sftp_port} from this computer: {e}"
        if r != "upload":
            return False, "Logged in, but the upload folder is missing."
        return True, (f"The receiver answers on {host}:{self.cfg.sftp_port} and the password works. If the camera still "
                      "fails, the problem is between the camera and this computer (address, port, firewall, network).")

    # -- the server -----------------------------------------------------------------------------
    async def _main(self) -> None:
        import asyncssh

        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        owner = self
        if os.environ.get("PLATEBOX_SFTP_DEBUG"):          # every SFTP request and answer, for finding a camera's quirk
            asyncssh.set_log_level(logging.DEBUG)
            asyncssh.set_sftp_log_level(logging.DEBUG)

        class Server(asyncssh.SSHServer):
            def connection_made(self, conn) -> None:
                self.peer = (conn.get_extra_info("peername") or ("?", 0))[0]
                local = (conn.get_extra_info("sockname") or ("", 0))[0]
                log.info("sftp: connection from %s to %s", self.peer, local)
                owner.note("connect", self.peer, local)
                if local:
                    owner.targets[self.peer] = (local, time.time())
                owner._connections += 1
                if owner._connections > MAX_CONNECTIONS:
                    conn.abort()

            def connection_lost(self, exc) -> None:
                owner._connections = max(0, owner._connections - 1)
                owner.note("closed", self.peer)

            def begin_auth(self, username: str) -> bool:
                return True                                  # always ask for a password

            def password_auth_supported(self) -> bool:
                return True

            async def validate_password(self, username: str, password: str) -> bool:
                want = owner.db.get("sftp_password") or ""
                user_ok = hmac.compare_digest(username.encode(), owner.cfg.sftp_user.encode())
                pw_ok = bool(want) and hmac.compare_digest(password.encode(), want.encode())
                if user_ok and pw_ok:                        # the right password is NEVER refused
                    log.info("sftp: camera logged in from %s", self.peer)
                    owner.note("login_ok", self.peer)
                    from .db import now_iso
                    owner.last_login = now_iso()
                    return True
                owner.note("login_user" if not user_ok else "login_password", self.peer)
                log.warning("sftp: wrong %s from %s", "user name" if not user_ok else "password", self.peer)
                await asyncio.sleep(owner._fail_delay(self.peer))      # slow guessing down; never lock anyone out
                return False

        def session(chan):
            peer = (chan.get_extra_info("peername") or ("?", 0))[0]
            owner.note("sftp", peer)
            return session_class(chan, chroot=os.fsencode(str(root)))

        root = self.cfg.sftp_root
        session_class = camera_sftp_server(asyncssh)
        (root / self.cfg.sftp_remote_dir).mkdir(parents=True, exist_ok=True)
        try:
            keys = ensure_host_keys(self.cfg.data_dir / "ssh")
            server = await asyncssh.create_server(
                Server, self.cfg.sftp_host, self.cfg.sftp_port,
                server_host_keys=keys,
                sftp_factory=session,
                allow_scp=False,
                password_auth=True, public_key_auth=False, kbdint_auth=False,
                login_timeout=20, keepalive_interval=30, keepalive_count_max=3,
            )
        except OSError as e:
            self.error = (f"The camera receiver could not start on port {self.cfg.sftp_port}: {e.strerror or e}. "
                          "Another program may be using that port.")
            log.error(self.error)
            self._ready.set()
            return
        except Exception as e:                                  # pragma: no cover - library trouble
            self.error = f"The camera receiver could not start: {e}"
            log.exception("sftp server failed to start")
            self._ready.set()
            return

        self.listening, self.error = True, ""
        log.info("sftp receiver listening on port %d", self.cfg.sftp_port)
        self._ready.set()
        await self._stop.wait()
        server.close()
        await server.wait_closed()
        self.listening = False
