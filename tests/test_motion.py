import os
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from platebox import motionlog
from platebox.db import Db
from platebox.intake import Intake
from platebox.motionlog import MB, MotionLog
from platebox.sightings import Sightings

from conftest import FakeReader, jpeg_bytes, plate


def noisy(w=800, h=450, seed=0):
    """A picture that does not compress well, so a few of them add up to megabytes."""
    return np.random.default_rng(seed).integers(0, 255, (h, w, 3), dtype=np.uint8)


def make(cfg):
    cfg.media_dir.mkdir(parents=True, exist_ok=True)
    db = Db(cfg.db_path)
    db.run("INSERT INTO cameras (id, serial, first_seen, last_seen) VALUES (1, 'TESTSERIAL', 'x', 'x')")
    return db, MotionLog(cfg, db)


def files(cfg):
    return sorted((cfg.media_dir / "motion").rglob("*.jpg")) if (cfg.media_dir / "motion").exists() else []


def test_a_picture_is_kept_as_a_reduced_copy_with_its_time_and_plates(cfg):
    import cv2
    db, m = make(cfg)
    big = noisy(2560, 1440)
    rid = m.record(1, big, "2026-10-18 14:20:13", ["MH12AB1234"])
    row = db.one("SELECT * FROM motion_events WHERE id = ?", (rid,))
    assert row["width"] == 800 and row["height"] == 450 and row["plates"] == "MH12AB1234"
    assert row["cam_time"] == "2026-10-18 14:20:13"
    p = cfg.media_dir / row["image"]
    assert p.stat().st_size == row["bytes"]
    assert cv2.imread(str(p)).shape[1] == 800
    e = m.list()[0]
    assert e["plates"] == ["MH12AB1234"] and e["id"] == rid


def test_only_one_picture_per_interval_but_a_plate_always_counts(cfg):
    db, m = make(cfg)
    img = noisy(seed=1)
    assert m.record(1, img, None, [], monotonic=100.0) is not None
    assert m.record(1, img, None, [], monotonic=101.0) is None               # the same movement a second later
    assert m.record(1, img, None, ["MH12AB1234"], monotonic=101.5) is not None  # a plate: always kept
    assert m.record(1, img, None, [], monotonic=103.5) is None                # 3 s after the last kept one: still too soon
    assert m.record(1, img, None, [], monotonic=105.0) is not None
    assert m.count() == 3 and m.count(plates_only=True) == 1
    # the gap is per camera
    db.run("INSERT INTO cameras (id, serial, first_seen, last_seen) VALUES (2, 'OTHERCAM', 'x', 'x')")
    assert m.record(2, img, None, [], monotonic=105.1) is not None


def test_off_means_nothing_is_kept(cfg):
    db, m = make(cfg)
    m.configure(False, 50, 3)
    assert m.record(1, noisy(), None, ["MH12AB1234"]) is None and m.count() == 0 and files(cfg) == []


def test_the_size_limit_deletes_the_oldest_first(cfg):
    db, m = make(cfg)
    m.configure(True, 5, 0)                       # 5 MB, no gap between pictures
    ids = []
    for i in range(40):
        ids.append(m.record(1, noisy(seed=i), f"2026-10-18 10:00:{i:02d}", []))
    assert all(ids)
    assert m.used_bytes() <= 5 * MB
    kept = [r["id"] for r in db.all("SELECT id FROM motion_events ORDER BY id")]
    assert kept and kept == sorted(kept)
    assert kept[-1] == ids[-1]                    # the newest is always there
    assert ids[0] not in kept                     # the oldest went first
    assert kept == list(range(kept[0], kept[-1] + 1))   # no holes: removal is strictly oldest-first
    assert len(files(cfg)) == len(kept)           # rows and files go together
    s = m.stats()
    assert s["used_mb"] <= 5 and s["percent"] <= 100 and s["count"] == len(kept)


def test_lowering_the_limit_trims_at_once(cfg):
    db, m = make(cfg)
    m.configure(True, 50, 0)
    for i in range(30):
        m.record(1, noisy(seed=i), None, [])
    before = m.used_bytes()
    assert before > 5 * MB
    m.configure(True, 5, 0)
    assert m.used_bytes() <= 5 * MB < before
    assert len(files(cfg)) == m.count()


def test_the_limit_is_validated(cfg):
    db, m = make(cfg)
    for bad in (0, 1, 4, 5001):
        with pytest.raises(ValueError):
            m.configure(True, bad, 3)
    with pytest.raises(ValueError):
        m.configure(True, 50, -1)
    with pytest.raises(ValueError):
        m.configure(True, 50, 601)
    m.configure(True, 5, 0)
    m.configure(True, 5000, 600)


def test_file_for_stays_inside_the_media_folder_and_clear_empties_it(cfg):
    db, m = make(cfg)
    rid = m.record(1, noisy(), None, [])
    assert m.file_for(rid).is_file()
    db.run("UPDATE motion_events SET image = ? WHERE id = ?", ("../../etc/passwd", rid))
    assert m.file_for(rid) is None and m.file_for(99999) is None
    db.run("UPDATE motion_events SET image = ? WHERE id = ?", (str(next(iter(files(cfg))).relative_to(cfg.media_dir)), rid))
    assert m.clear() == 1 and m.count() == 0 and files(cfg) == []


