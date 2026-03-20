"""
COMMAND CENTER — Universal Hardware Dashboard
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Copyright (c) 2025 Josh Stanley.  All rights reserved.

This software and its source code are the intellectual property
of Josh Stanley.  Unauthorised copying, redistribution, or
modification, via any medium, is strictly prohibited without
the express written permission of the author.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Fully dynamic: auto-detects CPU, GPU, cores, TDP, thresholds, drive letters.
Works on any Windows PC — AMD/Intel CPU, NVIDIA/AMD GPU, any core count.
"""

__author__    = "Josh Stanley"
__copyright__ = "Copyright (c) 2025 Josh Stanley"
__version__   = "1.1.0"

import sys
import os
import re
import logging
import configparser
import threading

# ── Auto-elevate ─────────────────────────────────────────────────────────────
def is_admin():
    try:
        import ctypes
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except:
        return False

def elevate():
    import ctypes
    ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, " ".join(sys.argv), None, 1)
    sys.exit(0)

if sys.platform == "win32" and not is_admin():
    elevate()

# ── Logging — must be set up BEFORE importing hardware.py so its init logs
#    are captured to the log file.
def _setup_logging():
    """Redirect stdout/stderr to a rolling log file in %APPDATA%."""
    import traceback

    log_base = os.path.join(
        os.environ.get("APPDATA", os.path.dirname(os.path.abspath(sys.argv[0]))),
        "CommandCenter"
    )
    os.makedirs(log_base, exist_ok=True)
    log_path = os.path.join(log_base, "CommandCenter.log")

    logging.basicConfig(
        filename   = log_path,
        filemode   = "a",
        level      = logging.DEBUG,
        format     = "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt    = "%Y-%m-%d %H:%M:%S",
    )

    # Mirror print() → log file
    class _LogStream:
        def __init__(self, level):
            self._level = level
            self._buf   = ""
        def write(self, msg):
            self._buf += msg
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                if line.strip():
                    logging.log(self._level, line)
        def flush(self):
            pass

    sys.stdout = _LogStream(logging.INFO)
    sys.stderr = _LogStream(logging.WARNING)

    # Catch unhandled exceptions
    def _excepthook(exc_type, exc_value, exc_tb):
        msg = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        logging.critical("UNHANDLED EXCEPTION:\n%s", msg)
        try:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.critical(None, "Command Center -- Crash",
                                 f"An unexpected error occurred:\n\n{exc_value}\n\n"
                                 f"See {log_path} for full details.")
        except Exception:
            pass

    sys.excepthook = _excepthook

    logging.info("=" * 60)
    logging.info("Command Center v%s starting  (admin=%s)", __version__, is_admin())
    logging.info("=" * 60)
    return log_path

_LOG_PATH = _setup_logging()

import collections
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QGridLayout,
    QVBoxLayout, QHBoxLayout, QLabel, QDialog,
    QComboBox, QCheckBox, QPushButton, QFormLayout,
    QDialogButtonBox, QMenu, QSizePolicy, QSystemTrayIcon,
    QScrollArea
)
from PyQt6.QtCore import Qt, QTimer, QRectF, QRect, QPointF, QSize, QPoint, QMimeData
from PyQt6.QtGui import (
    QPainter, QColor, QPen, QBrush, QFont,
    QLinearGradient, QPainterPath, QPalette, QAction, QIcon, QDrag, QPixmap
)

from hardware import HardwareMonitor, HW_PROFILE, hwinfo_available, get_foreground_fps

log = logging.getLogger(__name__)

# ── App data directory — writable on all machines (avoids Program Files issues) ─
_APP_DATA = os.path.join(
    os.environ.get("APPDATA", os.path.dirname(os.path.abspath(sys.argv[0]))),
    "CommandCenter"
)
os.makedirs(_APP_DATA, exist_ok=True)

# ── Settings file ─────────────────────────────────────────────────────────────
SETTINGS_PATH = os.path.join(_APP_DATA, "settings.ini")

_DEFAULT_LAYOUT_STR = ("cpu_temp,cpu_load,cpu_power,cpu_freq,"
                       "gpu_temp,gpu_load,gpu_power,gpu_vram,"
                       "fps,ram,cpu_voltage,cpu_elec_current,"
                       "gpu_clock,gpu_voltage,cpu_therm_current,gpu_mem_clk")

def load_settings():
    cfg = configparser.ConfigParser()
    cfg.read(SETTINGS_PATH)
    return {
        "monitor":      cfg.get("window", "monitor",      fallback="0"),
        "startup":      cfg.getboolean("window", "startup", fallback=False),
        "width":        cfg.getint("window", "width",      fallback=1920),
        "height":       cfg.getint("window", "height",     fallback=1080),
        "theme":        cfg.get("window", "theme",         fallback="dark"),
        "accent":       cfg.get("window", "accent",        fallback="blue"),
        "layout":        cfg.get("window", "layout",        fallback=_DEFAULT_LAYOUT_STR),
        "hidden_panels": cfg.get("window", "hidden_panels", fallback=""),
    }

def save_settings(s):
    cfg = configparser.ConfigParser()
    cfg["window"] = {
        "monitor":      str(s["monitor"]),
        "startup":      str(s["startup"]),
        "width":        str(s["width"]),
        "height":       str(s["height"]),
        "theme":        str(s.get("theme", "dark")),
        "accent":       str(s.get("accent", "blue")),
        "layout":        str(s.get("layout", _DEFAULT_LAYOUT_STR)),
        "hidden_panels": str(s.get("hidden_panels", "")),
    }
    with open(SETTINGS_PATH, "w") as f:
        cfg.write(f)

# ── Windows startup registry ──────────────────────────────────────────────────
STARTUP_KEY  = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_NAME = "CommandCenter"

def set_startup(enabled: bool):
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_KEY,
                             0, winreg.KEY_SET_VALUE)
        if enabled:
            exe = os.path.abspath(sys.argv[0])
            winreg.SetValueEx(key, STARTUP_NAME, 0, winreg.REG_SZ, f'"{exe}"')
        else:
            try:
                winreg.DeleteValue(key, STARTUP_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
    except Exception as e:
        print(f"[startup] registry error: {e}")

def get_startup() -> bool:
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_KEY, 0,
                             winreg.KEY_READ)
        winreg.QueryValueEx(key, STARTUP_NAME)
        winreg.CloseKey(key)
        return True
    except Exception:
        return False

# ── Theme ─────────────────────────────────────────────────────────────────────
THEMES = {
    "dark": {
        "bg":     QColor(22, 22, 28),
        "card":   QColor(32, 32, 40),
        "border": QColor(255, 255, 255, 28),
        "blue":   QColor(60, 140, 255),
        "warn":   QColor(255, 180, 0),
        "crit":   QColor(255, 60, 60),
        "text":   QColor(220, 220, 230),
        "dim":    QColor(130, 130, 150),
        "green":  QColor(0, 210, 120),
        "titlebar_bg": QColor(18, 18, 24),
        "btn_hover":   QColor(255, 255, 255, 25),
    },
    "light": {
        "bg":     QColor(235, 236, 242),
        "card":   QColor(245, 246, 250),
        "border": QColor(0, 0, 0, 30),
        "blue":   QColor(30, 100, 220),
        "warn":   QColor(200, 120, 0),
        "crit":   QColor(200, 40, 40),
        "text":   QColor(30, 30, 45),
        "dim":    QColor(100, 105, 125),
        "green":  QColor(0, 160, 90),
        "titlebar_bg": QColor(220, 222, 230),
        "btn_hover":   QColor(0, 0, 0, 20),
    },
}

_theme_name = "dark"

def _t(key):
    return THEMES[_theme_name][key]

# ── Paint cache — QPen/QBrush/QFont built once per theme change, not per frame ─
class _PC:
    """Pre-built paint objects. Rebuild only when theme changes."""
    pen_border = QPen()
    pen_blue   = QPen()
    pen_warn   = QPen()
    pen_crit   = QPen()
    pen_text   = QPen()
    pen_dim    = QPen()
    pen_green  = QPen()

    brush_card  = QBrush()
    brush_blue  = QBrush()
    brush_warn  = QBrush()
    brush_crit  = QBrush()
    brush_track = QBrush()

    @classmethod
    def rebuild(cls):
        t = THEMES[_theme_name]
        cls.pen_border = QPen(t["border"], 1)
        cls.pen_blue   = QPen(t["blue"])
        cls.pen_warn   = QPen(t["warn"])
        cls.pen_crit   = QPen(t["crit"])
        cls.pen_text   = QPen(t["text"])
        cls.pen_dim    = QPen(t["dim"])
        cls.pen_green  = QPen(t["green"])
        cls.brush_card  = QBrush(t["card"])
        cls.brush_blue  = QBrush(t["blue"])
        cls.brush_warn  = QBrush(t["warn"])
        cls.brush_crit  = QBrush(t["crit"])
        track = QColor(0,0,0,30) if _theme_name=="light" else QColor(255,255,255,18)
        cls.brush_track = QBrush(track)

_PC.rebuild()

# QColor convenience (still needed for direct fillRect / QColor args)
def C_BG():     return _t("bg")
def C_CARD():   return _t("card")
def C_BORDER(): return _t("border")
def C_BLUE():   return _t("blue")
def C_WARN():   return _t("warn")
def C_CRIT():   return _t("crit")
def C_TEXT():   return _t("text")
def C_DIM():    return _t("dim")
def C_GREEN():  return _t("green")

# Cached pen/brush accessors — zero allocation per call
def PEN_BORDER(): return _PC.pen_border
def PEN_BLUE():   return _PC.pen_blue
def PEN_WARN():   return _PC.pen_warn
def PEN_CRIT():   return _PC.pen_crit
def PEN_TEXT():   return _PC.pen_text
def PEN_DIM():    return _PC.pen_dim
def PEN_GREEN():  return _PC.pen_green
def BRUSH_CARD(): return _PC.brush_card
def BRUSH_BLUE(): return _PC.brush_blue
def BRUSH_WARN(): return _PC.brush_warn
def BRUSH_CRIT(): return _PC.brush_crit
def BRUSH_TRACK():return _PC.brush_track

def _fill_brush(pct: float) -> QBrush:
    if pct > 90: return _PC.brush_crit
    if pct > 75: return _PC.brush_warn
    return _PC.brush_blue

def _fill_pen(pct: float) -> QPen:
    if pct > 90: return _PC.pen_crit
    if pct > 75: return _PC.pen_warn
    return _PC.pen_blue

# ── Font cache — QFont objects keyed by (family, size, bold) ─────────────────
# QFont construction is surprisingly expensive; cache every distinct combo.
class _FC:
    _cache: dict = {}

    @classmethod
    def get(cls, family: str, size: int, bold: bool = False) -> QFont:
        key = (family, size, bold)
        f   = cls._cache.get(key)
        if f is None:
            f = QFont(family, size, QFont.Weight.Bold if bold else QFont.Weight.Normal)
            cls._cache[key] = f
        return f

    @classmethod
    def clear(cls):
        cls._cache.clear()

def F(family: str, size: int, bold: bool = False) -> QFont:
    """Return a cached QFont — zero allocation after first call per combo."""
    return _FC.get(family, size, bold)


def _fsz(px_avail: int, n_chars: int, cap: int = 999, mn: int = 6) -> int:
    """Max font-pt so n_chars fit in px_avail pixels.

    Character width ≈ 0.62 × pt (Consolas / Segoe UI at 96 DPI).
    For height limiting pass rect_px as px_avail and 1 as n_chars
    (gives pt such that the glyph cell fits the rect: 1pt ≈ 1.333px,
    so effective coefficient is 1/0.62 ≈ 0.75 × rect_px).
    """
    return max(mn, min(cap, int(px_avail / max(n_chars, 1) / 0.62)))


FONT_NUM = "Consolas"
FONT_LBL = "Segoe UI"

NET_POINTS = 40   # was 60 — 40 points at 1s = 40s history, saves ~33%
IO_POINTS  = 40

# ── Accent color presets ─────────────────────────────────────────────────────
ACCENT_PRESETS = {
    "blue":   (60, 140, 255),
    "purple": (160, 80, 255),
    "teal":   (0, 200, 180),
    "orange": (255, 140, 40),
    "red":    (255, 70, 70),
    "cyan":   (0, 190, 230),
    "pink":   (255, 80, 160),
    "yellow": (240, 200, 40),
}

_accent_name = "blue"

def _apply_accent(name):
    """Set the accent colour in both themes and rebuild paint caches."""
    global _accent_name
    _accent_name = name
    r, g, b = ACCENT_PRESETS.get(name, ACCENT_PRESETS["blue"])
    THEMES["dark"]["blue"]  = QColor(r, g, b)
    THEMES["light"]["blue"] = QColor(max(0, r - 30), max(0, g - 40), max(0, b - 20))
    _PC.rebuild()
    _FC.clear()

