import re
import time

import pytest
from fastapi.testclient import TestClient

from conftest import FakeReader, jpeg_bytes, plate


def code_of(client):
    return client.app.state.box.setup_code()


def claim(client, password="a long password", code=None):
    return client.post("/setup/claim", data={"code": code or code_of(client), "password": password,
                                              "confirm": password}, follow_redirects=False)


@pytest.fixture
def client(make_app):
    with TestClient(make_app(), follow_redirects=False) as c:
        yield c


def test_a_fresh_box_sends_everyone_to_setup_and_shows_only_the_claim_form(client):
    assert client.get("/").headers["location"] == "/setup"
    assert client.get("/login").headers["location"] == "/setup"
    r = client.get("/setup")
    assert r.status_code == 200 and "Setup code" in r.text and "Wi-Fi networks" not in r.text
    # nothing else is open before the box is claimed
    assert client.get("/api/setup/status").status_code == 401
    assert client.get("/api/net/scan").status_code == 401
    assert client.post("/api/camera/select", json={"ip": "1.2.3.4"}).status_code == 401
    assert client.get("/healthz").json()["claimed"] is False


def test_claiming_needs_the_code_and_a_decent_password(client):
    assert claim(client, code="000000").status_code == 200                       # wrong code: stays on the form
    assert "not right" in claim(client, code="000000").text
    r = client.post("/setup/claim", data={"code": code_of(client), "password": "abcdefgh", "confirm": "abcdefgi"})
    assert "different" in r.text
    r = client.post("/setup/claim", data={"code": code_of(client), "password": "short", "confirm": "short"})
    assert "at least" in r.text
    r = claim(client)
    assert r.status_code == 303 and r.headers["location"] == "/setup" and "pb_session" in r.headers["set-cookie"]
    assert "HttpOnly" in r.headers["set-cookie"] and "SameSite=strict" in r.headers["set-cookie"]
    # a second claim, even with the right code, is refused
    assert claim(client).headers["location"] == "/login"
    assert client.get("/healthz").json()["claimed"] is True


def test_claim_attempts_are_throttled(client):
    for _ in range(5):
        claim(client, code="111111")
    assert "Too many" in claim(client, code=code_of(client)).text


def test_login_logout_and_throttle(client):
    claim(client)
    client.post("/logout")
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    assert "not right" in client.post("/login", data={"password": "nope nope"}).text
    r = client.post("/login", data={"password": "a long password"})
    assert r.status_code == 303 and client.get("/setup").status_code in (200, 303)
    client.post("/logout")
    for _ in range(5):
        client.post("/login", data={"password": "bad bad bad"})
    assert "Too many" in client.post("/login", data={"password": "a long password"}).text


def test_cross_site_posts_are_refused(client):
    claim(client)
    r = client.post("/api/setup/step", json={"step": "camera"}, headers={"origin": "http://evil.example"})
    assert r.status_code == 403
    r = client.post("/logout", headers={"origin": "http://evil.example"})
    assert r.status_code == 403


