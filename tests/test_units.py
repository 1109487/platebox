import time
from datetime import datetime, timedelta, timezone

from platebox import auth, discovery, netmgr
from platebox.config import Config
from platebox.db import Db
from platebox.intake import Candidate, Intake, plan_backlog
from platebox.reader import looks_valid, normalise
from platebox.sightings import Sightings, camera_time_from_path, edit_distance, same_vehicle

from conftest import FakeReader, jpeg_bytes, plate


# ---- passwords, sessions, throttling --------------------------------------------------------
def test_password_hash_roundtrip_and_wrong_password():
    h = auth.hash_password("correct horse")
    assert auth.verify_password("correct horse", h)
    assert not auth.verify_password("correct horsf", h)
    assert not auth.verify_password("x", "garbage")
    assert h != auth.hash_password("correct horse")          # salted


def test_password_rules():
    assert auth.password_problem("short")
    assert auth.password_problem("12345678")
    assert auth.password_problem("a long enough one") is None


def test_signed_cookie_expires_and_rejects_tampering():
    s = auth.Signer(b"k" * 32)
    t = s.sign("admin", 60, now=1000)
    assert s.verify(t, now=1030) == "admin"
    assert s.verify(t, now=1100) is None                       # expired
    assert s.verify(t[:-2] + "xx", now=1030) is None           # tampered
    assert auth.Signer(b"j" * 32).verify(t, now=1030) is None  # other key
    assert s.verify(None) is None and s.verify("nodot") is None


def test_throttle_blocks_then_forgives():
    th = auth.Throttle(attempts=3, window_s=100)
    for i in range(3):
        assert th.blocked("ip", now=i) == 0
        th.fail("ip", now=i)
    assert th.blocked("ip", now=5) > 0
    assert th.blocked("other", now=5) == 0
    assert th.blocked("ip", now=500) == 0                      # window passed
    th.fail("ip", now=500); th.ok("ip")
    assert th.blocked("ip", now=501) == 0


# ---- plate text -----------------------------------------------------------------------------
def test_plate_formats():
    for ok in ("MH12AB1234", "DL1CAB1234", "MH47BP8265", "22BH6517A", "KL54A2670"):
        assert looks_valid(ok), ok
    for bad in ("KL5462670", "HR26DQ555", "DL3SEXOO0", "", "ABC"):
        assert not looks_valid(bad), bad
    assert normalise("mh 12-ab 1234") == "MH12AB1234"


def test_edit_distance_and_same_vehicle():
    assert edit_distance("KITTEN", "SITTING") == 3
    assert same_vehicle("MH12AB1234", "MH12AB1234")
    # a different valid registration is a different vehicle however alike
    assert not same_vehicle("MH12AB1234", "MH12AB1235")
    # an invalid reading near a valid one is a misread of it
    assert same_vehicle("MH12AB1234", "MH12AB123")
    assert same_vehicle("KL5462670", "KL54A2670")                        # one invalid reading, one valid: a misread
    assert not same_vehicle("AB1", "AB2")                              # too short to judge


def test_camera_time_from_path():
    p = "2KN9B54Y6F9XYK4Y/2026-10-18/pic_001/14/14.20.13[M][0@0][0].jpg"
    assert camera_time_from_path(p) == "2026-10-18 14:20:13"
    assert camera_time_from_path("whatever.jpg") is None


# ---- sightings ------------------------------------------------------------------------------
def make_sightings(cfg):
    cfg.media_dir.mkdir(parents=True, exist_ok=True)
    db = Db(cfg.db_path)
    db.run("INSERT INTO cameras (id, serial, first_seen, last_seen) VALUES (1, 'TESTSERIAL', 'x', 'x')")
    return db, Sightings(cfg, db)


def test_one_pass_many_pictures_is_one_sighting_with_the_best_reading(cfg):
    db, s = make_sightings(cfg)
    db.put("strict_format", "0")                                  # this test is about merging misreads
    t0 = datetime(2026, 10, 18, 9, 0, 0, tzinfo=timezone.utc)
    j = jpeg_bytes()
    assert s.record(1, plate("MH12AB123", 0.70), j, None, t0).kind == "new"                 # misread, no last digit
    assert s.record(1, plate("MH12AB1234", 0.80), j, None, t0 + timedelta(seconds=1)).kind == "updated"
    assert s.record(1, plate("MH12AB1234", 0.75), j, None, t0 + timedelta(seconds=2)).kind == "merged"
    assert s.record(1, plate("MH12AB1234", 0.95), j, None, t0 + timedelta(seconds=3)).kind == "updated"
    rows = db.all("SELECT plate, confidence FROM sightings")
    assert len(rows) == 1 and rows[0]["plate"] == "MH12AB1234" and rows[0]["confidence"] == 0.95
    # the replaced pictures are gone from disk: only the latest pair remains
    assert len(list(cfg.media_dir.rglob("*.jpg"))) == 2


