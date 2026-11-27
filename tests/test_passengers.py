"""Bus mode: a face at the door starts a ride, the same face later ends it, and the gateway's positions give the
distance and the fare. The face model is replaced by scripted faces here; test_faces_real.py runs the real one."""
import math
import time

import pytest
from fastapi.testclient import TestClient

from platebox.faces import Face
from platebox.passengers import pack, unpack

from conftest import jpeg_bytes

PASSWORD = "correct horse battery"


def person(n: int, wobble: float = 0.0) -> list[float]:
    """A made-up face: person n points along axis n. `wobble` tilts it, as a different picture of the same person does."""
    v = [0.0] * 128
    v[n] = 1.0
    v[(n + 64) % 128] = wobble
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v]


def face(n: int, wobble: float = 0.0, width: int = 120) -> Face:
    return Face((100, 80, width, width), 0.95, person(n, wobble), None)


class FakeFaces:
    def __init__(self):
        self.script = []

    def faces(self, bgr):
        return self.script.pop(0) if self.script else []


@pytest.fixture
def bus(make_app, cfg):
    from platebox.netmgr import FakeBackend
    from platebox.web import create_app

    from conftest import FakeReader

    finder = FakeFaces()
    app = create_app(cfg, reader=FakeReader(), net=FakeBackend(local_ip="192.168.1.50"), faces=finder)
    box = app.state.box
    with TestClient(app) as c:
        c.post("/setup/claim", data={"code": box.setup_code(), "password": PASSWORD, "confirm": PASSWORD})
        box.db.put("setup_step", "done")
        box.db.put("mode", "bus")
        yield c, box, finder


T0 = float(int(time.time()) - 3000)


def route(box, minutes=40):
    """The bus drives north for `minutes`, 0.0005 degrees of latitude (about 55 m) every 5 s: 40 km/h."""
    dev_id, _ = box.devices.create("Bus 7")
    pts = [{"t": T0 + i * 5, "lat": 18.5 + i * 0.0005, "lon": 73.85} for i in range(minutes * 12 + 1)]
    for i in range(0, len(pts), 400):
        box.devices.add_positions(dev_id, pts[i:i + 400])
    return dev_id


def see(box, faces, at, serial="CAM1"):
    cam = box.intake._camera(serial)
    return box.passengers.process(cam, serial, faces, jpeg_bytes(), None, received=at)


def test_vectors_survive_storage():
    v = person(3, 0.2)
    assert max(abs(a - b) for a, b in zip(unpack(pack(v)), v)) < 1e-6


def test_a_ride_from_boarding_to_leaving(bus):
    c, box, _ = bus
    route(box)
    assert see(box, [face(1)], T0 + 10)[0]["what"] == "boarded"
    assert see(box, [face(1, 0.2)], T0 + 12)[0]["what"] == "still"          # second picture of the same boarding
    out = see(box, [face(1, 0.3)], T0 + 610)[0]                            # ten minutes later, at the door again
    assert out["what"] == "left"
    ride = box.passengers.list()[0]
    assert ride["state"] == "done" and ride["shots"] == 2
    assert abs(ride["entry_lat"] - 18.501) < 1e-4 and abs(ride["exit_lat"] - 18.561) < 1e-4
    assert 6.5 < ride["distance_km"] < 6.8                                 # 0.06 degrees of latitude is 6.67 km
    assert ride["fare"] == round(10 + 2 * ride["distance_km"])
    assert ride["minutes"] == 10.0 and ride["similarity"] > 0.9


def test_two_people_are_two_rides_and_do_not_end_each_other(bus):
    c, box, _ = bus
    route(box)
    see(box, [face(1), face(2)], T0 + 10)                                  # two people board in one picture
    assert box.passengers.summary()["onboard"] == 2
    assert see(box, [face(2, 0.2)], T0 + 400)[0]["what"] == "left"
    rides = {r["state"] for r in box.passengers.list()}
    assert rides == {"onboard", "done"}
    assert see(box, [face(1, 0.2)], T0 + 900)[0]["what"] == "left"
    done = box.passengers.list(state="done")
    assert len(done) == 2 and done[0]["distance_km"] != done[1]["distance_km"]


