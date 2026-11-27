"""The screens that explain why a camera's Test failed, and the camera password options."""
import socket

import pytest
from fastapi.testclient import TestClient

from platebox.system import CUSTOM_PASSWORD, random_password


@pytest.fixture
def client(cfg, make_app):
    cfg.sftp_mode = "builtin"
    cfg.sftp_host = "127.0.0.1"
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    cfg.sftp_port = s.getsockname()[1]
    s.close()
    with TestClient(make_app(), follow_redirects=False) as c:
        box = c.app.state.box
        box.receiver.start()
        c.post("/setup/claim", data={"code": box.setup_code(), "password": "a long password", "confirm": "a long password"})
        c.post("/api/camera/select", json={"ip": "192.168.1.64", "label": "x"})
        yield c
        box.receiver.stop()


def test_random_passwords_are_easy_to_read_and_type():
    for _ in range(300):
        pw = random_password()
        assert len(pw) == 12 and CUSTOM_PASSWORD.match(pw)
        assert not set(pw) & set("0O1lI"), pw                       # no lookalikes


def test_the_connect_screen_data_explains_what_the_camera_did(client):
    st = client.get("/api/setup/status").json()
    assert st["receiver"]["listening"] is True
    assert st["receiver"]["diagnosis"]["kind"] == "silence" and "Save" in st["receiver"]["diagnosis"]["text"]
    box = client.app.state.box
    box.receiver.note("login_password", "192.168.1.64")
    st = client.get("/api/setup/status").json()
    assert st["receiver"]["diagnosis"]["kind"] == "password" and st["receiver"]["events"][0]["what"] == "wrong password"
    assert "Press Save on the camera first" in client.get("/setup").text


def test_settings_show_the_diary_and_the_check_button(client):
    client.post("/api/setup/finish", json={"force": True})
    box = client.app.state.box
    box.receiver.note("login_password", "192.168.1.64")
    page = client.get("/settings")
    assert "Is the camera reaching this computer?" in page.text and "wrong password" in page.text and 'id="recv-check"' in page.text
    r = client.post("/api/receiver/check").json()
    assert r["message"] and r["diagnosis"]["kind"] in ("password", "ok", "silence", "partial", "connecting", "user")


def test_the_check_needs_a_login_and_a_same_site_request(client):
    client.post("/api/setup/finish", json={"force": True})
    assert client.post("/api/receiver/check", headers={"origin": "http://evil.example"}).status_code == 403
    client.post("/logout")
    assert client.post("/api/receiver/check").status_code == 401


def test_choosing_your_own_camera_password(client):
    client.post("/api/setup/finish", json={"force": True})
    box = client.app.state.box
    assert "8 to 32" in client.post("/settings/sftp-password", data={"password": "short"}).text
    assert "8 to 32" in client.post("/settings/sftp-password", data={"password": "has space in it"}).text
    assert "8 to 32" in client.post("/settings/sftp-password", data={"password": "symbols!!!!!!"}).text
    ok = client.post("/settings/sftp-password", data={"password": "MyOwnPass2026"})
    assert "New camera password set" in ok.text and box.sftp.password() == "MyOwnPass2026"
    again = client.post("/settings/sftp-password", data={"password": ""})                    # empty: a made-up one
    assert "New camera password set" in again.text and box.sftp.password() != "MyOwnPass2026" and len(box.sftp.password()) == 12