def test_a_later_pass_is_a_new_sighting_and_every_time_is_listed(cfg):
    db, s = make_sightings(cfg)
    t0 = datetime(2026, 10, 18, 9, 0, 0, tzinfo=timezone.utc)
    j = jpeg_bytes()
    s.record(1, plate("MH12AB1234"), j, "2026-10-18 14:20:13", t0)
    s.record(1, plate("MH12AB1234"), j, None, t0 + timedelta(minutes=5))
    s.record(1, plate("DL1CAB9999"), j, None, t0 + timedelta(minutes=6))
    groups = {g["plate"]: g for g in s.grouped()}
    assert groups["MH12AB1234"]["count"] == 2 and len(groups["MH12AB1234"]["times"]) == 2
    assert groups["MH12AB1234"]["times"][1]["cam_time"] == "2026-10-18 14:20:13"
    assert groups["DL1CAB9999"]["count"] == 1
    assert [g["plate"] for g in s.grouped()][0] == "DL1CAB9999"               # most recent first
    assert [g["plate"] for g in s.grouped("ab12")] == ["MH12AB1234"]


def test_two_different_valid_plates_together_are_two_vehicles(cfg):
    db, s = make_sightings(cfg)
    t0 = datetime(2026, 10, 18, 9, 0, 0, tzinfo=timezone.utc)
    j = jpeg_bytes()
    s.record(1, plate("MH12AB1234"), j, None, t0)
    assert s.record(1, plate("MH12AB1235"), j, None, t0 + timedelta(seconds=2)).kind == "new"
    assert db.one("SELECT COUNT(*) AS n FROM sightings")["n"] == 2


def test_low_confidence_is_ignored_and_settings_apply_at_once(cfg):
    db, s = make_sightings(cfg)
    j = jpeg_bytes()
    assert s.record(1, plate("MH12AB1234", 0.3), j, None).kind == "ignored"
    db.put("min_confidence", "0.2")
    assert s.record(1, plate("MH12AB1234", 0.3), j, None).kind == "new"


def test_retention_delete_and_export(cfg):
    db, s = make_sightings(cfg)
    j = jpeg_bytes()
    old = datetime.now(timezone.utc) - timedelta(days=100)
    s.record(1, plate("MH12AB1234"), j, None, old)
    s.record(1, plate("DL1CAB9999"), j, None)
    assert s.sweep() == 1                                                     # 90-day default
    assert [g["plate"] for g in s.grouped()] == ["DL1CAB9999"]
    assert "DL1CAB9999" in s.export_csv()
    assert s.delete_plate("DL1CAB9999") == 1
    assert list(cfg.media_dir.rglob("*.jpg")) == []


def test_file_for_never_leaves_the_media_folder(cfg):
    db, s = make_sightings(cfg)
    sid = s.record(1, plate(), jpeg_bytes(), None).sighting_id
    assert s.file_for(sid, "full").is_file()
    db.run("UPDATE sightings SET image = ? WHERE id = ?", ("../../etc/passwd", sid))
    assert s.file_for(sid, "full") is None


# ---- intake ---------------------------------------------------------------------------------
_shade = iter(range(100, 250))


def drop(cfg, serial="2KN9B54Y6F9XYK4Y", name="14.20.13[M][0@0][0].jpg", data=None, age=10):
    d = cfg.intake_dir / serial / "2026-10-18" / "pic_001" / "14"
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_bytes(data if data is not None else jpeg_bytes(shade=next(_shade)))
    t = time.time() - age
    import os
    os.utime(f, (t, t))
    return f


