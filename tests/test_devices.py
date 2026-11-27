"""Gateways: signing in with a token, being offered firmware, downloading it, and reporting positions."""
import struct
import time

import pytest
from fastapi.testclient import TestClient

from platebox.devices import FirmwareError, distance_km, read_app_description

PASSWORD = "correct horse battery"


def image(version="0.2.0", project="ba-gateway", size=4096) -> bytes:
    """The start of a real ESP-IDF application image: header, first segment header, then the app description."""
    desc = struct.pack("<IIII", 0xABCD5432, 0, 0, 0) + version.encode().ljust(32, b"\0") + project.encode().ljust(32, b"\0")
    head = bytes([0xE9, 5, 2, 0x20]) + b"\0" * 20 + b"\0" * 8 + desc
    return head + bytes((i * 7) & 0xFF for i in range(size - len(head)))


@pytest.fixture
def site(make_app):
    app = make_app()
    box = app.state.box
    with TestClient(app) as c:
        c.post("/setup/claim", data={"code": box.setup_code(), "password": PASSWORD, "confirm": PASSWORD})
        box.db.put("setup_step", "done")
        yield c, box


def bearer(token):
    return {"Authorization": "Bearer " + token}


def test_reads_name_and_version_from_the_image():
    assert read_app_description(image("1.4.2", "ba-gateway")) == ("ba-gateway", "1.4.2")


@pytest.mark.parametrize("bad", [b"", b"hello" * 100, bytes([0xE9]) + b"\0" * 400, image()[:100]])
def test_refuses_what_is_not_an_application_image(bad):
    with pytest.raises(FirmwareError):
        read_app_description(bad)


def test_refuses_a_version_with_odd_characters():
    with pytest.raises(FirmwareError):
        read_app_description(image("1.0/../x"))


def test_a_gateway_needs_its_token(site):
    c, box = site
    assert c.post("/device/v1/checkin", json={}).status_code == 401
    assert c.post("/device/v1/checkin", json={}, headers=bearer("nonsense")).status_code == 401
    assert c.get("/device/v1/firmware/1", headers=bearer("nonsense")).status_code == 401
    assert c.post("/device/v1/positions", json={"positions": []}).status_code == 401


def test_wrong_tokens_are_slowed_down(site):
    c, box = site
    codes = [c.post("/device/v1/checkin", json={}, headers=bearer(f"guess{i}")).status_code for i in range(12)]
    assert 429 in codes


def test_checkin_records_the_gateway_and_offers_newer_firmware(site):
    c, box = site
    _, token = box.devices.create("Gate 1")
    info = {"model": "ba-gateway", "version": "0.2.0", "signal_dbm": -81, "carrier": "Vi India", "link": "USB",
            "fix": True, "lat": 18.52, "lon": 73.85, "password": "must not be kept"}
    r = c.post("/device/v1/checkin", json=info, headers=bearer(token)).json()
    assert r["ok"] and r["firmware"] is None and r["mode"] == "parking" and abs(r["time"] - time.time()) < 5
    g = box.devices.list()[0]
    assert (g["model"], g["version"], g["info"]["signal_dbm"]) == ("ba-gateway", "0.2.0", -81)
    assert "password" not in g["info"]

    fw = box.devices.add_firmware(image("0.2.1"), "faster")
    r = c.post("/device/v1/checkin", json=info, headers=bearer(token)).json()
    assert r["firmware"]["version"] == "0.2.1" and r["firmware"]["sha256"] == fw["sha256"]
    got = c.get(r["firmware"]["path"], headers=bearer(token))
    assert got.status_code == 200 and got.content == image("0.2.1")

    info["version"] = "0.2.1"                                  # it installed it: nothing more to offer
    assert c.post("/device/v1/checkin", json=info, headers=bearer(token)).json()["firmware"] is None


def test_firmware_for_another_kind_of_device_is_not_offered(site):
    c, box = site
    _, token = box.devices.create("Gate 1")
    box.devices.add_firmware(image("9.0.0", "something-else"))
    r = c.post("/device/v1/checkin", json={"model": "ba-gateway", "version": "0.2.0"}, headers=bearer(token)).json()
    assert r["firmware"] is None


