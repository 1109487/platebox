"""Answers the camera's name and time lookups from this computer, so it never waits for the internet.

Found the hard way: the CP Plus camera, between starting its SFTP login and sending the password, makes some
network lookup (a name or the time). With internet that takes 0.2 s; without it the camera waits until it times
out, about a minute, gives up, and retries, so NOTHING reaches PlateBox. (Live view still worked, because that is
PlateBox pulling from the camera, not the camera pushing.) At a client's site with no internet the demo showed
nothing.

So PlateBox answers instead, on the ports a camera would ask:
  * DNS (UDP 53): every name is "found", at this computer. A lookup that ends in "connection refused" is quick,
    where a lookup that never answers is a minute.
  * Time (UDP 123, SNTP): this computer's clock. It also gives the camera the right time with no internet, so the
    times in its file names, which PlateBox shows, are right.
On the camera, set its DNS server and its NTP (time) server to this computer's address. Nothing else changes.

This is deliberately small and does nothing but answer: it never forwards a question anywhere.
"""
from __future__ import annotations

import ipaddress
import logging
import socket
import struct
import threading
import time

from .config import Config
from .db import Db
from .netmgr import local_ip_towards

log = logging.getLogger("platebox.offline")

NTP_EPOCH_OFFSET = 2208988800          # seconds from 1900 (NTP) to 1970 (Unix)


def dns_reply(query: bytes, answer_ip: str) -> bytes | None:
    """The reply to one DNS question: an A record pointing at `answer_ip`; other kinds of question get an empty,
    successful answer. None if it is not a plain single-question query (those are ignored)."""
    if len(query) < 17:
        return None
    flags = struct.unpack("!H", query[2:4])[0]
    if flags & 0x8000 or struct.unpack("!H", query[4:6])[0] != 1:       # a response, or not exactly one question
        return None
    i = 12
    while i < len(query) and query[i] != 0:
        if query[i] & 0xC0:                                              # compression has no place in a question
            return None
        i += query[i] + 1
    end = i + 1
    if end + 4 > len(query):
        return None
    qtype, qclass = struct.unpack("!HH", query[end:end + 4])
    question = query[12:end + 4]
    rflags = 0x8000 | (flags & 0x0100) | 0x0080                          # response, recursion-desired echoed, available
    if qtype == 1 and qclass == 1:                                        # A, IN
        answer = b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 60, 4) + socket.inet_aton(answer_ip)
        return query[:2] + struct.pack("!HHHHH", rflags, 1, 1, 0, 0) + question + answer
    return query[:2] + struct.pack("!HHHHH", rflags, 1, 0, 0, 0) + question


def ntp_reply(packet: bytes, now: float | None = None) -> bytes | None:
    """A minimal SNTP server reply carrying this computer's clock. None if it is not an NTP client request."""
    if len(packet) < 48:
        return None
    version, mode = (packet[0] >> 3) & 7, packet[0] & 7
    if mode not in (1, 3) or not 1 <= version <= 4:
        return None
    t = (now if now is not None else time.time()) + NTP_EPOCH_OFFSET
    sec, frac = int(t), int((t - int(t)) * (1 << 32))
    stamp = struct.pack("!II", sec & 0xFFFFFFFF, frac)
    r = bytearray(48)
    r[0] = (version << 3) | 4              # no warning, the client's version, mode 4 (server)
    r[1], r[2], r[3] = 2, 6, 0xEC          # stratum 2, poll 64 s, precision about 1 microsecond
    r[12:16] = b"LOCL"
    r[16:24] = stamp                       # reference time
    r[24:32] = packet[40:48]               # originate: echo the client's transmit time, which it checks
    r[32:40] = stamp                       # receive
    r[40:48] = stamp                       # transmit
    return bytes(r)


WILDCARD = ("", "0.0.0.0")
RESCAN_S = 15