def test_intake_reads_records_and_keeps_what_it_has_dealt_with(cfg):
    cfg.media_dir.mkdir(parents=True, exist_ok=True)
    db = Db(cfg.db_path); s = Sightings(cfg, db)
    reader = FakeReader([[plate("MH12AB1234")], []])
    it = Intake(cfg, db, reader, s)
    a = drop(cfg, name="14.20.13[M][0@0][0].jpg"); b = drop(cfg, name="14.20.14[M][0@0][0].jpg")
    stats = it.scan_once()
    assert stats["read"] == 2 and reader.calls == 2
    assert a.exists() and b.exists()                                           # kept: the camera asks if they are there
    assert db.one("SELECT COUNT(*) AS n FROM delivered")["n"] == 2              # but remembered as done
    assert it.scan_once()["read"] == 0 and reader.calls == 2                  # and never read twice
    g = s.grouped()
    assert len(g) == 1 and g[0]["plate"] == "MH12AB1234"
    cam = db.one("SELECT * FROM cameras")
    assert cam["serial"] == "2KN9B54Y6F9XYK4Y" and cam["uploads"] == 2
    kinds = [r["kind"] for r in db.all("SELECT kind FROM events ORDER BY id")]
    assert kinds == ["camera_connected", "first_picture"]


def test_intake_waits_for_unfinished_files_and_survives_garbage(cfg):
    cfg.media_dir.mkdir(parents=True, exist_ok=True)
    db = Db(cfg.db_path); s = Sightings(cfg, db)
    cfg.intake_settle_s = 5.0
    it = Intake(cfg, db, FakeReader(), s)
    fresh = drop(cfg, age=0.1)
    junk = drop(cfg, name="14.20.20[M].jpg", data=b"not a picture", age=30)
    it.scan_once()
    assert fresh.exists()                    # may still be mid-upload: left alone
    assert db.one("SELECT 1 FROM delivered WHERE path LIKE '%14.20.20%'")   # not a picture: dealt with, no crash


def test_intake_ignores_other_files_symlinks_and_bad_serials(cfg):
    import os
    cfg.media_dir.mkdir(parents=True, exist_ok=True)
    db = Db(cfg.db_path)
    it = Intake(cfg, db, FakeReader(), Sightings(cfg, db))
    marker = cfg.intake_dir / "2KN9B54Y6F9XYK4Y" / "IPCWorkDirectory"
    marker.parent.mkdir(parents=True); marker.write_bytes(b"x" * 10)
    (cfg.intake_dir / "bad serial!").mkdir()
    (cfg.intake_dir / "bad serial!" / "a.jpg").write_bytes(jpeg_bytes())
    target = cfg.data_dir / "secret.jpg"; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(jpeg_bytes())
    try:
        os.symlink(target, cfg.intake_dir / "2KN9B54Y6F9XYK4Y" / "link.jpg")
    except OSError:
        pass
    it.scan_once()
    assert marker.exists()                                                     # not a picture: left alone
    assert (cfg.intake_dir / "bad serial!" / "a.jpg").exists()                 # not a serial: never touched
    assert target.exists()                                                     # a link is never followed or deleted
    assert db.one("SELECT COUNT(*) AS n FROM cameras")["n"] == 1               # the marker alone means "camera connected"


def test_backlog_keeps_the_newest():
    c = [Candidate(None, float(i), 1) for i in range(10)]
    drop_, take = plan_backlog(c, 4, 25)
    assert [x.mtime for x in drop_] == [0, 1, 2, 3, 4, 5]
    assert [x.mtime for x in take] == [6, 7, 8, 9]
    assert len(plan_backlog(c, 100, 3)[1]) == 3


# ---- network and camera discovery -----------------------------------------------------------
def test_nmcli_terse_parsing():
    assert netmgr.split_terse(r"*:My\:Net:80:WPA2") == ["*", "My:Net", "80", "WPA2"]
    text = "*:Office:82:WPA2\n:Office:60:WPA2\n:Guest:64:\n::40:WPA2\n:Back\\\\slash:50:WPA3\n"
    nets = netmgr.parse_wifi_list(text)
    assert [n.ssid for n in nets] == ["Office", "Guest", "Back\\slash"]
    assert nets[0].in_use and nets[0].signal == 82 and nets[0].secured
    assert not nets[1].secured


def test_fake_backend_wifi():
    n = netmgr.FakeBackend("10.0.0.5")
    assert n.status().ip == "10.0.0.5"
    assert n.connect("Office-WiFi", "")[0] is False
    assert n.connect("Office-WiFi", "wrong")[0] is False
    assert n.connect("Office-WiFi", "right")[0] is True and n.status().ssid == "Office-WiFi"
    assert n.connect("Guest", None)[0] is True


