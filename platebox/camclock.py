"""What time the camera thinks it is, asked of the camera itself (ONVIF, no password needed).

Used to tell a picture the camera took a moment ago from an old one it is sending again: the camera stamps each
picture with its OWN clock, so the only fair comparison is with the camera's clock now, not this computer's.
"""
from __future__ import annotations

import logging
import re
import ssl
import time
import urllib.request
from datetime import datetime, timedelta

from .db import Db

log = logging.getLogger("platebox.camclock")

_SOAP = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>'
         '<GetSystemDateAndTime xmlns="http://www.onvif.org/ver10/device/wsdl"/></s:Body></s:Envelope>')
CACHE_S = 120
RETRY_S = 60


def _block_time(xml: str, tag: str) -> datetime | None:
    m = re.search(rf"<(?:\w+:)?{tag}>(.*?)</(?:\w+:)?{tag}>", xml, re.S)
    if not m:
        return None
    block = m.group(1)

    def part(name: str) -> int | None:
        mm = re.search(rf"<(?:\w+:)?{name}>(\d+)<", block)
        return int(mm.group(1)) if mm else None

    vals = [part(t) for t in ("Year", "Month", "Day", "Hour", "Minute", "Second")]
    if any(v is None for v in vals):
        return None
    try:
        return datetime(*vals)                              # type: ignore[arg-type]
    except ValueError:
        return None


def posix_tz_offset(tz: str) -> timedelta | None:
    """'CST-5:30' (POSIX style: the sign is the other way round) -> +5:30. None if it is not of that form."""
    m = re.match(r"^[A-Za-z]{3,}([+-]?)(\d{1,2})(?::(\d{2}))?", tz.strip())
    if not m:
        return None
    mins = int(m.group(2)) * 60 + int(m.group(3) or 0)
    return timedelta(minutes=mins if m.group(1) == "-" else -mins)


def parse_local_time(xml: str, pc_offset: timedelta | None = None) -> datetime | None:
    """The camera's LOCAL date and time from an ONVIF GetSystemDateAndTimeResponse (naive, as printed on pictures).
    Cameras that give only UTC are handled with this computer's time zone."""
    local = _block_time(xml, "LocalDateTime")
    if local:
        return local
    utc = _block_time(xml, "UTCDateTime")
    if not utc:
        return None
    # The camera's own time-zone text cannot be trusted: this camera writes 'GMT+05:30' for UTC+5:30, where the
    # standard way of writing it means UTC-5:30, which once made PlateBox think the camera was 11 hours behind.
    # Pictures are stamped in the camera's local time, which here is this computer's.
    if pc_offset is None:
        pc_offset = datetime.now().astimezone().utcoffset() or timedelta(0)
    return utc + pc_offset


def parse_camera_stamp(text: str | None) -> datetime | None:
    """'2026-10-19 03:29:32' (what camera_time_from_path returns) -> datetime."""
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def parse_http_date(value: str, pc_offset: timedelta | None = None) -> datetime | None:
    """An HTTP 'Date: Thu, 22 Oct 2026 05:12:01 GMT' header -> that moment as local time (this computer's zone)."""
    from email.utils import parsedate_to_datetime

    try:
        d = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if d is None:
        return None
    off = pc_offset if pc_offset is not None else timedelta(seconds=-time.timezone)
    return d.replace(tzinfo=None) + off


class CameraClock:
    """Three ways to learn the camera's clock, best first:
      1. ONVIF, which gives its local time exactly;
      2. the Date line of the camera's own web page;
      3. this computer's clock, but only if the camera has been getting its time FROM PlateBox lately (then they agree).
    `how` says which one worked, and `problem` why none did, for the Settings page."""

    def __init__(self, db: Db, enabled: bool = True) -> None:
        self.db = db
        self.enabled = enabled                                    # off where there is no real camera (tests, demos)
        self.synced_from_us = lambda: False                       # set by the box: has the camera asked PlateBox the time?
        self.how = ""
        self.problem = ""
        self._sample: tuple[datetime, float] | None = None       # (camera local time, our monotonic clock then)
        self._failed_at = -1e9
        self._url_that_worked: str | None = None

    def _fetch_web_date(self, ip: str) -> datetime | None:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        for url in (f"https://{ip}/", f"http://{ip}/"):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=1.5, context=ctx) as r:
                    d = r.headers.get("Date")
                t = parse_http_date(d) if d else None
                if t:
                    return t
            except Exception as e:
                if hasattr(e, "headers") and e.headers.get("Date"):         # an error page still carries the Date line
                    t = parse_http_date(e.headers["Date"])
                    if t:
                        return t
        return None

    def _fetch(self, ip: str) -> datetime | None:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        urls = [f"http://{ip}/onvif/device_service", f"https://{ip}/onvif/device_service"]
        if self._url_that_worked and self._url_that_worked in urls:
            urls.insert(0, urls.pop(urls.index(self._url_that_worked)))
        for url in urls:
            try:
                req = urllib.request.Request(url, data=_SOAP.encode(), headers={"Content-Type": "application/soap+xml"})
                with urllib.request.urlopen(req, timeout=1.5, context=ctx) as r:
                    t = parse_local_time(r.read(20000).decode("utf-8", "replace"))
                if t:
                    self._url_that_worked = url
                    return t
            except Exception:                                    # unreachable, refused, not ONVIF...
                continue
        return None

    def now_local(self) -> datetime | None:
        """The camera's clock right now, or None when it cannot be read (then nothing is judged old)."""
        if not self.enabled:
            return None
        mono = time.monotonic()
        if self._sample and mono - self._sample[1] < CACHE_S:
            return self._sample[0] + timedelta(seconds=mono - self._sample[1])
        if mono - self._failed_at < RETRY_S:
            return None
        ip = self.db.get("camera_ip")
        t, how = None, ""
        if ip:
            t, how = self._fetch(ip), "the camera's ONVIF service"
            if t is None:
                t, how = self._fetch_web_date(ip), "the Date line of the camera's web page"
        if t is None and self.synced_from_us():
            t, how = datetime.now(), "this computer's clock (the camera takes its time from PlateBox)"
        if t is None:
            self._failed_at = mono
            self.how = ""
            self.problem = ("the camera did not answer a request for its time" if ip else "no camera is chosen yet")
            log.warning("cannot read the camera's clock: %s", self.problem)
            return None
        if how != self.how:
            log.info("the camera's clock is read from %s", how)
        self.how, self.problem = how, ""
        self._sample = (t, mono)
        return t

    def describe(self) -> str:
        """For the Settings page."""
        if not self.enabled:
            return ""
        t = self.now_local()
        if t is None:
            return f"PlateBox cannot read the camera's clock ({self.problem}), so it cannot tell old pictures from new ones."
        return f"The camera's clock reads {t:%d %b %Y %H:%M:%S} (from {self.how})."
