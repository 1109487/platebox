"""Joining a Wi-Fi network, and finding out which address the camera can reach us on.

Raspberry Pi OS (Bookworm) and most Debian desktops run NetworkManager, so the real
backend is a thin wrapper around `nmcli`. A fake backend stands in where there is no
NetworkManager (development, tests), so everything else can be exercised.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import subprocess
from dataclasses import dataclass, field


@dataclass
class WifiNetwork:
    ssid: str
    signal: int
    security: str          # "" for an open network
    in_use: bool = False

    @property
    def secured(self) -> bool:
        return bool(self.security) and self.security != "--"


@dataclass
class NetStatus:
    connected: bool = False
    kind: str | None = None        # "wifi" | "ethernet" | "hotspot" | None
    ssid: str | None = None
    iface: str | None = None
    ip: str | None = None
    has_wifi: bool = False
    wifi_ips: list[str] = field(default_factory=list)


def split_terse(line: str) -> list[str]:
    """`nmcli -t` separates fields with ':' and escapes literal ':' and '\\' with a backslash."""
    out, cur, i = [], [], 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            cur.append(line[i + 1])
            i += 2
            continue
        if c == ":":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    out.append("".join(cur))
    return out


def parse_wifi_list(text: str) -> list[WifiNetwork]:
    """Output of: nmcli -t -f IN-USE,SSID,SIGNAL,SECURITY dev wifi list"""
    best: dict[str, WifiNetwork] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        f = split_terse(line)
        if len(f) < 4:
            continue
        in_use, ssid, signal, security = f[0] == "*", f[1], f[2], f[3]
        if not ssid:            # hidden network: nothing to show
            continue
        try:
            sig = int(signal)
        except ValueError:
            sig = 0
        n = WifiNetwork(ssid=ssid, signal=sig, security=security if security != "--" else "", in_use=in_use)
        old = best.get(ssid)
        if old is None or n.signal > old.signal or n.in_use:
            best[ssid] = n
    return sorted(best.values(), key=lambda n: (not n.in_use, -n.signal, n.ssid.lower()))


def local_ip_towards(peer_ip: str) -> str | None:
    """The address of this box that the peer would see. This is what to type into the camera."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((peer_ip, 9))        # no packet is sent: the kernel just picks a route
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return None


