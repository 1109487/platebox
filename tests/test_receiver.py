"""Why a camera's Test fails, in plain words, and the rule that a right password is never refused."""
import asyncio
import time

import pytest

from platebox import sftp_server

from test_windows import asyncssh, connect, receiver  # noqa: F401  (the fixture and helpers live there)


def test_a_wrong_password_waits_but_the_right_one_is_never_refused(receiver, monkeypatch):
    """The earlier version locked an address out for 5 minutes after 5 wrong tries, silently, even for the right password."""
    cfg, db, r = receiver
    monkeypatch.setattr(sftp_server, "FAIL_DELAY_BASE_S", 2.0)           # a penalty far bigger than any login time

    async def attempt(pw):
        try:
            async with connect(cfg, password=pw):
                return True
        except asyncssh.PermissionDenied:
            return False

    async def go():
        t = time.time()
        first_wrong = await attempt("wrong-wrong")
        waited = time.time() - t
        # nine more wrong ones at once: far more than the old limit of five
        many = await asyncio.gather(*[attempt("wrong-wrong") for _ in range(9)])
        t = time.time()
        right = await attempt("Pa55word1234abcd")
        return first_wrong, waited, many, right, time.time() - t

    first_wrong, waited, many, right, right_took = asyncio.run(go())
    assert first_wrong is False and not any(many)
    assert waited >= 2.0                                     # a wrong password is made to wait
    assert right is True and right_took < 3.5                # the right one is let straight in (a penalty would be 5 s)


def test_the_diary_and_the_plain_words(receiver):
    cfg, db, r = receiver
    assert r.diagnosis("192.168.1.3")["kind"] == "silence"                                  # nothing has arrived

    async def one(user="ftpcam", pw="Pa55word1234abcd", sftp=False):
        try:
            async with connect(cfg, user=user, password=pw) as c:
                if sftp:
                    async with c.start_sftp_client() as s:
                        await s.listdir("/")
        except asyncssh.PermissionDenied:
            pass

    asyncio.run(one(pw="not-the-password"))
    d = r.diagnosis("192.168.1.3")
    assert d["kind"] == "password" and d["level"] == "bad" and "password" in d["text"] and "127.0.0.1" in d["text"]
    asyncio.run(one(user="admin"))
    assert r.diagnosis()["kind"] == "user" and "ftpcam" in r.diagnosis()["text"]
    asyncio.run(one(sftp=True))
    assert r.diagnosis()["kind"] == "ok" and r.diagnosis()["level"] == "ok"
    kinds = [e["kind"] for e in r.recent_events(20)]
    assert "login_password" in kinds and "login_user" in kinds and "login_ok" in kinds and "sftp" in kinds


def test_the_explanations_by_themselves():
    t = 1000.0

    def ev(kind, ago=5):
        return {"t": t - ago, "kind": kind, "ip": "192.168.1.64"}

    def explain(events, listening=True, error=""):
        return sftp_server.explain(events, listening, error, "192.168.1.3", 2222, "ftpcam", now=t)

    d = explain([])
    assert d["kind"] == "silence" and "192.168.1.3" in d["text"] and "Save" in d["text"] and "Firewall" in d["text"]
    assert explain([ev("login_password"), ev("connect", 6)])["kind"] == "password"
    assert explain([ev("login_user")])["kind"] == "user"
    assert explain([ev("login_ok")])["kind"] == "partial"
    assert explain([ev("sftp"), ev("login_ok", 6)])["kind"] == "ok"
    assert explain([ev("login_password", ago=5000)])["kind"] == "silence"                 # an old failure is not "just now"
    down = explain([], listening=False, error="Port 2222 is in use")
    assert down["kind"] == "not_running" and "2222" in down["text"]


def test_the_receiver_can_check_itself(receiver):
    cfg, db, r = receiver
    ok, msg = r.self_test("127.0.0.1")
    assert ok and "password works" in msg
    db.put("sftp_password", "")                                      # a broken setup is reported, not hidden
    ok, msg = r.self_test("127.0.0.1")
    assert not ok and "Could not log in" in msg
    r.stop()
    ok, msg = r.self_test("127.0.0.1")
    assert not ok
