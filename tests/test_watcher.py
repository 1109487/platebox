"""Watching the camera's own video: movement and plates found without the camera pushing anything."""
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import FakeReader, jpeg_bytes, plate
from platebox.addrguard import AddressGuard, NullHostNet
from platebox.db import Db
from platebox.live import LiveView
from platebox.motionlog import MotionLog
from platebox.netmgr import FakeBackend
from platebox.sightings import Sightings
from platebox.watcher import MotionDetector, Watcher

FAKE = str(Path(__file__).parent / "fake_ffmpeg.py")
FFMPEG = shutil.which("ffmpeg") or next(iter(Path(r"C:\ba-build\win-dl").rglob("ffmpeg.exe")), None)


def picture(shade=40, box=None):
    """A JPEG: flat grey, with an optional white block (a vehicle) at box=(x, y, w, h)."""
    import cv2
    import numpy as np

    img = np.full((360, 640, 3), shade, np.uint8)
    if box:
        x, y, w, h = box
        cv2.rectangle(img, (x, y), (x + w, y + h), (240, 240, 240), -1)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return buf.tobytes()


def make(cfg, reader=None, motion=True, creds=True):
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.media_dir.mkdir(parents=True, exist_ok=True)
    db = Db(cfg.db_path)
    db.put("camera_ip", "192.168.1.8")
    if creds:
        db.put("live_user", "viewer"); db.put("live_pass", "pw-pw-pw-pw")
    live = LiveView(cfg, db, spawn=lambda url, one: [sys.executable, FAKE, "stream"])
    s = Sightings(cfg, db)
    m = MotionLog(cfg, db) if motion else None
    if m:
        m.configure(True, 50, 0)
    return db, Watcher(cfg, db, live, reader or FakeReader(), s, m, spawn=lambda url: [sys.executable, FAKE, "stream"]), s, m


def test_movement_is_a_changed_part_of_the_picture_and_light_changes_are_not():
    d = MotionDetector()
    assert d.changed_percent(__import__("cv2").imdecode(__import__("numpy").frombuffer(picture(), "uint8"), 1)) == 0.0
    import cv2
    import numpy as np

    dec = lambda b: cv2.imdecode(np.frombuffer(b, np.uint8), cv2.IMREAD_COLOR)     # noqa: E731
    assert d.changed_percent(dec(picture())) < 0.2                                   # nothing moved
    assert d.changed_percent(dec(picture(box=(150, 100, 200, 120)))) > 5             # a vehicle came into view
    d2 = MotionDetector(); d2.changed_percent(dec(picture(shade=40)))
    assert d2.changed_percent(dec(picture(shade=200))) == 0.0                        # the IR filter switched: not movement


def test_a_vehicle_in_view_is_read_logged_and_remembered(cfg):
    db, w, s, m = make(cfg, FakeReader([[plate("MH12AB1234")]] + [[plate("MH12AB1234")]] * 5))
    t = 1000.0
    assert w.handle(picture(), t) == "still" and w.handle(picture(), t + 1) == "still"        # an empty road: nothing
    assert db.one("SELECT COUNT(*) AS n FROM motion_events")["n"] == 0
    assert w.handle(picture(box=(150, 100, 200, 120)), t + 2) == "moving"
    assert s.grouped()[0]["plate"] == "MH12AB1234"                                            # read from the video
    assert db.one("SELECT COUNT(*) AS n FROM motion_events")["n"] == 1
    cam = db.one("SELECT * FROM cameras")
    assert cam["serial"] == "VIDEO" and cam["uploads"] == 1 and cam["last_upload"]
    assert w.handle(picture(box=(150, 100, 200, 120)), t + 3) == "moving"                     # a vehicle is not one frame
    for i in range(60):                                                                       # the road is empty again: it settles
        last = w.handle(picture(), t + 20 + i)
    assert last == "still"
    assert w.last_motion > 0 and w.status()["motion_frames"] >= 2


def test_it_uses_the_camera_already_known_from_its_uploads(cfg):
    db, w, s, m = make(cfg, FakeReader([[]] * 4))
    now = "2026-10-22T05:00:00Z"
    cid = db.run("INSERT INTO cameras (serial, ip, label, first_seen, last_seen) VALUES ('2KN9B54Y6F9XYK4Y','192.168.1.8','x',?,?)", (now, now))
    w.handle(picture(), 10.0); w.handle(picture(box=(100, 100, 300, 150)), 11.0)
    assert db.one("SELECT COUNT(*) AS n FROM cameras")["n"] == 1 and db.one("SELECT uploads FROM cameras WHERE id = ?", (cid,))["uploads"] == 1


