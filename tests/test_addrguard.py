"""The address guard: the camera sends to an address that is no longer this computer's."""
import pytest
from fastapi.testclient import TestClient

from platebox.addrguard import (AddressGuard, HostNet, Nic, NullHostNet, parse_ip_json, parse_neighbours,
                                parse_netsh_addresses)
from platebox.db import Db
from platebox.intake import is_camera_folder
from platebox.netmgr import FakeBackend

CAM_MAC = "f8:20:97:f1:f1:fb"


class FakeHost(NullHostNet):
    """A pretend operating system we can steer."""

    def __init__(self, nics):
        self._nics = list(nics)
        self.owners: dict[str, str] = {}              # address -> hardware address of whoever answers there
        self.up: set[str] = {"192.168.1.8"}           # addresses that answer on the camera's ports
        self.found: dict[str, str] = {}               # hardware address -> address, for the search
        self.added: list[tuple[str, str]] = []
        self.removed: list[tuple[str, str]] = []
        self.add_error = ""
        self.have: set[str] = set()                   # addresses currently added by us
        self.neigh: dict[str, str] = {}

    def nics(self):
        return list(self._nics)

    def neighbours(self):
        return dict(self.neigh)

    def owner_of(self, ip):
        return self.owners.get(ip)

    def reachable(self, ip):
        return ip in self.up

    def find_by_mac(self, mac, nic):
        return self.found.get(mac)

    def add_address(self, nic, ip):
        if self.add_error:
            return False, self.add_error
        self.added.append((nic.name, ip))
        self.have.add(ip)
        return True, "Ok."

    def remove_address(self, nic_name, ip):
        self.removed.append((nic_name, ip))
        self.have.discard(ip)

    def can_bind(self, ip):
        return ip in self.have


class FakeReceiver:
    def __init__(self):
        self.target = None

    def target_of(self, ip, within_s=1800):
        return self.target


@pytest.fixture
def world(cfg):
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.advertise_ip = "192.168.1.2"                       # this computer's own address, as the network gave it
    db = Db(cfg.db_path)
    db.put("camera_ip", "192.168.1.8")
    db.put("camera_mac", CAM_MAC)
    db.put("ip_told", "192.168.1.4")                       # what the camera was told long ago
    host = FakeHost([Nic("Wi-Fi", "192.168.1.2", 24)])
    rx = FakeReceiver()
    guard = AddressGuard(cfg, db, FakeBackend("192.168.1.2"), rx, lambda: True, lambda: db.get("camera_ip"), host=host)
    guard.rx, guard.db_, guard.host_ = rx, db, host
    return guard


def test_parsers():
    netsh = '''
Configuration for interface "Wi-Fi"
    DHCP enabled:                         Yes
    IP Address:                           192.168.1.2
    Subnet Prefix:                        192.168.1.0/24 (mask 255.255.255.0)
    IP Address:                           192.168.1.4
    Subnet Prefix:                        192.168.1.0/24 (mask 255.255.255.0)
    Default Gateway:                      192.168.1.1

Configuration for interface "Loopback Pseudo-Interface 1"
    IP Address:                           127.0.0.1
    Subnet Prefix:                        127.0.0.0/8 (mask 255.0.0.0)
'''
    nics = parse_netsh_addresses(netsh)
    assert [(n.name, n.ip, n.prefix) for n in nics[:2]] == [("Wi-Fi", "192.168.1.2", 24), ("Wi-Fi", "192.168.1.4", 24)]
    assert nics[0].mask == "255.255.255.0"
    assert parse_ip_json('[{"ifname":"eth0","addr_info":[{"family":"inet","local":"10.0.0.5","prefixlen":16}]},'
                         '{"ifname":"lo","addr_info":[{"family":"inet","local":"127.0.0.1","prefixlen":8}]}]') == [Nic("eth0", "10.0.0.5", 16)]
    arp = "  192.168.1.8           f8-20-97-f1-f1-fb     dynamic   \n  192.168.1.255         ff-ff-ff-ff-ff-ff     static    \n"
    assert parse_neighbours(arp) == {"192.168.1.8": CAM_MAC}
    assert parse_neighbours("192.168.1.9 dev eth0 lladdr AA:bb:cc:dd:ee:01 REACHABLE\n") == {"192.168.1.9": "aa:bb:cc:dd:ee:01"}
    assert HostNet is not None


