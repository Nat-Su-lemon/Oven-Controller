# PyInstaller build recipe. Run from this folder:  pyinstaller --noconfirm --clean CureOven.spec
# Windows -> dist/CureOven.exe (single file)
# macOS   -> dist/CureOven.app
# Linux   -> dist/CureOven (single file)
import os
import sys

APP_NAME = "CureOven"
SCRIPT = os.path.join(SPECPATH, "app", "cure_gui.py")

a = Analysis(
    [SCRIPT],
    hiddenimports=["serial.tools.list_ports", "matplotlib.backends.backend_tkagg"],
    # Keep the bundle small: none of these are used
    excludes=["PyQt5", "PyQt6", "PySide2", "PySide6", "IPython", "pandas", "scipy", "notebook"],
    noarchive=False,
)
pyz = PYZ(a.pure)

if sys.platform == "darwin":
    # macOS: a normal .app bundle (one-file .app bundles are deprecated in PyInstaller)
    exe = EXE(
        pyz, a.scripts, [],
        exclude_binaries=True,
        name=APP_NAME,
        console=False,
        argv_emulation=False,
    )
    coll = COLLECT(exe, a.binaries, a.datas, name=APP_NAME)
    app = BUNDLE(
        coll,
        name=APP_NAME + ".app",
        bundle_identifier="local.cureoven.gui",
        info_plist={
            "CFBundleDisplayName": "Cure Oven",
            "CFBundleShortVersionString": "1.0.0",
            "NSHighResolutionCapable": True,
        },
    )
else:
    # Windows and Linux: one self-contained executable
    exe = EXE(
        pyz, a.scripts, a.binaries, a.datas, [],
        name=APP_NAME,
        console=False,
        upx=False,
    )
