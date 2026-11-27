"""Finding faces in a picture and turning each into a list of numbers that is alike for the same person.

Two small models from the OpenCV project, run on the CPU by OpenCV itself (no extra library):
  YuNet  (face_detection_yunet_2023mar.onnx, MIT)       finds faces and their eyes, nose and mouth corners
  SFace  (face_recognition_sface_2021dec.onnx, Apache-2) turns an aligned face into 128 numbers

Two faces are compared by the cosine of their numbers: 1.0 is identical; the SFace authors put "same person" at 0.363
and above. Everything else in PlateBox only sees `Face`.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

log = logging.getLogger("platebox.faces")

DETECTOR_FILE = "face_detection_yunet_2023mar.onnx"
RECOGNISER_FILE = "face_recognition_sface_2021dec.onnx"
SAME_PERSON = 0.363            # the model authors' threshold; the default in Settings is stricter
DETECT_WIDTH = 1280            # a 4 MP picture is shrunk to this for finding faces; the face itself is cut from the original


@dataclass
class Face:
    box: tuple[int, int, int, int]       # x, y, w, h in the full picture
    score: float                         # 0..1: how sure the detector is that this is a face
    embedding: "list[float]"             # 128 numbers, length 1
    crop: "object | None" = None         # the aligned 112x112 face, BGR (what is stored and shown)

    @property
    def width_px(self) -> int:
        return self.box[2]


def similarity(a, b) -> float:
    """Cosine of two embeddings (both already length 1)."""
    import numpy as np

    return float(np.dot(np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)))


class FaceFinder(Protocol):
    def faces(self, bgr) -> list[Face]: ...


def models_dir(data_dir: Path) -> Path:
    """Where the two model files are: PLATEBOX_FACE_MODELS, else next to the program, else in the data folder."""
    env = os.environ.get("PLATEBOX_FACE_MODELS")
    if env:
        return Path(env)
    shipped = Path(__file__).resolve().parent.parent / "models"
    return shipped if (shipped / RECOGNISER_FILE).is_file() else data_dir / "models"


class CvFaceFinder:
    def __init__(self, folder: Path, min_score: float = 0.75, min_width_px: int = 56) -> None:
        self.folder, self.min_score, self.min_width_px = Path(folder), min_score, min_width_px
        self._det = self._rec = None
        self._lock = threading.Lock()
        self.error: str | None = None

    @property
    def available(self) -> bool:
        return (self.folder / DETECTOR_FILE).is_file() and (self.folder / RECOGNISER_FILE).is_file()

    def load(self) -> bool:
        with self._lock:
            if self._det is not None:
                return True
            if not self.available:
                self.error = f"The face models are not in {self.folder} ({DETECTOR_FILE}, {RECOGNISER_FILE})."
                return False
            try:
                import cv2

                self._det = cv2.FaceDetectorYN.create(str(self.folder / DETECTOR_FILE), "", (320, 320), self.min_score, 0.3, 50)
                self._rec = cv2.FaceRecognizerSF.create(str(self.folder / RECOGNISER_FILE), "")
                self.error = None
                return True
            except Exception as e:                      # a broken model file must not take the whole program down
                log.exception("the face models did not load")
                self._det = self._rec = None
                self.error = f"The face models did not load: {e}"
                return False

    def faces(self, bgr) -> list[Face]:
        import cv2
        import numpy as np

        if not self.load():
            return []
        h, w = bgr.shape[:2]
        scale = min(1.0, DETECT_WIDTH / float(w))
        small = cv2.resize(bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA) if scale < 1.0 else bgr
        out: list[Face] = []
        with self._lock:
            self._det.setInputSize((small.shape[1], small.shape[0]))
            _, found = self._det.detect(small)
            if found is None:
                return []
            for row in found:
                row = row.astype(np.float32).copy()
                row[:14] /= scale                       # box and the five landmarks, back in the original picture
                x, y, bw, bh = (int(round(v)) for v in row[:4])
                score = float(row[14])
                if score < self.min_score or bw < self.min_width_px:
                    continue
                aligned = self._rec.alignCrop(bgr, row.reshape(1, -1))
                vec = self._rec.feature(aligned).flatten().astype(np.float32)
                norm = float(np.linalg.norm(vec))
                if norm <= 0:
                    continue
                out.append(Face((max(0, x), max(0, y), bw, bh), score, (vec / norm).tolist(), aligned.copy()))
        out.sort(key=lambda f: -f.width_px)
        return out