def test_matching_addresses_mean_nothing_to_do(world):
    world.db_.put("ip_told", "192.168.1.2")
    st = world.tick()
    assert st["level"] == "ok" and st["code"] == "ok" and not world.host_.added


def test_a_changed_address_is_kept_working_without_touching_the_camera(world):
    st = world.tick()
    assert st["level"] == "fixed" and st["code"] == "kept" and st["claimed"] == "192.168.1.4"
    assert world.host_.added == [("Wi-Fi", "192.168.1.4")]
    assert world.db_.get("addr_claim") == "192.168.1.4|Wi-Fi"
    world.tick()
    assert len(world.host_.added) == 1                       # kept, not added again
    # the person later types the new address into the camera and says so: the extra address is let go
    st = world.guard_ack() if hasattr(world, "guard_ack") else world.acknowledge()
    assert st["level"] == "ok" and world.host_.removed == [("Wi-Fi", "192.168.1.4")]
    assert world.db_.get("ip_told") == "192.168.1.2"


def test_an_address_someone_else_has_is_not_taken(world):
    world.host_.owners["192.168.1.4"] = "aa:bb:cc:00:00:01"
    st = world.tick()
    assert st["level"] == "bad" and st["code"] == "taken" and not world.host_.added
    assert "192.168.1.2" in st["fix"] and "Server Address" in st["fix"]


def test_the_camera_sitting_on_the_address_it_sends_to_is_named(world):
    world.host_.owners["192.168.1.4"] = CAM_MAC             # exactly what happened on the D-Link router
    st = world.tick()
    assert st["code"] == "camera-has-it" and "camera itself" in st["text"]


def test_an_address_on_another_network_cannot_be_kept(world):
    world.db_.put("ip_told", "10.1.1.9")
    st = world.tick()
    assert st["code"] == "other-network" and not world.host_.added


def test_not_being_allowed_is_said_plainly(world):
    world.host_.add_error = "The requested operation requires elevation (Run as administrator)."
    st = world.tick()
    assert st["code"] == "need-admin" and "administrator" in st["text"]
    again = world.tick()                                     # not hammered every few seconds
    assert again["level"] == "bad"


def test_switched_off_still_says_what_to_type(world):
    world.db_.put("address_keep", "0")
    st = world.tick()
    assert st["code"] == "off" and not world.host_.added and "192.168.1.2" in st["text"]


def test_what_the_camera_really_connected_to_wins(world):
    world.rx.target = "192.168.1.2"                          # the camera already sends to this computer's own address
    st = world.tick()
    assert st["level"] == "ok" and world.db_.get("ip_told") == "192.168.1.2"
    world.rx.target = "192.168.1.7"                          # or to some other address of this computer
    world.host_.owners["192.168.1.7"] = None
    st = world.tick()
    assert world.db_.get("ip_told") == "192.168.1.7"


def test_the_camera_is_followed_when_the_router_moves_it(world):
    world.db_.put("ip_told", "192.168.1.2")
    world.host_.up.clear()
    world.host_.found[CAM_MAC] = "192.168.1.31"
    world.tick(now=1000.0)
    assert world.db_.get("camera_ip") == "192.168.1.8"       # not lost for long enough yet
    st = world.tick(now=1100.0)
    assert world.db_.get("camera_ip") == "192.168.1.31"
    assert "moved from 192.168.1.8 to 192.168.1.31" in st["notice"]


def test_the_camera_hardware_address_is_learned(world):
    world.db_.put("camera_mac", "")
    world.db_.put("ip_told", "192.168.1.2")
    world.host_.neigh["192.168.1.8"] = CAM_MAC
    world.tick()
    assert world.db_.get("camera_mac") == CAM_MAC


def test_nothing_happens_before_setup_is_finished(cfg):
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    db = Db(cfg.db_path)
    g = AddressGuard(cfg, db, FakeBackend("192.168.1.2"), None, lambda: False, lambda: "192.168.1.8", host=FakeHost([]))
    assert g.tick()["code"] == "setup"


