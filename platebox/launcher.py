"""Starting the program: `platebox` (the server) and `platebox --open` (open the page in the browser).

The Windows build is a windowed program with no console, so everything it has to say goes to a log file, and the
web port is chosen at start (80 if it is free, else 8080 and so on) and written down so the shortcut can find it.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import shutil
import socket
import sys
from pathlib import Path

PORTS = (80, 8080, 8888, 8000)


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path:
    """Where the program's own files (models, templates) are: the unpacked bundle when frozen."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))


def install_dir() -> Path:
    return Path(sys.executable).parent if frozen() else Path(__file__).resolve().parent.parent


def port_free(port: int, host: str = "0.0.0.0") -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def choose_port(preferred=PORTS, wanted: int | None = None) -> int:
    """The first port that is free. `wanted` (from the environment) is tried first and never silently replaced."""
    if wanted:
        return wanted
    for p in preferred:
        if port_free(p):
            return p
    raise OSError("No web port is free (tried " + ", ".join(map(str, preferred)) + ").")


def prepare_models(data_dir: Path) -> Path | None:
    """The plate models are looked for under the user's home folder. Point 'home' at a folder we own and,
    on first run, fill it from the copy bundled inside the program, so nothing is ever downloaded."""
    src = bundle_dir() / "models_home"
    if not src.is_dir():
        return None
    home = data_dir / "home"
    if not (home / ".cache").is_dir():
        shutil.copytree(src / ".cache", home / ".cache", dirs_exist_ok=True)
    os.environ["HOME"] = os.environ["USERPROFILE"] = str(home)
    return home


def setup_logging(data_dir: Path) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    if frozen() or os.environ.get("PLATEBOX_LOG_FILE"):
        d = data_dir / "logs"
        d.mkdir(parents=True, exist_ok=True)
        h: logging.Handler = logging.handlers.RotatingFileHandler(d / "platebox.log", maxBytes=5_000_000, backupCount=8,
                                                                  encoding="utf-8")
    else:
        h = logging.StreamHandler()
    h.setFormatter(fmt)
    root.handlers[:] = [h]


def listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def open_browser(data_dir: Path, wait_s: float = 40.0) -> int:
    """Open the page. Right after installation or a restart the program may still be starting: wait for it."""
    import time
    import webbrowser

    port_file = data_dir / "public" / "port.txt"      # the one place ordinary users may read (the rest is locked down)
    port = None
    end = time.time() + wait_s
    while True:
        try:
            port = int(port_file.read_text().strip())
            if listening(port):
                break
        except (OSError, ValueError):
            pass
        if time.time() >= end:
            print("PlateBox is not running yet. Wait a minute, or restart the computer.", file=sys.stderr)
            return 1
        time.sleep(1)
    webbrowser.open("http://localhost" + ("" if port == 80 else f":{port}"))
    return 0


_LOCK = None


def single_instance(port: int = 50321) -> bool:
    """True if this is the only copy running. The lock is a local port held for the life of the process, which the
    system releases by itself if the program dies (a lock file would be left behind)."""
    global _LOCK
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        s.listen(1)
    except OSError:
        s.close()
        return False
    _LOCK = s
    return True


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    # A windowed program has no console: give the libraries something to write to.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")

    from .config import Config

    cfg = Config.from_env()
    if "--open" in argv:
        return open_browser(cfg.data_dir)

    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(cfg.data_dir)
    log = logging.getLogger("platebox")
    if not single_instance(int(os.environ.get("PLATEBOX_LOCK_PORT", "50321"))):
        log.warning("PlateBox is already running; this copy will not start.")
        return 0

    prepare_models(cfg.data_dir)
    ffmpeg = install_dir() / "ffmpeg" / "ffmpeg.exe"
    if frozen() and ffmpeg.is_file() and not os.environ.get("PLATEBOX_FFMPEG"):
        cfg.ffmpeg_path = str(ffmpeg)

    try:
        cfg.http_port = choose_port(wanted=int(os.environ["PLATEBOX_PORT"]) if os.environ.get("PLATEBOX_PORT") else None)
    except OSError as e:
        log.error("%s", e)
        return 2
    (cfg.data_dir / "public").mkdir(parents=True, exist_ok=True)
    (cfg.data_dir / "public" / "port.txt").write_text(str(cfg.http_port))

    import uvicorn

    from .web import create_app

    log.info("PlateBox starting: web port %d, camera receiver port %d (%s)", cfg.http_port, cfg.sftp_port, cfg.sftp_mode)
    # On a server the page is reached through an HTTPS front (Caddy, nginx). Believe the visitor's address it passes on,
    # so that the wrong-password slow-down counts per visitor and not once for everybody. Only that front may be believed:
    # PLATEBOX_TRUSTED_PROXY names it (an address, or "*" when the web port is not reachable from anywhere else).
    trusted = os.environ.get("PLATEBOX_TRUSTED_PROXY", "").strip()
    uvicorn.run(create_app(cfg), host=cfg.http_host, port=cfg.http_port, log_config=None, access_log=False,
                proxy_headers=bool(cfg.server_mode and trusted), forwarded_allow_ips=trusted or None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
