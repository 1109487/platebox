"""Reading number plates out of a picture. Everything else in PlateBox only sees `PlateRead`."""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Protocol

# One plate reader, shared by the intake and the stream watcher: only one picture is read at a time.
READ_LOCK = threading.Lock()

# MH12AB1234, MH12A1234, DL1CAB1234   /   22BH1234AA (Bharat series)
_STANDARD = re.compile(r"^[A-Z]{2}\d{1,2}[A-Z]{1,3}\d{4}$")
_BH = re.compile(r"^\d{2}BH\d{4}[A-Z]{1,2}$")


def normalise(text: str) -> str:
    """Only A-Z and 0-9, upper case: what a plate comparison and a search should use."""
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def looks_valid(text: str) -> bool:
    return bool(_STANDARD.match(text) or _BH.match(text))


@dataclass
class PlateRead:
    text: str
    confidence: float          # 0..1, mean over characters
    det_confidence: float
    box: tuple[int, int, int, int]       # x, y, w, h in the full picture

    @property
    def valid(self) -> bool:
        return looks_valid(self.text)

    @property
    def width_px(self) -> int:
        return self.box[2]


class Reader(Protocol):
    def read(self, bgr) -> list[PlateRead]: ...


class AlprReader:
    """fast-alpr: an ONNX plate detector followed by an OCR model, CPU only.

    The models are loaded on first use (seconds), then kept. One picture is read at a
    time: two at once only make both slower on a small board.
    """

    def __init__(self, detector: str, ocr: str) -> None:
        self.detector_name, self.ocr_name = detector, ocr
        self._alpr = None
        self._lock = threading.Lock()

    def load(self) -> None:
        with self._lock:
            if self._alpr is None:
                from fast_alpr import ALPR

                self._alpr = ALPR(detector_model=self.detector_name, ocr_model=self.ocr_name)

    def read(self, bgr) -> list[PlateRead]:
        import numpy as np

        self.load()
        out: list[PlateRead] = []
        with self._lock:
            results = self._alpr.predict(bgr)
        for r in results:
            ocr, det = r.ocr, r.detection
            if ocr is None or not getattr(ocr, "text", None):
                continue
            c = getattr(ocr, "confidence", None)
            try:
                conf = float(np.asarray(c, dtype="float64").ravel().mean()) if c is not None else 0.0
            except (TypeError, ValueError):
                conf = 0.0
            bb = det.bounding_box
            x1, y1, x2, y2 = int(bb.x1), int(bb.y1), int(bb.x2), int(bb.y2)
            text = normalise(str(ocr.text))
            if text:
                out.append(PlateRead(text, conf, float(det.confidence or 0.0), (x1, y1, x2 - x1, y2 - y1)))
        return out
