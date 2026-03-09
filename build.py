"""
build.py — One-command build to CommandCenter.exe
══════════════════════════════════════════════════
Copyright (c) 2025 Josh Stanley.  All rights reserved.

Usage (from project root, on Windows with Python 3.10+):
    python build.py

Produces:  dist/CommandCenter.exe

Requirements:
    pip install PyQt6 psutil pynvml pythonnet pyinstaller pillow

Optional (for CPU temps without AIDA64):
    Copy LibreHardwareMonitorLib.dll (from LibreHardwareMonitor release ZIP)
    into this project folder before running build.py — it will be bundled.
"""

import os
import sys
import subprocess
import shutil

HERE    = os.path.dirname(os.path.abspath(__file__))
DIST    = os.path.join(HERE, "dist")
WORK    = os.path.join(HERE, "build_tmp")
ICON    = os.path.join(HERE, "icon.ico")
LHM_DLL = os.path.join(HERE, "LibreHardwareMonitorLib.dll")

# ── UAC manifest (requestedExecutionLevel = requireAdministrator) ─────────
UAC_MANIFEST = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
  <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3">
    <security>
      <requestedPrivileges>
        <requestedExecutionLevel level="requireAdministrator" uiAccess="false"/>
      </requestedPrivileges>
    </security>
  </trustInfo>
  <compatibility xmlns="urn:schemas-microsoft-com:compatibility.v1">
    <application>
      <!-- Windows 10 / 11 -->
      <supportedOS Id="{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}"/>
    </application>
  </compatibility>
</assembly>
"""

MANIFEST_PATH = os.path.join(HERE, "CommandCenter.exe.manifest")

# ── Generate a minimal .ico if none exists ────────────────────────────────
def make_icon():
    try:
        from PIL import Image, ImageDraw
        size = 256
        img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        # Simple cyan hex badge
        cx, cy = size // 2, size // 2
        r = size // 2 - 8
        pts = [
            (cx + r * __import__('math').cos(__import__('math').radians(a)),
             cy + r * __import__('math').sin(__import__('math').radians(a)))
            for a in range(30, 360, 60)
        ]
        draw.polygon(pts, fill=(0, 200, 220, 255))
        img.save(ICON, format="ICO", sizes=[(256, 256), (64, 64), (32, 32), (16, 16)])
        print("[+] icon.ico generated")
    except ImportError:
        print("[!] Pillow not installed — skipping icon (EXE will use default)")

# ── Build ─────────────────────────────────────────────────────────────────
def build():
    # Write UAC manifest
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        f.write(UAC_MANIFEST)
    print("[+] UAC manifest written")

    if not os.path.exists(ICON):
        make_icon()

    # Compose PyInstaller args
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onefile",
        "--windowed",                         # no console window
        "--name", "CommandCenter",
        "--distpath", DIST,
        "--workpath", WORK,
        "--manifest", MANIFEST_PATH,          # EMBED UAC manifest → requireAdministrator
    ]

    if os.path.exists(ICON):
        args += ["--icon", ICON]

    # Bundle LHM DLL if present
    if os.path.exists(LHM_DLL):
        args += ["--add-binary", f"{LHM_DLL};."]
        print("[+] LibreHardwareMonitorLib.dll will be bundled")
    else:
        print("[!] LibreHardwareMonitorLib.dll NOT found — CPU temps via WMI fallback only")
        print("    Download from: https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases")

    # ── Hidden imports ────────────────────────────────────────────────────────
    # pynvml: both the legacy package name and the official nvidia-ml-py rename
    hidden = [
        "pynvml",
        "nvidia_ml_py",
        # psutil
        "psutil",
        "psutil._pswindows",
        # WMI (used for CPU/GPU name, disk labels, thermal fallback)
        "wmi",
        "wmi.WMI",
        "win32com.client",
        "win32com.server",
        "pythoncom",
        "pywintypes",
        # pythonnet / CLR bridge for LibreHardwareMonitor
        "clr",
        "clr._bootstrap",
        "clr._bootstrap_ext",
        "clr.interop",
        "Python.Runtime",
    ]
    for h in hidden:
        args += ["--hidden-import", h]

    # pythonnet needs all its internal modules collected — a simple hidden-import
    # list is not sufficient for frozen builds.
    args += ["--collect-all", "pythonnet"]

    args += [os.path.join(HERE, "main.py")]

    print("\n[+] Running PyInstaller…")
    result = subprocess.run(args, cwd=HERE)

    if result.returncode == 0:
        exe = os.path.join(DIST, "CommandCenter.exe")
        print(f"\n✅  Build successful → {exe}")
        print("    Right-click → Run as Administrator, or set UAC to auto-elevate.")
    else:
        print("\n❌  Build failed — check output above.")
        sys.exit(1)


if __name__ == "__main__":
    build()
