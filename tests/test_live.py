import os
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from platebox import live
from platebox.db import Db
from platebox.live import FrameSplitter, LiveView, explain, redact, rtsp_url

from conftest import jpeg_bytes, plate

FAKE = str(Path(__file__).parent / "fake_ffmpeg.py")


def make_view(cfg, mode="stream", enabled=True, spawned=None):
    db = Db(cfg.db_path)
    db.put("camera_ip", "192.168.1.64")
    db.put("live_user", "viewer")
    db.put("live_pass", "s3cret/pa:ss")
    db.put("live_enabled", "1" if enabled else "0")

    def spawn(url, one_frame):
        if spawned is not None:
            spawned.append(url)
        return [sys.executable, FAKE, "one" if one_frame and mode == "stream" else mode]

    return LiveView(cfg, db, spawn=spawn)


def wait(cond, secs=4.0):
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_url_is_quoted_and_secrets_are_redacted():
    u = rtsp_url("10.0.0.5", "us er", "p@ss:w/rd")
    assert u == "rtsps://us%20er:p%40ss%3Aw%2Frd@10.0.0.5:554/video/live?channel=1&subtype=1"
    assert "p@ss" not in redact(u + " p@ss:w/rd", "p@ss:w/rd")


def test_the_certificate_flag_is_added_only_where_ffmpeg_checks_by_default(monkeypatch):
    class Out:
        def __init__(self, text):
            self.stdout, self.stderr = text, ""

    windows = "  -tls_verify        <boolean>    ED......... Verify the peer certificate (default true)"
    debian = "  -tls_verify        <int>        ED......... Verify the peer certificate (from 0 to 1) (default 0)"
    for name, text, want in (("win", windows, ["-tls_verify", "0"]), ("deb", debian, [])):
        live._tls_cache.clear()
        monkeypatch.setattr(live.subprocess, "run", lambda *a, _t=text, **k: Out(_t))
        assert live.tls_flags(name) == want
    args = live.ffmpeg_args("ffmpeg", "rtsps://x", 8, 960, pre=["-tls_verify", "0"])
    assert args.index("-tls_verify") < args.index("-i")                  # an input option, so before -i
    live._tls_cache.clear()


def test_splitter_handles_any_chunking():
    f1 = b"\xff\xd8" + b"a" * 50 + b"\xff\xd9"
    f2 = b"\xff\xd8" + b"b" * 7 + b"\xff\xd9"
    data = b"junk" + f1 + f2 + b"tail"
    for n in (1, 3, 11, 1000):
        sp, got = FrameSplitter(), []
        for i in range(0, len(data), n):
            got += sp.feed(data[i:i + n])
        assert got == [f1, f2], n


def test_explaining_ffmpeg_errors():
    assert "user name and password" in explain("method DESCRIBE failed: 401 Unauthorized")
    assert "cannot be reached" in explain("Connection refused")
    assert "could not be opened" in explain("something odd")


def test_off_until_configured_and_enabled(cfg):
    lv = make_view(cfg, enabled=False)
    assert lv.subscribe() is None and not lv.enabled()
    lv.save("viewer", "", True)                       # blank password keeps the stored one
    assert lv.enabled() and lv.credentials()[1] == "s3cret/pa:ss"
    lv.forget()
    assert not lv.configured() and lv.subscribe() is None


def test_viewers_share_one_process_and_it_stops_when_they_leave(cfg, monkeypatch):
    monkeypatch.setattr(live, "IDLE_STOP_S", 0.3)
    spawned = []
    lv = make_view(cfg, spawned=spawned)
    a, b = lv.subscribe(), lv.subscribe()
    fa, fb = a.frames(), b.frames()
    assert next(fa)[:2] == b"\xff\xd8" and next(fb)[:2] == b"\xff\xd8"
    assert len(spawned) == 1 and lv.viewers() == 2 and lv.running()
    assert [next(fa) for _ in range(3)]                 # keeps coming
    fa.close()
    fb.close()
    assert lv.viewers() == 0
    assert wait(lambda: not lv.running()), "ffmpeg must stop after the last viewer leaves"


def test_a_viewer_never_gets_an_old_picture(cfg):
    lv = make_view(cfg)
    first = lv.subscribe()
    f = first.frames()
    next(f)
    seq_then = lv._seq
    second = lv.subscribe()
    g = second.frames()
    next(g)
    assert lv._seq > seq_then                           # the second viewer waited for a NEW picture
    f.close()
    g.close()
    lv.stop()


def test_viewer_cap(cfg):
    lv = make_view(cfg)
    subs = [lv.subscribe() for _ in range(live.MAX_VIEWERS)]
    assert all(subs) and lv.subscribe() is None
    for s in subs:
        s.close()
    assert lv.subscribe() is not None
    lv.stop()


def test_restarts_when_the_camera_drops_while_watched(cfg, monkeypatch):
    monkeypatch.setattr(live, "RESTART_AFTER_S", 0.1)
    spawned = []
    lv = make_view(cfg, mode="crash", spawned=spawned)
    s = lv.subscribe()
    g = s.frames()
    for _ in range(5):                                  # more pictures than one run produces
        next(g)
    assert len(spawned) >= 2
    g.close()
    lv.stop()


