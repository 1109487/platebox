import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from platebox.config import Config  # noqa: E402
from platebox.netmgr import FakeBackend  # noqa: E402
from platebox.reader import PlateRead  # noqa: E402
from platebox.web import create_app  # noqa: E402


class FakeReader:
    """Returns the scripted answer for each picture, in order, then nothing."""

    def __init__(self, script=None):
        self.script = list(script or [])
        self.calls = 0

    def read(self, bgr):
        self.calls += 1
        return self.script.pop(0) if self.script else []


def plate(text="MH12AB1234", conf=0.9, w=300):
    return PlateRead(text, conf, 0.9, (100, 100, w, w // 3))


def jpeg_bytes(w=640, h=360, shade=120):
    import cv2
    import numpy as np

    img = np.full((h, w, 3), shade, np.uint8)
    cv2.rectangle(img, (100, 100), (400, 200), (255, 255, 255), -1)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return buf.tobytes()


@pytest.fixture
def cfg(tmp_path):
    return Config(data_dir=tmp_path / "data", sftp_root=tmp_path / "sftp", fake_net=True, background=False,
                  intake_settle_s=0.0, cookie_secure=False)


@pytest.fixture
def make_app(cfg):
    def make(reader=None):
        return create_app(cfg, reader=reader or FakeReader(), net=FakeBackend(local_ip="192.168.1.50"))
    return make
