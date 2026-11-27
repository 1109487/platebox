import asyncio
import os
import socket
import time
from pathlib import Path

import pytest

from platebox import launcher, netmgr
from platebox.config import Config
from platebox.db import Db
from platebox.intake import Intake
from platebox.sftp_server import BuiltinSftp
from platebox.sightings import Sightings

from conftest import FakeReader, jpeg_bytes, plate

asyncssh = pytest.importorskip("asyncssh")

INTERFACES = """
There is 1 interface on the system:

    Name                   : Wi-Fi
    Description            : MediaTek Wi-Fi 6 MT7921 Wireless LAN Card
    GUID                   : 69477e7d-5e6f-43c9-8983-0a3c6fabf6ed
    Physical address       : c8:a3:e8:c1:a3:f9
    Interface type         : Primary
    State                  : connected
    SSID                   : OfficeNet
    AP BSSID               : d2:a9:15:2c:e5:88
    Band                   : 5 GHz
    Authentication         : WPA2-Personal
    Signal                 : 79%
"""

NETWORKS = """
Interface name : Wi-Fi
There are 3 networks currently visible.

SSID 1 : OfficeNet
    Network type            : Infrastructure
    Authentication          : WPA2-Personal
    Encryption              : CCMP
    BSSID 1                 : d2:a9:15:2c:e5:88
         Signal             : 39%
         Radio type         : 802.11ac
    BSSID 2                 : d2:a9:15:2c:e5:89
         Signal             : 81%

SSID 2 : Guest
    Network type            : Infrastructure
    Authentication          : Open
    Encryption              : None
    BSSID 1                 : aa:bb:cc:dd:ee:ff
         Signal             : 55%

SSID 3 :
    Network type            : Infrastructure
    Authentication          : WPA2-Personal
    BSSID 1                 : 11:22:33:44:55:66
         Signal             : 70%
"""


# ---- Windows Wi-Fi ----------------------------------------------------------------------------
def test_netsh_interfaces_parsing():
    w = netmgr.parse_netsh_interfaces(INTERFACES)
    assert w == {"name": "Wi-Fi", "state": "connected", "ssid": "OfficeNet", "signal": 79}
    assert netmgr.parse_netsh_interfaces("There is no wireless interface on the system.") == {}


def test_netsh_networks_parsing_takes_the_strongest_radio_and_skips_hidden():
    nets = netmgr.parse_netsh_networks(NETWORKS)
    assert [(n.ssid, n.signal, n.secured) for n in nets] == [("OfficeNet", 81, True), ("Guest", 55, False)]


def test_profile_xml_escapes_and_picks_the_right_security():
    x = netmgr.wlan_profile_xml('A&B <"net">', "p<a>ss&word")
    assert "A&amp;B &lt;" in x and "p&lt;a&gt;ss&amp;word" in x and "WPA2PSK" in x and "<" not in x.split("<name>")[1].split("</name>")[0]
    assert "WPA3SAE" in netmgr.wlan_profile_xml("n", "pw", security="WPA3-Personal")
    open_ = netmgr.wlan_profile_xml("Guest", None)
    assert "<authentication>open</authentication>" in open_ and "keyMaterial" not in open_


def test_windows_connect_adds_a_profile_then_connects_and_never_leaves_the_password(monkeypatch, tmp_path):
    calls, seen_files = [], []

    class R:
        def __init__(self, code=0, out=""):
            self.returncode, self.stdout, self.stderr = code, out, ""

    def fake_netsh(self, *args, timeout=25):
        calls.append(args)
        if args[:3] == ("wlan", "show", "networks"):
            return R(0, NETWORKS)
        if args[:2] == ("wlan", "add"):
            path = [a for a in args if a.startswith("filename=")][0][9:]
            seen_files.append(path)
            assert "hunter2" in Path(path).read_text()
        return R(0)

    b = netmgr.WindowsBackend()
    monkeypatch.setattr(netmgr.WindowsBackend, "_netsh", fake_netsh)
    state = {"n": 0}

    def fake_wifi(self):
        state["n"] += 1
        return {"name": "Wi-Fi", "state": "connected" if state["n"] > 2 else "disconnected", "ssid": "OfficeNet"}

    monkeypatch.setattr(netmgr.WindowsBackend, "_wifi", fake_wifi)
    monkeypatch.setattr("time.sleep", lambda s: None)
    ok, msg = b.connect("OfficeNet", "hunter2")
    assert ok, msg
    kinds = [c[:2] for c in calls]
    assert ("wlan", "add") in kinds and ("wlan", "connect") in kinds
    assert not Path(seen_files[0]).exists(), "the profile file holds the Wi-Fi password and must be deleted"