def test_login_test_reports_plainly(cfg):
    assert make_view(cfg, mode="stream").test()[0] is True
    ok, msg = make_view(cfg, mode="unauthorized").test()
    assert not ok and "user name and password" in msg and "s3cret" not in msg
    ok, msg = make_view(cfg, mode="unreachable").test()
    assert not ok and "cannot be reached" in msg


# ---- the web side ---------------------------------------------------------------------------
@pytest.fixture
def client(make_app):
    with TestClient(make_app(), follow_redirects=False) as c:
        c.post("/setup/claim", data={"code": c.app.state.box.setup_code(), "password": "a long password",
                                      "confirm": "a long password"})
        c.post("/api/camera/select", json={"ip": "192.168.1.64", "label": "x"})
        c.post("/api/setup/finish", json={"force": True})
        c.app.state.box.live._spawn_args = lambda url, one: [sys.executable, FAKE, "one" if one else "stream"]
        yield c


def test_live_pages_need_a_login(make_app):
    with TestClient(make_app(), follow_redirects=False) as c:
        assert c.get("/live").headers["location"] == "/setup"
        assert c.get("/live/stream.mjpg").status_code == 401
        assert c.get("/api/live/status").status_code == 401
        assert c.post("/api/live/test").status_code == 401


def test_turning_it_on_and_never_echoing_the_password(client, monkeypatch):
    monkeypatch.setattr(live, "NO_FRAME_S", 0.6)      # a finite stream: the test client cannot cancel one
    monkeypatch.setattr(live, "IDLE_STOP_S", 0.3)
    page = client.get("/live")
    assert page.status_code == 200 and "Turn on live view" in page.text
    assert client.get("/live/stream.mjpg").status_code == 503                      # off: refused

    assert "user name" in client.post("/live/settings", data={"username": "", "password": "x", "enabled": "1"}).text
    r = client.post("/live/settings", data={"username": "viewer", "password": "very-secret-pw", "enabled": "1"})
    assert r.status_code == 303 and r.headers["location"].startswith("/live")
    page = client.get("/live?saved=1")
    assert "very-secret-pw" not in page.text and 'value="viewer"' in page.text and "Connecting" in page.text
    assert client.post("/api/live/test").json()["ok"] is True
    st = client.get("/api/live/status").json()
    assert st["enabled"] and st["viewers"] == 0 and "very-secret-pw" not in str(st)

    box = client.app.state.box
    box.live._spawn_args = lambda url, one: [sys.executable, FAKE, "one" if one else "burst"]
    with client.stream("GET", "/live/stream.mjpg") as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("multipart/x-mixed-replace")
        body = b""
        for chunk in r.iter_bytes():
            body += chunk
        assert body.count(b"--frame") == 5
        assert b"Content-Type: image/jpeg" in body and b"\xff\xd8" in body
    assert wait(lambda: box.live.viewers() == 0)

    assert client.post("/live/forget").status_code == 303
    assert client.get("/live/stream.mjpg").status_code == 503


def test_a_closed_tab_gives_its_slot_back(cfg, monkeypatch):
    """The viewer disconnects mid-stream: the slot must be released and ffmpeg must stop."""
    import asyncio

    from platebox.web import mjpeg_body

    monkeypatch.setattr(live, "IDLE_STOP_S", 0.3)
    lv = make_view(cfg)

    class Gone:
        def __init__(self, after):
            self.n, self.after = 0, after

        async def is_disconnected(self):
            self.n += 1
            return self.n > self.after

    async def run():
        sub = lv.subscribe()
        parts = [p async for p in mjpeg_body(Gone(4), sub)]
        return sub, parts

    sub, parts = asyncio.run(run())
    assert 1 <= len(parts) <= 4 and parts[0].startswith(b"--frame\r\nContent-Type: image/jpeg")
    assert lv.viewers() == 0                                     # the leak that used to keep ffmpeg alive
    assert wait(lambda: not lv.running())


def test_plate_reading_is_unaffected_by_live_view(client, cfg):
    """The point of an add-on: with the stream running, the intake still reads and records."""
    box = client.app.state.box
    client.post("/live/settings", data={"username": "viewer", "password": "pw-pw-pw-pw", "enabled": "1"})
    sub = box.live.subscribe()
    frames = sub.frames()
    next(frames)
    d = cfg.intake_dir / "CAMSER0001" / "2026-10-18" / "pic_001" / "14"
    d.mkdir(parents=True)
    f = d / "14.20.13[M][0@0][0].jpg"
    f.write_bytes(jpeg_bytes())
    old = time.time() - 10
    os.utime(f, (old, old))
    box.reader.script = [[plate("MH12AB1234")]]
    t0 = time.time()
    box.intake.scan_once()
    assert box.sightings.grouped()[0]["plate"] == "MH12AB1234" and box.db.one("SELECT 1 FROM delivered") is not None
    assert time.time() - t0 < 5
    frames.close()
    box.live.stop()