def test_ftp_test_is_not_a_camera():
    assert is_camera_folder("2KN9B54Y6F9XYK4Y") and not is_camera_folder("FTP_TEST") and not is_camera_folder("ftp_test")
    assert not is_camera_folder("ab")


# -- through the web interface -------------------------------------------------------------------------------
def done_box(make_app, cfg):
    cfg.advertise_ip = "192.168.1.2"
    c = TestClient(make_app(), follow_redirects=False)
    c.__enter__()
    box = c.app.state.box
    c.post("/setup/claim", data={"code": box.setup_code(), "password": "a long password", "confirm": "a long password"})
    c.post("/api/camera/select", json={"ip": "192.168.1.8", "label": "x"})
    c.post("/api/setup/finish", json={"force": True})
    box.guard.host = FakeHost([Nic("Wi-Fi", "192.168.1.2", 24)])
    return c, box


def test_the_warning_is_on_every_page_and_clears_when_acknowledged(make_app, cfg):
    c, box = done_box(make_app, cfg)
    try:
        box.db.put("ip_told", "192.168.1.4")
        box.guard.host.owners["192.168.1.4"] = "aa:bb:cc:00:00:01"
        box.guard.tick()
        for path in ("/", "/motion", "/settings"):
            page = c.get(path)
            assert "The camera is sending pictures to the wrong address" in page.text, path
            assert "I have changed it on the camera" in page.text
        api = c.get("/api/address").json()
        assert api["level"] == "bad" and api["code"] == "taken"
        assert c.post("/api/address/ack", json={}, headers={"origin": "http://evil.example"}).status_code == 403
        assert c.post("/api/address/ack", json={}).json()["level"] == "ok"
        assert "wrong address" not in c.get("/").text
    finally:
        c.__exit__(None, None, None)


def test_settings_explain_and_switch_the_guard(make_app, cfg):
    c, box = done_box(make_app, cfg)
    try:
        page = c.get("/settings")
        assert "keeping the camera and this computer in touch" in page.text and "fixed addresses" in page.text
        assert box.guard.enabled() is True
        assert "Saved" in c.post("/settings/address-guard", data={}).text and box.guard.enabled() is False
        assert "Saved" in c.post("/settings/address-guard", data={"enabled": "1"}).text and box.guard.enabled() is True
        assert "Icetronix" in page.text
    finally:
        c.__exit__(None, None, None)


# -- the camera password chosen at install ------------------------------------------------------------------
def test_a_password_chosen_at_install_is_used_once_and_the_file_goes(cfg):
    from platebox.system import Sftp

    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    f = cfg.data_dir / "initial-camera-password.txt"
    f.write_text("Icetronix2026\r\n", encoding="utf-8")
    s = Sftp(cfg, Db(cfg.db_path))
    assert s.password() == "Icetronix2026" and not f.exists()
    f.write_text("SomethingElse99", encoding="utf-8")        # a later file does not replace a password already in use
    assert s.password() == "Icetronix2026" and not f.exists()


def test_a_bad_install_password_is_ignored_for_a_made_up_one(cfg):
    from platebox.system import Sftp

    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    (cfg.data_dir / "initial-camera-password.txt").write_text("has spaces!", encoding="utf-8")
    pw = Sftp(cfg, Db(cfg.db_path)).password()
    assert pw != "has spaces!" and len(pw) == 12


def test_after_the_camera_is_changed_by_hand_the_old_address_does_not_come_back(world):
    class Rx:
        targets = {"192.168.1.8": ("192.168.1.4", 9e18)}

        def target_of(self, ip, within_s=1800):
            hit = self.targets.get(ip)
            return hit[0] if hit else None

    world.receiver = Rx()
    world.tick()                                              # sees the camera still sending to .4: keeps it
    assert world.db_.get("addr_claim") == "192.168.1.4|Wi-Fi"
    st = world.acknowledge()
    assert st["level"] == "ok" and world.db_.get("ip_told") == "192.168.1.2" and not world.db_.get("addr_claim")
    assert world.tick()["level"] == "ok"                      # and it stays that way