def test_onvif_probe_match_parsing():
    xml = ('<s:Envelope><s:Body><d:ProbeMatches><d:ProbeMatch>'
           '<d:Types>dn:NetworkVideoTransmitter</d:Types>'
           '<d:Scopes>onvif://www.onvif.org/name/CPPLUS onvif://www.onvif.org/hardware/CP-UNC-TA41L6C-D-Q2 '
           'onvif://www.onvif.org/Profile/Streaming</d:Scopes>'
           '<d:XAddrs>http://192.168.29.207/onvif/device_service</d:XAddrs>'
           '</d:ProbeMatch></d:ProbeMatches></s:Body></s:Envelope>')
    c = discovery.parse_probe_match(xml, "192.168.29.207")
    assert c.ip == "192.168.29.207" and c.name == "CPPLUS" and c.hardware == "CP-UNC-TA41L6C-D-Q2"
    assert "CP-UNC" in c.label and c.via == ["onvif"]
    assert discovery.parse_probe_match("<hello/>", "1.2.3.4") is None
    assert discovery.parse_probe_match("<ProbeMatch><Types>other</Types></ProbeMatch>", "1.2.3.4") is None


def test_local_ip_towards_returns_something_on_loopback():
    assert netmgr.local_ip_towards("127.0.0.1") == "127.0.0.1"


def test_netwatch_raises_hotspot_only_after_the_grace_period():
    from platebox.system import NetWatch

    class Net:
        def __init__(self): self.kind, self.started = None, 0
        def status(self): return netmgr.NetStatus(connected=False, kind=self.kind, has_wifi=True)
        def start_hotspot(self): self.started += 1

    n = Net(); w = NetWatch(n)
    assert w.tick(now=0) == "waiting"
    assert w.tick(now=30) == "waiting" and n.started == 0
    assert w.tick(now=50) == "hotspot-started" and n.started == 1
    n.kind = "wifi"
    assert w.tick(now=60) == "online"


# ---- what the camera sent before is not sent again, and an old picture is not new movement ---------------------
class FixedClock:
    def __init__(self, now):
        self.now = now

    def now_local(self):
        return self.now


def test_a_picture_the_camera_asks_about_is_still_there(cfg):
    """The camera asks 'is this picture there?' and re-sends it if not: a processed picture must stay."""
    cfg.media_dir.mkdir(parents=True, exist_ok=True)
    db = Db(cfg.db_path)
    it = Intake(cfg, db, FakeReader([[]]), Sightings(cfg, db))
    f = drop(cfg)
    it.scan_once()
    assert f.exists() and f.stat().st_size > 0                    # same file, same size: the camera's check passes


def test_old_kept_pictures_are_let_go_by_age_and_by_space(cfg):
    cfg.media_dir.mkdir(parents=True, exist_ok=True)
    db = Db(cfg.db_path)
    it = Intake(cfg, db, FakeReader(), Sightings(cfg, db))
    it.clock = FixedClock(None)
    a = drop(cfg, name="14.20.13[M][0@0][0].jpg"); b = drop(cfg, name="14.20.14[M][0@0][0].jpg")
    it.scan_once()
    assert it.prune_delivered(time.time()) == 0 and a.exists() and b.exists()
    assert it.prune_delivered(time.time() + 13 * 3600) == 2 and not a.exists() and not b.exists()   # past 12 hours
    c = drop(cfg, name="14.20.15[M][0@0][0].jpg"); d = drop(cfg, name="14.20.16[M][0@0][0].jpg")
    it.scan_once()
    cfg.delivered_max_mb = 0                                                                         # off: nothing is kept
    it.prune_delivered(time.time())
    assert not c.exists() and not d.exists()


def test_a_picture_the_camera_stamped_long_ago_is_ignored_not_read(cfg):
    from datetime import datetime

    cfg.media_dir.mkdir(parents=True, exist_ok=True)
    db = Db(cfg.db_path); s = Sightings(cfg, db)
    reader = FakeReader([[plate("MH12AB1234")]])
    it = Intake(cfg, db, reader, s)
    it.clock = FixedClock(datetime(2026, 10, 22, 10, 12, 0))          # the camera's clock now
    old = drop(cfg, name="14.20.13[M][0@0][0].jpg")                    # in a folder dated 2026-10-18: days old
    st = it.scan_once()
    assert st["stale"] == 1 and st["read"] == 0 and reader.calls == 0 and s.grouped() == []
    assert db.get("stale_ignored") == "1" and old.exists()             # kept, so the camera stops sending it
    assert it.scan_once()["stale"] == 0                                # and not judged twice


