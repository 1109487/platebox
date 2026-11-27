"""The web interface: a first-run wizard, then one screen of plates and a settings page.

One administrator. Everything except /login, /setup (until the box is claimed) and /healthz
needs that administrator's session.
"""
from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
import secrets
import subprocess
import threading
import time
from dataclasses import asdict
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from . import __version__
from .addrguard import AddressGuard
from .auth import MIN_PASSWORD, Signer, Throttle, hash_password, new_code, password_problem, verify_password
from .config import Config
from .db import Db, now_iso
from .discovery import find_cameras
from .intake import Intake
from .live import LiveView
from .offline_helper import OfflineHelper
from .motionlog import MAX_MB as MOTION_MAX_MB, MIN_MB as MOTION_MIN_MB, MotionLog
from .reader import AlprReader, Reader
from .sftp_server import BuiltinSftp
from .sightings import Sightings
from .system import NetWatch, Sftp, address_for_camera, make_net
from .watcher import Watcher

log = logging.getLogger("platebox.web")
HERE = Path(__file__).parent
COOKIE = "pb_session"
STEPS = ("network", "camera", "configure", "done")


class Box:
    """Everything the web layer needs, built once."""

    def __init__(self, cfg: Config, reader: Reader | None = None, net=None) -> None:
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        cfg.intake_dir.mkdir(parents=True, exist_ok=True)
        cfg.media_dir.mkdir(parents=True, exist_ok=True)
        self.cfg = cfg
        self.db = Db(cfg.db_path)
        self.net = net or make_net(cfg)
        self.sftp = Sftp(cfg, self.db)
        self.receiver = BuiltinSftp(cfg, self.db) if cfg.sftp_mode == "builtin" else None
        self.reader: Reader = reader or AlprReader(cfg.detector, cfg.ocr)
        self.sightings = Sightings(cfg, self.db)
        self.motion = MotionLog(cfg, self.db)
        self.intake = Intake(cfg, self.db, self.reader, self.sightings, self.motion)
        self.netwatch = NetWatch(self.net)
        self.live = LiveView(cfg, self.db)
        self.watcher = Watcher(cfg, self.db, self.live, self.reader, self.sightings, self.motion)
        self.helper = OfflineHelper(cfg, self.db)
        self.intake.clock.synced_from_us = lambda: self.helper.heard("ntp", 3600)
        self.guard = AddressGuard(cfg, self.db, self.net, self.receiver, lambda: self.done, self.camera_ip)
        self.intake.skip_uploads = lambda: self.db.get("watch_ignore_uploads", "1") == "1" and self.watcher.healthy()
        self.guard.pulling = self.watcher.healthy      # pictures coming from the video itself need no upload address
        self.throttle = Throttle()
        # The camera's own "Test" button writes into a folder named FTP_TEST. That is not a camera.
        self.db.run("DELETE FROM cameras WHERE serial = 'FTP_TEST' AND uploads = 0")
        self.search_job: dict = {"running": False, "results": [], "at": None, "error": None}
        self.signer = Signer(self._secret())
        # The camera's password exists and the operating-system account really has it: re-applied
        # at every start, so a replaced account (a re-imaged card, a recreated container) heals itself.
        self.sftp.apply(self.sftp.password())

    def _secret(self) -> bytes:
        s = self.db.get("secret")
        if not s:
            s = secrets.token_hex(32)
            self.db.put("secret", s)
        return bytes.fromhex(s)

    # -- state ---------------------------------------------------------------------------
    @property
    def claimed(self) -> bool:
        return self.db.get("admin_hash") is not None

    @property
    def step(self) -> str:
        step = self.db.get("setup_step", "network") or "network"
        # On a server there is no network to join and no camera to search for: those two steps do not exist.
        if self.cfg.server_mode and step in ("network", "camera"):
            return "configure"
        return step

    @property
    def done(self) -> bool:
        return self.step == "done"

    def setup_code(self) -> str:
        f = self.cfg.setup_code_file
        if f.exists() and f.read_text().strip():
            return f.read_text().strip()
        code = new_code()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(code + "\n")
        try:
            f.chmod(0o600)
        except OSError:
            pass
        return code

    def camera_ip(self) -> str | None:
        return self.db.get("camera_ip")

    def camera_status(self) -> dict:
        cam = self.db.one("SELECT * FROM cameras ORDER BY last_upload IS NULL, last_upload DESC, id LIMIT 1")
        ip_now = address_for_camera(self.net, self.camera_ip(), self.cfg.advertise_ip)
        ip_told = self.db.get("ip_told")
        kept = (self.db.get("addr_claim") or "").split("|")[0]
        return {
            "camera_connected": cam is not None or bool(self.receiver and self.receiver.recent_login()),
            "picture_received": bool(cam and cam["uploads"] > 0),
            "serial": cam["serial"] if cam else None,
            "last_upload": cam["last_upload"] if cam else None,
            "uploads": cam["uploads"] if cam else 0,
            "ip_now": ip_now,
            "ip_told": ip_told,
            "ip_changed": bool(not self.cfg.server_mode and ip_told and ip_now and ip_told != ip_now and ip_told != kept),
            "camera_ip": self.camera_ip(),
            "camera_label": self.db.get("camera_label"),
        }

    # -- background ----------------------------------------------------------------------
    def start(self) -> None:
        if not self.cfg.background:
            return
        if self.receiver:
            self.receiver.start()
        if self.cfg.offline_helper:
            self.helper.start()
        self.intake.start()
        if not self.cfg.server_mode:          # these three manage or reach into the camera's own network
            self.netwatch.start()
            self.guard.start()
            self.watcher.start()
        if isinstance(self.reader, AlprReader):
            threading.Thread(target=self.reader.load, name="reader-warm", daemon=True).start()

    def stop(self) -> None:
        self.intake.stop()
        self.netwatch.stop()
        self.guard.stop()
        self.watcher.stop()
        self.live.stop()
        if self.receiver:
            self.receiver.stop()
        self.helper.stop()

    def run_search(self) -> None:
        if self.search_job["running"]:
            return
        self.search_job.update(running=True, results=[], error=None)

        def work() -> None:
            try:
                self.search_job["results"] = [c.as_dict() for c in find_cameras()]
            except Exception as e:     # pragma: no cover - network dependent
                log.exception("camera search failed")
                self.search_job["error"] = str(e)
            finally:
                self.search_job.update(running=False, at=now_iso())

        threading.Thread(target=work, name="camera-search", daemon=True).start()


