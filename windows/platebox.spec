# -*- mode: python ; coding: utf-8 -*-
# PyInstaller recipe for the Windows program.   Build with windows\build.ps1 (it prepares models_home first).
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

HERE = Path(SPECPATH)            # plate-box\windows
ROOT = HERE.parent               # plate-box

datas, binaries, hiddenimports = [], [], []

# Packages that carry data files, native libraries or import things dynamically.
for pkg in ("fast_alpr", "fast_plate_ocr", "open_image_models", "onnxruntime", "asyncssh", "cv2"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

hiddenimports += collect_submodules("uvicorn") + collect_submodules("platebox") + [
    "multipart", "python_multipart", "h11", "anyio._backends._asyncio", "email.mime.multipart",
]

datas += [
    (str(ROOT / "platebox" / "templates"), "platebox/templates"),
    (str(ROOT / "platebox" / "static"), "platebox/static"),
    # The plate models, already downloaded, so the program never needs the internet.
    (str(HERE / "models_home"), "models_home"),
]

a = Analysis(
    [str(HERE / "entry.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "pytest", "IPython", "notebook", "scipy"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="platebox",
    console=False,               # no black window: it runs in the background, and logs to a file
    icon=None,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="PlateBox")