def test_a_new_picture_is_read_and_so_is_everything_when_the_clock_is_unknown(cfg):
    from datetime import datetime

    cfg.media_dir.mkdir(parents=True, exist_ok=True)
    db = Db(cfg.db_path); s = Sightings(cfg, db)
    reader = FakeReader([[plate("MH12AB1234")], []])
    it = Intake(cfg, db, reader, s)
    it.clock = FixedClock(datetime(2026, 10, 18, 14, 20, 40))          # 27 seconds after the picture was taken
    drop(cfg, name="14.20.13[M][0@0][0].jpg")
    assert it.scan_once()["read"] == 1 and s.grouped()[0]["plate"] == "MH12AB1234"
    it.clock = FixedClock(None)                                        # cannot read the camera's clock: judge nothing old
    drop(cfg, name="14.20.14[M][0@0][0].jpg")
    st = it.scan_once()
    assert st["read"] == 1 and st["stale"] == 0
    db.put("stale_minutes", "0")                                       # switched off
    it.clock = FixedClock(datetime(2030, 1, 1))
    drop(cfg, name="14.20.15[M][0@0][0].jpg")
    assert it.scan_once()["stale"] == 0


def test_the_cameras_clock_is_read_from_its_onvif_answer():
    from platebox.camclock import parse_camera_stamp, parse_local_time

    xml = ("<s:Envelope><s:Body><tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime>"
           "<tt:UTCDateTime><tt:Time><tt:Hour>14</tt:Hour><tt:Minute>42</tt:Minute><tt:Second>1</tt:Second></tt:Time>"
           "<tt:Date><tt:Year>2026</tt:Year><tt:Month>10</tt:Month><tt:Day>21</tt:Day></tt:Date></tt:UTCDateTime>"
           "<tt:LocalDateTime><tt:Time><tt:Hour>20</tt:Hour><tt:Minute>12</tt:Minute><tt:Second>1</tt:Second></tt:Time>"
           "<tt:Date><tt:Year>2026</tt:Year><tt:Month>10</tt:Month><tt:Day>21</tt:Day></tt:Date></tt:LocalDateTime>"
           "</tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse></s:Body></s:Envelope>")
    t = parse_local_time(xml)
    assert (t.hour, t.minute, t.day) == (20, 12, 21)                   # the LOCAL time, not the UTC one
    assert parse_local_time("<nothing/>") is None
    assert parse_camera_stamp("2026-10-19 03:29:32").minute == 29 and parse_camera_stamp("junk") is None


def test_the_motion_log_does_not_log_the_same_picture_twice(cfg):
    from platebox.motionlog import MotionLog

    cfg.media_dir.mkdir(parents=True, exist_ok=True)
    db = Db(cfg.db_path)
    cam = db.run("INSERT INTO cameras (serial, first_seen, last_seen) VALUES ('X1234', 'a', 'a')")
    m = MotionLog(cfg, db)
    m.configure(True, 50, 0)
    import cv2, numpy as np
    img = cv2.imdecode(np.frombuffer(jpeg_bytes(), np.uint8), cv2.IMREAD_COLOR)
    assert m.record(cam, img, "2026-10-19 03:29:32", []) is not None
    assert m.record(cam, img, "2026-10-19 03:29:32", []) is None       # the camera sent it again
    assert m.record(cam, img, "2026-10-19 03:29:33", []) is not None