# ── Panel swap system ────────────────────────────────────────────────────────
PANEL_CHOICES = [
    ("cpu_temp", "CPU Temperature"),
    ("cpu_load", "CPU Load"),
    ("gpu_temp", "GPU Temperature"),
    ("gpu_load", "GPU Load"),
    ("fps",      "FPS Counter"),
    ("ram",      "RAM Usage"),
    ("network",  "Network"),
    ("disk_io",  "Disk I/O"),
    ("net_ssd",  "Network + Disk I/O"),
]

# Grid positions for 6 slots: (row, col, rowspan, colspan)
SLOT_GRID = [
    (0, 0, 1, 1),   # slot 0
    (0, 1, 1, 1),   # slot 1
    (0, 2, 1, 1),   # slot 2
    (1, 0, 1, 1),   # slot 3
    (1, 1, 1, 1),   # slot 4
    (1, 2, 1, 1),   # slot 5
]

# Fan rename persistence
FAN_NAMES_PATH = os.path.join(_APP_DATA, "fan_names.ini")

def load_fan_names() -> dict:
    cfg = configparser.ConfigParser()
    cfg.read(FAN_NAMES_PATH)
    return dict(cfg["names"]) if "names" in cfg else {}

def save_fan_names(names: dict):
    cfg = configparser.ConfigParser()
    cfg["names"] = names
    with open(FAN_NAMES_PATH, "w") as f:
        cfg.write(f)


