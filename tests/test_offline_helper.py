"""PlateBox answers the camera's name and time lookups, so a site with no internet does not stall the camera."""
import socket
import struct
import time

import pytest
from fastapi.testclient import TestClient

from platebox.db import Db
from platebox.offline_helper import NTP_EPOCH_OFFSET, OfflineHelper, dns_reply, ntp_reply


def dns_query(name="time.example.com", qtype=1, tid=b"\x12\x34", rd=True):
    q = tid + struct.pack("!HHHHH", 0x0100 if rd else 0, 1, 0, 0, 0)
    for label in name.split("."):
        q += bytes([len(label)]) + label.encode()
    return q + b"\x00" + struct.pack("!HH", qtype, 1)


def test_every_name_is_found_at_this_computer():
    r = dns_reply(dns_query(), "192.168.1.4")
    assert r[:2] == b"\x12\x34"                                          # same id
    flags, qd, an = struct.unpack("!HHH", r[2:8])
    assert flags & 0x8000 and flags & 0x0100 and flags & 0x0080 and flags & 0x000F == 0   # response, RD, RA, no error
    assert qd == 1 and an == 1
    assert r[-4:] == socket.inet_aton("192.168.1.4")                      # the address at the end of the A record
    assert r[-16:-14] == b"\xc0\x0c"                                      # points back at the question's name


def test_other_kinds_of_question_get_an_empty_successful_answer():
    r = dns_reply(dns_query(qtype=28), "192.168.1.4")                    # AAAA
    flags, qd, an = struct.unpack("!HHH", r[2:8])
    assert an == 0 and flags & 0x000F == 0 and qd == 1


def test_junk_is_ignored_not_answered():
    assert dns_reply(b"", "1.2.3.4") is None
    assert dns_reply(b"\x00" * 12, "1.2.3.4") is None
    assert dns_reply(dns_query()[:20], "1.2.3.4") is None                 # cut short
    response = bytearray(dns_query()); response[2] |= 0x80
    assert dns_reply(bytes(response), "1.2.3.4") is None                 # a response is not a question
    bad = bytearray(dns_query()); bad[12] = 0xC0                         # a compression pointer in a question
    assert dns_reply(bytes(bad), "1.2.3.4") is None


def ntp_request(version=4, mode=3, transmit=b"\x01\x02\x03\x04\x05\x06\x07\x08"):
    p = bytearray(48)
    p[0] = (version << 3) | mode
    p[40:48] = transmit
    return bytes(p)


def test_the_time_answer_carries_this_computers_clock_and_echoes_the_request():
    now = 1_800_000_000.5
    r = ntp_reply(ntp_request(version=3), now=now)
    assert len(r) == 48 and r[0] & 7 == 4 and (r[0] >> 3) & 7 == 3          # server mode, the client's version
    assert r[24:32] == b"\x01\x02\x03\x04\x05\x06\x07\x08"                   # the client checks this echo
    sec = struct.unpack("!I", r[40:44])[0]
    assert sec == int(now) + NTP_EPOCH_OFFSET
    assert ntp_reply(b"short") is None and ntp_reply(ntp_request(mode=4)) is None


def free_udp():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def helper(cfg):
    cfg.sftp_host = "127.0.0.1"
    cfg.dns_port, cfg.ntp_port = free_udp(), free_udp()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    h = OfflineHelper(cfg, Db(cfg.db_path))
    h.start()
    yield h
    h.stop()


def ask(port, payload, wait=2.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(wait)
    try:
        s.sendto(payload, ("127.0.0.1", port))
        return s.recvfrom(512)[0]
    except socket.timeout:
        return None
    finally:
        s.close()


def test_it_really_answers_over_the_network(helper, cfg):
    assert helper.listening == {"dns": True, "ntp": True}
    t = time.time()
    r = ask(cfg.dns_port, dns_query("p2p.somecloud.example"))
    assert r and r[-4:] == socket.inet_aton("127.0.0.1") and time.time() - t < 1.0      # at once, not after a timeout
    n = ask(cfg.ntp_port, ntp_request())
    assert n and len(n) == 48 and abs(struct.unpack("!I", n[40:44])[0] - NTP_EPOCH_OFFSET - time.time()) < 3
    assert any(x["what"].startswith("dns") for x in helper.status()["recent"])
    assert any(x["what"].startswith("ntp") for x in helper.status()["recent"])


def test_switched_off_means_silence(helper, cfg):
    helper.db.put("offline_helper", "0")
    assert ask(cfg.dns_port, dns_query(), wait=0.8) is None
    helper.db.put("offline_helper", "1")
    assert ask(cfg.dns_port, dns_query()) is not None


def test_a_busy_port_is_reported_and_does_not_stop_the_other_service(cfg):
    cfg.sftp_host = "127.0.0.1"
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    taken = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    taken.bind(("127.0.0.1", 0))
    cfg.dns_port, cfg.ntp_port = taken.getsockname()[1], free_udp()
    h = OfflineHelper(cfg, Db(cfg.db_path))
    try:
        h.start()
        assert h.listening == {"dns": False, "ntp": True} and "in use" in h.errors["dns"]
        assert ask(cfg.ntp_port, ntp_request()) is not None
    finally:
        h.stop()
        taken.close()


def test_the_settings_page_explains_and_can_switch_it(cfg, make_app):
    with TestClient(make_app(), follow_redirects=False) as c:
        box = c.app.state.box
        c.post("/setup/claim", data={"code": box.setup_code(), "password": "a long password", "confirm": "a long password"})
        c.post("/api/camera/select", json={"ip": "192.168.1.64", "label": "x"})
        c.post("/api/setup/finish", json={"force": True})
        page = c.get("/settings")
        assert "No internet at this site?" in page.text and "DNS server" in page.text and "NTP (time) server" in page.text
        assert "192.168.1.50" in page.text                                   # the address to type into the camera
        assert box.helper.enabled() is True
        assert "Saved" in c.post("/settings/offline-helper", data={}).text and box.helper.enabled() is False
        assert "Saved" in c.post("/settings/offline-helper", data={"enabled": "1"}).text and box.helper.enabled() is True
        assert c.post("/settings/offline-helper", data={}, headers={"origin": "http://evil.example"}).status_code == 403


def test_only_the_local_network_is_answered(helper, cfg, monkeypatch):
    import platebox.offline_helper as oh
    real = oh.ipaddress.ip_address
    monkeypatch.setattr(oh.ipaddress, "ip_address", lambda p: real("8.8.8.8"))      # pretend the asker is on the internet
    assert ask(cfg.dns_port, dns_query(), wait=0.8) is None
    monkeypatch.setattr(oh.ipaddress, "ip_address", real)
    assert ask(cfg.dns_port, dns_query()) is not None
