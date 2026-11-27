"""Server mode: PlateBox on a server on the internet, the camera sending to it (for example through a 4G gateway).

No local network to join, no camera to search for, no video to pull: password -> camera settings -> done.
"""
import pytest
from fastapi.testclient import TestClient

from platebox.config import Config
from platebox.netmgr import FakeBackend
from platebox.web import create_app

from conftest import FakeReader, jpeg_bytes, plate

PASSWORD = "correct horse battery"


@pytest.fixture
def scfg(tmp_path):
    return Config(data_dir=tmp_path / "data", sftp_root=tmp_path / "sftp", background=False, intake_settle_s=0.0,
                  cookie_secure=False, server_mode=True, public_host="plates.example.com")


def make(scfg, reader=None):
    return create_app(scfg, reader=reader or FakeReader(), net=FakeBackend(local_ip="10.0.0.5"))


def claim(c, box):
    r = c.post("/setup/claim", data={"code": box.setup_code(), "password": PASSWORD, "confirm": PASSWORD},
               follow_redirects=False)
    assert r.status_code == 303
    return r


def test_server_mode_sets_the_safe_defaults(scfg):
    assert scfg.sftp_mode == "builtin"            # no second sshd on the internet
    assert scfg.offline_helper is False           # no DNS/NTP answering on the internet
    assert scfg.fake_net is True                  # the server's network is not ours to manage
    assert scfg.advertise_ip == "plates.example.com"


def test_from_env_reads_server_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("PLATEBOX_SERVER", "1")
    monkeypatch.setenv("PLATEBOX_PUBLIC_HOST", " plates.example.com ")
    monkeypatch.setenv("PLATEBOX_DATA", str(tmp_path))
    cfg = Config.from_env()
    assert cfg.server_mode and cfg.public_host == "plates.example.com" and cfg.sftp_mode == "builtin"


def test_wizard_goes_straight_to_the_camera_settings(scfg):
    app = make(scfg)
    box = app.state.box
    with TestClient(app) as c:
        claim(c, box)
        assert box.step == "configure"
        page = c.get("/setup").text
        assert "Connect the camera" in page
        assert "Find the camera" not in page and "Wi-Fi networks" not in page
        assert "back-camera" not in page                       # nothing to go back to
        st = c.get("/api/setup/status").json()
        assert st["step"] == "configure"
        assert st["sftp"]["server"] == "plates.example.com"    # the public name, not the server's inner address
        assert st["sftp"]["protocol"] == "SFTP" and st["sftp"]["username"] == "ftpcam"


def test_an_old_step_left_in_the_database_cannot_strand_the_wizard(scfg):
    app = make(scfg)
    box = app.state.box
    with TestClient(app) as c:
        claim(c, box)
        box.db.put("setup_step", "network")
        assert box.step == "configure"
        assert c.get("/setup").status_code == 200


def test_a_picture_finishes_setup_and_shows_the_plate(scfg):
    app = make(scfg, FakeReader([[plate("MH12AB1234")]]))
    box = app.state.box
    with TestClient(app) as c:
        claim(c, box)
        d = scfg.intake_dir / "SERIAL123" / "2026-11-27" / "pic_001"
        d.mkdir(parents=True)
        (d / "12[M][0@0][0].jpg").write_bytes(jpeg_bytes())
        box.intake.scan_once()
        st = c.get("/api/setup/status").json()
        assert st["camera"]["picture_received"] is True
        assert c.post("/api/setup/finish", json={}).json()["ok"] is True
        assert box.done
        assert "MH12AB1234" in c.get("/").text


def test_pages_hide_what_only_a_box_at_the_site_can_do(scfg):
    app = make(scfg)
    box = app.state.box
    with TestClient(app) as c:
        claim(c, box)
        c.post("/api/setup/finish", json={"force": True})
        home = c.get("/").text
        assert 'href="/live"' not in home                      # the camera's video cannot be pulled from a server
        assert 'id="alert"' not in home                        # no address guard
        settings = c.get("/settings").text
        for gone in ("Watching the camera directly", 'id="address"', "No internet at this site?", "/settings/reboot",
                     "/settings/poweroff", "Find or change the camera"):
            assert gone not in settings, gone
        assert "plates.example.com" in settings
        assert "Administrator password" in settings and "Motion log" in settings


def test_box_only_actions_are_refused(scfg):
    app = make(scfg)
    box = app.state.box
    with TestClient(app) as c:
        claim(c, box)
        c.post("/api/setup/finish", json={"force": True})
        assert c.post("/settings/reboot").status_code == 403
        assert c.post("/settings/poweroff").status_code == 403
        assert c.get("/live", follow_redirects=False).headers["location"] == "/"
        r = c.post("/settings/camera", follow_redirects=False)
        assert r.headers["location"] == "/settings" and box.done      # setup is not reopened
        r = c.post("/settings/network", follow_redirects=False)
        assert r.headers["location"] == "/settings" and box.done


def test_background_parts_that_reach_into_the_site_do_not_start(scfg, monkeypatch):
    scfg.background = True
    app = make(scfg)
    box = app.state.box
    started = []
    for name in ("netwatch", "guard", "watcher", "helper"):
        monkeypatch.setattr(getattr(box, name), "start", lambda n=name: started.append(n))
    monkeypatch.setattr(box.intake, "start", lambda: started.append("intake"))
    monkeypatch.setattr(box.receiver, "start", lambda: started.append("receiver"))
    box.start()
    assert sorted(started) == ["intake", "receiver"]


def test_the_box_at_the_site_is_unchanged(cfg, make_app):
    """The same build without server mode still has every step and every page."""
    app = make_app()
    box = app.state.box
    with TestClient(app) as c:
        claim(c, box)
        assert box.step == "network"
        assert "Wi-Fi networks" in c.get("/setup").text
        box.db.put("setup_step", "done")
        assert 'href="/live"' in c.get("/").text
        assert "Watching the camera directly" in c.get("/settings").text