# ══════════════════════════════════════════════════════════════════════════════
#  SSD PANEL — one row per disk, read/write sparklines
# ══════════════════════════════════════════════════════════════════════════════
class SsdPanel(QWidget):
    """Shows per-disk I/O. Takes combined read/write bps — individual disk
    breakdown requires perdisk=True from psutil which we add in hardware."""

    def __init__(self, parent=None):
        super().__init__(parent)
        # dict: drive_label -> {read_hist, write_hist, read_now, write_now, peak}
        self._drives = {}
        self._order  = []
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(100, 120)

    def push(self, disk_io: list):
        """disk_io: list of (label, read_bps, write_bps)"""
        seen = set()
        for label, rbps, wbps in disk_io:
            seen.add(label)
            if label not in self._drives:
                self._drives[label] = {
                    "rh": collections.deque([0.0] * IO_POINTS, maxlen=IO_POINTS),
                    "wh": collections.deque([0.0] * IO_POINTS, maxlen=IO_POINTS),
                    "rn": 0.0, "wn": 0.0, "peak": 1.0,
                }
                self._order.append(label)
            d = self._drives[label]
            d["rh"].append(rbps); d["wh"].append(wbps)
            d["rn"] = rbps; d["wn"] = wbps
            d["peak"] = max(max(d["rh"]), max(d["wh"]), 1.0)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        pad  = max(8, int(w * 0.04))

        title_sz = max(7, int(h * 0.065))
        p.setPen(PEN_DIM())
        p.setFont(F(FONT_LBL, title_sz))
        p.drawText(QRect(0, int(h * 0.01), w, int(h * 0.11)),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                   "DISK I/O")

        drives = [lbl for lbl in self._order if lbl in self._drives]
        if not drives:
            p.end(); return

        n        = len(drives)
        row_top  = int(h * 0.12)
        row_h    = (h - row_top - pad) / max(n, 1)
        leg_sz   = max(6, int(row_h * 0.13))
        lbl_sz   = max(6, int(row_h * 0.15))
        graph_h  = max(10, int(row_h * 0.38))
        # Pre-make semi-transparent fill colours once per frame
        _blue_fill = QColor(C_BLUE()); _blue_fill.setAlpha(25)
        _grn_fill  = QColor(C_GREEN()); _grn_fill.setAlpha(25)

        for i, lbl in enumerate(drives):
            d  = self._drives[lbl]
            ry = row_top + i * row_h

            p.setPen(PEN_TEXT())
            p.setFont(F(FONT_LBL, lbl_sz, True))
            p.drawText(QRect(pad, int(ry + row_h * 0.02), w - pad * 2, int(row_h * 0.22)),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, lbl)

            legend_y = int(ry + row_h * 0.24)
            p.setPen(PEN_BLUE())
            p.setFont(F(FONT_NUM, leg_sz, True))
            p.drawText(QRect(pad, legend_y, (w - pad * 2) // 2, int(row_h * 0.18)),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       f"R {_fmt(d['rn'])}")
            p.setPen(PEN_GREEN())
            p.drawText(QRect(pad + (w - pad * 2) // 2, legend_y, (w - pad * 2) // 2, int(row_h * 0.18)),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       f"W {_fmt(d['wn'])}")

            gx = pad; gy = int(ry + row_h * 0.46); gw = w - pad * 2
            p.setBrush(BRUSH_TRACK())
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(QRectF(gx, gy, gw, graph_h), 3, 3)

            pk = d["peak"]
            for buf, stroke_pen, fill_col in (
                    (d["rh"], PEN_BLUE(),  _blue_fill),
                    (d["wh"], PEN_GREEN(), _grn_fill)):
                pts = list(buf); nn = len(pts)
                path = QPainterPath()
                for j, v in enumerate(pts):
                    x = gx + gw * j / max(nn - 1, 1)
                    y = gy + graph_h - graph_h * (v / pk) * 0.92
                    if j == 0: path.moveTo(x, y)
                    else:      path.lineTo(x, y)
                fill = QPainterPath(path)
                fill.lineTo(gx + gw, gy + graph_h)
                fill.lineTo(gx, gy + graph_h)
                fill.closeSubpath()
                p.setBrush(QBrush(fill_col)); p.setPen(Qt.PenStyle.NoPen)
                p.drawPath(fill)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(stroke_pen)
                p.drawPath(path)

            if i < n - 1:
                p.setPen(PEN_BORDER())
                p.drawLine(pad, int(ry + row_h - 2), w - pad, int(ry + row_h - 2))

        p.end()


# ══════════════════════════════════════════════════════════════════════════════
#  FAN PANEL — wider right panel, rename on double-click, persisted names
# ══════════════════════════════════════════════════════════════════════════════
class FanPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._fans      = []          # list of (orig_label, rpm)
        self._names     = load_fan_names()   # orig_label -> custom_name
        self._row_rects = []          # list of QRect per row for hit-testing
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self.setMinimumWidth(220)
        self.setMaximumWidth(400)

    def set_fans(self, fans):
        # Only repaint if RPM values changed meaningfully
        dirty = (len(fans) != len(self._fans) or
                 any(abs(a[1]-b[1]) > 20 for a, b in zip(fans, self._fans)))
        self._fans = fans
        if dirty: self.update()

    def mouseDoubleClickEvent(self, e):
        """Double-click a fan row to rename it."""
        pos = e.position().toPoint()
        for i, rect in enumerate(self._row_rects):
            if rect.contains(pos) and i < len(self._fans):
                orig_label, _ = self._fans[i]
                current_name  = self._names.get(orig_label, orig_label)
                self._prompt_rename(i, orig_label, current_name)
                break

    def _prompt_rename(self, idx, orig_label, current_name):
        from PyQt6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(
            self, "Rename Fan",
            f"New name for  \"{current_name}\":",
            text=current_name
        )
        if ok and name.strip():
            self._names[orig_label] = name.strip()
            save_fan_names(self._names)
            self.update()

    def _display_name(self, orig_label):
        return self._names.get(orig_label, orig_label)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        # Card background
        p.setBrush(QBrush(C_CARD()))
        p.setPen(PEN_BORDER())
        p.drawRoundedRect(QRectF(1, 1, w - 2, h - 2), 14, 14)

        pad     = 12
        avail_w = w - pad * 2
        title_sz = min(int(h * 0.036), _fsz(avail_w, 10))  # "FAN SPEEDS" = 10 chars
        title_sz = max(8, title_sz)
        title_h  = int(title_sz * 1.6 + 4)

        # Title
        p.setPen(PEN_DIM())
        p.setFont(F(FONT_LBL, title_sz))
        p.drawText(QRect(pad, 6, avail_w, title_h),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                   "FAN SPEEDS")

        # Hint — fits width, "dbl-click row to rename" = 23 chars
        hint_sz = max(5, min(int(h * 0.018), _fsz(avail_w, 23)))
        p.setFont(F(FONT_LBL, hint_sz))
        p.drawText(QRect(pad, title_h + 4, avail_w, hint_sz + 6),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                   "dbl-click row to rename")

        # Divider
        p.setPen(PEN_BORDER())
        div_y = title_h + hint_sz + 12
        p.drawLine(pad, div_y, w - pad, div_y)

        if not self._fans:
            p.setPen(PEN_DIM())
            p.setFont(F(FONT_LBL, 9))
            p.drawText(QRect(0, div_y, w, h - div_y),
                       Qt.AlignmentFlag.AlignCenter, "Fan speeds:\nneeds HWiNFO")
            p.end()
            return

        n            = len(self._fans)
        row_area_top = div_y + 6
        row_area_h   = h - row_area_top - pad
        row_h        = row_area_h / max(n, 1)
        max_fan      = max(HW_PROFILE.fan_max_rpm, max((r for _, r in self._fans), default=1))

        self._row_rects = []

        for i, (orig_label, rpm) in enumerate(self._fans):
            ry      = row_area_top + i * row_h
            bar_w   = avail_w
            bar_h   = max(5, int(row_h * 0.16))
            # Height cap: 1pt ≈ 1.333px, label rect = 32% of row_h, rpm rect = 30%
            lbl_sz  = max(7, min(int(row_h * 0.20), _fsz(bar_w, 20)))
            rpm_sz  = max(8, min(int(row_h * 0.21), _fsz(bar_w, 10)))

            self._row_rects.append(QRect(0, int(ry), w, int(row_h)))

            disp_name = self._display_name(orig_label)

            # Fan name — top portion of row
            p.setPen(PEN_DIM())
            p.setFont(F(FONT_LBL, lbl_sz))
            p.drawText(QRect(pad, int(ry + row_h * 0.04), bar_w, int(row_h * 0.32)),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       disp_name)

            # RPM value — below name, left-aligned, coloured
            rpm_color = (C_CRIT() if rpm > max_fan * 0.85
                         else C_WARN() if rpm > max_fan * 0.60
                         else C_BLUE())
            p.setPen(QPen(rpm_color))
            p.setFont(F(FONT_NUM, rpm_sz, True))
            p.drawText(QRect(pad, int(ry + row_h * 0.36), bar_w, int(row_h * 0.30)),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       f"{rpm:.0f} RPM")

            # Bar track
            bar_y = int(ry + row_h * 0.70)
            p.setPen(Qt.PenStyle.NoPen)
            bar_track_c = QColor(0, 0, 0, 30) if _theme_name == "light" else QColor(255, 255, 255, 18)
            p.setBrush(QBrush(bar_track_c))
            p.drawRoundedRect(QRectF(pad, bar_y, bar_w, bar_h), bar_h / 2, bar_h / 2)

            # Bar fill + glow
            fill_w = max(bar_h, bar_w * (rpm / max_fan))
            glow   = QColor(rpm_color); glow.setAlpha(45)
            p.setBrush(QBrush(glow))
            p.drawRoundedRect(QRectF(pad, bar_y - 1, fill_w, bar_h + 2), bar_h / 2, bar_h / 2)
            p.setBrush(QBrush(rpm_color))
            p.drawRoundedRect(QRectF(pad, bar_y, fill_w, bar_h), bar_h / 2, bar_h / 2)

            # Separator
            if i < n - 1:
                p.setPen(PEN_BORDER())
                p.drawLine(pad, int(ry + row_h - 1), w - pad, int(ry + row_h - 1))

        p.end()


# ══════════════════════════════════════════════════════════════════════════════
#  EXTRA SENSORS PANEL — dynamic panel for non-CPU/GPU/DIMM HWiNFO readings.
#  Covers: mobo temps, water cooling, VRM, chipset, Aquaero/Octo channels,
#  flow rates.  Auto-appears only when extra sensors are detected.
# ══════════════════════════════════════════════════════════════════════════════
class ExtraSensorsPanel(QWidget):
    _TITLE_H = 36
    _ROW_H   = 26

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sensors = []   # list of (label, value, unit_str)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(220)
        self.setMaximumWidth(400)
        self.setVisible(False)

    def set_sensors(self, sensors: list):
        if sensors == self._sensors:
            return
        self._sensors = sensors
        visible = bool(sensors)
        if visible:
            self.setFixedHeight(self._TITLE_H + len(sensors) * self._ROW_H + 10)
        self.setVisible(visible)
        if visible:
            self.update()

    def paintEvent(self, _):
        if not self._sensors:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        # Card background
        p.setBrush(QBrush(C_CARD()))
        p.setPen(PEN_BORDER())
        p.drawRoundedRect(QRectF(1, 1, w - 2, h - 2), 14, 14)

        pad = 16

        # Title
        p.setPen(PEN_DIM())
        p.setFont(F(FONT_LBL, max(8, int(self._TITLE_H * 0.44))))
        p.drawText(QRect(pad, 4, w - pad * 2, self._TITLE_H - 4),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                   "EXTRA SENSORS")

        # Divider
        p.setPen(PEN_BORDER())
        p.drawLine(pad, self._TITLE_H - 2, w - pad, self._TITLE_H - 2)

        for i, (lbl, val, unit) in enumerate(self._sensors):
            ry = self._TITLE_H + 2 + i * self._ROW_H
            sz = max(7, int(self._ROW_H * 0.44))

            if unit == "°C":
                val_c   = C_CRIT() if val > 85 else (C_WARN() if val > 70 else C_TEXT())
                val_str = f"{val:.0f}°C"
            else:
                val_c   = C_BLUE()
                val_str = f"{val:.1f} {unit}"

            p.setPen(PEN_DIM())
            p.setFont(F(FONT_LBL, sz))
            p.drawText(QRect(pad, ry, int(w * 0.60), self._ROW_H),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       lbl)

            p.setPen(QPen(val_c))
            p.setFont(F(FONT_NUM, sz, True))
            p.drawText(QRect(0, ry, w - pad, self._ROW_H),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       val_str)

            if i < len(self._sensors) - 1:
                p.setPen(PEN_BORDER())
                p.drawLine(pad, ry + self._ROW_H - 1, w - pad, ry + self._ROW_H - 1)

        p.end()


# ══════════════════════════════════════════════════════════════════════════════
#  FOOTER BAR — network + disk sparklines with values
# ══════════════════════════════════════════════════════════════════════════════
class FooterBar(QWidget):
    FOOT_H   = 72
    _NPTS    = 40

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(self.FOOT_H)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)

        self._tx_now   = 0.0;  self._rx_now   = 0.0
        self._ping_ms  = -1.0; self._pkt_loss = 0.0
        self._tx_hist  = collections.deque([0.0] * self._NPTS, maxlen=self._NPTS)
        self._rx_hist  = collections.deque([0.0] * self._NPTS, maxlen=self._NPTS)
        self._net_peak = 1.0

        self._disk_io     = {}
        self._drive_order = []
        self._sel_drive   = None
        self._drive_rect  = QRect()
        self._dr_hist     = collections.deque([0.0] * self._NPTS, maxlen=self._NPTS)
        self._dw_hist     = collections.deque([0.0] * self._NPTS, maxlen=self._NPTS)
        self._disk_peak   = 1.0

    def push_network(self, tx, rx, ping_ms=-1.0, pkt_loss=0.0):
        self._tx_now = tx; self._rx_now = rx
        self._ping_ms = ping_ms; self._pkt_loss = pkt_loss
        self._tx_hist.append(tx); self._rx_hist.append(rx)
        self._net_peak = max(max(self._tx_hist), max(self._rx_hist), 1.0)
        self.update()

    def push_disk(self, disk_io: list):
        for label, rbps, wbps in disk_io:
            if label not in self._disk_io:
                self._drive_order.append(label)
                if self._sel_drive is None:
                    self._sel_drive = label
            self._disk_io[label] = {"rn": rbps, "wn": wbps}
        if self._sel_drive and self._sel_drive in self._disk_io:
            d = self._disk_io[self._sel_drive]
            self._dr_hist.append(d["rn"]); self._dw_hist.append(d["wn"])
            self._disk_peak = max(max(self._dr_hist), max(self._dw_hist), 1.0)
        self.update()

    def mousePressEvent(self, e):
        if (e.button() == Qt.MouseButton.LeftButton and
                self._drive_rect.contains(e.position().toPoint()) and
                len(self._drive_order) > 1):
            self._show_drive_menu()

    def _show_drive_menu(self):
        menu = QMenu(self)
        is_dark = _theme_name == "dark"
        menu.setStyleSheet(
            "QMenu{background:#2a2a36;color:#dde0f0;border:1px solid #444;padding:4px;}"
            "QMenu::item{padding:5px 18px;}"
            "QMenu::item:selected{background:#3c8cff;}"
            if is_dark else
            "QMenu{background:#f0f0f6;color:#1e1e2e;border:1px solid #ccc;padding:4px;}"
            "QMenu::item{padding:5px 18px;}"
            "QMenu::item:selected{background:#3c8cff;color:white;}"
        )
        for label in self._drive_order:
            a = menu.addAction(label); a.setData(label)
        chosen = menu.exec(self.mapToGlobal(self._drive_rect.bottomLeft()))
        if chosen:
            self._sel_drive = chosen.data()
            self._dr_hist  = collections.deque([0.0] * self._NPTS, maxlen=self._NPTS)
            self._dw_hist  = collections.deque([0.0] * self._NPTS, maxlen=self._NPTS)
            self._disk_peak = 1.0
            self.update()

    def _draw_sparkline(self, p, hist, peak, color, fill_alpha,
                        gx, gy, gw, gh):
        pts  = list(hist)
        n    = len(pts)
        path = QPainterPath()
        for i, v in enumerate(pts):
            x = gx + gw * i / max(n - 1, 1)
            y = gy + gh - gh * (v / peak) * 0.92
            if i == 0: path.moveTo(x, y)
            else:       path.lineTo(x, y)
        fill = QPainterPath(path)
        fill.lineTo(gx + gw, gy + gh); fill.lineTo(gx, gy + gh); fill.closeSubpath()
        fc = QColor(color); fc.setAlpha(fill_alpha)
        p.setBrush(QBrush(fc)); p.setPen(Qt.PenStyle.NoPen); p.drawPath(fill)
        p.setBrush(Qt.BrushStyle.NoBrush); p.setPen(QPen(color, 1.2)); p.drawPath(path)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        bg = QColor(16, 16, 22) if _theme_name == "dark" else QColor(208, 210, 222)
        p.fillRect(0, 0, w, h, bg)
        p.setPen(QPen(_t("border"), 1)); p.drawLine(0, 0, w, 0)

        pad   = 10
        vsz   = max(8, int(h * 0.23))    # value font
        lsz   = max(6, int(h * 0.17))    # label font
        half  = w // 2
        row_h = h // 2                   # each value row height

        # Sparkline area — full height, minus top/bottom padding
        gy  = 5
        gh  = h - 10

        # Fixed column widths — val_w tied to font size, not window width
        lbl_w = 40                        # "NET" / "C:▾"
        val_w = max(90, int(vsz * 8.0))  # "▲ 100.0 MB/s" — wide enough for full string

        # ── NET half ──────────────────────────────────────────────
        val_x  = pad + lbl_w + 6
        gx_n   = val_x + val_w + 6
        max_gw = max(120, min(260, int(w * 0.16)))
        gw_n   = min(half - gx_n - pad, max_gw)

        # "NET" label — vertically centred
        p.setPen(PEN_DIM()); p.setFont(F(FONT_LBL, lsz, True))
        p.drawText(QRect(pad, 0, lbl_w, h),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "NET")

        # TX / RX values — vertically centred pair
        p.setPen(PEN_GREEN()); p.setFont(F(FONT_NUM, vsz, True))
        p.drawText(QRect(val_x, 1, val_w, row_h + 1),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
                   f"▲ {_fmt(self._tx_now)}")
        p.setPen(PEN_BLUE())
        p.drawText(QRect(val_x, row_h - 1, val_w, row_h + 1),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                   f"▼ {_fmt(self._rx_now)}")

        # Sparkline — right of values, full height
        if gw_n > 10:
            p.setBrush(BRUSH_TRACK()); p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(QRectF(gx_n, gy, gw_n, gh), 4, 4)
            self._draw_sparkline(p, self._rx_hist, self._net_peak,
                                 C_BLUE(), 25, gx_n, gy, gw_n, gh)
            self._draw_sparkline(p, self._tx_hist, self._net_peak,
                                 C_GREEN(), 25, gx_n, gy, gw_n, gh)

        # ── Separator ──
        p.setPen(QPen(_t("border"), 1)); p.drawLine(half, 6, half, h - 6)

        # ── DISK half ─────────────────────────────────────────────
        dx     = half + pad
        dlbl_w = max(56, int(vsz * 5.0))   # wider to fit "C: ▾" without overlap
        val_dx = dx + dlbl_w + 4
        gx_d  = val_dx + val_w + 6
        gw_d  = min(w - gx_d - pad, max_gw)

        if self._sel_drive and self._sel_drive in self._disk_io:
            d     = self._disk_io[self._sel_drive]
            multi = len(self._drive_order) > 1
            dr_str = self._sel_drive + ("  ▾" if multi else "")
            self._drive_rect = QRect(dx, 0, dlbl_w, h)

            if multi:
                hov = QColor(_t("blue")); hov.setAlpha(25)
                p.setBrush(QBrush(hov)); p.setPen(Qt.PenStyle.NoPen)
                p.drawRoundedRect(QRectF(dx - 3, 3, dlbl_w + 2, h - 6), 4, 4)

            p.setPen(PEN_DIM()); p.setFont(F(FONT_LBL, lsz, True))
            p.drawText(QRect(dx, 0, dlbl_w, h),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, dr_str)

            p.setPen(PEN_BLUE()); p.setFont(F(FONT_NUM, vsz, True))
            p.drawText(QRect(val_dx, 1, val_w, row_h + 1),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
                       f"R: {_fmt(d['rn'])}")
            p.setPen(PEN_GREEN())
            p.drawText(QRect(val_dx, row_h - 1, val_w, row_h + 1),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                       f"W: {_fmt(d['wn'])}")

            if gw_d > 10:
                p.setBrush(BRUSH_TRACK()); p.setPen(Qt.PenStyle.NoPen)
                p.drawRoundedRect(QRectF(gx_d, gy, gw_d, gh), 4, 4)
                self._draw_sparkline(p, self._dr_hist, self._disk_peak,
                                     C_BLUE(), 25, gx_d, gy, gw_d, gh)
                self._draw_sparkline(p, self._dw_hist, self._disk_peak,
                                     C_GREEN(), 25, gx_d, gy, gw_d, gh)
        else:
            self._drive_rect = QRect()
            p.setPen(PEN_DIM()); p.setFont(F(FONT_LBL, lsz))
            p.drawText(QRect(dx, 0, w - dx - pad, h),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "DISK  —")

        p.end()


# ══════════════════════════════════════════════════════════════════════════════
#  SENSOR LIST PANEL — AMD-style: sections with eye-toggle rows
# ══════════════════════════════════════════════════════════════════════════════
_SENSOR_SECTIONS = [
    ("CPU",     [("cpu_temp",          "CPU Temperature"),
                 ("cpu_load",          "CPU Load"),
                 ("cpu_power",         "CPU Power"),
                 ("cpu_freq",          "CPU Clock Speed"),
                 ("cpu_voltage",       "CPU Voltage"),
                 ("cpu_elec_current",  "CPU Electrical Current"),
                 ("cpu_therm_current", "CPU Thermal Current")]),
    ("GPU",     [("gpu_temp",    "GPU Temperature"),
                 ("gpu_load",    "GPU Load"),
                 ("gpu_power",   "GPU Power"),
                 ("gpu_vram",    "GPU VRAM"),
                 ("gpu_clock",   "GPU Clock Speed"),
                 ("gpu_voltage", "GPU Voltage"),
                 ("gpu_mem_clk", "GPU Memory Clock")]),
    ("DISPLAY", [("fps", "FPS Counter")]),
    ("MEMORY",  [("ram", "RAM Usage")]),
]


class _SectionHeader(QWidget):
    def __init__(self, label, parent=None):
        super().__init__(parent)
        self._label = label
        self.setFixedHeight(24)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def paintEvent(self, _):
        p = QPainter(self)
        w, h = self.width(), self.height()
        pad  = 10
        sz   = max(6, int(h * 0.46))
        p.setPen(PEN_DIM())
        p.setFont(F(FONT_LBL, sz, True))
        p.drawText(QRect(pad, 0, w - pad * 2, h - 2),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   self._label)
        p.setPen(QPen(_t("border"), 1))
        p.drawLine(pad, h - 1, w - pad, h - 1)
        p.end()


class _ToggleRow(QWidget):
    ROW_H = 30

    def __init__(self, panel_id, label, visible, on_toggle, parent=None):
        super().__init__(parent)
        self._panel_id = panel_id
        self._label    = label
        self._vis      = visible
        self._on_toggle = on_toggle
        self._hovered  = False
        self.setFixedHeight(self.ROW_H)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_visible(self, v):
        self._vis = v
        self.update()

    def enterEvent(self, _): self._hovered = True;  self.update()
    def leaveEvent(self, _): self._hovered = False; self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._vis = not self._vis
            self._on_toggle(self._panel_id, self._vis)
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        if self._hovered:
            hov = QColor(_t("blue")); hov.setAlpha(18)
            p.fillRect(0, 0, w, h, hov)

        pad   = 10
        sz    = max(7, int(h * 0.38))
        eye_d = max(10, int(h * 0.38))
        eye_x = w - pad - eye_d
        eye_y = (h - eye_d) // 2

        # Eye icon
        if self._vis:
            eye_c = _t("blue")
            p.setBrush(QBrush(eye_c)); p.setPen(Qt.PenStyle.NoPen)
            # Outer oval
            p.drawEllipse(QRectF(eye_x, eye_y + eye_d * 0.15,
                                 eye_d, eye_d * 0.70))
            # Pupil
            p.setBrush(QBrush(_t("card")))
            p.drawEllipse(QRectF(eye_x + eye_d * 0.30, eye_y + eye_d * 0.28,
                                 eye_d * 0.40, eye_d * 0.40))
        else:
            eye_c = _t("dim")
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(eye_c, 1.2))
            p.drawEllipse(QRectF(eye_x, eye_y + eye_d * 0.15,
                                 eye_d, eye_d * 0.70))
            # Slash through eye
            p.drawLine(QPointF(eye_x + eye_d * 0.1, eye_y + eye_d * 0.85),
                       QPointF(eye_x + eye_d * 0.9, eye_y + eye_d * 0.15))

        # Label
        lbl_c = _t("text") if self._vis else _t("dim")
        p.setPen(QPen(lbl_c))
        p.setFont(F(FONT_LBL, sz))
        p.drawText(QRect(pad, 0, eye_x - pad - 4, h),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   self._label)

        # Bottom divider
        p.setPen(QPen(_t("border"), 1))
        p.drawLine(pad, h - 1, w - pad, h - 1)
        p.end()


class _SidePanelTab(QWidget):
    """24px-wide vertical toggle strip."""
    def __init__(self, on_click, parent=None):
        super().__init__(parent)
        self._on_click = on_click
        self._expanded = True
        self._hovered  = False
        self.setFixedWidth(24)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMouseTracking(True)

    def set_expanded(self, v):
        self._expanded = v
        self.update()

    def enterEvent(self, _): self._hovered = True;  self.update()
    def leaveEvent(self, _): self._hovered = False; self.update()
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._on_click()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        c = QColor(_t("card"))
        if self._hovered:
            c = c.lighter(130) if _theme_name == "dark" else c.darker(108)
        p.fillRect(0, 0, w, h, c)
        p.setPen(PEN_BORDER()); p.drawLine(0, 0, 0, h)
        arrow = "◀" if self._expanded else "▶"
        p.save()
        p.translate(w // 2, h // 2); p.rotate(-90)
        p.setPen(PEN_DIM()); p.setFont(F(FONT_LBL, 8))
        p.drawText(QRect(-50, -w // 2, 100, w), Qt.AlignmentFlag.AlignCenter,
                   f"SENSORS  {arrow}")
        p.restore()
        p.end()


class SensorListPanel(QWidget):
    """AMD-style side panel with eye-toggle rows per sensor + fan/extra sections."""
    EXPANDED_W  = 260
    COLLAPSED_W = 0

    def __init__(self, on_visibility_change, parent=None):
        super().__init__(parent)
        self._on_vis  = on_visibility_change
        self._expanded = True
        self._rows    = {}   # panel_id -> _ToggleRow

        self.setFixedWidth(self.EXPANDED_W + 24)   # content + tab
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Scroll area for all content
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet(
            "QScrollArea{border:none;background:transparent;}"
            "QScrollBar:vertical{background:transparent;width:6px;border-radius:3px;}"
            "QScrollBar::handle:vertical{background:#555;border-radius:3px;min-height:20px;}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
        )

        content = QWidget()
        content.setStyleSheet("background: transparent;")
        cl = QVBoxLayout(content)
        cl.setContentsMargins(0, 6, 0, 8)
        cl.setSpacing(0)

        # Sensor toggle sections
        for section_name, items in _SENSOR_SECTIONS:
            cl.addWidget(_SectionHeader(section_name))
            for panel_id, label in items:
                row = _ToggleRow(panel_id, label, True, self._row_toggled)
                self._rows[panel_id] = row
                cl.addWidget(row)

        cl.addStretch()
        self._scroll.setWidget(content)
        outer.addWidget(self._scroll, stretch=1)

        # Tab strip on right edge — toggle button between panel and main grid
        self._tab = _SidePanelTab(self.toggle_expand)
        outer.addWidget(self._tab)

    def _row_toggled(self, panel_id, visible):
        self._on_vis(panel_id, visible)

    def set_panel_visible(self, panel_id, visible):
        if panel_id in self._rows:
            self._rows[panel_id].set_visible(visible)

    def toggle_expand(self):
        self._expanded = not self._expanded
        self._scroll.setVisible(self._expanded)
        self.setFixedWidth((self.EXPANDED_W + 24) if self._expanded else 24)
        self._tab.set_expanded(self._expanded)

    def is_expanded(self):
        return self._expanded


# ══════════════════════════════════════════════════════════════════════════════
#  RIGHT PANEL — fans + extra sensors (right side, always visible)
# ══════════════════════════════════════════════════════════════════════════════
class RightPanel(QWidget):
    """Holds FanPanel, DIMM temps, and ExtraSensorsPanel stacked vertically on the right."""
    WIDTH = 240

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(self.WIDTH)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

        vbox = QVBoxLayout(self)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(8)

        self.fan_panel   = FanPanel()
        self.ram_info    = RamInfoStrip()
        self.extra_panel = ExtraSensorsPanel()

        vbox.addWidget(self.fan_panel, stretch=1)
        vbox.addWidget(self.ram_info)
        vbox.addWidget(self.extra_panel)


# ══════════════════════════════════════════════════════════════════════════════
#  TITLE BAR — draggable bar with settings / fullscreen / close icons
# ══════════════════════════════════════════════════════════════════════════════
class TitleBar(QWidget):
    sig_settings   = None   # set by CommandCenter after init
    sig_fullscreen = None
    sig_close      = None

    def __init__(self, on_settings, on_fullscreen, on_minimize, on_close,
                 on_panel_toggle=None, parent=None):
        super().__init__(parent)
        self._on_settings   = on_settings
        self._on_fullscreen = on_fullscreen
        self._on_minimize   = on_minimize
        self._on_close      = on_close
        self._drag_pos      = None
        self._hovered       = None   # "settings" | "fs" | "min" | "close"
        self.setMinimumHeight(28)
        self.setMaximumHeight(48)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setMouseTracking(True)

    # ── Button rects (computed each paint) ───────────────────────────────────
    def _btn_rects(self):
        h   = self.height()
        bw  = max(28, int(h * 0.85))
        bh  = max(20, int(h * 0.70))
        y   = (h - bh) // 2
        gap = max(3, int(bw * 0.12))
        x4 = self.width() - bw - 6
        x3 = x4 - bw - gap
        x2 = x3 - bw - gap
        x1 = x2 - bw - gap
        return {
            "settings": QRect(x1, y, bw, bh),
            "fs":       QRect(x2, y, bw, bh),
            "min":      QRect(x3, y, bw, bh),
            "close":    QRect(x4, y, bw, bh),
        }

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        tb_bg = _t("titlebar_bg")
        p.fillRect(0, 0, w, h, tb_bg)

        # Bottom border
        p.setPen(QPen(_t("border"), 1))
        p.drawLine(0, h - 1, w, h - 1)

        # Title with detected hardware — scales with bar height
        title_sz = max(7, int(h * 0.30))
        cpu_short = _shorten_cpu(HW_PROFILE.cpu_name)
        gpu_short = _shorten_gpu(HW_PROFILE.gpu_name)
        title_str = f"COMMAND CENTER  ·  {cpu_short}  ·  {gpu_short}"
        p.setPen(QPen(_t("dim")))
        p.setFont(F(FONT_LBL, title_sz, True))
        p.drawText(QRect(14, 0, int(w * 0.75), h),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   title_str)

        # Author watermark — right side of title bar, left of the buttons
        rects_tmp = self._btn_rects()
        btn_left  = min(r.left() for r in rects_tmp.values()) - 8
        author_c  = QColor(_t("dim")); author_c.setAlpha(120)
        p.setPen(QPen(author_c))
        p.setFont(F(FONT_LBL, max(6, int(h * 0.24))))
        p.drawText(QRect(int(w * 0.50), 0, btn_left - int(w * 0.50), h),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   "© 2025 Josh Stanley")

        # Buttons
        rects = self._btn_rects()
        icon_sz = max(8, int(h * 0.34))
        icons = {"settings": "⚙", "fs": "⛶", "min": "—", "close": "✕"}
        for key, rect in rects.items():
            # Hover bg
            if self._hovered == key:
                hover_c = QColor(200, 40, 40, 180) if key == "close" else _t("btn_hover")
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(hover_c))
                p.drawRoundedRect(QRectF(rect), 5, 5)

            icon_c = QColor(220, 60, 60) if (key == "close" and self._hovered == "close") else _t("dim")
            p.setPen(QPen(icon_c))
            p.setFont(F(FONT_LBL, icon_sz))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, icons[key])

        p.end()

    def mouseMoveEvent(self, e):
        pos = e.position().toPoint()
        rects = self._btn_rects()
        prev = self._hovered
        self._hovered = next((k for k, r in rects.items() if r.contains(pos)), None)
        if self._hovered != prev:
            self.update()
        # Drag window if not over a button
        if not self._hovered and e.buttons() == Qt.MouseButton.LeftButton and self._drag_pos:
            win = self.window()
            win.move(win.pos() + e.globalPosition().toPoint() - self._drag_pos)
            self._drag_pos = e.globalPosition().toPoint()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = e.globalPosition().toPoint()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            pos   = e.position().toPoint()
            rects = self._btn_rects()
            if   rects["settings"].contains(pos): self._on_settings()
            elif rects["fs"].contains(pos):       self._on_fullscreen()
            elif rects["min"].contains(pos):      self._on_minimize()
            elif rects["close"].contains(pos):    self._on_close()
            self._drag_pos = None

    def leaveEvent(self, _):
        self._hovered = None
        self.update()


# ══════════════════════════════════════════════════════════════════════════════
#  SETTINGS DIALOG
# ══════════════════════════════════════════════════════════════════════════════
class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Command Center — Settings")
        self.setModal(True)
        self.setMinimumWidth(380)

        is_dark = _theme_name == "dark"
        bg    = "#1a1a22" if is_dark else "#f0f0f6"
        text  = "#dde0f0" if is_dark else "#1e1e2e"
        lbl   = "#8890a0" if is_dark else "#606070"
        inp   = "#2a2a36" if is_dark else "#e0e0ec"
        brd   = "#444"    if is_dark else "#ccc"

        self.setStyleSheet(f"""
            QDialog     {{ background: {bg}; color: {text}; }}
            QLabel      {{ color: {lbl}; }}
            QComboBox   {{ background: {inp}; color: {text}; border: 1px solid {brd}; padding: 4px; border-radius: 4px; }}
            QCheckBox   {{ color: {text}; }}
            QPushButton {{ background: #3c8cff; color: white; border: none;
                           padding: 6px 18px; border-radius: 4px; }}
            QPushButton:hover {{ background: #5aa0ff; }}
        """)

        layout = QFormLayout(self)
        layout.setSpacing(14)
        layout.setContentsMargins(20, 20, 20, 20)

        # Monitor picker
        self._monitor_cb = QComboBox()
        screens = QApplication.screens()
        for i, s in enumerate(screens):
            g = s.geometry()
            self._monitor_cb.addItem(
                f"Monitor {i+1}  —  {g.width()}×{g.height()}  ({s.name()})", i)
        try:
            self._monitor_cb.setCurrentIndex(int(settings["monitor"]))
        except Exception:
            self._monitor_cb.setCurrentIndex(0)
        layout.addRow("Display:", self._monitor_cb)

        # Theme toggle
        self._theme_cb = QComboBox()
        self._theme_cb.addItem("🌙  Dark",  "dark")
        self._theme_cb.addItem("☀  Light", "light")
        self._theme_cb.setCurrentIndex(0 if _theme_name == "dark" else 1)
        layout.addRow("Theme:", self._theme_cb)

        # Accent colour
        self._accent_cb = QComboBox()
        _accent_labels = {
            "blue": "🔵  Blue", "purple": "🟣  Purple", "teal": "🟢  Teal",
            "orange": "🟠  Orange", "red": "🔴  Red", "cyan": "🔵  Cyan",
            "pink": "🩷  Pink", "yellow": "🟡  Yellow",
        }
        for key in ACCENT_PRESETS:
            self._accent_cb.addItem(_accent_labels.get(key, key.title()), key)
        cur_accent = settings.get("accent", _accent_name)
        accent_keys = list(ACCENT_PRESETS.keys())
        self._accent_cb.setCurrentIndex(
            accent_keys.index(cur_accent) if cur_accent in accent_keys else 0)
        layout.addRow("Accent:", self._accent_cb)

        # Startup checkbox
        self._startup_cb = QCheckBox("Launch Command Center on Windows startup")
        self._startup_cb.setChecked(get_startup())
        layout.addRow("", self._startup_cb)

        # About row
        about_lbl = QLabel(
            "<span style='font-size:10px;'>"
            "<b>Command Center</b> v1.1 &nbsp;·&nbsp; "
            "© 2025 <b>Josh Stanley</b> &nbsp;·&nbsp; All rights reserved."
            "</span>"
        )
        about_lbl.setOpenExternalLinks(False)
        layout.addRow("", about_lbl)

        # Buttons
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addRow(btns)

    def get_values(self):
        return {
            "monitor": self._monitor_cb.currentData(),
            "startup": self._startup_cb.isChecked(),
            "theme":   self._theme_cb.currentData(),
            "accent":  self._accent_cb.currentData(),
        }


# ══════════════════════════════════════════════════════════════════════════════
#  CARD — dark rounded tile, fully scalable
# ══════════════════════════════════════════════════════════════════════════════
class Card(QWidget):
    _MIME = "application/x-cc-panel"

    def __init__(self, slot_id=None, panel_id=None, on_swap=None, parent=None):
        super().__init__(parent)
        self._layout   = QVBoxLayout(self)
        self._layout.setContentsMargins(10, 10, 10, 10)
        self._layout.setSpacing(4)
        self._slot_id  = slot_id
        self._panel_id = panel_id
        self._on_swap  = on_swap
        self._drag_start = None
        self._drop_hover = False
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)
        self.setAcceptDrops(True)

    def add(self, w, stretch=0):
        self._layout.addWidget(w, stretch)
        w.show()

    def clear(self):
        while self._layout.count():
            item = self._layout.takeAt(0)
            w = item.widget()
            if w:
                w.setParent(None)

    # ── Drag source ──────────────────────────────────────────────────────────
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_start = e.position().toPoint()

    def mouseReleaseEvent(self, e):
        self._drag_start = None

    def mouseMoveEvent(self, e):
        if (self._drag_start is None or self._panel_id is None or
                not (e.buttons() & Qt.MouseButton.LeftButton)):
            return
        if ((e.position().toPoint() - self._drag_start).manhattanLength()
                < QApplication.startDragDistance()):
            return

        drag = QDrag(self)
        mime = QMimeData()
        mime.setText(self._panel_id)
        drag.setMimeData(mime)

        # Grab a semi-transparent thumbnail of this card as the drag image
        pix = self.grab()
        w2  = max(60, pix.width() // 2)
        h2  = max(40, pix.height() // 2)
        scaled = pix.scaled(w2, h2,
                             Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
        faded = QPixmap(scaled.size())
        faded.fill(Qt.GlobalColor.transparent)
        pp = QPainter(faded)
        pp.setOpacity(0.75)
        pp.drawPixmap(0, 0, scaled)
        pp.end()
        drag.setPixmap(faded)
        drag.setHotSpot(QPoint(faded.width() // 2, faded.height() // 2))

        drag.exec(Qt.DropAction.MoveAction)
        self._drag_start = None

    # ── Drop target ──────────────────────────────────────────────────────────
    def dragEnterEvent(self, e):
        if e.mimeData().hasText() and e.mimeData().text() != self._panel_id:
            self._drop_hover = True
            self.update()
            e.acceptProposedAction()

    def dragLeaveEvent(self, e):
        self._drop_hover = False
        self.update()

    def dropEvent(self, e):
        self._drop_hover = False
        src = e.mimeData().text()
        if src and self._on_swap and self._slot_id is not None:
            self._on_swap(self._slot_id, src)
        e.acceptProposedAction()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QBrush(C_CARD()))
        border = QColor(_t("blue")) if self._drop_hover else _t("border")
        pen_w  = 2 if self._drop_hover else 1
        p.setPen(QPen(border, pen_w))
        p.drawRoundedRect(QRectF(1, 1, self.width()-2, self.height()-2), 14, 14)
        if self._drop_hover:
            hl = QColor(_t("blue")); hl.setAlpha(18)
            p.fillRect(2, 2, self.width()-4, self.height()-4, hl)
        p.end()


# ══════════════════════════════════════════════════════════════════════════════
#  SELF RAM STRIP — shows how much RAM this process is using
# ══════════════════════════════════════════════════════════════════════════════
class RamInfoStrip(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._used_mb   = 0.0
        self._total_mb  = 0.0
        self._dimm_temps = []
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(30)

    def set_values(self, used_mb: float, total_mb: float, dimm_temps: list = None):
        self._used_mb    = used_mb
        self._total_mb   = total_mb
        self._dimm_temps = dimm_temps or []
        # 30px system row + 24px per DIMM (or 20px for the hint when none)
        extra = len(self._dimm_temps) * 24 if self._dimm_temps else 20
        self.setFixedHeight(30 + extra)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h   = self.width(), self.height()
        pad    = 10
        avail  = w - pad * 2
        row_h  = 30
        # font size scales with both row height AND available width
        sz = max(6, min(int(row_h * 0.46), _fsz(avail, 22)))

        p.setPen(PEN_BORDER())
        p.drawLine(pad, 0, w - pad, 0)

        used_gb  = self._used_mb  / 1024
        total_gb = self._total_mb / 1024
        pct      = used_gb / max(total_gb, 1)
        ram_c    = C_CRIT() if pct > 0.90 else (C_WARN() if pct > 0.75 else C_TEXT())

        val_str  = f"{used_gb:.1f} / {total_gb:.0f} GB"
        lbl_str  = "System"
        # Split width: label left half, value right half (no overlap)
        half = avail // 2
        p.setPen(PEN_DIM())
        p.setFont(F(FONT_LBL, sz))
        p.drawText(QRect(pad, 0, half, row_h),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   lbl_str)
        p.setPen(QPen(ram_c))
        p.setFont(F(FONT_NUM, sz, True))
        p.drawText(QRect(pad + half, 0, half, row_h),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   val_str)

        for i, (lbl, temp) in enumerate(self._dimm_temps):
            y    = row_h + i * 24
            rh   = 24
            sz2  = max(6, min(int(rh * 0.44), _fsz(avail, 18)))
            tc   = C_CRIT() if temp > 85 else (C_WARN() if temp > 70 else C_TEXT())

            p.setPen(PEN_DIM())
            p.setFont(F(FONT_LBL, sz2))
            p.drawText(QRect(pad, y, half, rh),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       lbl)
            p.setPen(QPen(tc))
            p.setFont(F(FONT_NUM, sz2, True))
            p.drawText(QRect(pad + half, y, half, rh),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       f"{temp:.0f}°C")

        if not self._dimm_temps:
            sz2 = max(5, min(int(20 * 0.40), _fsz(avail, 28)))
            p.setPen(PEN_DIM())
            p.setFont(F(FONT_LBL, sz2))
            p.drawText(QRect(pad, row_h, avail, 20),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       "Start HWiNFO for DIMM temps")
        p.end()


# ══════════════════════════════════════════════════════════════════════════════
#  STATS STRIP — compact info bar at bottom of gauge cards
#  MODE_CPU_INFO:  ⚡W  |  freq GHz  |  VCore          → under CPU TEMP
#  MODE_CPU_CORES: per-core bars with % labels          → under CPU LOAD
#  MODE_GPU_INFO:  ⚡W  |  core MHz  |  voltage         → under GPU TEMP
#  MODE_GPU_MEM:   🧠 VRAM used/total  |  mem clock     → under GPU LOAD
# ══════════════════════════════════════════════════════════════════════════════
class StatsStrip(QWidget):
    MODE_CPU_INFO  = "cpu_info"
    MODE_CPU_CORES = "cpu_cores"
    MODE_GPU_INFO  = "gpu_info"
    MODE_GPU_MEM   = "gpu_mem"

    def __init__(self, mode=MODE_CPU_INFO, parent=None):
        super().__init__(parent)
        self._mode       = mode
        self._watts      = 0.0
        self._freq       = 0.0
        self._mem_clk    = 0.0
        self._cores      = []
        self._core_freqs = []
        self._voltage    = 0.0
        self._mem_used   = 0.0
        self._mem_tot    = 0.0
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        heights = {
            self.MODE_CPU_INFO:  36,
            self.MODE_CPU_CORES: 72,
            self.MODE_GPU_INFO:  36,
            self.MODE_GPU_MEM:   68,
        }
        self.setFixedHeight(heights.get(mode, 36))

    def set_cpu(self, watts, freq_mhz, cores, voltage=0.0, core_freqs=None):
        dirty = (abs(watts - self._watts) > 0.5 or
                 abs(freq_mhz - self._freq) > 5 or
                 abs(voltage - self._voltage) > 0.001 or
                 self._mode == self.MODE_CPU_CORES)
        self._watts      = watts
        self._freq       = freq_mhz
        self._cores      = list(cores)
        self._voltage    = voltage
        self._core_freqs = list(core_freqs) if core_freqs else []
        if dirty: self.update()

    def set_gpu(self, watts, core_mhz, mem_mhz, mem_used=0.0, mem_total=0.0, voltage=0.0):
        dirty = (abs(watts - self._watts) > 0.5 or
                 abs(core_mhz - self._freq) > 5 or
                 abs(mem_used - self._mem_used) > 10)
        self._watts    = watts
        self._freq     = core_mhz
        self._mem_clk  = mem_mhz
        self._mem_used = mem_used
        self._mem_tot  = mem_total
        self._voltage  = voltage
        if dirty: self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        pad  = 10

        p.setPen(PEN_BORDER())
        p.drawLine(pad, 0, w - pad, 0)

        if   self._mode == self.MODE_CPU_INFO:   self._paint_cpu_info(p, w, h, pad)
        elif self._mode == self.MODE_CPU_CORES:  self._paint_cpu_cores(p, w, h, pad)
        elif self._mode == self.MODE_GPU_INFO:   self._paint_gpu_info(p, w, h, pad)
        elif self._mode == self.MODE_GPU_MEM:    self._paint_gpu_mem(p, w, h, pad)
        p.end()

    # ── CPU INFO: ⚡W | freq | VCore ─────────────────────────────────────────
    def _paint_cpu_info(self, p, w, h, pad):
        col_w = max(1, (w - pad * 2) // 3)
        sz  = max(8, min(int(h * 0.36), _fsz(col_w, 9)))   # "9.99 GHz" ≈ 9 chars
        hsz = max(6, min(int(h * 0.26), _fsz(w - pad * 2, 40)))
        mid = int(h * 0.5)
        rh  = int(h * 0.60)

        no_hwinfo = (self._watts == 0 and self._voltage == 0)

        if no_hwinfo:
            # Single centred hint line — clean, no clutter
            p.setPen(PEN_DIM())
            p.setFont(F(FONT_LBL, hsz))
            p.drawText(QRect(pad, 0, w - pad*2, h),
                       Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter,
                       f"⚡ —    {self._freq/1000:.2f} GHz    ⚠ Start HWiNFO for power & voltage")
            return

        watt_c = C_WARN() if self._watts > HW_PROFILE.cpu_power_warn else C_BLUE()
        p.setPen(QPen(watt_c))
        p.setFont(F(FONT_NUM, sz, True))
        p.drawText(QRect(pad, mid - rh//2, w // 3, rh),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   f"⚡{self._watts:.0f}W")

        p.setPen(PEN_DIM())
        p.setFont(F(FONT_NUM, sz, True))
        freq_str = (f"{self._freq/1000:.2f} GHz" if self._freq >= 1000
                    else f"{self._freq:.0f} MHz")
        p.drawText(QRect(pad + w // 3, mid - rh//2, w // 3, rh),
                   Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter,
                   freq_str)

        p.setPen(PEN_GREEN())
        p.setFont(F(FONT_NUM, sz, True))
        p.drawText(QRect(0, mid - rh//2, w - pad, rh),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   f"{self._voltage:.3f}V")

    # ── CPU CORES: clock on top, bar, core number on bottom ──────────────────
    def _paint_cpu_cores(self, p, w, h, pad):
        cores = self._cores
        n     = len(cores)
        if n == 0:
            return

        has_freqs = len(self._core_freqs) == n

        top_lbl_h  = int(h * 0.22)   # clock label zone
        bot_lbl_h  = int(h * 0.20)   # core number zone
        bar_top    = top_lbl_h + 2
        bar_h      = h - top_lbl_h - bot_lbl_h - 4
        bar_area   = w - pad * 2
        gap        = max(1, int(bar_area / n * 0.15))
        bw         = max(3, (bar_area - gap * (n - 1)) // n)
        lbl_sz     = max(4, int(min(top_lbl_h, bw) * 0.55))

        p.setPen(Qt.PenStyle.NoPen)
        for i, pct in enumerate(cores):
            bx     = pad + i * (bw + gap)
            p.setBrush(BRUSH_TRACK())
            p.drawRoundedRect(QRectF(bx, bar_top, bw, bar_h), 1, 1)

            fill_h = max(1, int(bar_h * pct / 100))
            p.setBrush(_fill_brush(pct))
            p.drawRoundedRect(QRectF(bx, bar_top + bar_h - fill_h, bw, fill_h), 1, 1)

            if bw < 8:
                continue

            p.setPen(PEN_DIM())
            p.setFont(F(FONT_NUM, lbl_sz))
            if has_freqs:
                ghz   = self._core_freqs[i] / 1000.0
                top_s = f"{ghz:.1f}"
            else:
                top_s = f"{pct:.0f}%"
            p.drawText(QRect(int(bx) - 1, 1, bw + 2, top_lbl_h),
                       Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter,
                       top_s)

            p.drawText(QRect(int(bx) - 1, bar_top + bar_h + 1, bw + 2, bot_lbl_h),
                       Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter,
                       f"C{i}")
            p.setPen(Qt.PenStyle.NoPen)

    # ── GPU INFO: ⚡W | core MHz | voltage ────────────────────────────────────
    def _paint_gpu_info(self, p, w, h, pad):
        col_w = max(1, (w - pad * 2) // 3)
        sz  = max(8, min(int(h * 0.36), _fsz(col_w, 9)))   # "9999 MHz" ≈ 9 chars
        hsz = max(6, min(int(h * 0.26), _fsz(w - pad * 2, 40)))
        mid = int(h * 0.5)
        rh  = int(h * 0.60)

        no_hwinfo = (self._watts == 0 and self._voltage == 0)

        if no_hwinfo:
            p.setPen(PEN_DIM())
            p.setFont(F(FONT_LBL, hsz))
            p.drawText(QRect(pad, 0, w - pad*2, h),
                       Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter,
                       f"⚡ —    {self._freq:.0f} MHz    ⚠ Start HWiNFO for power & voltage")
            return

        watt_c = C_WARN() if self._watts > HW_PROFILE.gpu_power_warn else C_BLUE()
        p.setPen(QPen(watt_c))
        p.setFont(F(FONT_NUM, sz, True))
        p.drawText(QRect(pad, mid - rh//2, w // 3, rh),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   f"⚡{self._watts:.0f}W")

        p.setPen(PEN_DIM())
        p.setFont(F(FONT_NUM, sz, True))
        p.drawText(QRect(pad + w // 3, mid - rh//2, w // 3, rh),
                   Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter,
                   f"{self._freq:.0f} MHz")

        p.setPen(PEN_GREEN())
        p.setFont(F(FONT_NUM, sz, True))
        p.drawText(QRect(0, mid - rh//2, w - pad, rh),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   f"{self._voltage:.3f}V")

    # ── GPU MEM: 🧠 label + used/total  +  filled progress bar  +  mem clock ──
    def _paint_gpu_mem(self, p, w, h, pad):
        half_w  = max(1, (w - pad * 2) // 2)
        sz      = max(6, min(int(h * 0.19), _fsz(half_w, 11)))
        bar_h   = max(6, int(h * 0.22))
        row1_y  = int(h * 0.06)
        row1_h  = int(h * 0.32)
        bar_y   = int(h * 0.44)
        row3_y  = int(h * 0.72)
        row3_h  = int(h * 0.26)

        # Row 1: 🧠  used / total GB  (right: mem clock)
        p.setPen(PEN_DIM())
        p.setFont(F(FONT_LBL, sz))
        p.drawText(QRect(pad, row1_y, int(w * 0.11), row1_h),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "🧠")

        vram_str = (f"{self._mem_used/1024:.1f} / {self._mem_tot/1024:.0f} GB"
                    if self._mem_tot > 0 else "— GB")
        p.setPen(PEN_TEXT())
        p.setFont(F(FONT_NUM, sz, True))
        p.drawText(QRect(pad + int(w * 0.12), row1_y, int(w * 0.52), row1_h),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   vram_str)

        p.setPen(PEN_GREEN())
        p.setFont(F(FONT_NUM, sz))
        p.drawText(QRect(0, row1_y, w - pad, row1_h),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   f"{self._mem_clk:.0f} MHz")

        # Row 2: VRAM progress bar
        bar_w   = w - pad * 2
        pct     = (self._mem_used / self._mem_tot) if self._mem_tot > 0 else 0.0
        pct     = max(0.0, min(1.0, pct))
        fill_c  = C_CRIT() if pct > 0.90 else (C_WARN() if pct > 0.75 else C_BLUE())

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(BRUSH_TRACK())
        p.drawRoundedRect(QRectF(pad, bar_y, bar_w, bar_h), bar_h/2, bar_h/2)

        if pct > 0:
            fill_w = max(bar_h, bar_w * pct)
            glow_c = QColor(fill_c); glow_c.setAlpha(45)
            p.setBrush(QBrush(glow_c))
            p.drawRoundedRect(QRectF(pad, bar_y - 1, fill_w, bar_h + 2), bar_h/2, bar_h/2)
            p.setBrush(_fill_brush(pct * 100))
            p.drawRoundedRect(QRectF(pad, bar_y, fill_w, bar_h), bar_h/2, bar_h/2)

        # Row 3: segment ticks every 4GB
        if self._mem_tot > 0:
            total_gb   = self._mem_tot / 1024
            tick_every = 4 if total_gb > 8 else 2
            p.setPen(PEN_BORDER())
            gb = tick_every
            while gb < total_gb:
                tx = pad + bar_w * (gb / total_gb)
                p.drawLine(int(tx), bar_y + 1, int(tx), bar_y + bar_h - 1)
                gb += tick_every



# ══════════════════════════════════════════════════════════════════════════════
#  GAUGE — scales with widget size
# ══════════════════════════════════════════════════════════════════════════════
class Gauge(QWidget):
    def __init__(self, title, unit, max_val=100, warn=75, crit=90, parent=None):
        super().__init__(parent)
        self.title   = title
        self.unit    = unit
        self.max_val = max_val
        self.warn    = warn
        self.crit    = crit
        self._val    = 0.0
        self._disp   = 0.0
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)
        self.setMinimumSize(120, 120)

    def set_value(self, v):
        nv = max(0.0, min(float(v), self.max_val))
        if abs(nv - self._val) > 0.05:   # ignore sub-0.05 jitter
            self._val = nv

    def tick(self):
        prev = self._disp
        self._disp += (self._val - self._disp) * 0.18
        if abs(self._disp - prev) > 0.05:
            self.update()

    def _arc_color(self):
        pct = self._disp / self.max_val * 100
        if pct >= self.crit: return C_CRIT()
        if pct >= self.warn: return C_WARN()
        return C_BLUE()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h   = self.width(), self.height()
        cx, cy = w / 2, h / 2 + h * 0.04
        r      = min(w, h) * 0.34
        pen_w  = max(6, r * 0.15)

        title_size = max(7, int(h * 0.072))
        p.setPen(PEN_DIM())
        p.setFont(F(FONT_LBL, title_size))
        p.drawText(QRect(0, int(h * 0.03), w, int(h * 0.13)),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                   self.title)

        rect = QRectF(cx - r, cy - r, r * 2, r * 2)

        # Track arc
        track_c = QColor(0,0,0,40) if _theme_name=="light" else QColor(255,255,255,22)
        track_pen = QPen(track_c, pen_w, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        p.setPen(track_pen)
        p.drawArc(rect, 225 * 16, -270 * 16)

        # Filled arc — reuse cached colour objects
        ac   = self._arc_color()
        span = int(-270 * 16 * (self._disp / self.max_val))
        glow = QColor(ac); glow.setAlpha(40)
        glow_pen = QPen(glow,  pen_w * 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        arc_pen  = QPen(ac,    pen_w,       Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        p.setPen(glow_pen); p.drawArc(rect, 225 * 16, span)
        p.setPen(arc_pen);  p.drawArc(rect, 225 * 16, span)

        num_size = max(16, int(r * 0.56))
        p.setPen(PEN_TEXT())
        p.setFont(F(FONT_NUM, num_size, True))
        p.drawText(QRectF(cx - r, cy - r * 0.52, r * 2, r * 0.88),
                   Qt.AlignmentFlag.AlignCenter, f"{self._disp:.0f}")

        unit_size = max(6, int(r * 0.19))
        p.setPen(PEN_DIM())
        p.setFont(F(FONT_LBL, unit_size))
        p.drawText(QRectF(cx - r, cy + r * 0.30, r * 2, r * 0.36),
                   Qt.AlignmentFlag.AlignCenter, self.unit)
        p.end()


# ══════════════════════════════════════════════════════════════════════════════
#  FPS DISPLAY — scales with widget size
# ══════════════════════════════════════════════════════════════════════════════
class FpsDisplay(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._val  = 0.0
        self._disp = 0.0
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)
        self.setMinimumSize(100, 120)

    def set_value(self, v):
        # -1 is sentinel meaning "no game active" — don't clamp it
        self._val = -1.0 if v < 0 else max(0.0, float(v))

    def tick(self):
        self._disp = self._val
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        title_size = max(7, int(h * 0.072))
        p.setPen(PEN_DIM())
        p.setFont(F(FONT_LBL, title_size))
        p.drawText(QRect(0, int(h * 0.03), w, int(h * 0.13)),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                   "FPS")

        display  = "—" if self._disp < 0 else f"{self._disp:.0f}"
        scale    = 0.30 if len(display) <= 2 else (0.22 if len(display) == 3 else 0.18)
        num_size = max(16, int(h * scale))
        p.setPen(PEN_TEXT())
        p.setFont(F(FONT_NUM, num_size, True))
        p.drawText(QRect(0, int(h * 0.18), w, int(h * 0.72)),
                   Qt.AlignmentFlag.AlignCenter, display)
        p.end()


# ══════════════════════════════════════════════════════════════════════════════
#  NETWORK GRAPH — slim, scales with widget
# ══════════════════════════════════════════════════════════════════════════════
class NetGraph(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._tx       = collections.deque([0.0] * NET_POINTS, maxlen=NET_POINTS)
        self._rx       = collections.deque([0.0] * NET_POINTS, maxlen=NET_POINTS)
        self._peak     = 1.0
        self._tx_now   = 0.0
        self._rx_now   = 0.0
        self._ping_ms  = -1.0
        self._pkt_loss = 0.0
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(100, 120)

    def push(self, tx, rx, ping_ms=-1.0, pkt_loss=0.0):
        self._tx.append(tx);  self._rx.append(rx)
        self._tx_now   = tx;  self._rx_now  = rx
        self._ping_ms  = ping_ms
        self._pkt_loss = pkt_loss
        self._peak     = max(max(self._tx), max(self._rx), 1.0)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        title_sz  = max(7, int(h * 0.068))
        legend_sz = max(6, int(h * 0.058))
        stat_sz   = max(6, int(h * 0.055))
        pad       = max(6, int(w * 0.03))

        # Title
        p.setPen(PEN_DIM())
        p.setFont(F(FONT_LBL, title_sz))
        p.drawText(QRect(0, int(h * 0.01), w, int(h * 0.12)),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                   "NETWORK")

        # TX / RX legends
        up_y = int(h * 0.15)
        dn_y = up_y + legend_sz + 5
        self._draw_legend(p, pad, up_y, PEN_GREEN(), f"▲  {_fmt(self._tx_now)}", legend_sz)
        self._draw_legend(p, pad, dn_y, PEN_BLUE(),  f"▼  {_fmt(self._rx_now)}", legend_sz)

        # Ping + packet loss — right-aligned alongside TX/RX
        ping_str = f"{self._ping_ms:.0f} ms" if self._ping_ms >= 0 else "— ms"
        ping_c   = (PEN_CRIT() if self._ping_ms > 150
                    else PEN_WARN() if self._ping_ms > 60
                    else PEN_GREEN()) if self._ping_ms >= 0 else PEN_DIM()
        p.setPen(ping_c)
        p.setFont(F(FONT_NUM, stat_sz, True))
        p.drawText(QRect(0, up_y - stat_sz, w - pad, stat_sz * 2),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   ping_str)

        loss_pct = self._pkt_loss
        loss_str = f"{loss_pct:.0f}% loss"
        loss_c   = (PEN_CRIT() if loss_pct > 10
                    else PEN_WARN() if loss_pct > 2
                    else PEN_DIM())
        p.setPen(loss_c)
        p.setFont(F(FONT_NUM, stat_sz))
        p.drawText(QRect(0, dn_y - stat_sz, w - pad, stat_sz * 2),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   loss_str)

        # Graph area — pushed below the two legend rows
        top = dn_y + legend_sz + 6
        bh  = h - top - pad

        p.setBrush(BRUSH_TRACK())
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(pad, top, w - pad * 2, bh), 6, 6)

        _blue_fill = QColor(C_BLUE());  _blue_fill.setAlpha(28)
        _grn_fill  = QColor(C_GREEN()); _grn_fill.setAlpha(28)

        for buf, stroke_pen, fill_col in (
                (self._rx, PEN_BLUE(),  _blue_fill),
                (self._tx, PEN_GREEN(), _grn_fill)):
            pts  = list(buf); n = len(pts)
            path = QPainterPath()
            for i, v in enumerate(pts):
                x = pad + (w - pad * 2) * i / max(n - 1, 1)
                y = top + bh - bh * (v / self._peak) * 0.90
                if i == 0: path.moveTo(x, y)
                else:      path.lineTo(x, y)
            fill = QPainterPath(path)
            fill.lineTo(pad + (w - pad * 2), top + bh)
            fill.lineTo(pad, top + bh)
            fill.closeSubpath()
            p.setBrush(QBrush(fill_col)); p.setPen(Qt.PenStyle.NoPen)
            p.drawPath(fill)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(stroke_pen)
            p.drawPath(path)

        p.end()

    def _draw_legend(self, p, x, y, pen, text, fsize):
        p.setPen(pen)
        p.setFont(F(FONT_NUM, fsize, True))
        p.drawText(QRect(x, y - fsize, self.width(), fsize * 2),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   text)


# ══════════════════════════════════════════════════════════════════════════════
#  HWINFO BANNER — shown at startup if shared memory not detected
# ══════════════════════════════════════════════════════════════════════════════
class HWinfoBanner(QWidget):
    """
    Slim dismissable banner that appears at the top of the window when
    HWiNFO64 shared memory is not available at startup.
    Auto-dismisses once HWiNFO comes online, or user clicks ✕.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(36)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._dismissed = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 8, 0)
        layout.setSpacing(8)

        icon = QLabel("⚠")
        icon.setStyleSheet("color: #f0a500; font-size: 14px;")
        layout.addWidget(icon)

        msg = QLabel(
            "HWiNFO64 shared memory not detected — "
            "fan speeds, voltages and DIMM temps unavailable.  "
            "Enable: HWiNFO Settings → Shared Memory Support"
        )
        msg.setStyleSheet("color: #c8a000; font-size: 11px;")
        layout.addWidget(msg, stretch=1)

        close_btn = QPushButton("✕")
        close_btn.setFixedSize(20, 20)
        close_btn.setStyleSheet(
            "QPushButton { background: transparent; color: #888; border: none; font-size: 12px; }"
            "QPushButton:hover { color: #fff; }"
        )
        close_btn.clicked.connect(self._dismiss)
        layout.addWidget(close_btn)

        # Poll every 5s to auto-dismiss when HWiNFO comes online
        self._check_timer = QTimer(self)
        self._check_timer.timeout.connect(self._check_hwinfo)
        self._check_timer.start(5000)

        self.setStyleSheet("background: #2a1f00; border-bottom: 1px solid #5a3f00;")

    def _check_hwinfo(self):
        if hwinfo_available():
            self._dismiss()

    def _dismiss(self):
        if not self._dismissed:
            self._dismissed = True
            self._check_timer.stop()
            # Remove from layout entirely so it doesn't steal space
            if self.parent() and self.parent().layout():
                self.parent().layout().removeWidget(self)
            self.setParent(None)
            self.deleteLater()


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN WINDOW
# ══════════════════════════════════════════════════════════════════════════════
class CommandCenter(QMainWindow):
    def __init__(self):
        super().__init__()
        self._hw        = HardwareMonitor()
        self._drag_pos  = None
        self._settings  = load_settings()

        # Apply saved theme + accent before building UI
        global _theme_name
        _theme_name = self._settings.get("theme", "dark")
        _apply_accent(self._settings.get("accent", "blue"))

        self.setWindowTitle("COMMAND CENTER")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool   # hide from taskbar — tray icon is the entry point
        )
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        self._is_fullscreen = False
        self._pre_fs_geo    = None   # saved geometry before going fullscreen

        self._build_ui()
        self._setup_tray()
        self._apply_monitor()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._apply_data)
        self._timer.start(1000)

        # Fast FPS refresh — polls ETW/RTSS counter directly, bypasses 1 s reader loop
        self._fps_timer = QTimer(self)
        self._fps_timer.timeout.connect(self._update_fps_fast)
        self._fps_timer.start(250)

        # Hardware reads happen on a background thread — never block the UI
        self._pending_data = None
        self._data_lock    = threading.Lock()
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    # ── System tray icon ──────────────────────────────────────────────────────
    def _setup_tray(self):
        self._tray = QSystemTrayIcon(self)

        # Use the bundled icon if available, otherwise fall back to a generic one
        icon_path = None
        candidates = [
            os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "icon.ico"),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico"),
        ]
        if hasattr(sys, "_MEIPASS"):
            candidates.insert(0, os.path.join(sys._MEIPASS, "icon.ico"))
        for candidate in candidates:
            if os.path.exists(candidate):
                icon_path = candidate
                break

        if icon_path:
            self._tray.setIcon(QIcon(icon_path))
        else:
            self._tray.setIcon(self.style().standardIcon(
                self.style().StandardPixmap.SP_ComputerIcon))

        self._tray.setToolTip("Command Center")

        # Context menu
        tray_menu = QMenu()
        show_action = QAction("Show", self)
        show_action.triggered.connect(self._tray_show)
        tray_menu.addAction(show_action)

        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self._tray_quit)
        tray_menu.addAction(quit_action)

        self._tray.setContextMenu(tray_menu)
        self._tray.activated.connect(self._tray_activated)
        self._tray.show()

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._tray_show()

    def _tray_show(self):
        self.showNormal()
        self.activateWindow()
        self.raise_()

    def _tray_quit(self):
        self._tray.hide()
        QApplication.quit()

    def closeEvent(self, event):
        """Minimize to tray instead of quitting."""
        event.ignore()
        self.hide()
        log.info("Minimized to system tray")

    # ── Position on correct monitor ──────────────────────────────────────────
    def _apply_monitor(self):
        screens = QApplication.screens()
        idx     = int(self._settings.get("monitor", 0))
        idx     = max(0, min(idx, len(screens) - 1))
        screen  = screens[idx]
        geo     = screen.geometry()

        w = int(self._settings.get("width",  geo.width()))
        h = int(self._settings.get("height", geo.height()))

        self.resize(w, h)
        # centre on chosen monitor
        self.move(geo.x() + (geo.width()  - w) // 2,
                  geo.y() + (geo.height() - h) // 2)

    # ── Build layout ─────────────────────────────────────────────────────────
    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)

        root_vbox = QVBoxLayout(root)
        root_vbox.setContentsMargins(0, 0, 0, 0)
        root_vbox.setSpacing(0)

        self._title_bar = TitleBar(
            on_settings     = self._open_settings,
            on_fullscreen   = self.toggle_fullscreen,
            on_minimize     = self.close,
            on_close        = self._tray_quit,
            on_panel_toggle = self._toggle_sensor_panel,
            parent          = root,
        )
        root_vbox.addWidget(self._title_bar)

        self._hwinfo_banner = HWinfoBanner(root)
        if not hwinfo_available():
            root_vbox.addWidget(self._hwinfo_banner)
        else:
            self._hwinfo_banner.setParent(None)

        # Middle row: sensor panel (left) + card grid (right)
        mid = QWidget()
        root_vbox.addWidget(mid, stretch=1)
        mid_hbox = QHBoxLayout(mid)
        mid_hbox.setContentsMargins(0, 10, 8, 10)
        mid_hbox.setSpacing(10)

        # Create sensor widgets
        self._cpu_temp        = Gauge("CPU TEMP", "°C", 110,
                                      HW_PROFILE.cpu_temp_warn, HW_PROFILE.cpu_temp_crit)
        self._cpu_load        = Gauge("CPU LOAD", "%",  100, 75, 90)
        self._cpu_power_gauge = Gauge("CPU PWR",  "W",
                                      max(150, int(HW_PROFILE.cpu_tdp * 1.4)),
                                      HW_PROFILE.cpu_power_warn, HW_PROFILE.cpu_tdp)
        self._gpu_temp        = Gauge("GPU TEMP", "°C", 110,
                                      HW_PROFILE.gpu_temp_warn, HW_PROFILE.gpu_temp_crit)
        self._gpu_load        = Gauge("GPU LOAD", "%",  100, 80, 95)
        self._gpu_vram_gauge      = Gauge("VRAM",      "%",   100, 75, 90)
        self._gpu_power_gauge     = Gauge("GPU PWR",   "W",
                                          max(200, int(HW_PROFILE.gpu_power_warn * 1.4)),
                                          HW_PROFILE.gpu_power_warn,
                                          int(HW_PROFILE.gpu_power_warn * 1.2))
        self._cpu_freq_gauge      = Gauge("CPU CLK",   "GHz", 8,    6,    7)
        self._cpu_voltage_gauge   = Gauge("CPU VOLT",  "V",   2.0,  1.3,  1.5)
        self._cpu_elec_gauge      = Gauge("CPU CURR",  "A",   200,  100,  150)
        self._cpu_therm_gauge     = Gauge("CPU THRM",  "A",   100,  50,   75)
        self._gpu_clock_gauge     = Gauge("GPU CLK",   "MHz", 3500, 2800, 3000)
        self._gpu_voltage_gauge   = Gauge("GPU VOLT",  "mV",  2000, 1000, 1400)
        self._gpu_mem_clk_gauge   = Gauge("VRAM CLK",  "MHz", 4000, 3200, 3600)
        self._fps             = FpsDisplay()
        self._strip_cpu_info  = StatsStrip(StatsStrip.MODE_CPU_INFO)
        self._strip_cpu_cores = StatsStrip(StatsStrip.MODE_CPU_CORES)
        self._strip_gpu_info  = StatsStrip(StatsStrip.MODE_GPU_INFO)
        self._strip_gpu_mem   = StatsStrip(StatsStrip.MODE_GPU_MEM)
        self._ram_load        = Gauge("RAM LOAD", "%", 100, 75, 90)
        self._net             = NetGraph()
        self._ssd             = SsdPanel()

        # Card grid — dynamic, 3 columns max
        main_widget = QWidget()
        self._grid = QGridLayout(main_widget)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(10)
        for c in range(4):
            self._grid.setColumnStretch(c, 1)

        # Load layout — migrate out legacy footer panels (network/disk moved to footer)
        _FOOTER_PANELS  = {"network", "disk_io", "net_ssd"}
        _VALID_PANELS   = set(_DEFAULT_LAYOUT_STR.split(","))
        layout_str = self._settings.get("layout", _DEFAULT_LAYOUT_STR)
        defaults   = _DEFAULT_LAYOUT_STR.split(",")
        loaded     = [p for p in layout_str.split(",")
                      if p not in _FOOTER_PANELS and p in _VALID_PANELS]
        # Fill any missing slots with defaults not already present
        for d in defaults:
            if d not in loaded:
                loaded.append(d)
        self._current_layout = loaded[:len(defaults)]

        # Load hidden panels
        hidden_str = self._settings.get("hidden_panels", "")
        self._hidden_panel_ids = set(x.strip() for x in hidden_str.split(",") if x.strip())

        # Sensor list panel (AMD-style) — left side
        self._sensor_panel = SensorListPanel(on_visibility_change=self._on_sensor_vis_change)
        mid_hbox.addWidget(self._sensor_panel)

        mid_hbox.addWidget(main_widget, stretch=1)

        # Right panel — fans + extra sensors
        self._right_panel = RightPanel()
        mid_hbox.addWidget(self._right_panel)

        # Sync initial visibility state to side panel
        for pid in self._hidden_panel_ids:
            self._sensor_panel.set_panel_visible(pid, False)

        # Build initial grid
        self._slot_cards = [None] * len(self._current_layout)
        self._rebuild_all_slots()

        # Footer
        self._footer = FooterBar()
        root_vbox.addWidget(self._footer)

    # ── Panel factory ────────────────────────────────────────────────────────
    def _create_panel_card(self, panel_id, slot_idx):
        """Create a Card populated with the right widget(s) for panel_id."""
        card = Card(slot_id=slot_idx, panel_id=panel_id, on_swap=self._swap_slot)

        if panel_id == "cpu_temp":
            card.add(self._cpu_temp, 1)
        elif panel_id == "cpu_load":
            card.add(self._cpu_load, 1)
        elif panel_id == "gpu_temp":
            card.add(self._gpu_temp, 1)
        elif panel_id == "gpu_load":
            card.add(self._gpu_load, 1)
        elif panel_id == "fps":
            card.add(self._fps, 1)
        elif panel_id == "ram":
            card.add(self._ram_load, 1)
        elif panel_id == "gpu_vram":
            card.add(self._gpu_vram_gauge, 1)
        elif panel_id == "gpu_power":
            card.add(self._gpu_power_gauge, 1)
        elif panel_id == "cpu_power":
            card.add(self._cpu_power_gauge, 1)
        elif panel_id == "cpu_freq":
            card.add(self._cpu_freq_gauge, 1)
        elif panel_id == "cpu_voltage":
            card.add(self._cpu_voltage_gauge, 1)
        elif panel_id == "cpu_elec_current":
            card.add(self._cpu_elec_gauge, 1)
        elif panel_id == "cpu_therm_current":
            card.add(self._cpu_therm_gauge, 1)
        elif panel_id == "gpu_clock":
            card.add(self._gpu_clock_gauge, 1)
        elif panel_id == "gpu_voltage":
            card.add(self._gpu_voltage_gauge, 1)
        elif panel_id == "gpu_mem_clk":
            card.add(self._gpu_mem_clk_gauge, 1)
        elif panel_id == "network":
            card.add(self._net, 1)
        elif panel_id == "disk_io":
            card.add(self._ssd, 1)
        elif panel_id == "net_ssd":
            ns_w = QWidget()
            ns_l = QHBoxLayout(ns_w)
            ns_l.setContentsMargins(0, 0, 0, 0)
            ns_l.setSpacing(10)
            ns_l.addWidget(self._net, stretch=1)
            div = QWidget(); div.setFixedWidth(1)
            div.setStyleSheet(
                "background: rgba(0,0,0,0.12);" if _theme_name == "light"
                else "background: rgba(255,255,255,0.08);")
            ns_l.addWidget(div)
            ns_l.addWidget(self._ssd, stretch=1)
            card.add(ns_w, 1)

        return card

    # ── Panel swap ───────────────────────────────────────────────────────────
    def _swap_slot(self, slot_idx, new_panel_id):
        """Swap panel in slot_idx to new_panel_id. If new_panel_id is already
        placed elsewhere, the two slots swap their panels."""
        layout = self._current_layout
        old_panel_id = layout[slot_idx]
        if old_panel_id == new_panel_id:
            return

        # Update layout
        if new_panel_id in layout:
            other_idx = layout.index(new_panel_id)
            layout[other_idx] = old_panel_id
        layout[slot_idx] = new_panel_id

        # Rebuild entire grid — detach all widgets, destroy all cards, recreate
        self._rebuild_all_slots()

        # Persist
        self._settings["layout"] = ",".join(layout)
        save_settings(self._settings)

    def _rebuild_all_slots(self):
        """Rebuild card grid — visible panels reflow left→right, 3 per row."""
        for w in (self._cpu_temp, self._cpu_load, self._cpu_power_gauge,
                  self._cpu_freq_gauge, self._cpu_voltage_gauge,
                  self._cpu_elec_gauge, self._cpu_therm_gauge,
                  self._gpu_temp, self._gpu_load, self._gpu_power_gauge,
                  self._gpu_vram_gauge, self._gpu_clock_gauge, self._gpu_voltage_gauge,
                  self._gpu_mem_clk_gauge, self._fps, self._ram_load,
                  self._net, self._ssd):
            w.setParent(None)
            w.hide()

        for card in self._slot_cards:
            if card is not None:
                self._grid.removeWidget(card)
                card.setParent(None)
                card.deleteLater()

        # Clear row stretches
        for r in range(5):
            self._grid.setRowStretch(r, 0)

        visible = [(i, pid) for i, pid in enumerate(self._current_layout)
                   if pid not in self._hidden_panel_ids]

        self._slot_cards = [None] * len(self._current_layout)
        for pos, (slot_idx, panel_id) in enumerate(visible):
            card = self._create_panel_card(panel_id, slot_idx)
            row  = pos // 4
            col  = pos % 4
            self._grid.addWidget(card, row, col)
            self._slot_cards[slot_idx] = card
            card.show()

        n_rows = max(1, (len(visible) + 3) // 4)
        for r in range(n_rows):
            self._grid.setRowStretch(r, 1)

    def _on_sensor_vis_change(self, panel_id, visible):
        """Called by SensorListPanel when user clicks an eye toggle."""
        if visible:
            self._hidden_panel_ids.discard(panel_id)
        else:
            self._hidden_panel_ids.add(panel_id)
        self._settings["hidden_panels"] = ",".join(self._hidden_panel_ids)
        save_settings(self._settings)
        self._rebuild_all_slots()

    def _toggle_sensor_panel(self):
        """Called by TitleBar panel button."""
        self._sensor_panel.toggle_expand()

    # ── Poll hardware ─────────────────────────────────────────────────────────
    def _reader_loop(self):
        """Background thread — reads hardware every second, never touches Qt."""
        import time
        while True:
            try:
                d = self._hw.read()
                with self._data_lock:
                    self._pending_data = d
            except Exception as ex:
                print(f"[reader] {ex}")
            time.sleep(1.0)

    def _update_fps_fast(self):
        """250 ms timer — pushes real RTSS fps when available; GPU estimate via _apply_data."""
        fps = get_foreground_fps()
        if fps > 0:
            self._fps.set_value(fps)
            self._fps.update()

    def _apply_data(self):
        """Main thread (QTimer) — picks up latest data and updates widgets."""
        with self._data_lock:
            d = self._pending_data
        if d is None:
            return
        self._cpu_temp.set_value(d.cpu_temp)
        self._cpu_load.set_value(d.cpu_load)
        self._gpu_temp.set_value(d.gpu_temp)
        self._gpu_load.set_value(d.gpu_load)
        self._strip_cpu_info.set_cpu(d.cpu_power, d.cpu_freq, d.cpu_cores, d.cpu_voltage, d.cpu_freqs)
        self._strip_cpu_cores.set_cpu(d.cpu_power, d.cpu_freq, d.cpu_cores, d.cpu_voltage, d.cpu_freqs)
        self._strip_gpu_info.set_gpu(d.gpu_power, d.gpu_core_clk, d.gpu_mem_clk,
                                     d.gpu_mem_used, d.gpu_mem_total, d.gpu_voltage)
        self._strip_gpu_mem.set_gpu(d.gpu_power, d.gpu_core_clk, d.gpu_mem_clk,
                                    d.gpu_mem_used, d.gpu_mem_total, d.gpu_voltage)
        self._ram_load.set_value((d.ram_used / max(d.ram_total, 1)) * 100)
        self._right_panel.ram_info.set_values(d.ram_used, d.ram_total, d.dimm_temps)
        vram_pct = (d.gpu_mem_used / max(d.gpu_mem_total, 1)) * 100 if d.gpu_mem_total > 0 else 0.0
        self._gpu_vram_gauge.set_value(vram_pct)
        self._gpu_power_gauge.set_value(d.gpu_power)
        self._cpu_power_gauge.set_value(d.cpu_power)
        self._cpu_freq_gauge.set_value(d.cpu_freq / 1000.0)   # MHz → GHz
        self._cpu_voltage_gauge.set_value(d.cpu_voltage)
        self._cpu_elec_gauge.set_value(d.cpu_electrical_current)
        self._cpu_therm_gauge.set_value(d.cpu_thermal_current)
        self._gpu_clock_gauge.set_value(d.gpu_core_clk)
        self._gpu_voltage_gauge.set_value(d.gpu_voltage * 1000.0)  # V → mV
        self._gpu_mem_clk_gauge.set_value(d.gpu_mem_clk)
        self._fps.set_value(d.fps)
        self._net.push(d.net_tx, d.net_rx, d.net_ping_ms, d.net_packet_loss)
        self._ssd.push(d.disk_io)
        self._footer.push_network(d.net_tx, d.net_rx, d.net_ping_ms, d.net_packet_loss)
        self._footer.push_disk(d.disk_io)
        self._right_panel.fan_panel.set_fans(d.fans)
        self._right_panel.extra_panel.set_sensors(d.extra_sensors)
        for w in (self._cpu_temp, self._cpu_load, self._cpu_power_gauge,
                  self._cpu_freq_gauge, self._cpu_voltage_gauge,
                  self._cpu_elec_gauge, self._cpu_therm_gauge,
                  self._gpu_temp, self._gpu_load, self._gpu_vram_gauge,
                  self._gpu_power_gauge, self._gpu_clock_gauge, self._gpu_voltage_gauge,
                  self._gpu_mem_clk_gauge, self._ram_load, self._fps):
            w.tick()

    # ── Fullscreen toggle ─────────────────────────────────────────────────────
    def toggle_fullscreen(self):
        if not self._is_fullscreen:
            # Save current geometry before going fullscreen
            self._pre_fs_geo = self.geometry()
            # Get the screen this window is currently on
            screen = QApplication.screenAt(self.geometry().center())
            if screen is None:
                screen = QApplication.primaryScreen()
            geo = screen.geometry()
            self.setGeometry(geo)
            self._is_fullscreen = True
        else:
            # Restore saved geometry
            if self._pre_fs_geo:
                self.setGeometry(self._pre_fs_geo)
            self._is_fullscreen = False

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_F11:
            self.toggle_fullscreen()
        elif e.key() == Qt.Key.Key_Escape and self._is_fullscreen:
            self.toggle_fullscreen()
        else:
            super().keyPressEvent(e)

    # ── Right-click context menu ──────────────────────────────────────────────
    # ── Settings ──────────────────────────────────────────────────────────────
    def _open_settings(self):
        dlg = SettingsDialog(self._settings, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            vals = dlg.get_values()
            self._settings["monitor"] = vals["monitor"]
            self._settings["startup"] = vals["startup"]
            self._settings["theme"]   = vals["theme"]
            self._settings["accent"]  = vals["accent"]
            save_settings(self._settings)
            set_startup(vals["startup"])
            self._apply_theme(vals["theme"], vals["accent"])
            self._apply_monitor()

    def _apply_theme(self, theme_name: str, accent_name: str = None):
        global _theme_name
        _theme_name = theme_name
        if accent_name:
            _apply_accent(accent_name)
        _PC.rebuild()
        _FC.clear()
        self.update()
        for w in self.findChildren(QWidget):
            w.update()

    # ── Drag to move (body, not title bar) ───────────────────────────────────
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = e.globalPosition().toPoint()

    def mouseMoveEvent(self, e):
        if self._drag_pos and e.buttons() == Qt.MouseButton.LeftButton:
            self.move(self.pos() + e.globalPosition().toPoint() - self._drag_pos)
            self._drag_pos = e.globalPosition().toPoint()

    def mouseReleaseEvent(self, _):
        self._drag_pos = None

    # ── Save size on resize ───────────────────────────────────────────────────
    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._settings["width"]  = self.width()
        self._settings["height"] = self.height()
        save_settings(self._settings)

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), C_BG())
        p.end()


# ── Helpers ───────────────────────────────────────────────────────────────────
def _fmt(bps):
    if bps < 1024:    return f"{bps:.0f} B/s"
    if bps < 1024**2: return f"{bps/1024:.1f} KB/s"
    if bps < 1024**3: return f"{bps/1024**2:.1f} MB/s"
    return f"{bps/1024**3:.2f} GB/s"


def _shorten_cpu(name: str) -> str:
    """'AMD Ryzen 7 9800X3D 8-Core Processor' → 'Ryzen 7 9800X3D'"""
    n = name.strip()
    # Strip trailing junk
    n = re.sub(r'\s+(processor|cpu|with\s+.+)$', '', n, flags=re.I)
    # Strip core-count suffix
    n = re.sub(r'\s+\d+-core.*$', '', n, flags=re.I)
    # Shorten brand prefix
    n = re.sub(r'^(amd|intel|apple)\s+', '', n, flags=re.I)
    return n.strip()[:32]


def _shorten_gpu(name: str) -> str:
    """'NVIDIA GeForce RTX 5070 Ti' → 'RTX 5070 Ti'"""
    n = name.strip()
    n = re.sub(r'^(nvidia|amd|intel)\s+', '', n, flags=re.I)
    n = re.sub(r'^geforce\s+', '', n, flags=re.I)
    n = re.sub(r'^radeon\s+', '', n, flags=re.I)
    return n.strip()[:28]


# ── Entry ─────────────────────────────────────────────────────────────────────
def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window,     C_BG())
    pal.setColor(QPalette.ColorRole.WindowText, C_TEXT())
    app.setPalette(pal)
    win = CommandCenter()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