def test_the_same_version_cannot_be_uploaded_twice_and_size_is_limited(site):
    c, box = site
    box.devices.add_firmware(image("0.2.1"))
    with pytest.raises(FirmwareError):
        box.devices.add_firmware(image("0.2.1"))
    with pytest.raises(FirmwareError):
        box.devices.add_firmware(image("0.3.0", size=0x1C0000 + 1))


def test_a_new_token_replaces_the_old_one(site):
    c, box = site
    dev_id, old = box.devices.create("Gate 1")
    new = box.devices.new_token(dev_id)
    assert c.post("/device/v1/checkin", json={}, headers=bearer(old)).status_code == 401
    assert c.post("/device/v1/checkin", json={}, headers=bearer(new)).status_code == 200


def test_the_page_shows_a_token_once_and_takes_an_upload(site):
    c, box = site
    assert c.get("/gateways").status_code == 200
    page = c.post("/gateways/add", data={"name": "Bus 7"}).text
    assert "Copy it now" in page and "server token " in page
    token = page.split("server token ")[1].split("<")[0].strip()
    assert box.devices.auth(token)["name"] == "Bus 7"
    assert token not in c.get("/gateways").text                 # never shown again
    r = c.post("/gateways/firmware", files={"file": ("ba-gateway.bin", image("0.2.5"), "application/octet-stream")},
               data={"note": "test"})
    assert "0.2.5" in r.text and box.devices.firmware_list()[0]["version"] == "0.2.5"
    r = c.post("/gateways/firmware", files={"file": ("x.bin", b"not firmware", "application/octet-stream")})
    assert "not an ESP32 firmware file" in r.text


def test_the_gateways_page_needs_the_administrator(make_app):
    app = make_app()
    with TestClient(app) as c:
        assert c.get("/gateways", follow_redirects=False).status_code == 303
        assert c.post("/gateways/add", data={"name": "x"}, follow_redirects=False).status_code == 303


def test_positions_place_and_distance(site):
    c, box = site
    dev_id, token = box.devices.create("Bus 7")
    t0 = float(int(time.time()) - 600)       # whole seconds: positions are kept to a tenth of a second
    # A bus driving north along a meridian: 0.001 degrees of latitude (about 111 m) every 10 s.
    pts = [{"t": t0 + i * 10, "lat": 18.5 + i * 0.001, "lon": 73.85, "speed_kmh": 40} for i in range(31)]
    pts += [{"t": t0, "lat": 0, "lon": 0}, {"t": t0, "lat": 95, "lon": 10}, "junk"]      # no fix, impossible, not a point
    r = c.post("/device/v1/positions", json={"positions": pts}, headers=bearer(token)).json()
    assert r == {"ok": True, "saved": 31}
    mid = box.devices.position_at(t0 + 15)                      # half way between two reports
    assert abs(mid["lat"] - 18.5015) < 1e-6 and mid["age_s"] == 5.0
    assert box.devices.position_at(t0 - 3600) is None           # nothing near that time
    km = box.devices.track_km(t0, t0 + 300)
    assert abs(km - distance_km(18.5, 73.85, 18.53, 73.85)) < 0.01 and 3.2 < km < 3.5


def test_standing_still_adds_no_distance(site):
    c, box = site
    dev_id, _ = box.devices.create("Bus 7")
    t0 = float(int(time.time()) - 600)       # whole seconds: positions are kept to a tenth of a second
    jitter = [{"t": t0 + i * 5, "lat": 18.5 + (0.00003 if i % 2 else 0), "lon": 73.85} for i in range(60)]
    box.devices.add_positions(dev_id, jitter)
    assert box.devices.track_km(t0, t0 + 290) == 0.0


def test_a_position_with_no_clock_gets_the_servers_time(site):
    c, box = site
    dev_id, _ = box.devices.create("Bus 7")
    box.devices.add_positions(dev_id, [{"t": 12, "lat": 18.5, "lon": 73.85}])
    assert abs(box.devices.latest_position()["t"] - time.time()) < 5