def is_private_ip(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
        return a.is_private and not a.is_loopback and not a.is_link_local
    except ValueError:
        return False


class OfflineHelper:
    def __init__(self, cfg: Config, db: Db) -> None:
        self.cfg, self.db = cfg, db
        self.errors: dict[str, str] = {}
        self.listening: dict[str, bool] = {"dns": False, "ntp": False}
        self.seen: dict[str, float] = {}                # who has asked, and when (diagnosis)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._socks: dict[tuple[str, str], socket.socket] = {}          # (service, address) -> its socket
        self._threads: list[threading.Thread] = []
        self._services: dict[str, tuple[int, object]] = {}
        self._each_address: set[str] = set()            # services that could not take the wildcard address

    def enabled(self) -> bool:
        return self.db.get("offline_helper", "1") == "1"

    def _open(self, name: str, addr: str) -> str | None:
        """Start answering `name` on one address. None on success, or what went wrong."""
        port, reply = self._services[name]
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind((addr, port))
        except OSError as e:
            s.close()
            return f"port {port} is in use ({e.strerror or e})"
        s.settimeout(0.5)
        with self._lock:
            self._socks[(name, addr)] = s
        t = threading.Thread(target=self._serve, args=(name, addr, s, reply), name=f"offline-{name}-{addr}", daemon=True)
        t.start()
        self._threads.append(t)
        return None

    def _local_addresses(self) -> list[str]:
        from .addrguard import HostNet

        return sorted({n.ip for n in HostNet().nics() if is_private_ip(n.ip)})

    def _rescan(self, name: str) -> None:
        """For a service that cannot have the wildcard address (Windows' internet sharing owns port 53): answer on
        each of this computer's own addresses instead, and notice when one appears or goes."""
        for ip in self._local_addresses():
            with self._lock:
                have = (name, ip) in self._socks
            if not have:
                err = self._open(name, ip)
                if err:
                    log.debug("offline helper: %s on %s: %s", name, ip, err)
        with self._lock:
            self.listening[name] = any(k[0] == name for k in self._socks)
            if self.listening[name]:
                self.errors.pop(name, None)

    def start(self) -> None:
        self._services = {"dns": (self.cfg.dns_port, self._dns), "ntp": (self.cfg.ntp_port, self._ntp)}
        for name, (port, _reply) in self._services.items():
            err = self._open(name, self.cfg.sftp_host)
            if err is None:
                self.listening[name] = True
                continue
            if self.cfg.sftp_host in WILDCARD:
                self._each_address.add(name)
                self._rescan(name)
                if self.listening[name]:
                    log.info("offline helper: %s: the general address is taken, answering on this computer's own addresses", name)
                    continue
            self.errors[name] = err
            log.warning("offline helper: %s not started: %s", name, err)
        if self._each_address:
            t = threading.Thread(target=self._watch, name="offline-watch", daemon=True)
            t.start()
            self._threads.append(t)
        log.info("offline helper: %s", ", ".join(f"{k} {'on' if v else 'off'}" for k, v in self.listening.items()))

    def _watch(self) -> None:
        while not self._stop.wait(RESCAN_S):
            for name in list(self._each_address):
                try:
                    self._rescan(name)
                except Exception:
                    log.exception("offline helper: rescan failed")

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            socks = list(self._socks.values())
            self._socks.clear()
        for s in socks:
            try:
                s.close()
            except OSError:
                pass
        for t in self._threads:
            t.join(timeout=2)
        self._threads.clear()
        self.listening = {"dns": False, "ntp": False}

    def _dns(self, data: bytes, peer: str) -> bytes | None:
        return dns_reply(data, local_ip_towards(peer) or "127.0.0.1")

    def _ntp(self, data: bytes, peer: str) -> bytes | None:
        return ntp_reply(data)

    def _serve(self, name: str, addr: str, s: socket.socket, reply) -> None:
        while not self._stop.is_set():
            try:
                data, (peer, port) = s.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:                                    # closed, or the address went away
                with self._lock:
                    if self._socks.get((name, addr)) is s:
                        del self._socks[(name, addr)]
                return
            if not self.enabled():
                continue
            try:
                if not ipaddress.ip_address(peer).is_private:        # only ever the local network, never the internet
                    continue
            except ValueError:
                continue
            try:
                out = reply(data, peer)
                if out:
                    s.sendto(out, (peer, port))
                    self.seen[f"{name} {peer}"] = time.time()
            except Exception:                                  # never let a odd packet stop the service
                log.exception("offline helper: %s failed on a packet", name)

    def heard(self, name: str, within_s: float) -> bool:
        """Has any camera asked this service ('dns' or 'ntp') in the last `within_s` seconds?"""
        now = time.time()
        return any(k.startswith(name + " ") and now - t <= within_s for k, t in list(self.seen.items()))

    def status(self) -> dict:
        now = time.time()
        return {"enabled": self.enabled(), "listening": dict(self.listening), "errors": dict(self.errors),
                "recent": [{"what": k, "ago_s": int(now - t)} for k, t in sorted(self.seen.items(), key=lambda kv: -kv[1])[:6]]}