def test_a_stranger_at_the_door_is_a_new_passenger_not_an_exit(bus):
    c, box, _ = bus
    route(box)
    see(box, [face(1)], T0 + 10)
    assert see(box, [face(5)], T0 + 500)[0]["what"] == "boarded"
    assert box.passengers.summary()["onboard"] == 2


def test_lingering_in_the_doorway_after_leaving_is_not_a_new_ride(bus):
    c, box, _ = bus
    route(box)
    see(box, [face(1)], T0 + 10)
    see(box, [face(1, 0.2)], T0 + 400)
    assert see(box, [face(1, 0.1)], T0 + 405)[0]["what"] == "still"
    assert len(box.passengers.list()) == 1
    assert see(box, [face(1)], T0 + 1200)[0]["what"] == "boarded"           # the same person, a later ride
    assert len(box.passengers.list()) == 2


def test_the_face_is_forgotten_when_the_ride_ends(bus):
    c, box, _ = bus
    route(box)
    see(box, [face(1)], T0 + 10)
    see(box, [face(1)], T0 + 400)
    box.passengers.sweep(T0 + 400 + 600)
    assert box.db.one("SELECT embedding FROM rides")["embedding"] is None


def test_a_ride_with_no_exit_is_closed_as_unseen_and_not_charged(bus):
    c, box, _ = bus
    route(box)
    see(box, [face(1)], T0 + 10)
    box.passengers.sweep(T0 + 5 * 3600)
    ride = box.passengers.list()[0]
    assert ride["state"] == "unseen" and ride["fare"] is None


def test_without_positions_a_ride_has_no_fare(bus):
    c, box, _ = bus
    see(box, [face(1)], T0 + 10)
    out = see(box, [face(1)], T0 + 400)[0]
    assert out["what"] == "left" and out["fare"] is None and out["distance_km"] is None
    assert "No position has arrived" in c.get("/passengers").text


def test_two_doors_entry_starts_and_exit_ends(bus):
    c, box, _ = bus
    route(box)
    box.intake._camera("FRONT"); box.intake._camera("BACK")
    box.passengers.set_camera_role("FRONT", "entry")
    box.passengers.set_camera_role("BACK", "exit")
    assert see(box, [face(1)], T0 + 10, "FRONT")[0]["what"] == "boarded"
    assert see(box, [face(1)], T0 + 500, "FRONT")[0]["what"] == "still"     # walked past the entry camera again
    assert see(box, [face(9)], T0 + 520, "BACK")[0]["what"] == "ignored"    # leaving, never seen boarding
    assert see(box, [face(1)], T0 + 30, "BACK")[0]["what"] == "left"        # the exit door ends a ride even a short one
    assert box.passengers.summary()["exits_unmatched"] == 1


def test_the_cameras_clock_places_a_late_picture(bus):
    """A picture taken at T0+300 that arrives twenty minutes later (no coverage) is placed where the bus was at T0+300."""
    c, box, _ = bus
    route(box)
    local = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(T0 + 300 + 330 * 60))      # the camera shows Indian time
    cam = box.intake._camera("CAM1")
    box.passengers.process(cam, "CAM1", [face(1)], jpeg_bytes(), local, received=T0 + 1500)
    ride = box.passengers.list()[0]
    assert ride["entry_at"] == T0 + 300 and abs(ride["entry_lat"] - 18.53) < 1e-4


def test_a_wrong_camera_clock_falls_back_to_the_arrival_time(bus):
    c, box, _ = bus
    assert box.passengers.picture_time("2019-01-01 00:00:00", T0) == T0
    assert box.passengers.picture_time("not a time", T0) == T0
    assert box.passengers.picture_time(None, T0) == T0


def test_last_stop_closes_everybody(bus):
    c, box, _ = bus
    route(box)
    see(box, [face(1), face(2)], T0 + 10)
    assert box.passengers.close_all(T0 + 1210) == 2
    done = box.passengers.list(state="done")
    assert len(done) == 2 and all(13.0 < r["distance_km"] < 13.7 and r["fare"] for r in done)