# ---- the intake feeds it ----------------------------------------------------------------------
_shade = iter(range(100, 250))


def drop(cfg, name, serial="2KN9B54Y6F9XYK4Y"):
    d = cfg.intake_dir / serial / "2026-10-18" / "pic_001" / "14"
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_bytes(jpeg_bytes(shade=next(_shade)))
    old = time.time() - 10
    os.utime(f, (old, old))
    return f


def test_every_picture_is_logged_whether_or_not_a_plate_was_found(cfg):
    db, _ = make(cfg)
    m = MotionLog(cfg, db)
    m.configure(True, 50, 0)
    s = Sightings(cfg, db)
    reader = FakeReader([[plate("MH12AB1234")], []])
    it = Intake(cfg, db, reader, s, m)
    drop(cfg, "14.20.13[M][0@0][0].jpg")
    drop(cfg, "14.20.14[M][0@0][0].jpg")
    it.scan_once()
    ev = m.list()
    assert len(ev) == 2
    by_time = {e["cam_time"]: e for e in ev}
    assert by_time["2026-10-18 14:20:13"]["plates"] == ["MH12AB1234"]
    assert by_time["2026-10-18 14:20:14"]["plates"] == []
    assert s.grouped()[0]["plate"] == "MH12AB1234"             # and the plate still got its own record


def test_a_broken_motion_log_never_loses_a_plate(cfg, monkeypatch):
    db, m = make(cfg)
    s = Sightings(cfg, db)

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(m, "record", boom)
    it = Intake(cfg, db, FakeReader([[plate("MH12AB1234")]]), s, m)
    drop(cfg, "14.20.13[M][0@0][0].jpg")
    it.scan_once()
    assert s.grouped()[0]["plate"] == "MH12AB1234"


# ---- the web side -------------------------------------------------------------------------------
@pytest.fixture
def client(make_app):
    with TestClient(make_app(), follow_redirects=False) as c:
        c.post("/setup/claim", data={"code": c.app.state.box.setup_code(), "password": "a long password",
                                      "confirm": "a long password"})
        c.post("/api/camera/select", json={"ip": "192.168.1.64", "label": "x"})
        c.post("/api/setup/finish", json={"force": True})
        c.app.state.box.db.run("INSERT INTO cameras (id, serial, first_seen, last_seen) VALUES (1, 'T', 'x', 'x')")
        yield c


def test_motion_pages_need_a_login(make_app):
    with TestClient(make_app(), follow_redirects=False) as c:
        assert c.get("/motion").headers["location"] == "/setup"
        assert c.get("/api/motion/summary").status_code == 401
        assert c.get("/motion/1.jpg").status_code == 401
        assert c.post("/settings/motion", data={"max_mb": "50", "interval_s": "3"}).status_code in (303, 401)


def test_the_motion_page_settings_and_pictures(client):
    box = client.app.state.box
    page = client.get("/motion")
    assert page.status_code == 200 and "No movement recorded yet" in page.text and "Motion" in page.text

    box.motion.configure(True, 50, 0)
    a = box.motion.record(1, noisy(seed=1), "2026-10-18 14:20:13", [])
    b = box.motion.record(1, noisy(seed=2), "2026-10-18 14:20:20", ["MH12AB1234"])
    page = client.get("/motion")
    assert f"/motion/{a}.jpg" in page.text and f"/motion/{b}.jpg" in page.text and "MH12AB1234" in page.text
    assert "of 50 MB used" in page.text and "2 pictures" in page.text
    only = client.get("/motion?plates=1")
    assert f"/motion/{b}.jpg" in only.text and f"/motion/{a}.jpg" not in only.text
    assert f"/motion/{a}.jpg" in client.get("/fragment/motion").text
    summ = client.get("/api/motion/summary").json()
    assert summ["count"] == 2 and summ["cap_mb"] == 50 and summ["enabled"] is True

    img = client.get(f"/motion/{a}.jpg")
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg" and img.content[:2] == b"\xff\xd8"
    assert client.get("/motion/99999.jpg").status_code == 404

    s = client.get("/settings")
    assert 'name="max_mb"' in s.text and 'value="50"' in s.text and "oldest" in s.text
    ok = client.post("/settings/motion", data={"enabled": "1", "max_mb": "100", "interval_s": "10"})
    assert "Saved" in ok.text and box.motion.max_mb() == 100 and box.motion.interval_s() == 10
    bad = client.post("/settings/motion", data={"enabled": "1", "max_mb": "2", "interval_s": "10"})
    assert "between 5 MB and 5000 MB" in bad.text and box.motion.max_mb() == 100
    off = client.post("/settings/motion", data={"max_mb": "100", "interval_s": "10"})            # box unticked
    assert "Saved" in off.text and box.motion.enabled() is False
    assert "switched off" in client.get("/motion").text

    assert "Deleted 2" in client.post("/motion/clear").text and box.motion.count() == 0
    assert client.post("/settings/motion", data={"max_mb": "50", "interval_s": "3"},
                       headers={"origin": "http://evil.example"}).status_code == 403