def test_what_it_says_in_each_situation(cfg):
    db, w, s, m = make(cfg, creds=False)
    assert w.describe()[0] == "wait" and "user name and password" in w.describe()[1]
    db.put("live_user", "viewer"); db.put("live_pass", "pw-pw-pw-pw")
    assert w.enabled() and w.describe()[0] == "bad"                                  # asked to watch, not connected yet
    w.last_frame = time.time()
    assert w.healthy() and w.describe()[0] == "ok" and "Watching the camera directly" in w.describe()[1]
    db.put("watch_enabled", "0")
    assert not w.enabled() and w.describe()[0] == "off"


def test_a_refused_login_is_said_in_plain_words(cfg):
    db, w, s, m = make(cfg)
    w._spawn_args = lambda url: [sys.executable, FAKE, "unauthorized"]
    assert w._session() is False
    assert "did not accept that user name and password" in w.error and "pw-pw-pw-pw" not in w.error


def test_it_keeps_up_by_looking_only_at_the_newest_picture(cfg):
    db, w, s, m = make(cfg, FakeReader([[]] * 400))
    w._spawn_args = lambda url: [sys.executable, FAKE, "burst"]
    stopper = threading.Timer(1.5, w.stop); stopper.start()
    w._session()
    assert w.last_frame > 0                                          # pictures arrived and the session ended cleanly


@pytest.mark.skipif(FFMPEG is None, reason="needs ffmpeg")
def test_real_ffmpeg_on_a_real_video_finds_the_vehicle_that_drives_in(cfg, tmp_path):
    """End to end with the real ffmpeg: an empty scene for 3 seconds, then a vehicle-sized block appears."""
    video = tmp_path / "road.mp4"
    subprocess.run([str(FFMPEG), "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=0x303030:s=640x360:r=10:d=9",
                    "-vf", "drawbox=x=150:y=100:w=240:h=140:color=white:t=fill:enable='gte(t,3)'", "-pix_fmt", "yuv420p",
                    str(video)], check=True, timeout=60)
    db, w, s, m = make(cfg, FakeReader([[plate("KA01AB1234")]] + [[]] * 200))
    w._spawn_args = lambda url: [str(FFMPEG), "-nostdin", "-loglevel", "error", "-re", "-i", str(video), "-an", "-vf",
                                 "fps=5", "-q:v", "3", "-f", "image2pipe", "-c:v", "mjpeg", "pipe:1"]
    w._session()
    assert w.frames >= 20 and w.motion_frames >= 1
    assert db.one("SELECT COUNT(*) AS n FROM motion_events")["n"] >= 1
    assert s.grouped() and s.grouped()[0]["plate"] == "KA01AB1234"


# -- the web side ---------------------------------------------------------------------------------------------
def done_client(make_app, cfg):
    cfg.advertise_ip = "192.168.1.2"
    c = TestClient(make_app(), follow_redirects=False)
    c.__enter__()
    box = c.app.state.box
    c.post("/setup/claim", data={"code": box.setup_code(), "password": "a long password", "confirm": "a long password"})
    c.post("/api/camera/select", json={"ip": "192.168.1.8", "label": "x"})
    c.post("/api/setup/finish", json={"force": True})
    return c, box


def test_the_settings_page_asks_for_the_login_and_saves_it(make_app, cfg):
    c, box = done_client(make_app, cfg)
    try:
        page = c.get("/settings")
        assert "Watching the camera directly" in page.text and "Camera user name" in page.text
        r = c.post("/settings/watch", data={"enabled": "1", "sensitivity": "1.5", "username": "viewer", "password": "secret-pw-1"})
        assert "Saved" in r.text
        assert box.live.credentials() == ("viewer", "secret-pw-1") and box.db.get("watch_sensitivity") == "1.5"
        assert "secret-pw-1" not in c.get("/settings").text                      # never shown again
        assert "Saved" not in c.post("/settings/watch", data={"sensitivity": "99"}).text      # out of range
        assert c.post("/settings/watch", data={"enabled": "1", "sensitivity": "1"}, headers={"origin": "http://evil.example"}).status_code == 403
        j = c.get("/api/watch").json()
        assert j["ok"] and j["enabled"] is True and "level" in j
        box.db.put("watch_enabled", "0")
    finally:
        c.__exit__(None, None, None)


