"""The address guard: keeps the camera and this computer able to find each other when a router shuffles addresses.

Found the hard way at a client's site: the camera's "server address" is a number typed into it once. On a network
where addresses are handed out freely, this computer later gets a different number, the camera keeps sending to the
old one, and PlateBox sees nothing. Live view still works (that goes the other way), so it looks like a fault in
the detection.

The guard needs no password for the camera. It does three things, every few seconds:

  1. It learns what the camera really sends to: the address it connected to (the receiver records it).
  2. If that is no longer this computer's address, it ADDS the old address to this computer's network card (a
     second address), provided it is free, so the camera's settings are right again without touching the camera.
  3. If it cannot, it says why in plain words and says exactly what to type into the camera.

It also remembers the camera's hardware address, so it can follow the camera when the router gives IT a new number.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .config import Config
from .db import Db
from .netmgr import local_ip_towards

log = logging.getLogger("platebox.address")

INTERVAL_S = 10
SEEN_WITHIN_S = 1800          # a camera connection this recent tells us what address it is configured for
RETRY_S = 60                  # do not try the same failing repair more often than this
LOST_AFTER_S = 40             # the camera has not answered for this long: go and look for it
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


@dataclass
class Nic:
    name: str
    ip: str
    prefix: int

    @property
    def mask(self) -> str:
        return str(ipaddress.ip_network(f"0.0.0.0/{self.prefix}").netmask)

    def network(self) -> ipaddress.IPv4Network:
        return ipaddress.ip_network(f"{self.ip}/{self.prefix}", strict=False)


# ---------------------------------------------------------------------------------------------------
# reading what the operating system says
# ---------------------------------------------------------------------------------------------------
def parse_netsh_addresses(text: str) -> list[Nic]:
    """`netsh interface ipv4 show addresses`: every address on every adapter, the extra ones included."""
    nics: list[Nic] = []
    name = ""
    pending: str | None = None
    for line in text.splitlines():
        m = re.match(r'^\s*Configuration for interface\s+"(.+)"\s*$', line)
        if m:
            name, pending = m.group(1), None
            continue
        m = re.match(r"^\s*IP Address:\s+(\d+\.\d+\.\d+\.\d+)\s*$", line)
        if m:
            pending = m.group(1)
            continue
        m = re.match(r"^\s*Subnet Prefix:\s+\d+\.\d+\.\d+\.\d+/(\d+)", line)
        if m and pending and name:
            nics.append(Nic(name, pending, int(m.group(1))))
            pending = None
    return nics


def parse_ip_json(text: str) -> list[Nic]:
    """`ip -j -4 addr`"""
    out: list[Nic] = []
    try:
        for ifc in json.loads(text or "[]"):
            if ifc.get("ifname") == "lo":
                continue
            for a in ifc.get("addr_info", []):
                if a.get("family") == "inet":
                    out.append(Nic(ifc["ifname"], a["local"], int(a.get("prefixlen", 24))))
    except (ValueError, KeyError):
        pass
    return out


def norm_mac(mac: str) -> str:
    return mac.strip().lower().replace("-", ":")


def parse_neighbours(text: str) -> dict[str, str]:
    """`arp -a` (Windows) or `ip neigh` (Linux) -> {ip: mac}. Incomplete and broadcast entries are left out."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"^\s*(\d+\.\d+\.\d+\.\d+)\s+([0-9a-fA-F]{2}(?:-[0-9a-fA-F]{2}){5})\s", line)        # Windows
        if not m:
            m = re.match(r"^\s*(\d+\.\d+\.\d+\.\d+)\s+dev\s+\S+\s+lladdr\s+([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})", line)
        if m:
            mac = norm_mac(m.group(2))
            if mac != "ff:ff:ff:ff:ff:ff":
                out[m.group(1)] = mac
    return out