def test_a_camera_that_only_gives_utc_is_still_understood():
    from datetime import timedelta
    from platebox.camclock import parse_local_time

    # The real answer of the CP Plus camera: UTC only, and a time-zone text that is NOT in the standard way of writing
    # ('GMT+05:30' here means five and a half hours AHEAD; read the standard way it is behind, which made PlateBox
    # believe the camera's clock was 11 hours slow). The zone text is ignored: this computer's offset is used.
    real = ("<tds:SystemDateAndTime><tt:DateTimeType>Manual</tt:DateTimeType><tt:TimeZone><tt:TZ>GMT+05:30PDT,M1.1.1/00:00:00,"
            "M1.1.2/00:00:00</tt:TZ></tt:TimeZone><tt:UTCDateTime><tt:Time><tt:Hour>6</tt:Hour><tt:Minute>35</tt:Minute>"
            "<tt:Second>33</tt:Second></tt:Time><tt:Date><tt:Year>2026</tt:Year><tt:Month>10</tt:Month><tt:Day>22</tt:Day>"
            "</tt:Date></tt:UTCDateTime></tds:SystemDateAndTime>")
    t = parse_local_time(real, timedelta(hours=5, minutes=30))
    assert (t.day, t.hour, t.minute, t.second) == (22, 12, 5, 33)          # 06:35:33 UTC is 12:05:33 in India
    assert parse_local_time(real, timedelta(hours=0)).hour == 6
    assert parse_local_time("<nothing/>") is None

def test_the_cameras_clock_can_come_from_its_web_page_or_from_plateboxs_own_time(cfg):
    from datetime import timedelta
    from platebox.camclock import CameraClock, parse_http_date

    t = parse_http_date("Thu, 22 Oct 2026 05:12:01 GMT", timedelta(hours=5, minutes=30))
    assert (t.hour, t.minute, t.day) == (10, 42, 22)                      # GMT -> this computer's local time
    assert parse_http_date("not a date") is None

    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    db = Db(cfg.db_path)
    db.put("camera_ip", "192.0.2.77")                                      # a documentation address: nothing answers
    clock = CameraClock(db)
    assert clock.now_local() is None and "did not answer" in clock.problem   # cannot be read: nothing is judged
    assert "cannot read the camera's clock" in clock.describe()
    clock2 = CameraClock(db)
    clock2.synced_from_us = lambda: True                                   # the camera takes its time from PlateBox
    assert clock2.now_local() is not None and "takes its time from PlateBox" in clock2.how
    assert "reads" in clock2.describe()


def test_the_helper_knows_who_asked_it(cfg):
    from platebox.offline_helper import OfflineHelper

    db = Db(cfg.db_path)
    h = OfflineHelper(cfg, db)
    assert not h.heard("ntp", 3600)
    h.seen["ntp 192.168.1.8"] = time.time()
    assert h.heard("ntp", 3600) and not h.heard("dns", 3600)
    h.seen["ntp 192.168.1.8"] = time.time() - 7200
    assert not h.heard("ntp", 3600)


def test_the_same_picture_sent_again_under_a_new_name_is_ignored(cfg):
    """The camera re-sends stored pictures with new file names and the picture's own old time printed on it."""
    cfg.media_dir.mkdir(parents=True, exist_ok=True)
    db = Db(cfg.db_path); s = Sightings(cfg, db)
    reader = FakeReader([[plate("MH12AB1234")], [plate("MH12AB1234")], []])
    it = Intake(cfg, db, reader, s)
    same = jpeg_bytes()
    drop(cfg, name="14.20.13[M][0@0][0].jpg", data=same)
    assert it.scan_once()["read"] == 1 and reader.calls == 1
    drop(cfg, name="23.46.40[M][0@0][0].jpg", data=same)               # a different name and time: the same picture
    it.scan_once()
    assert reader.calls == 1 and db.get("repeat_ignored") == "1"       # not read again
    assert db.one("SELECT uploads FROM cameras")["uploads"] == 1       # and not counted as a new picture
    drop(cfg, name="23.46.41[M][0@0][0].jpg", data=jpeg_bytes(shade=121))   # a different picture is read as usual
    it.scan_once()
    assert reader.calls == 2


def test_the_support_file_has_what_helps_and_nothing_secret(make_app, cfg):
    from fastapi.testclient import TestClient

    with TestClient(make_app(), follow_redirects=False) as c:
        box = c.app.state.box
        c.post("/setup/claim", data={"code": box.setup_code(), "password": "a long password", "confirm": "a long password"})
        c.post("/api/camera/select", json={"ip": "192.168.1.8", "label": "x"})
        c.post("/api/setup/finish", json={"force": True})
        secret = box.sftp.password()
        r = c.get("/settings/support.txt")
        assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
        assert "== Camera" in r.text and "== Address guard" in r.text and "== Log" in r.text
        assert secret not in r.text and "a long password" not in r.text and box.db.get("admin_hash") not in r.text
        c.cookies.clear()
        assert c.get("/settings/support.txt").status_code in (303, 401)    # only for the signed-in administrator