async def mjpeg_body(request, sub):
    """The live picture stream: one multipart part per picture.

    Every second it checks that the viewer is still there and, when they are not (tab closed,
    network dropped), gives their slot back, so a closed tab can never keep ffmpeg running.
    """
    try:
        while not await request.is_disconnected():
            frame = await run_in_threadpool(sub.poll, 1.0)
            if frame is not None:
                yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(frame) + frame + b"\r\n"
            elif sub.expired:
                break
    finally:
        sub.close()


def create_app(cfg: Config | None = None, *, reader: Reader | None = None, net=None) -> FastAPI:
    cfg = cfg or Config.from_env()
    box = Box(cfg, reader, net)
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if not box.claimed:
            log.warning("PlateBox is not set up yet. Setup code: %s", box.setup_code())
        box.start()
        try:
            yield
        finally:
            box.stop()

    app = FastAPI(title="PlateBox", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.box = box
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.globals["version"] = __version__
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    # -- guards --------------------------------------------------------------------------
    def logged_in(request: Request) -> bool:
        return box.signer.verify(request.cookies.get(COOKIE)) == "admin"

    def page_admin(request: Request) -> None:
        if not box.claimed:
            raise HTTPException(status_code=303, headers={"Location": "/setup"})
        if not logged_in(request):
            raise HTTPException(status_code=303, headers={"Location": "/login"})

    def api_admin(request: Request) -> None:
        if not logged_in(request):
            raise HTTPException(status_code=401, detail="Sign in first.")

    def same_origin(request: Request) -> None:
        """Belt and braces on top of SameSite=Strict: a form post must come from this site."""
        origin = request.headers.get("origin")
        if origin and request.headers.get("host") and origin.split("://", 1)[-1] != request.headers["host"]:
            raise HTTPException(status_code=403, detail="Cross-site request refused.")

    def need_done(request: Request) -> None:
        page_admin(request)
        if not box.done:
            raise HTTPException(status_code=303, headers={"Location": "/setup"})

    def login_response(target: str) -> RedirectResponse:
        r = RedirectResponse(target, status_code=303)
        r.set_cookie(COOKIE, box.signer.sign("admin", cfg.session_hours * 3600), max_age=cfg.session_hours * 3600,
                     httponly=True, samesite="strict", secure=cfg.cookie_secure, path="/")
        return r

    def render(request: Request, name: str, **ctx) -> HTMLResponse:
        nav = logged_in(request) and box.done
        guard = box.guard.status() if nav and not cfg.server_mode else None
        hint = bool(nav and not cfg.server_mode and box.camera_ip() and box.live.credentials() is None and box.db.get("watch_enabled", "1") != "0")
        return templates.TemplateResponse(request, name, {"box": box, "cfg": cfg, "nav": nav, "guard": guard, "watch_hint": hint, **ctx})

    @app.exception_handler(HTTPException)
    async def _http_exc(request: Request, exc: HTTPException):
        if exc.status_code == 303:
            return RedirectResponse(exc.headers["Location"], status_code=303)
        if request.url.path.startswith("/api/"):
            return JSONResponse({"ok": False, "error": exc.detail}, status_code=exc.status_code)
        return PlainTextResponse(str(exc.detail), status_code=exc.status_code)

    # -- sign in -------------------------------------------------------------------------
    @app.get("/healthz")
    def healthz():
        return {"ok": True, "version": __version__, "claimed": box.claimed, "setup_done": box.done}

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        if not box.claimed:
            return RedirectResponse("/setup", status_code=303)
        if logged_in(request):
            return RedirectResponse("/", status_code=303)
        return render(request, "login.html", error=None)

    @app.post("/login", dependencies=[Depends(same_origin)])
    def login(request: Request, password: str = Form("")):
        key = request.client.host if request.client else "?"
        wait = box.throttle.blocked(key)
        if wait:
            return render(request, "login.html", error=f"Too many wrong passwords. Try again in {wait} seconds.")
        stored = box.db.get("admin_hash") or ""
        if stored and verify_password(password, stored):
            box.throttle.ok(key)
            return login_response("/")
        box.throttle.fail(key)
        return render(request, "login.html", error="That password is not right.")

    @app.post("/logout", dependencies=[Depends(same_origin)])
    def logout():
        r = RedirectResponse("/login", status_code=303)
        r.delete_cookie(COOKIE, path="/")
        return r

    # -- setup wizard ----------------------------------------------------------------------
    @app.get("/setup", response_class=HTMLResponse)
    def setup(request: Request):
        if box.claimed and not logged_in(request):
            return RedirectResponse("/login", status_code=303)
        if box.claimed and box.done:
            return RedirectResponse("/", status_code=303)
        return render(request, "setup.html", claimed=box.claimed, step=box.step if box.claimed else "claim",
                      error=None, min_password=MIN_PASSWORD)

    @app.post("/setup/claim", dependencies=[Depends(same_origin)])
    def claim(request: Request, code: str = Form(""), password: str = Form(""), confirm: str = Form("")):
        def fail(msg: str):
            return render(request, "setup.html", claimed=False, step="claim", error=msg, min_password=MIN_PASSWORD)

        if box.claimed:
            return RedirectResponse("/login", status_code=303)
        key = "claim:" + (request.client.host if request.client else "?")
        wait = box.throttle.blocked(key)
        if wait:
            return fail(f"Too many tries. Wait {wait} seconds.")
        if not hmac.compare_digest(code.strip(), box.setup_code()):
            box.throttle.fail(key)
            return fail("That setup code is not right. It is on the label on the box, or in the file platebox-setup-code.txt.")
        if password != confirm:
            return fail("The two passwords are different.")
        problem = password_problem(password)
        if problem:
            return fail(problem)
        box.db.put("admin_hash", hash_password(password))
        box.db.put("setup_step", "configure" if cfg.server_mode else "network")
        box.db.event("claimed")
        box.throttle.ok(key)
        return login_response("/setup")

    @app.post("/api/setup/step", dependencies=[Depends(api_admin), Depends(same_origin)])
    async def set_step(request: Request):
        body = await request.json()
        step = body.get("step")
        if step not in ("network", "camera", "configure"):
            raise HTTPException(400, "Unknown step.")
        box.db.put("setup_step", step)
        return {"ok": True, "step": step}

    @app.get("/api/setup/status", dependencies=[Depends(api_admin)])
    def setup_status():
        st = box.camera_status()
        if not box.done and box.step == "configure" and st["ip_now"]:
            box.db.put("ip_told", st["ip_now"])        # what the screen is showing right now
            st["ip_told"] = st["ip_now"]
        net = box.net.status()
        server = box.sftp.camera_settings(st["ip_now"] or "") if st["ip_now"] else None
        receiver = ({"listening": box.receiver.listening, "error": box.receiver.error,
                     "events": box.receiver.recent_events(),
                     "diagnosis": box.receiver.diagnosis(st["ip_now"] or "")} if box.receiver else None)
        return {"ok": True, "step": box.step, "net": asdict(net), "camera": st, "sftp": server, "receiver": receiver}

    @app.post("/api/setup/finish", dependencies=[Depends(api_admin), Depends(same_origin)])
    async def finish(request: Request):
        body = await request.json() if (await request.body()) else {}
        st = box.camera_status()
        if not st["picture_received"] and not body.get("force"):
            raise HTTPException(409, "No picture has arrived from the camera yet.")
        if st["ip_now"]:
            box.db.put("ip_told", st["ip_now"])
        box.db.put("setup_step", "done")
        box.db.event("setup_done")
        return {"ok": True}

    # -- network -------------------------------------------------------------------------
    @app.get("/api/net/status", dependencies=[Depends(api_admin)])
    def net_status():
        return {"ok": True, **asdict(box.net.status())}

    @app.get("/api/net/scan", dependencies=[Depends(api_admin)])
    def net_scan():
        return {"ok": True, "networks": [asdict(n) | {"secured": n.secured} for n in box.net.scan()]}

    @app.post("/api/net/connect", dependencies=[Depends(api_admin), Depends(same_origin)])
    async def net_connect(request: Request):
        body = await request.json()
        ssid = str(body.get("ssid", "")).strip()
        if not ssid:
            raise HTTPException(400, "Choose a network.")
        ok, msg = box.net.connect(ssid, body.get("password") or None)
        if ok:
            box.db.event("wifi_joined", ssid)
        return {"ok": ok, "message": msg, "net": asdict(box.net.status())}

    # -- camera --------------------------------------------------------------------------
    @app.post("/api/camera/search", dependencies=[Depends(api_admin), Depends(same_origin)])
    def camera_search():
        box.run_search()
        return {"ok": True, **box.search_job}

    @app.get("/api/camera/search", dependencies=[Depends(api_admin)])
    def camera_search_status():
        return {"ok": True, **box.search_job}

    @app.post("/api/camera/select", dependencies=[Depends(api_admin), Depends(same_origin)])
    async def camera_select(request: Request):
        body = await request.json()
        ip = str(body.get("ip", "")).strip()
        parts = ip.split(".")
        if len(parts) != 4 or not all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
            raise HTTPException(400, "That is not an IP address.")
        box.db.put("camera_ip", ip)
        box.db.put("camera_label", str(body.get("label", ""))[:80])
        box.db.put("setup_step", "configure")
        return {"ok": True}

    # -- plates ----------------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(need_done)])
    def plates(request: Request, q: str = ""):
        return render(request, "plates.html", q=q, items=box.sightings.grouped(q), total=box.sightings.count_plates(q),
                      totals=box.sightings.totals(), cam=box.camera_status(), active="plates")

    @app.get("/fragment/plates", response_class=HTMLResponse, dependencies=[Depends(need_done)])
    def plates_fragment(request: Request, q: str = ""):
        return render(request, "_plates.html", q=q, items=box.sightings.grouped(q), total=box.sightings.count_plates(q),
                      totals=box.sightings.totals())

    @app.get("/api/plates/summary", dependencies=[Depends(api_admin)])
    def plates_summary():
        return {"ok": True, **box.sightings.totals(), "camera": box.camera_status()}

    @app.get("/media/{sighting_id}/{kind}.jpg", dependencies=[Depends(api_admin)])
    def media(sighting_id: int, kind: str):
        if kind not in ("crop", "full"):
            raise HTTPException(404, "Not found.")
        p = box.sightings.file_for(sighting_id, "crop" if kind == "crop" else "full")
        if p is None and kind == "crop":
            p = box.sightings.file_for(sighting_id, "full")
        if p is None:
            raise HTTPException(404, "Not found.")
        return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "private, no-cache"})

    @app.post("/plates/delete", dependencies=[Depends(need_done), Depends(same_origin)])
    def delete_plate(plate: str = Form("")):
        box.sightings.delete_plate(plate.strip().upper())
        return RedirectResponse("/", status_code=303)

    @app.get("/export.csv", dependencies=[Depends(need_done)])
    def export():
        return PlainTextResponse(box.sightings.export_csv(), media_type="text/csv",
                                 headers={"Content-Disposition": 'attachment; filename="plates.csv"'})

    # -- motion log -------------------------------------------------------------------------
    PAGE = 60

    def motion_ctx(plates: str, page: int) -> dict:
        po, page = plates == "1", max(0, page)
        return dict(items=box.motion.list(PAGE, page * PAGE, po), stats=box.motion.stats(), plates_only=po,
                    total=box.motion.count(po), page=page, pages=max(1, -(-box.motion.count(po) // PAGE)))

    @app.get("/motion", response_class=HTMLResponse, dependencies=[Depends(need_done)])
    def motion_page(request: Request, plates: str = "", page: int = 0):
        return render(request, "motion.html", active="motion", **motion_ctx(plates, page))

    @app.get("/fragment/motion", response_class=HTMLResponse, dependencies=[Depends(need_done)])
    def motion_fragment(request: Request, plates: str = "", page: int = 0):
        return render(request, "_motion.html", **motion_ctx(plates, page))

    @app.get("/api/motion/summary", dependencies=[Depends(api_admin)])
    def motion_summary():
        return {"ok": True, **box.motion.stats()}

    @app.get("/motion/{event_id}.jpg", dependencies=[Depends(api_admin)])
    def motion_image(event_id: int):
        p = box.motion.file_for(event_id)
        if p is None:
            raise HTTPException(404, "Not found.")
        return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "private, no-cache"})

    @app.post("/settings/motion", dependencies=[Depends(need_done), Depends(same_origin)])
    def change_motion(request: Request, enabled: str = Form(""), max_mb: int = Form(...), interval_s: int = Form(...)):
        try:
            box.motion.configure(bool(enabled), max_mb, interval_s)
        except ValueError as e:
            return render(request, "settings.html", **settings_ctx(request, error=str(e)))
        return render(request, "settings.html", **settings_ctx(request, saved="Saved."))

    @app.post("/motion/clear", dependencies=[Depends(need_done), Depends(same_origin)])
    def motion_clear(request: Request):
        n = box.motion.clear()
        return render(request, "settings.html", **settings_ctx(request, saved=f"Deleted {n} motion pictures."))

    # -- live view (an add-on: see live.py) ----------------------------------------------------
    @app.get("/live", response_class=HTMLResponse, dependencies=[Depends(need_done)])
    def live_page(request: Request, saved: str = ""):
        if cfg.server_mode:                 # the camera is behind the site's 4G connection: its video cannot be pulled from here
            return RedirectResponse("/", status_code=303)
        cam = box.camera_status()
        return render(request, "live.html", active="live", live=box.live.status(), live_user=box.live.user(),
                      camera_ip=cam["camera_ip"], saved=saved, msg=None)

    @app.get("/live/stream.mjpg", dependencies=[Depends(api_admin)])
    def live_stream(request: Request):
        sub = box.live.subscribe()
        if sub is None:
            raise HTTPException(503, "Live view is off, full, or not set up.")

        return StreamingResponse(mjpeg_body(request, sub), media_type="multipart/x-mixed-replace; boundary=frame",
                                 headers={"Cache-Control": "no-store"})

    @app.get("/api/live/status", dependencies=[Depends(api_admin)])
    def live_status():
        return {"ok": True, **box.live.status()}

    @app.post("/live/settings", dependencies=[Depends(need_done), Depends(same_origin)])
    def live_settings(request: Request, username: str = Form(""), password: str = Form(""), enabled: str = Form("")):
        if not box.camera_ip():
            return render(request, "live.html", active="live", live=box.live.status(), live_user=box.live.user(),
                          camera_ip=None, saved="", msg="Choose the camera first (Settings, then Find or change the camera).")
        if not username.strip() and not box.live.user():
            return render(request, "live.html", active="live", live=box.live.status(), live_user="",
                          camera_ip=box.camera_ip(), saved="", msg="Enter the camera's user name.")
        if not password and not box.live.credentials():
            return render(request, "live.html", active="live", live=box.live.status(), live_user=username,
                          camera_ip=box.camera_ip(), saved="", msg="Enter the camera's password.")
        box.live.save(username, password, bool(enabled))
        return RedirectResponse("/live?saved=1", status_code=303)

    @app.post("/api/live/test", dependencies=[Depends(api_admin), Depends(same_origin)])
    def live_test():
        ok, msg = box.live.test()
        return {"ok": ok, "message": msg}

    @app.post("/live/forget", dependencies=[Depends(need_done), Depends(same_origin)])
    def live_forget():
        box.live.forget()
        return RedirectResponse("/live", status_code=303)

    # -- settings --------------------------------------------------------------------------
    def settings_ctx(request: Request, **extra):
        cam = box.camera_status()
        server = box.sftp.camera_settings(cam["ip_now"]) if cam["ip_now"] else None
        ctx = dict(cam=cam, sftp=server, net=box.net.status(), active="settings", saved=None, error=None,
                   reading={"min_confidence": box.sightings.min_confidence(),
                            "dedupe_seconds": box.sightings.dedupe_seconds(),
                            "retention_days": box.sightings.retention_days(),
                            "strict_format": box.sightings.strict_format(),
                            "stale_minutes": int(box.db.get("stale_minutes", str(cfg.stale_minutes)) or 0),
                            "stale_ignored": int(box.db.get("stale_ignored", "0") or 0),
                            "repeat_ignored": int(box.db.get("repeat_ignored", "0") or 0),
                            "clock_note": box.intake.clock.describe(),
                            "invalid_count": box.sightings.count_invalid()},
                   recv=({"diagnosis": box.receiver.diagnosis(cam["ip_now"] or ""), "events": box.receiver.recent_events(10),
                          "listening": box.receiver.listening} if box.receiver else None),
                   offline=box.helper.status(), offline_ip=cam["ip_now"],
                   watch=dict(zip(("level", "text"), box.watcher.describe())), watch_on=box.db.get("watch_enabled", "1") != "0",
                   watch_user=box.live.user(), watch_sensitivity=box.watcher.sensitivity(),
                   watch_ignore=box.db.get("watch_ignore_uploads", "1") == "1",
                   totals=box.sightings.totals(), motion=box.motion.stats(),
                   motion_limits=(MOTION_MIN_MB, MOTION_MAX_MB))
        ctx.update(extra)
        return ctx

    @app.get("/settings", response_class=HTMLResponse, dependencies=[Depends(need_done)])
    def settings(request: Request):
        return render(request, "settings.html", **settings_ctx(request))

    @app.post("/settings/password", dependencies=[Depends(need_done), Depends(same_origin)])
    def change_password(request: Request, current: str = Form(""), password: str = Form(""), confirm: str = Form("")):
        def back(**kw):
            return render(request, "settings.html", **settings_ctx(request, **kw))

        if not verify_password(current, box.db.get("admin_hash") or ""):
            return back(error="The current password is not right.")
        if password != confirm:
            return back(error="The two new passwords are different.")
        problem = password_problem(password)
        if problem:
            return back(error=problem)
        box.db.put("admin_hash", hash_password(password))
        return back(saved="Password changed.")

    @app.post("/settings/reading", dependencies=[Depends(need_done), Depends(same_origin)])
    def change_reading(request: Request, min_confidence: float = Form(...), dedupe_seconds: int = Form(...),
                       retention_days: int = Form(...), strict_format: str = Form(""), stale_minutes: int = Form(10)):
        if not (0.2 <= min_confidence <= 0.99 and 0 <= dedupe_seconds <= 600 and 0 <= retention_days <= 3650
                and 0 <= stale_minutes <= 1440):
            return render(request, "settings.html", **settings_ctx(request, error="One of those values is out of range."))
        box.db.put("stale_minutes", str(stale_minutes))
        box.db.put("min_confidence", f"{min_confidence:.2f}")
        box.db.put("dedupe_seconds", str(dedupe_seconds))
        box.db.put("retention_days", str(retention_days))
        box.db.put("strict_format", "1" if strict_format else "0")
        return render(request, "settings.html", **settings_ctx(request, saved="Saved."))

    @app.post("/settings/clean-invalid", dependencies=[Depends(need_done), Depends(same_origin)])
    def clean_invalid(request: Request):
        n = box.sightings.delete_invalid()
        word = "entry" if n == 1 else "entries"
        return render(request, "settings.html", **settings_ctx(
            request, saved=f"Removed {n} {word} that did not look like a number plate."))

    @app.get("/api/watch", dependencies=[Depends(api_admin)])
    def watch_status():
        lvl, text = box.watcher.describe()
        return {"ok": True, "level": lvl, "text": text, **box.watcher.status()}

    @app.post("/settings/watch", dependencies=[Depends(need_done), Depends(same_origin)])
    def watch_settings(request: Request, enabled: str = Form(""), sensitivity: float = Form(1.0),
                       username: str = Form(""), password: str = Form(""), ignore_uploads: str = Form("")):
        if not 0.1 <= sensitivity <= 30:
            return render(request, "settings.html", **settings_ctx(request, error="Sensitivity must be between 0.1 and 30."))
        if (username.strip() or password) and not box.camera_ip():
            return render(request, "settings.html", **settings_ctx(request, error="Choose the camera first."))
        box.db.put("watch_enabled", "1" if enabled else "0")
        box.db.put("watch_sensitivity", f"{sensitivity:g}")
        box.db.put("watch_ignore_uploads", "1" if ignore_uploads else "0")
        if username.strip() or password:
            box.live.save(username, password, True)         # the same login serves the Live page and the watcher
        box.watcher.restart()
        return render(request, "settings.html", **settings_ctx(request, saved="Saved. PlateBox is connecting to the camera's video."))

    @app.post("/settings/offline-helper", dependencies=[Depends(need_done), Depends(same_origin)])
    def offline_helper(request: Request, enabled: str = Form("")):
        box.db.put("offline_helper", "1" if enabled else "0")
        return render(request, "settings.html", **settings_ctx(request, saved="Saved."))

    @app.get("/api/address", dependencies=[Depends(api_admin)])
    def address_status():
        return {"ok": True, **box.guard.status()}

    @app.post("/api/address/ack", dependencies=[Depends(api_admin), Depends(same_origin)])
    def address_ack():
        return {"ok": True, **box.guard.acknowledge()}

    @app.post("/api/address/check", dependencies=[Depends(api_admin), Depends(same_origin)])
    def address_check():
        return {"ok": True, **box.guard.tick()}

    @app.post("/settings/address-guard", dependencies=[Depends(need_done), Depends(same_origin)])
    def address_guard_setting(request: Request, enabled: str = Form("")):
        box.db.put("address_keep", "1" if enabled else "0")
        box.guard.tick()
        return render(request, "settings.html", **settings_ctx(request, saved="Saved."))

    @app.post("/settings/camera", dependencies=[Depends(need_done), Depends(same_origin)])
    def rerun_camera():
        if cfg.server_mode:
            return RedirectResponse("/settings", status_code=303)
        box.db.put("setup_step", "camera")
        return RedirectResponse("/setup", status_code=303)

    @app.post("/settings/network", dependencies=[Depends(need_done), Depends(same_origin)])
    def rerun_network():
        if cfg.server_mode:
            return RedirectResponse("/settings", status_code=303)
        box.db.put("setup_step", "network")
        return RedirectResponse("/setup", status_code=303)

    @app.post("/settings/sftp-password", dependencies=[Depends(need_done), Depends(same_origin)])
    def new_sftp_password(request: Request, password: str = Form("")):
        try:
            box.sftp.reset(password.strip())
        except ValueError as e:
            return render(request, "settings.html", **settings_ctx(request, error=str(e)))
        return render(request, "settings.html", **settings_ctx(
            request, saved="New camera password set. Enter it on the camera too (then Save, then test), or pictures will stop."))

    @app.post("/api/receiver/check", dependencies=[Depends(api_admin), Depends(same_origin)])
    def receiver_check():
        if not box.receiver:
            return {"ok": False, "message": "This installation uses the system's own SFTP service, which has no built-in check."}
        # On a server the public name may not be reachable from inside the server itself: test the receiver directly.
        host = "127.0.0.1" if cfg.server_mode else (address_for_camera(box.net, box.camera_ip(), cfg.advertise_ip) or "127.0.0.1")
        ok, msg = box.receiver.self_test(host)
        return {"ok": ok, "message": msg, "diagnosis": box.receiver.diagnosis(host), "events": box.receiver.recent_events()}

    @app.post("/api/receiver/trace", dependencies=[Depends(api_admin), Depends(same_origin)])
    def receiver_trace():
        if not box.receiver:
            return {"ok": False, "message": "This installation uses the system's own SFTP service: no trace is available."}
        box.receiver.trace(120)
        return {"ok": True, "message": "Recording everything the camera asks for, for 2 minutes. Wave in front of the "
                                       "camera now, then download the support file."}

    def support_text() -> str:
        """Everything a person helping needs, and nothing secret: no passwords, no pictures."""
        cam = box.camera_status()
        rows: list[str] = []
        add = rows.append
        add(f"PlateBox {__version__} support file, {time.strftime('%Y-%m-%d %H:%M:%S')} (this computer's clock)")
        add("")
        add("== Camera")
        for k in ("camera_label", "camera_ip", "ip_now", "ip_told", "uploads", "last_upload", "serial"):
            add(f"{k}: {cam.get(k)}")
        add(f"camera_mac: {box.db.get('camera_mac')}")
        add(f"clock: {box.intake.clock.describe()}")
        add("")
        add("== Address guard")
        g = box.guard.status()
        add(f"{g.get('level')} / {g.get('code')}: {g.get('title')} {g.get('text')}")
        add(f"claimed address: {box.db.get('addr_claim')}")
        add("")
        add("== Counters and settings")
        for k in ("stale_minutes", "stale_ignored", "stale_last", "repeat_ignored", "repeat_last", "address_keep",
                  "offline_helper", "min_confidence", "dedupe_seconds", "strict_format", "motion_enabled",
                  "motion_interval_s", "setup_step"):
            add(f"{k}: {box.db.get(k)}")
        add(f"pictures remembered (fingerprints): {box.db.one('SELECT COUNT(*) AS n FROM seen_pictures')['n']}")
        add(f"pictures kept as delivered: {box.db.one('SELECT COUNT(*) AS n FROM delivered')['n']}")
        add("")
        add("== The camera's last 30 files kept (path, bytes, when it arrived)")
        for r in box.db.all("SELECT path, size, mtime FROM delivered ORDER BY t DESC LIMIT 30"):
            add(f"{r['path']}  {r['size']}  {time.strftime('%H:%M:%S', time.localtime(r['mtime']))}")
        add("")
        add("== Motion log (newest 15: arrived, the camera's own time from the file name)")
        for r in box.db.all("SELECT seen_at, cam_time FROM motion_events ORDER BY id DESC LIMIT 15"):
            add(f"{r['seen_at']}  {r['cam_time']}")
        add("")
        if box.receiver:
            add("== Receiver: what the camera did lately (newest first)")
            add(box.receiver.diagnosis(cam["ip_now"] or "")["text"])
            for e in box.receiver.recent_events(40):
                add(f"{e['ago_s']:>5}s ago  {e['ip']}  {e['what']}")
            add(f"detailed trace: {'ON, ' + str(int(box.receiver.tracing())) + ' s left' if box.receiver.tracing() else 'off'}")
            add("")
        add("== Answered by PlateBox")
        add(str(box.helper.status()))
        add("")
        add("== Log (last 400 lines)")
        f = cfg.data_dir / "logs" / "platebox.log"
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]
        except OSError:
            lines = ["(no log file: this run logs to the console)"]
        rows.extend(lines)
        return "\n".join(rows) + "\n"

    @app.get("/settings/support.txt", dependencies=[Depends(need_done)])
    def support_file():
        return PlainTextResponse(support_text(), headers={"Content-Disposition": 'attachment; filename="platebox-support.txt"',
                                                          "Cache-Control": "no-store"})

    @app.post("/settings/clear", dependencies=[Depends(need_done), Depends(same_origin)])
    def clear_all(request: Request):
        n = box.sightings.delete_all()
        return render(request, "settings.html", **settings_ctx(request, saved=f"Deleted {n} sightings and their pictures."))

    def _power(action: str) -> None:
        if cfg.fake_net:
            return
        try:
            subprocess.Popen(["sudo", "-n", "systemctl", action])
        except OSError:
            log.error("could not %s", action)

    @app.post("/settings/reboot", dependencies=[Depends(need_done), Depends(same_origin)])
    def reboot(request: Request):
        if cfg.server_mode:
            raise HTTPException(403, "Not available on a server.")
        _power("reboot")
        return render(request, "settings.html", **settings_ctx(request, saved="Restarting. Give it a minute."))

    @app.post("/settings/poweroff", dependencies=[Depends(need_done), Depends(same_origin)])
    def poweroff(request: Request):
        if cfg.server_mode:
            raise HTTPException(403, "Not available on a server.")
        _power("poweroff")
        return render(request, "settings.html", **settings_ctx(request, saved="Shutting down. Wait for the light to go out before unplugging."))

    return app