def test_the_wizard_end_to_end(client):
    box = client.app.state.box
    claim(client)
    assert client.get("/setup").status_code == 200 and "Connect the box" in client.get("/setup").text

    st = client.get("/api/net/status").json()
    assert st["connected"] and st["ip"] == "192.168.1.50"
    nets = client.get("/api/net/scan").json()["networks"]
    assert nets and nets[0]["ssid"]
    assert client.post("/api/net/connect", json={"ssid": "Office-WiFi", "password": "wrong"}).json()["ok"] is False
    assert client.post("/api/net/connect", json={"ssid": "Office-WiFi", "password": "right"}).json()["ok"] is True
    assert client.post("/api/net/connect", json={"ssid": ""}).status_code == 400

    assert client.post("/api/setup/step", json={"step": "camera"}).status_code == 200
    assert "Find the camera" in client.get("/setup").text
    assert client.post("/api/setup/step", json={"step": "nonsense"}).status_code == 400
    assert client.post("/api/camera/select", json={"ip": "not-an-ip"}).status_code == 400
    assert client.post("/api/camera/select", json={"ip": "999.1.1.1"}).status_code == 400
    assert client.post("/api/camera/select", json={"ip": "192.168.1.64", "label": "CP Plus"}).status_code == 200

    s = client.get("/api/setup/status").json()
    assert s["step"] == "configure"
    assert s["sftp"]["protocol"] == "SFTP" and s["sftp"]["port"] == 2222 and s["sftp"]["username"] == "ftpcam"
    assert s["sftp"]["directory"] == "upload" and len(s["sftp"]["password"]) == 12
    assert s["camera"]["camera_connected"] is False and s["camera"]["picture_received"] is False
    assert client.post("/api/setup/finish", json={}).status_code == 409               # no picture yet

    # the camera connects (its marker file appears), then sends a picture
    cfg = box.cfg
    marker = cfg.intake_dir / "2KN9B54Y6F9XYK4Y" / "IPCWorkDirectory"
    marker.parent.mkdir(parents=True); marker.write_bytes(b"x")
    box.intake.scan_once()
    s = client.get("/api/setup/status").json()["camera"]
    assert s["camera_connected"] and not s["picture_received"] and s["serial"] == "2KN9B54Y6F9XYK4Y"

    d = cfg.intake_dir / "2KN9B54Y6F9XYK4Y" / "2026-10-18" / "pic_001" / "14"
    d.mkdir(parents=True)
    (d / "14.20.13[M][0@0][0].jpg").write_bytes(jpeg_bytes())
    old = time.time() - 10
    import os; os.utime(d / "14.20.13[M][0@0][0].jpg", (old, old))
    box.reader.script = [[plate("MH47BP8265", 0.97, 420)]]
    box.intake.scan_once()
    assert client.get("/api/setup/status").json()["camera"]["picture_received"] is True
    assert client.post("/api/setup/finish", json={}).status_code == 200

    # the plates screen
    page = client.get("/")
    assert page.status_code == 200 and "MH47BP8265" in page.text and "Format OK" in page.text
    assert 'data-local="2026-10-18 14:20:13"' in page.text                            # the camera's own clock
    sid = int(re.search(r"/media/(\d+)/crop\.jpg", page.text).group(1))
    for kind in ("crop", "full"):
        m = client.get(f"/media/{sid}/{kind}.jpg")
        assert m.status_code == 200 and m.headers["content-type"] == "image/jpeg" and m.content[:2] == b"\xff\xd8"
    assert client.get(f"/media/{sid}/other.jpg").status_code == 404
    assert client.get("/media/9999/crop.jpg").status_code == 404
    assert "MH47BP8265" in client.get("/fragment/plates").text
    assert "No plate matches" in client.get("/fragment/plates?q=ZZ").text
    assert client.get("/api/plates/summary").json()["sightings"] == 1
    assert client.get("/export.csv").text.splitlines()[1].startswith("MH47BP8265")
    assert client.get("/setup").headers["location"] == "/"                             # done: no more wizard

    # unauthenticated callers see none of it
    client.post("/logout")
    assert client.get(f"/media/{sid}/full.jpg").status_code == 401
    assert client.get("/export.csv").headers["location"] == "/login"


def test_settings_page_and_changes(client):
    claim(client)
    client.post("/api/camera/select", json={"ip": "192.168.1.64", "label": "CP Plus"})
    client.post("/api/setup/finish", json={"force": True})
    r = client.get("/settings")
    assert r.status_code == 200 and "SFTP" in r.text and "192.168.1.50" in r.text and "ftpcam" in r.text
    pw = client.app.state.box.sftp.password()
    assert pw in r.text                                                               # shown (hidden by default) for re-entry

    assert "Saved" in client.post("/settings/reading", data={"min_confidence": "0.7", "dedupe_seconds": "40",
                                                              "retention_days": "30"}).text
    box = client.app.state.box
    assert box.sightings.min_confidence() == 0.7 and box.sightings.dedupe_seconds() == 40
    assert "out of range" in client.post("/settings/reading", data={"min_confidence": "5", "dedupe_seconds": "40",
                                                                     "retention_days": "30"}).text

    assert "not right" in client.post("/settings/password", data={"current": "wrong wrong", "password": "another long one",
                                                                   "confirm": "another long one"}).text
    assert "Password changed" in client.post("/settings/password", data={"current": "a long password",
                                                                          "password": "another long one",
                                                                          "confirm": "another long one"}).text
    client.post("/logout")
    assert "not right" in client.post("/login", data={"password": "a long password"}).text
    assert client.post("/login", data={"password": "another long one"}).status_code == 303

    new = client.post("/settings/sftp-password")
    assert "New camera password set" in new.text and box.sftp.password() != pw

    # re-running the camera step takes the wizard back, and finishing again works at once
    assert client.post("/settings/camera").headers["location"] == "/setup"
    assert "Find the camera" in client.get("/setup").text


def test_address_change_is_reported(client):
    claim(client)
    client.post("/api/camera/select", json={"ip": "192.168.1.64", "label": "x"})
    client.get("/api/setup/status")                                                   # records the address the screen showed
    client.post("/api/setup/finish", json={"force": True})
    box = client.app.state.box
    assert box.camera_status()["ip_changed"] is False
    box.db.put("ip_told", "10.9.9.9")                                                 # the box has since moved
    assert box.camera_status()["ip_changed"] is True
    assert "address changed" in client.get("/").text


def test_clear_all_removes_pictures(client):
    claim(client)
    client.post("/api/setup/finish", json={"force": True})
    box = client.app.state.box
    box.db.run("INSERT INTO cameras (id, serial, first_seen, last_seen) VALUES (1, 'TESTSERIAL', 'x', 'x')")
    box.sightings.record(1, plate(), jpeg_bytes(), None)
    assert list(box.cfg.media_dir.rglob("*.jpg"))
    assert "Deleted 1" in client.post("/settings/clear").text
    assert not list(box.cfg.media_dir.rglob("*.jpg"))
