"""Finding the camera on the local network.

Two independent ways, because neither works everywhere:

 1. ONVIF WS-Discovery: a multicast question that every ONVIF camera answers.
    Exact, but some switches and Wi-Fi access points drop multicast.
 2. A sweep of the local subnet for something that answers like a camera
    (RTSP on port 554, a web page). Slower, but needs nothing from the network.
"""
from __future__ import annotations

import ipaddress
import json
import re
import select
import socket
import ssl
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

WS_ADDR = ("239.255.255.250", 3702)

_PROBE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
    'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
    'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
    'xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
    "<e:Header><w:MessageID>uuid:{mid}</w:MessageID>"
    '<w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>'
    '<w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>'
    "</e:Header><e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body></e:Envelope>"
)


@dataclass
class Candidate:
    ip: str
    name: str = ""                  # what the camera calls itself, if it said
    hardware: str = ""
    via: list[str] = field(default_factory=list)   # "onvif", "rtsp", "web"
    web_title: str = ""

    @property
    def label(self) -> str:
        bits = [b for b in (self.name, self.hardware, self.web_title) if b]
        return " / ".join(dict.fromkeys(bits)) or "Network camera"

    def as_dict(self) -> dict:
        return {"ip": self.ip, "label": self.label, "via": self.via}


def parse_probe_match(xml: str, source_ip: str) -> Candidate | None:
    """One WS-Discovery reply -> a candidate, or None if it isn't a video device."""
    if "ProbeMatch" not in xml:
        return None
    scopes = " ".join(re.findall(r"<[^>]*Scopes[^>]*>([^<]*)<", xml))
    if "NetworkVideoTransmitter" not in xml and "video_encoder" not in scopes.lower():
        return None
    xaddrs = " ".join(re.findall(r"<[^>]*XAddrs[^>]*>([^<]*)<", xml))
    m = re.search(r"https?://([0-9.]+)", xaddrs)
    ip = m.group(1) if m else source_ip

    def scope(key: str) -> str:
        mm = re.search(rf"onvif://www\.onvif\.org/{key}/(\S+)", scopes)
        return re.sub(r"%20", " ", mm.group(1)).strip() if mm else ""

    return Candidate(ip=ip, name=scope("name"), hardware=scope("hardware"), via=["onvif"])


def local_networks() -> list[tuple[str, int]]:
    """(address, prefix length) of each IPv4 interface that is up, loopback excluded."""
    out: list[tuple[str, int]] = []
    try:
        r = subprocess.run(["ip", "-j", "-4", "addr"], capture_output=True, text=True, timeout=5)
        for ifc in json.loads(r.stdout or "[]"):
            if ifc.get("ifname") == "lo":
                continue
            for a in ifc.get("addr_info", []):
                if a.get("family") == "inet":
                    out.append((a["local"], int(a.get("prefixlen", 24))))
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    if not out:        # no `ip` (Windows, tests): fall back on what the resolver knows
        try:
            for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
                if not ip.startswith("127."):
                    out.append((ip, 24))
        except OSError:
            pass
    return out


def ws_discovery(addresses: list[str], timeout: float = 3.0) -> list[Candidate]:
    found: dict[str, Candidate] = {}
    for addr in addresses:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            s.bind((addr, 0))
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(addr))
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            s.sendto(_PROBE.format(mid=uuid.uuid4()).encode(), WS_ADDR)
            end = time.time() + timeout
            while time.time() < end:
                ready, _, _ = select.select([s], [], [], max(0.05, end - time.time()))
                if not ready:
                    break
                data, (src, _port) = s.recvfrom(65535)
                c = parse_probe_match(data.decode("utf-8", "replace"), src)
                if c and c.ip not in found:
                    found[c.ip] = c
        except OSError:
            continue
        finally:
            s.close()
    return list(found.values())


def _unverified() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _tcp_open(ip: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def _rtsp_answers(ip: str, timeout: float = 1.5) -> bool:
    """Does something on 554 speak RTSP, in the clear or over TLS?"""
    req = f"OPTIONS rtsp://{ip}:554/ RTSP/1.0\r\nCSeq: 1\r\n\r\n".encode()
    for tls in (False, True):
        try:
            s = socket.create_connection((ip, 554), timeout=timeout)
            if tls:
                s = _unverified().wrap_socket(s, server_hostname=ip)
            s.settimeout(timeout)
            s.sendall(req)
            if s.recv(64).startswith(b"RTSP/"):
                s.close()
                return True
            s.close()
        except (OSError, ssl.SSLError):
            continue
    return False


def _web_title(ip: str, timeout: float = 2.0) -> str:
    for scheme, port in (("https", 443), ("http", 80)):
        try:
            s = socket.create_connection((ip, port), timeout=timeout)
            if scheme == "https":
                s = _unverified().wrap_socket(s, server_hostname=ip)
            s.settimeout(timeout)
            s.sendall(f"GET / HTTP/1.0\r\nHost: {ip}\r\nConnection: close\r\n\r\n".encode())
            data = b""
            while len(data) < 16384:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
            s.close()
            m = re.search(rb"<title[^>]*>([^<]{1,80})</title>", data, re.I)
            if m:
                return m.group(1).decode("utf-8", "replace").strip()
        except (OSError, ssl.SSLError):
            continue
    return ""


def identify(ip: str) -> Candidate | None:
    """Is this host a camera? Used on every host a sweep finds with 554 open."""
    if not _tcp_open(ip, 554, 0.5):
        return None
    c = Candidate(ip=ip)
    if _rtsp_answers(ip):
        c.via.append("rtsp")
    title = _web_title(ip)
    if title:
        c.via.append("web")
        c.web_title = title
    return c if c.via else None


def sweep(address: str, prefix: int, workers: int = 64, limit: int = 1022) -> list[Candidate]:
    net = ipaddress.ip_network(f"{address}/{prefix}", strict=False)
    hosts = [str(h) for h in net.hosts() if str(h) != address][:limit]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        open_hosts = [h for h, ok in zip(hosts, pool.map(lambda h: _tcp_open(h, 554, 0.4), hosts)) if ok]
        found = [c for c in pool.map(identify, open_hosts) if c]
    return found


def find_cameras(timeout: float = 3.0) -> list[Candidate]:
    """ONVIF first, then a sweep; the results are merged by address."""
    nets = local_networks()
    by_ip: dict[str, Candidate] = {}
    for c in ws_discovery([a for a, _ in nets], timeout):
        by_ip[c.ip] = c
    for addr, prefix in nets:
        for c in sweep(addr, max(prefix, 22)):
            if c.ip in by_ip:
                by_ip[c.ip].via = list(dict.fromkeys(by_ip[c.ip].via + c.via))
                by_ip[c.ip].web_title = by_ip[c.ip].web_title or c.web_title
            else:
                by_ip[c.ip] = c
    return sorted(by_ip.values(), key=lambda c: (("onvif" not in c.via), [int(p) for p in c.ip.split(".")]))