def test_windows_status_without_wifi_is_ethernet(monkeypatch):
    monkeypatch.setattr(netmgr.WindowsBackend, "_wifi", lambda self: {})
    monkeypatch.setattr(netmgr, "local_ip_towards", lambda ip: "192.168.1.9")
    st = netmgr.WindowsBackend().status()
    assert st.connected and st.kind == "ethernet" and st.ip == "192.168.1.9" and not st.has_wifi


# ---- launcher ---------------------------------------------------------------------------------
def test_port_choice_skips_a_busy_port_and_respects_a_wish():
    s = socket.socket()
    s.bind(("0.0.0.0", 0))
    busy = s.getsockname()[1]
    try:
        free = launcher.choose_port(preferred=(busy, busy + 1000 if busy < 60000 else busy - 1000))
        assert free != busy
        assert launcher.choose_port(preferred=(busy,), wanted=1234) == 1234
        with pytest.raises(OSError):
            launcher.choose_port(preferred=(busy,))
    finally:
        s.close()


def test_models_are_copied_once_and_home_points_at_them(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle" / "models_home" / ".cache" / "open-image-models"
    bundle.mkdir(parents=True)
    (bundle / "m.onnx").write_bytes(b"model")
    monkeypatch.setattr(launcher, "bundle_dir", lambda: tmp_path / "bundle")
    home = launcher.prepare_models(tmp_path / "data")
    assert (home / ".cache" / "open-image-models" / "m.onnx").read_bytes() == b"model"
    assert os.environ["HOME"] == str(home)
    (home / ".cache" / "open-image-models" / "m.onnx").write_bytes(b"changed")
    launcher.prepare_models(tmp_path / "data")                       # second run must not overwrite
    assert (home / ".cache" / "open-image-models" / "m.onnx").read_bytes() == b"changed"
    monkeypatch.setattr(launcher, "bundle_dir", lambda: tmp_path / "nothing")
    assert launcher.prepare_models(tmp_path / "data2") is None


def test_a_second_copy_does_not_start(monkeypatch):
    monkeypatch.setattr(launcher, "_LOCK", None)
    port = free_port()
    assert launcher.single_instance(port) is True
    assert launcher.single_instance(port) is False                    # the lock is held by the first
    launcher._LOCK.close()
    assert launcher.single_instance(port) is True                     # released when the first goes away
    launcher._LOCK.close()


def test_open_waits_for_the_program_to_come_up(tmp_path, monkeypatch):
    opened, state = [], {"n": 0}
    monkeypatch.setattr("webbrowser.open", lambda u: opened.append(u))
    monkeypatch.setattr("time.sleep", lambda s: None)

    def listening(p):
        state["n"] += 1
        return state["n"] >= 3                                        # not up for the first two looks

    monkeypatch.setattr(launcher, "listening", listening)
    (tmp_path / "public").mkdir()
    (tmp_path / "public" / "port.txt").write_text("8080")
    assert launcher.open_browser(tmp_path, wait_s=30) == 0 and opened == ["http://localhost:8080"] and state["n"] == 3


def test_open_uses_the_recorded_port(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda u: opened.append(u))
    monkeypatch.setattr(launcher, "listening", lambda p: True)
    (tmp_path / "public").mkdir()
    (tmp_path / "public" / "port.txt").write_text("8080")
    assert launcher.open_browser(tmp_path, wait_s=0) == 0 and opened == ["http://localhost:8080"]
    (tmp_path / "public" / "port.txt").write_text("80")
    launcher.open_browser(tmp_path, wait_s=0)
    assert opened[-1] == "http://localhost"
    assert launcher.open_browser(tmp_path / "missing", wait_s=0) == 1


# ---- the built-in camera receiver ---------------------------------------------------------------
def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def receiver(tmp_path, monkeypatch):
    monkeypatch.setattr("platebox.sftp_server.FAIL_DELAY_BASE_S", 0.05)      # keep the tests quick
    cfg = Config(data_dir=tmp_path / "data", sftp_root=tmp_path / "sftp", sftp_mode="builtin", sftp_host="127.0.0.1",
                 sftp_port=free_port(), fake_net=True, background=False)
    cfg.data_dir.mkdir(parents=True)
    db = Db(cfg.db_path)
    db.put("sftp_password", "Pa55word1234abcd")
    r = BuiltinSftp(cfg, db)
    assert r.start(), r.error
    yield cfg, db, r
    r.stop()


def connect(cfg, user="ftpcam", password="Pa55word1234abcd"):
    return asyncssh.connect("127.0.0.1", cfg.sftp_port, username=user, password=password, known_hosts=None,
                            client_keys=None, agent_path=None)


def test_the_camera_can_upload_into_its_folder_and_only_there(receiver):
    cfg, db, r = receiver

    async def go():
        async with connect(cfg) as conn:
            async with conn.start_sftp_client() as sftp:
                assert sorted(set(await sftp.listdir("/")) - {".", ".."}) == ["upload"]   # the whole world it can see
                await sftp.makedirs("upload/2KN9B54Y6F9XYK4Y/2026-10-18/pic_001/14", exist_ok=True)
                await sftp.put(__file__, "upload/2KN9B54Y6F9XYK4Y/2026-10-18/pic_001/14/14.20.13[M][0@0][0].jpg")
                # "up and out" is held at the top of its own folder: it still sees only `upload`
                assert sorted(set(await sftp.listdir("/../../..")) - {".", ".."}) == ["upload"]
                with pytest.raises(Exception):
                    await sftp.listdir("/etc")                                           # nothing else exists for it
                with pytest.raises(Exception):
                    await sftp.get("/../../../../etc/hostname", str(Path(__file__).parent / "never"))

    asyncio.run(go())
    landed = list((cfg.sftp_root / "upload").rglob("*.jpg"))
    assert len(landed) == 1 and landed[0].read_bytes() == Path(__file__).read_bytes()
    assert r.last_login


def test_a_missing_folder_is_reported_missing_so_a_camera_can_create_it(receiver):
    """The CP Plus 'test' button opens a folder, and only if that fails does it create it, then writes a file."""
    cfg, db, r = receiver

    async def go():
        async with connect(cfg) as conn:
            async with conn.start_sftp_client() as sftp:
                with pytest.raises(asyncssh.SFTPNoSuchFile):
                    await sftp.listdir("/upload/FTP_TEST/2026-10-181792324368/")
                await sftp.makedirs("/upload/FTP_TEST/2026-10-181792324368", exist_ok=True)
                async with sftp.open("/upload/FTP_TEST/2026-10-181792324368/ftp_test.txt", "wb") as f:
                    await f.write(b"test")
                assert "ftp_test.txt" in await sftp.listdir("/upload/FTP_TEST/2026-10-181792324368/")
                await sftp.remove("/upload/FTP_TEST/2026-10-181792324368/ftp_test.txt")

    asyncio.run(go())


def test_wrong_logins_are_refused_and_a_changed_password_applies_at_once(receiver):
    cfg, db, r = receiver

    async def refused(**kw):
        try:
            async with connect(cfg, **kw):
                return False
        except asyncssh.PermissionDenied:
            return True

    assert asyncio.run(refused(password="nope")) is True
    assert asyncio.run(refused(user="root")) is True
    assert asyncio.run(refused(user="ftpcam", password="")) is True
    db.put("sftp_password", "NewPassword12345x")
    assert asyncio.run(refused(password="Pa55word1234abcd")) is True                    # the old one is dead
    assert asyncio.run(refused(password="NewPassword12345x")) is False                  # the new one works


def test_no_shell_no_commands_no_forwarding(receiver):
    cfg, db, r = receiver

    async def go():
        async with connect(cfg) as conn:
            with pytest.raises(Exception):
                await conn.run("id", check=True)
            with pytest.raises(Exception):
                await conn.open_connection("127.0.0.1", 80)                           # asking the server to dial out

    asyncio.run(go())


def test_host_key_is_kept_across_restarts(tmp_path):
    from platebox.sftp_server import ensure_host_keys
    a = ensure_host_keys(tmp_path / "keys")
    before = [Path(p).read_bytes() for p in a]
    b = ensure_host_keys(tmp_path / "keys")
    assert a == b and before == [Path(p).read_bytes() for p in b] and len(a) == 3


def test_a_port_already_in_use_is_reported_not_fatal(tmp_path):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    cfg = Config(data_dir=tmp_path / "d", sftp_root=tmp_path / "s", sftp_mode="builtin", sftp_host="127.0.0.1",
                 sftp_port=s.getsockname()[1], fake_net=True, background=False)
    cfg.data_dir.mkdir(parents=True)
    r = BuiltinSftp(cfg, Db(cfg.db_path))
    try:
        assert r.start() is False and "port" in r.error.lower() and not r.listening
    finally:
        s.close()
        r.stop()


def test_a_picture_uploaded_by_sftp_becomes_a_plate(receiver):
    """The whole chain without the web page: camera uploads over SFTP, the intake reads it."""
    cfg, db, r = receiver
    cfg.media_dir.mkdir(parents=True, exist_ok=True)

    async def go():
        async with connect(cfg) as conn:
            async with conn.start_sftp_client() as sftp:
                await sftp.makedirs("upload/CAMSER0001/2026-10-18/pic_001/14", exist_ok=True)
                async with sftp.open("upload/CAMSER0001/2026-10-18/pic_001/14/14.20.13[M][0@0][0].jpg", "wb") as f:
                    await f.write(jpeg_bytes())

    asyncio.run(go())
    f = next((cfg.sftp_root / "upload").rglob("*.jpg"))
    old = time.time() - 10
    os.utime(f, (old, old))
    s = Sightings(cfg, db)
    Intake(cfg, db, FakeReader([[plate("MH12AB1234")]]), s).scan_once()
    assert s.grouped()[0]["plate"] == "MH12AB1234" and db.one("SELECT 1 FROM delivered") is not None


def test_the_camera_finds_a_picture_it_already_sent_so_it_does_not_send_it_again(receiver):
    """The camera checks 'is this file there?' before uploading. After PlateBox has read it, it must still be."""
    cfg, db, r = receiver
    cfg.media_dir.mkdir(parents=True, exist_ok=True)
    path = "upload/CAMSER0001/2026-10-18/pic_001/14/14.20.13[M][0@0][0].jpg"
    data = jpeg_bytes()

    async def send():
        async with connect(cfg) as conn:
            async with conn.start_sftp_client() as sftp:
                await sftp.makedirs("upload/CAMSER0001/2026-10-18/pic_001/14", exist_ok=True)
                async with sftp.open(path, "wb") as f:
                    await f.write(data)

    async def ask():
        async with connect(cfg) as conn:
            async with conn.start_sftp_client() as sftp:
                async with sftp.open(path, "rb") as f:                 # what the camera's check does
                    return (await f.stat()).size

    asyncio.run(send())
    f = next((cfg.sftp_root / "upload").rglob("*.jpg"))
    old = time.time() - 10
    os.utime(f, (old, old))
    Intake(cfg, db, FakeReader([[]]), Sightings(cfg, db)).scan_once()
    assert asyncio.run(ask()) == len(data)


def test_the_camera_gets_a_real_answer_to_how_much_room_is_there(receiver):
    """The CP Plus asks (statvfs) before each upload; the library used to answer 'unsupported' on Windows."""
    cfg, db, r = receiver

    async def go():
        async with connect(cfg) as conn:
            async with conn.start_sftp_client() as sftp:
                # The Windows server does not advertise this request (the library thinks Windows has none), so a polite
                # client would not send it. The camera's libssh2 sends it regardless: do the same.
                sftp._handler._supports_statvfs = True
                return await sftp.statvfs("/")

    v = asyncio.run(go())
    assert v.bavail > 0 and v.frsize == 4096 and v.namemax == 255


def test_a_detailed_trace_switches_itself_on_and_off(receiver):
    import logging

    cfg, db, r = receiver
    before = logging.getLogger("asyncssh.sftp").level
    r.trace(0.3)
    assert r.tracing() > 0 and logging.getLogger("asyncssh.sftp").level == logging.DEBUG
    time.sleep(0.8)
    assert r.tracing() == 0 and logging.getLogger("asyncssh.sftp").level == before