def test_the_upload_address_warning_is_quiet_while_the_video_itself_is_being_watched(cfg):
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.advertise_ip = "192.168.1.2"
    db = Db(cfg.db_path)
    db.put("camera_ip", "192.168.1.8"); db.put("ip_told", "192.168.1.4")
    g = AddressGuard(cfg, db, FakeBackend("192.168.1.2"), None, lambda: True, lambda: "192.168.1.8", host=NullHostNet())
    assert g.tick()["level"] == "bad"                                       # the camera is sending to the wrong place
    g.pulling = lambda: True
    assert g.tick()["level"] == "ok"                                        # but pictures come from the video: no alarm


def test_while_the_video_is_watched_the_cameras_own_uploads_are_set_aside(cfg):
    from platebox.intake import Intake
    from test_units import drop

    cfg.media_dir.mkdir(parents=True, exist_ok=True)
    db = Db(cfg.db_path); s = Sightings(cfg, db)
    reader = FakeReader([[plate("MH12AB1234")]] * 3)
    it = Intake(cfg, db, reader, s)
    it.skip_uploads = lambda: True
    f = drop(cfg)
    st = it.scan_once()
    assert st["skipped"] == 1 and st["read"] == 0 and reader.calls == 0 and s.grouped() == []
    assert f.exists()                                                  # still kept, so the camera sees it as delivered
    it.skip_uploads = lambda: False
    drop(cfg, name="14.20.14[M][0@0][0].jpg")
    assert it.scan_once()["read"] == 1 and s.grouped()[0]["plate"] == "MH12AB1234"          # and read again when it is not


def test_a_gentle_hint_asks_for_the_camera_login_until_it_is_given(make_app, cfg):
    c, box = done_client(make_app, cfg)
    try:
        assert "Let PlateBox watch the camera's video itself" in c.get("/").text
        c.post("/settings/watch", data={"enabled": "1", "sensitivity": "1", "username": "viewer", "password": "pw-pw-pw-pw"})
        assert "Let PlateBox watch the camera's video itself" not in c.get("/").text
    finally:
        c.__exit__(None, None, None)


def test_a_reused_row_number_can_never_show_an_old_picture(make_app, cfg):
    """After a database reset the picture numbers start again at 1. The browser used to keep picture 1 for 24 hours
    and show the OLD picture 1 for the NEW one. The address now carries a token unique to each picture, and the
    browser is told to check each time."""
    import re
    import cv2
    import numpy as np

    c, box = done_client(make_app, cfg)
    try:
        cam = box.db.run("INSERT INTO cameras (serial, first_seen, last_seen) VALUES ('X1234', 'a', 'a')")
        box.motion.configure(True, 50, 0)
        red = np.zeros((90, 160, 3), np.uint8); red[:] = (0, 0, 255)
        blue = np.zeros((90, 160, 3), np.uint8); blue[:] = (255, 0, 0)
        box.motion.record(cam, red, "2026-10-21 19:30:00", [])
        first = re.search(r'src="(/motion/\d+\.jpg\?v=[0-9a-f]+)"', c.get("/motion").text).group(1)
        r1 = c.get(first)
        assert r1.status_code == 200 and "no-cache" in r1.headers["cache-control"] and "max-age" not in r1.headers["cache-control"]

        box.motion.clear()                                                   # the reset: reinstall, new database, clean-up...
        box.motion._last_saved.clear()                                      # (numbering starts again from 1)
        box.motion.record(cam, blue, "2026-10-22 12:00:00", [])
        second = re.search(r'src="(/motion/\d+\.jpg\?v=[0-9a-f]+)"', c.get("/motion").text).group(1)

        assert second.split("?")[0] == first.split("?")[0]                    # the SAME row number...
        assert second != first                                                # ...but not the same address
        img = cv2.imdecode(np.frombuffer(c.get(second).content, np.uint8), cv2.IMREAD_COLOR)
        assert img[10, 10][0] > 200 and img[10, 10][2] < 60                   # and it really is the new (blue) picture
    finally:
        c.__exit__(None, None, None)