def test_pictures_from_the_camera_become_rides_in_bus_mode(bus, cfg):
    """Through the real intake: a file the camera uploaded is read for faces, not plates."""
    c, box, finder = bus
    route(box)
    finder.script = [[face(4)]]
    ist = time.gmtime(time.time() - 60 + 330 * 60)          # the camera's clock: Indian time, a minute ago
    d = cfg.intake_dir / "SERIALBUS" / time.strftime("%Y-%m-%d", ist) / "pic_001"
    d.mkdir(parents=True)
    (d / (time.strftime("%H.%M.%S", ist) + "[M][0@0][0].jpg")).write_bytes(jpeg_bytes(shade=90))
    stats = box.intake.scan_once()
    assert stats["read"] == 1 and stats["with_plate"] == 1
    ride = box.passengers.list()[0]
    assert ride["state"] == "onboard" and ride["entry_image"]
    assert c.get(f"/rides/{ride['id']}/entry.jpg").status_code == 200
    assert c.get(f"/rides/{ride['id']}/nonsense.jpg").status_code == 404
    assert box.sightings.totals()["sightings"] == 0


def test_pages_and_settings(bus):
    c, box, _ = bus
    route(box)
    see(box, [face(1)], T0 + 10)
    see(box, [face(1)], T0 + 610)
    page = c.get("/passengers").text
    assert "km" in page and "Rs" in page and 'href="/passengers"' in page
    assert c.get("/", follow_redirects=False).headers["location"] == "/passengers"
    assert c.get("/?plates=1").status_code == 200
    assert "ride,state,boarded_utc" in c.get("/passengers/export.csv").text
    r = c.post("/settings/bus", data={"fare_base": "15", "fare_per_km": "3.5", "fare_currency": "INR", "face_threshold": "0.5"})
    assert "Bus settings saved" in r.text and box.passengers.fare(10) == 50.0
    assert "must be between" in c.post("/settings/bus", data={"face_threshold": "5"}).text
    assert "must be numbers" in c.post("/settings/bus", data={"fare_base": "abc"}).text
    assert c.post("/passengers/close-all").status_code == 200
    rid = box.passengers.list()[0]["id"]
    c.post(f"/passengers/{rid}/delete")
    assert box.passengers.list() == []


def test_switching_to_bus_mode_needs_the_consent_tick(make_app):
    app = make_app()
    box = app.state.box
    with TestClient(app) as c:
        c.post("/setup/claim", data={"code": box.setup_code(), "password": PASSWORD, "confirm": PASSWORD})
        box.db.put("setup_step", "done")
        r = c.post("/settings/mode", data={"mode": "bus"})
        assert "Tick the box" in r.text and box.devices.mode() == "parking"
        assert 'href="/passengers"' not in c.get("/").text


def test_bus_mode_is_refused_without_the_models_and_reaches_the_gateway_with_them(bus):
    c, box, _ = bus
    box.db.put("mode", "parking")
    r = c.post("/settings/mode", data={"mode": "bus", "consent": "1"})
    assert "Now recording bus passengers" in r.text
    _, token = box.devices.create("Bus 7")
    answer = c.post("/device/v1/checkin", json={"model": "ba-gateway", "version": "0.2.0"},
                    headers={"Authorization": "Bearer " + token}).json()
    assert answer["mode"] == "bus"
    box.faces.available = False
    box.faces.error = "the files are missing"
    box.db.put("mode", "parking")
    assert "not available" in c.post("/settings/mode", data={"mode": "bus", "consent": "1"}).text
    assert box.devices.mode() == "parking"


def test_old_rides_and_their_pictures_are_removed(bus, cfg):
    c, box, _ = bus
    see(box, [Face((0, 0, 100, 100), 0.9, person(1), None)], T0)
    ride = box.passengers.list()[0]
    picture = cfg.media_dir / ride["entry_image"]
    assert picture.is_file()
    box.passengers.sweep(T0 + 31 * 86400)
    assert box.passengers.list() == [] and not picture.exists()
