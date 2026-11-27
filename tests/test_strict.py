"""The camera draws its own name and clock on every picture; the detector used to record them as plates."""
import pytest
from fastapi.testclient import TestClient

from platebox.db import Db
from platebox.sightings import Sightings

from conftest import jpeg_bytes, plate


def make(cfg):
    cfg.media_dir.mkdir(parents=True, exist_ok=True)
    db = Db(cfg.db_path)
    db.run("INSERT INTO cameras (id, serial, first_seen, last_seen) VALUES (1, 'TESTSERIAL', 'x', 'x')")
    return db, Sightings(cfg, db)


def test_the_cameras_own_label_and_clock_are_not_plates(cfg):
    db, s = make(cfg)
    j = jpeg_bytes()
    for text in ("CPIPC8111", "CPIPC801", "CPIP08111", "CPIPC2111", "18102026171922"):
        assert s.record(1, plate(text, 0.94, 337), j, None).kind == "ignored", text
    assert s.grouped() == [] and list(cfg.media_dir.rglob("*.jpg")) == []        # nothing stored at all
    assert s.record(1, plate("MH12AB1234", 0.9), j, None).kind == "new"


def test_unusual_plates_can_be_allowed_and_the_old_junk_cleared(cfg):
    db, s = make(cfg)
    j = jpeg_bytes()
    db.put("strict_format", "0")
    s.record(1, plate("CPIPC8111", 0.94), j, None)
    s.record(1, plate("MH12AB1234", 0.90), j, None)
    assert s.count_invalid() == 1 and s.strict_format() is False
    assert s.delete_invalid() == 1
    assert [g["plate"] for g in s.grouped()] == ["MH12AB1234"] and len(list(cfg.media_dir.rglob("*.jpg"))) == 2


def test_a_label_reading_never_reaches_the_motion_log_tags_or_the_plates_page(cfg):
    from platebox.intake import Intake
    from platebox.motionlog import MotionLog
    from conftest import FakeReader
    import os
    import time
    db, s = make(cfg)
    m = MotionLog(cfg, db)
    m.configure(True, 50, 0)
    it = Intake(cfg, db, FakeReader([[plate("CPIPC8111", 0.94), plate("MH47BP8265", 0.97, 420)]]), s, m)
    d = cfg.intake_dir / "SERIAL0001" / "2026-10-18" / "pic_001" / "17"
    d.mkdir(parents=True)
    f = d / "17.19.22[M][0@0][0].jpg"
    f.write_bytes(jpeg_bytes())
    os.utime(f, (time.time() - 10, time.time() - 10))
    it.scan_once()
    assert [g["plate"] for g in s.grouped()] == ["MH47BP8265"]
    assert m.list()[0]["plates"] == ["MH47BP8265"]                                # the label is not shown as a plate


@pytest.fixture
def client(make_app):
    with TestClient(make_app(), follow_redirects=False) as c:
        c.post("/setup/claim", data={"code": c.app.state.box.setup_code(), "password": "a long password",
                                      "confirm": "a long password"})
        c.post("/api/camera/select", json={"ip": "192.168.1.64", "label": "x"})
        c.post("/api/setup/finish", json={"force": True})
        c.app.state.box.db.run("INSERT INTO cameras (id, serial, first_seen, last_seen) VALUES (1, 'T', 'x', 'x')")
        yield c


def test_the_setting_and_the_clean_up_button(client):
    box = client.app.state.box
    assert box.sightings.strict_format() is True
    page = client.get("/settings")
    assert 'name="strict_format"' in page.text and "checked" in page.text and "Remove the" not in page.text

    box.db.put("strict_format", "0")                                    # let a label through, as it used to be
    box.sightings.record(1, plate("CPIPC8111", 0.94), jpeg_bytes(), None)
    box.sightings.record(1, plate("MH12AB1234", 0.90), jpeg_bytes(), None)
    assert "Remove the 1 entry" in client.get("/settings").text

    data = {"min_confidence": "0.6", "dedupe_seconds": "30", "retention_days": "90"}
    assert "Saved" in client.post("/settings/reading", data=data).text          # box unticked: strict off
    assert box.sightings.strict_format() is False
    assert "Saved" in client.post("/settings/reading", data=data | {"strict_format": "1"}).text
    assert box.sightings.strict_format() is True

    assert "Removed 1 entry" in client.post("/settings/clean-invalid").text
    assert [g["plate"] for g in box.sightings.grouped()] == ["MH12AB1234"]
    assert client.post("/settings/clean-invalid", headers={"origin": "http://evil.example"}).status_code == 403