def is_private(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_private
    except ValueError:
        return False


class NmcliBackend:
    """Real network control. Needs NetworkManager and permission to change connections."""

    def __init__(self, hotspot_ssid: str, hotspot_password: str) -> None:
        self.hotspot_ssid = hotspot_ssid
        self.hotspot_password = hotspot_password

    def _run(self, args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
        return subprocess.run(["nmcli", *args], capture_output=True, text=True, timeout=timeout)

    def _wifi_iface(self) -> str | None:
        r = self._run(["-t", "-f", "DEVICE,TYPE", "dev"])
        for line in r.stdout.splitlines():
            f = split_terse(line)
            if len(f) >= 2 and f[1] == "wifi":
                return f[0]
        return None

    def status(self) -> NetStatus:
        st = NetStatus(has_wifi=self._wifi_iface() is not None)
        r = self._run(["-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "dev"])
        for line in r.stdout.splitlines():
            f = split_terse(line)
            if len(f) < 4 or f[2] != "connected":
                continue
            dev, typ, _state, conn = f[:4]
            if typ not in ("wifi", "ethernet"):
                continue
            ip = self._ipv4(dev)
            if not ip:
                continue
            hotspot = typ == "wifi" and conn == self.hotspot_ssid
            kind = "hotspot" if hotspot else typ
            if typ == "wifi":
                st.wifi_ips.append(ip)
            # A real network beats the hotspot we raised ourselves.
            if not st.connected or (st.kind == "hotspot" and not hotspot):
                st.connected, st.kind, st.iface, st.ip = True, kind, dev, ip
                st.ssid = conn if typ == "wifi" else None
        return st

    def _ipv4(self, iface: str) -> str | None:
        r = subprocess.run(["ip", "-j", "-4", "addr", "show", "dev", iface], capture_output=True, text=True, timeout=10)
        try:
            for a in json.loads(r.stdout or "[]"):
                for i in a.get("addr_info", []):
                    if i.get("family") == "inet":
                        return i["local"]
        except (ValueError, KeyError):
            pass
        return None

    def scan(self) -> list[WifiNetwork]:
        self._run(["dev", "wifi", "rescan"], timeout=15)
        r = self._run(["-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "dev", "wifi", "list"])
        return parse_wifi_list(r.stdout)

    def connect(self, ssid: str, password: str | None) -> tuple[bool, str]:
        iface = self._wifi_iface()
        if not iface:
            return False, "This box has no Wi-Fi."
        # Drop our own hotspot first: one radio cannot be an access point and a client.
        self.stop_hotspot()
        args = ["dev", "wifi", "connect", ssid, "ifname", iface]
        if password:
            args += ["password", password]
        r = self._run(args, timeout=60)
        if r.returncode == 0:
            return True, "Connected."
        msg = (r.stderr or r.stdout).strip().splitlines()[-1:] or ["Could not connect."]
        return False, msg[0]

    def start_hotspot(self) -> bool:
        iface = self._wifi_iface()
        if not iface:
            return False
        self._run(["connection", "delete", self.hotspot_ssid])
        r = self._run(["dev", "wifi", "hotspot", "ifname", iface, "con-name", self.hotspot_ssid,
                       "ssid", self.hotspot_ssid, "password", self.hotspot_password], timeout=30)
        return r.returncode == 0

    def stop_hotspot(self) -> None:
        self._run(["connection", "down", self.hotspot_ssid])

    def forget(self, ssid: str) -> None:
        self._run(["connection", "delete", ssid])


class FakeBackend:
    """Stands in for NetworkManager where there isn't one. Deterministic."""

    NETWORKS = [
        WifiNetwork("Office-WiFi", 82, "WPA2"),
        WifiNetwork("Guest", 64, ""),
        WifiNetwork("Warehouse-5G", 41, "WPA2 WPA3"),
    ]

    def __init__(self, local_ip: str | None = None) -> None:
        self._ip = local_ip
        self.joined: str | None = None
        self.hotspot = False

    def status(self) -> NetStatus:
        ip = self._ip or local_ip_towards("192.0.2.1")
        return NetStatus(connected=bool(ip), kind="ethernet", ip=ip, iface="eth0", has_wifi=True,
                         ssid=self.joined)

    def scan(self) -> list[WifiNetwork]:
        return list(self.NETWORKS)

    def connect(self, ssid: str, password: str | None) -> tuple[bool, str]:
        if any(n.ssid == ssid and n.secured for n in self.NETWORKS) and not password:
            return False, "A password is needed."
        if password == "wrong":
            return False, "Secrets were required, but not provided."
        self.joined = ssid
        return True, "Connected."

    def start_hotspot(self) -> bool:
        self.hotspot = True
        return True

    def stop_hotspot(self) -> None:
        self.hotspot = False

    def forget(self, ssid: str) -> None:
        if self.joined == ssid:
            self.joined = None


# ---------------------------------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------------------------------
def parse_netsh_interfaces(text: str) -> dict:
    """First Wi-Fi interface of `netsh wlan show interfaces`: {name, state, ssid, signal}. Empty if none."""
    out: dict = {}
    for line in text.splitlines():
        m = re.match(r"^\s*([A-Za-z][A-Za-z ]*?)\s*:\s*(.*?)\s*$", line)
        if not m:
            continue
        key, val = m.group(1).lower(), m.group(2)
        if key in ("name", "state", "ssid", "signal") and key not in out:
            out[key] = val
        if key == "name" and "name" in out and out["name"] != val:      # a second interface starts: stop at the first
            break
    if "signal" in out:
        out["signal"] = int(re.sub(r"\D", "", out["signal"]) or 0)
    return out


def parse_netsh_networks(text: str) -> list[WifiNetwork]:
    """`netsh wlan show networks mode=bssid` -> networks, strongest radio of each."""
    nets: dict[str, WifiNetwork] = {}
    cur: WifiNetwork | None = None
    for line in text.splitlines():
        m = re.match(r"^SSID\s+\d+\s*:\s*(.*?)\s*$", line)
        if m:
            cur = None
            if m.group(1):
                cur = WifiNetwork(ssid=m.group(1), signal=0, security="")
                nets[cur.ssid] = cur
            continue
        if cur is None:
            continue
        m = re.match(r"^\s*Authentication\s*:\s*(.*?)\s*$", line)
        if m:
            cur.security = "" if m.group(1).lower() == "open" else m.group(1)
            continue
        m = re.match(r"^\s*Signal\s*:\s*(\d+)\s*%", line)
        if m:
            cur.signal = max(cur.signal, int(m.group(1)))
    return sorted(nets.values(), key=lambda n: (-n.signal, n.ssid.lower()))


def wlan_profile_xml(ssid: str, password: str | None, security: str = "") -> str:
    """A Windows Wi-Fi profile. WPA3 networks get WPA3SAE, other secured ones WPA2PSK, open ones none."""
    from xml.sax.saxutils import escape

    s = escape(ssid)
    if not password:
        sec = "<authEncryption><authentication>open</authentication><encryption>none</encryption><useOneX>false</useOneX></authEncryption>"
    else:
        auth = "WPA3SAE" if "wpa3" in security.lower() else "WPA2PSK"
        sec = (f"<authEncryption><authentication>{auth}</authentication><encryption>AES</encryption><useOneX>false</useOneX></authEncryption>"
               f"<sharedKey><keyType>passPhrase</keyType><protected>false</protected><keyMaterial>{escape(password)}</keyMaterial></sharedKey>")
    return ('<?xml version="1.0"?>'
            '<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">'
            f"<name>{s}</name><SSIDConfig><SSID><name>{s}</name></SSID></SSIDConfig>"
            f"<connectionType>ESS</connectionType><connectionMode>auto</connectionMode>"
            f"<MSM><security>{sec}</security></MSM></WLANProfile>")


class WindowsBackend:
    """Wi-Fi on Windows through `netsh`. There is no setup hotspot here: the PC has Windows' own network settings."""

    NO_WINDOW = 0x08000000

    def _netsh(self, *args: str, timeout: int = 25) -> subprocess.CompletedProcess:
        return subprocess.run(["netsh", *args], capture_output=True, text=True, timeout=timeout,
                              creationflags=self.NO_WINDOW if os.name == "nt" else 0)

    def _wifi(self) -> dict:
        try:
            return parse_netsh_interfaces(self._netsh("wlan", "show", "interfaces").stdout)
        except (OSError, subprocess.SubprocessError):
            return {}

    def status(self) -> NetStatus:
        wifi = self._wifi()
        ip = local_ip_towards("192.0.2.1")
        st = NetStatus(has_wifi=bool(wifi))
        if ip:
            st.connected, st.ip, st.iface = True, ip, wifi.get("name") or "network"
            on_wifi = wifi.get("state", "").lower() == "connected" and bool(wifi.get("ssid"))
            st.kind = "wifi" if on_wifi else "ethernet"
            st.ssid = wifi.get("ssid") if on_wifi else None
        return st

    def scan(self) -> list[WifiNetwork]:
        nets = parse_netsh_networks(self._netsh("wlan", "show", "networks", "mode=bssid").stdout)
        cur = self._wifi().get("ssid")
        for n in nets:
            n.in_use = n.ssid == cur
        return nets

    def connect(self, ssid: str, password: str | None) -> tuple[bool, str]:
        import tempfile
        import time

        if not self._wifi():
            return False, "This computer has no Wi-Fi."
        security = next((n.security for n in self.scan() if n.ssid == ssid), "")
        if password or not security:
            fd, path = tempfile.mkstemp(suffix=".xml")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(wlan_profile_xml(ssid, password, security))
                r = self._netsh("wlan", "add", "profile", f"filename={path}")
            finally:
                try:
                    os.unlink(path)               # it holds the Wi-Fi password: never leave it behind
                except OSError:
                    pass
            if r.returncode != 0:
                return False, "Windows would not take that Wi-Fi profile."
        r = self._netsh("wlan", "connect", f"name={ssid}", f"ssid={ssid}")
        if r.returncode != 0:
            return False, (r.stdout or r.stderr).strip().splitlines()[-1][:160] if (r.stdout or r.stderr).strip() else "Could not connect."
        for _ in range(20):                       # Windows answers at once; the connection takes a few seconds
            w = self._wifi()
            if w.get("state", "").lower() == "connected" and w.get("ssid") == ssid:
                return True, "Connected."
            time.sleep(1)
        return False, "Could not connect. Check the password."

    def start_hotspot(self) -> bool:
        return False

    def stop_hotspot(self) -> None:
        return None

    def forget(self, ssid: str) -> None:
        self._netsh("wlan", "delete", "profile", f"name={ssid}")