class HostNet:
    """The real operating system. Everything the guard does to or asks of the network goes through here, so that a
    test can stand in a pretend one."""

    def _run(self, args: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW)

    def nics(self) -> list[Nic]:
        try:
            if os.name == "nt":
                found = parse_netsh_addresses(self._run(["netsh", "interface", "ipv4", "show", "addresses"]).stdout)
            else:
                found = parse_ip_json(self._run(["ip", "-j", "-4", "addr"]).stdout)
        except (OSError, subprocess.SubprocessError):
            return []
        return [n for n in found if not n.ip.startswith(("127.", "169.254."))]

    def neighbours(self) -> dict[str, str]:
        try:
            cmd = ["arp", "-a"] if os.name == "nt" else ["ip", "neigh"]
            return parse_neighbours(self._run(cmd).stdout)
        except (OSError, subprocess.SubprocessError):
            return {}

    def touch(self, ip: str) -> None:
        """Make this computer ask the network who has `ip`, so it shows in the neighbour table if anyone does."""
        try:
            if os.name == "nt":
                self._run(["ping", "-n", "1", "-w", "500", ip], timeout=5)
            else:
                self._run(["ping", "-c", "1", "-W", "1", ip], timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass

    def owner_of(self, ip: str) -> str | None:
        """Hardware address of whoever answers at `ip`, or None if nobody does."""
        self.touch(ip)
        return self.neighbours().get(ip)

    def reachable(self, ip: str) -> bool:
        for port in (443, 554, 80):
            try:
                with socket.create_connection((ip, port), timeout=1.0):
                    return True
            except OSError:
                continue
        return False

    def find_by_mac(self, mac: str, nic: Nic) -> str | None:
        """Look through this computer's whole network for the device with this hardware address."""
        hosts = [str(h) for h in nic.network().hosts() if str(h) != nic.ip][:1022]

        def poke(h: str) -> None:
            for port in (443, 554, 80):
                try:
                    socket.create_connection((h, port), timeout=0.35).close()
                    return
                except OSError:
                    continue

        with ThreadPoolExecutor(max_workers=64) as pool:
            list(pool.map(poke, hosts))
        for ip, m in self.neighbours().items():
            if m == mac:
                return ip
        return None

    def add_address(self, nic: Nic, ip: str) -> tuple[bool, str]:
        try:
            if os.name == "nt":
                r = self._run(["netsh", "interface", "ipv4", "add", "address", f"name={nic.name}", f"address={ip}",
                               f"mask={nic.mask}", "skipassource=true", "store=active"])
            else:
                cmd = ["ip", "addr", "add", f"{ip}/{nic.prefix}", "dev", nic.name]
                r = self._run(cmd if os.geteuid() == 0 else ["sudo", "-n", *cmd])
        except (OSError, subprocess.SubprocessError) as e:
            return False, str(e)
        text = (r.stdout + r.stderr).strip()
        return r.returncode == 0, text

    def remove_address(self, nic_name: str, ip: str) -> None:
        try:
            if os.name == "nt":
                self._run(["netsh", "interface", "ipv4", "delete", "address", f"name={nic_name}", f"address={ip}"])
            else:
                cmd = ["ip", "addr", "del", f"{ip}/32", "dev", nic_name]
                self._run(cmd if os.geteuid() == 0 else ["sudo", "-n", *cmd])
        except (OSError, subprocess.SubprocessError):
            pass

    def can_bind(self, ip: str) -> bool:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind((ip, 0))
            return True
        except OSError:
            return False
        finally:
            s.close()


class NullHostNet(HostNet):
    """For tests and demos: touches nothing, sees nothing."""

    def nics(self) -> list[Nic]:
        return []

    def neighbours(self) -> dict[str, str]:
        return {}

    def touch(self, ip: str) -> None:
        return None

    def reachable(self, ip: str) -> bool:
        return True

    def find_by_mac(self, mac: str, nic: Nic) -> str | None:
        return None

    def add_address(self, nic: Nic, ip: str) -> tuple[bool, str]:
        return False, "not available here"

    def remove_address(self, nic_name: str, ip: str) -> None:
        return None

    def can_bind(self, ip: str) -> bool:
        return False


# ---------------------------------------------------------------------------------------------------
# the guard
# ---------------------------------------------------------------------------------------------------
def status(level: str, code: str, title: str, text: str, **extra) -> dict:
    return {"level": level, "code": code, "title": title, "text": text, **extra}


class AddressGuard:
    def __init__(self, cfg: Config, db: Db, net, receiver, is_done, camera_ip, host: HostNet | None = None) -> None:
        self.cfg, self.db, self.net, self.receiver = cfg, db, net, receiver
        self.is_done, self.camera_ip = is_done, camera_ip
        self.host = host or (NullHostNet() if cfg.fake_net else HostNet())
        self.pulling = lambda: False          # True while PlateBox is watching the camera's video itself
        self._status: dict = status("ok", "starting", "", "")
        self._retry_at: dict[str, float] = {}
        self._camera_lost_since: float | None = None
        self._last_find = 0.0
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- settings -------------------------------------------------------------------------------
    def enabled(self) -> bool:
        return self.db.get("address_keep", "1") == "1"

    def _claim(self) -> tuple[str, str] | None:
        v = self.db.get("addr_claim")
        if v and "|" in v:
            ip, nic = v.split("|", 1)
            return ip, nic
        return None

    # -- one look ---------------------------------------------------------------------------------
    def tick(self, now: float | None = None) -> dict:
        with self._lock:
            now = now if now is not None else time.time()
            try:
                st = self._look(now)
                if st.get("level") == "bad" and st.get("code") in ("taken", "camera-has-it", "other-network", "need-admin",
                                                                  "no-adapter", "off", "wait", "failed") and self.pulling():
                    st = status("ok", "pulling", "", "", **{k: v for k, v in st.items() if k in ("ip_now", "ip_told", "camera_ip")})
                self._status = st
            except Exception:
                log.exception("address guard failed")
            return dict(self._status)

    def status(self) -> dict:
        with self._lock:
            return dict(self._status)

    def _look(self, now: float) -> dict:
        if not self.is_done():
            return status("ok", "setup", "", "")
        cam = self.camera_ip()
        if not cam:
            return status("ok", "no-camera", "", "")

        followed = self._follow_camera(cam, now)
        cam = self.camera_ip() or cam
        ip_now = self._ip_now(cam)
        if not ip_now:
            return status("bad", "no-network", "This computer has no network address",
                          "This computer is not connected to a network, so the camera cannot reach it. "
                          "Check the cable or the Wi-Fi.")
        told = self.db.get("ip_told") or ""
        seen = self._seen(cam)
        if seen and seen != told:                       # what the camera really sends to beats what we remember
            self.db.put("ip_told", seen)
            told = seen
        if not told:
            self.db.put("ip_told", ip_now)
            told = ip_now

        base = {"ip_now": ip_now, "ip_told": told, "camera_ip": cam, "claimed": None}
        claim = self._claim()
        if told == ip_now:
            self._release("the camera now uses the computer's own address")
            return self._with_notice(status("ok", "ok", "", "", **base), followed)
        if not self.enabled():
            return self._cannot(base, "off", "")

        if claim and claim[0] == told and self.host.can_bind(told):
            return self._with_notice(status(
                "fixed", "kept", "The network changed this computer's address; PlateBox kept the camera working",
                f"The camera sends to {told}, but the network gave this computer {ip_now}. PlateBox is also answering on "
                f"{told}, so nothing needs doing now. For a lasting fix, give this computer a fixed address (see Settings).",
                **{**base, "claimed": told}), followed)
        if claim:
            self._release("the address being kept is not the one the camera uses")

        return self._repair(base, cam, ip_now, told, now, followed)

    def _with_notice(self, st: dict, followed: str) -> dict:
        if followed:
            st["notice"] = followed
        return st

    def _ip_now(self, cam: str) -> str | None:
        if self.cfg.advertise_ip:
            return self.cfg.advertise_ip
        ip = local_ip_towards(cam) if cam else None
        return ip or self.net.status().ip

    def _seen(self, cam: str) -> str:
        fn = getattr(self.receiver, "target_of", None) if self.receiver else None
        return (fn(cam, SEEN_WITHIN_S) if fn else None) or ""

    # -- following the camera ------------------------------------------------------------------
    def _follow_camera(self, cam: str, now: float) -> str:
        """Keep the camera's hardware address; if the camera stops answering, look for it by that address."""
        mac = self.db.get("camera_mac") or ""
        if self.host.reachable(cam):
            self._camera_lost_since = None
            if not mac:
                m = self.host.neighbours().get(cam)
                if m:
                    self.db.put("camera_mac", m)
            return ""
        if self._camera_lost_since is None:
            self._camera_lost_since = now
        if not mac or now - self._camera_lost_since < LOST_AFTER_S or now - self._last_find < 45:
            return ""
        self._last_find = now
        nic = next((n for n in self.host.nics() if n.ip == (self._ip_now(cam) or "")), None)
        if not nic:
            return ""
        found = self.host.find_by_mac(mac, nic)
        if found and found != cam:
            self.db.put("camera_ip", found)
            self.db.event("camera_moved", f"{cam} -> {found}")
            log.info("the camera moved from %s to %s: followed it", cam, found)
            self._camera_lost_since = None
            return f"The camera moved from {cam} to {found}. PlateBox found it again by its hardware address."
        return ""

    # -- repairing ------------------------------------------------------------------------------
    def _repair(self, base: dict, cam: str, ip_now: str, told: str, now: float, followed: str) -> dict:
        key = f"{told}>{ip_now}"
        nic = next((n for n in self.host.nics() if n.ip == ip_now), None)
        if nic is None:
            return self._cannot(base, "no-adapter", "")
        try:
            inside = ipaddress.ip_address(told) in nic.network() and ipaddress.ip_address(told) not in (
                nic.network().network_address, nic.network().broadcast_address)
        except ValueError:
            inside = False
        if not inside:
            return self._cannot(base, "other-network", "")
        if now < self._retry_at.get(key, 0):
            return self._cannot(base, self.db.get("addr_last_reason") or "wait", "")
        owner = self.host.owner_of(told)
        if owner:
            mac = self.db.get("camera_mac") or ""
            return self._cannot(base, "camera-has-it" if mac and owner == mac else "taken", owner, now, key)
        ok, why = self.host.add_address(nic, told)
        if ok:
            for _ in range(10):                          # Windows checks the address is not a duplicate first
                if self.host.can_bind(told):
                    break
                time.sleep(0.3)
            else:
                ok, why = False, "The address was added but would not become usable."
        if not ok:
            reason = "need-admin" if re.search(r"elevat|administrator|access is denied|permitted|Operation not", why, re.I) else "failed"
            self.host.remove_address(nic.name, told)
            return self._cannot(base, reason, why[:160], now, key)
        self.db.put("addr_claim", f"{told}|{nic.name}")
        self.db.event("address_kept", f"{told} (this computer is {ip_now})")
        log.info("kept %s for the camera (this computer's own address is %s)", told, ip_now)
        return self._with_notice(status(
            "fixed", "kept", "The network changed this computer's address; PlateBox kept the camera working",
            f"The camera sends to {told}, but the network gave this computer {ip_now}. PlateBox is also answering on "
            f"{told}, so nothing needs doing now. For a lasting fix, give this computer a fixed address (see Settings).",
            **{**base, "claimed": told}), followed)

    def _cannot(self, base: dict, reason: str, detail: str, now: float | None = None, key: str = "") -> dict:
        if key:
            self._retry_at[key] = (now or time.time()) + RETRY_S
            self.db.put("addr_last_reason", reason)
        told, ip_now = base["ip_told"], base["ip_now"]
        fix = (f"On the camera, open its web page, go to Storage, Destination, FTP, change Server Address to "
               f"{ip_now}, press Save, then test. Then press the button on this page.")
        why = {
            "taken": f"Another device on the network already has {told}, so PlateBox cannot use it.",
            "camera-has-it": f"The camera itself is now using {told}, the address it is trying to send pictures to.",
            "other-network": f"{told} is not on this computer's network any more, so PlateBox cannot take it over.",
            "need-admin": "PlateBox was not allowed to add the address (it needs to run as administrator).",
            "no-adapter": "PlateBox could not find this computer's network adapter.",
            "off": "Keeping the old address is switched off in Settings.",
            "failed": "Windows would not add the address.",
            "wait": "PlateBox will try again in a minute.",
        }.get(reason, "PlateBox could not keep the old address.")
        return status("bad", reason, "The camera is sending pictures to the wrong address",
                      f"The camera is set to send to {told}, but this computer is now {ip_now}, so pictures cannot arrive. "
                      f"{why} {fix}", **{**base, "detail": detail, "fix": fix})

    def _release(self, reason: str) -> None:
        claim = self._claim()
        if not claim:
            return
        ip, nic = claim
        self.host.remove_address(nic, ip)
        self.db.put("addr_claim", "")
        log.info("stopped keeping %s (%s)", ip, reason)

    def acknowledge(self) -> dict:
        """The person has typed the new address into the camera."""
        with self._lock:
            cam = self.camera_ip()
            ip_now = self._ip_now(cam or "")
            if ip_now:
                self.db.put("ip_told", ip_now)
            targets = getattr(self.receiver, "targets", None)
            if isinstance(targets, dict) and cam:
                targets.pop(cam, None)                  # forget the old address the camera last connected to
            self._release("the camera was changed by hand")
            self._retry_at.clear()
        return self.tick()

    # -- running --------------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread:
            return
        stale = self._claim()
        if stale:                                        # left over from a run that did not shut down cleanly
            self.host.remove_address(stale[1], stale[0])
            self.db.put("addr_claim", "")
        self._thread = threading.Thread(target=self._run, name="address-guard", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        self._release("shutting down")

    def _run(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(INTERVAL_S)
