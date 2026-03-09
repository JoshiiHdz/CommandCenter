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
__version__   = "1.0.0"

import sys
import os
import re
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

import collections
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QGridLayout,
    QVBoxLayout, QHBoxLayout, QLabel, QDialog,
    QComboBox, QCheckBox, QPushButton, QFormLayout,
    QDialogButtonBox, QMenu, QSizePolicy
)
from PyQt6.QtCore import Qt, QTimer, QRectF, QRect, QPointF, QSize
from PyQt6.QtGui import (
    QPainter, QColor, QPen, QBrush, QFont,
    QLinearGradient, QPainterPath, QPalette, QAction
)

from hardware import HardwareMonitor, HW_PROFILE, hwinfo_available

# ── App data directory — writable on all machines (avoids Program Files issues) ─
_APP_DATA = os.path.join(
    os.environ.get("APPDATA", os.path.dirname(os.path.abspath(sys.argv[0]))),
    "CommandCenter"
)
os.makedirs(_APP_DATA, exist_ok=True)

# ── Settings file ─────────────────────────────────────────────────────────────
SETTINGS_PATH = os.path.join(_APP_DATA, "settings.ini")

def load_settings():
    cfg = configparser.ConfigParser()
    cfg.read(SETTINGS_PATH)
    return {
        "monitor":  cfg.get("window", "monitor",  fallback="0"),
        "startup":  cfg.getboolean("window", "startup", fallback=False),
        "width":    cfg.getint("window", "width",  fallback=1920),
        "height":   cfg.getint("window", "height", fallback=1080),
        "theme":    cfg.get("window", "theme",    fallback="dark"),
    }

def save_settings(s):
    cfg = configparser.ConfigParser()
    cfg["window"] = {
        "monitor": str(s["monitor"]),
        "startup": str(s["startup"]),
        "width":   str(s["width"]),
        "height":  str(s["height"]),
        "theme":   str(s.get("theme", "dark")),
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

        pad     = 16
        title_h = int(h * 0.07)

        # Title
        p.setPen(PEN_DIM())
        p.setFont(F(FONT_LBL, max(8, int(h * 0.036))))
        p.drawText(QRect(pad, 8, w - pad * 2, title_h),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                   "FAN SPEEDS")

        # Hint — smaller, italics feel, clipped to panel width
        hint_sz = max(5, min(int(h * 0.020), _fsz(w - pad * 2, 24)))
        p.setFont(F(FONT_LBL, hint_sz))
        p.drawText(QRect(pad, title_h - 2, w - pad * 2, hint_sz + 6),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                   "dbl-click row to rename")

        # Divider
        p.setPen(PEN_BORDER())
        div_y = title_h + hint_sz + 10
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
            bar_w   = w - pad * 2
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
#  TITLE BAR — draggable bar with settings / fullscreen / close icons
# ══════════════════════════════════════════════════════════════════════════════
class TitleBar(QWidget):
    sig_settings   = None   # set by CommandCenter after init
    sig_fullscreen = None
    sig_close      = None

    def __init__(self, on_settings, on_fullscreen, on_close, parent=None):
        super().__init__(parent)
        self._on_settings   = on_settings
        self._on_fullscreen = on_fullscreen
        self._on_close      = on_close
        self._drag_pos      = None
        self._hovered       = None   # "settings" | "fs" | "close"
        self.setFixedHeight(34)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)

    # ── Button rects (computed each paint) ───────────────────────────────────
    def _btn_rects(self):
        bw, bh = 32, 24
        y  = (self.height() - bh) // 2
        x3 = self.width() - bw - 6
        x2 = x3 - bw - 4
        x1 = x2 - bw - 4
        return {
            "settings": QRect(x1, y, bw, bh),
            "fs":       QRect(x2, y, bw, bh),
            "close":    QRect(x3, y, bw, bh),
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

        # Title with detected hardware
        cpu_short = _shorten_cpu(HW_PROFILE.cpu_name)
        gpu_short = _shorten_gpu(HW_PROFILE.gpu_name)
        title_str = f"COMMAND CENTER  ·  {cpu_short}  ·  {gpu_short}"
        p.setPen(QPen(_t("dim")))
        p.setFont(F(FONT_LBL, 9, True))
        p.drawText(QRect(14, 0, int(w * 0.75), h),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   title_str)

        # Author watermark — right side of title bar, left of the buttons
        rects_tmp = self._btn_rects()
        btn_left  = min(r.left() for r in rects_tmp.values()) - 8
        author_c  = QColor(_t("dim")); author_c.setAlpha(120)
        p.setPen(QPen(author_c))
        p.setFont(F(FONT_LBL, 8))
        p.drawText(QRect(int(w * 0.50), 0, btn_left - int(w * 0.50), h),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   "© 2025 Josh Stanley")

        # Buttons
        rects = self._btn_rects()
        icons = {"settings": "⚙", "fs": "⛶", "close": "✕"}
        for key, rect in rects.items():
            # Hover bg
            if self._hovered == key:
                hover_c = QColor(200, 40, 40, 180) if key == "close" else _t("btn_hover")
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(hover_c))
                p.drawRoundedRect(QRectF(rect), 5, 5)

            icon_c = QColor(220, 60, 60) if (key == "close" and self._hovered == "close") else _t("dim")
            p.setPen(QPen(icon_c))
            p.setFont(F(FONT_LBL, 11))
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
            if rects["settings"].contains(pos):  self._on_settings()
            elif rects["fs"].contains(pos):      self._on_fullscreen()
            elif rects["close"].contains(pos):   self._on_close()
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

        # Startup checkbox
        self._startup_cb = QCheckBox("Launch Command Center on Windows startup")
        self._startup_cb.setChecked(get_startup())
        layout.addRow("", self._startup_cb)

        # About row
        about_lbl = QLabel(
            "<span style='font-size:10px;'>"
            "<b>Command Center</b> v1.0 &nbsp;·&nbsp; "
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
        }


# ══════════════════════════════════════════════════════════════════════════════
#  CARD — dark rounded tile, fully scalable
# ══════════════════════════════════════════════════════════════════════════════
class Card(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(12, 12, 12, 12)
        self._layout.setSpacing(4)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)

    def add(self, w, stretch=0):
        self._layout.addWidget(w, stretch)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QBrush(C_CARD()))
        p.setPen(PEN_BORDER())
        p.drawRoundedRect(QRectF(1, 1, self.width()-2, self.height()-2), 14, 14)
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
        w, h  = self.width(), self.height()
        pad   = 10
        row_h = 30
        sz    = max(6, min(int(row_h * 0.50), _fsz(w - pad * 2, 20)))

        p.setPen(PEN_BORDER())
        p.drawLine(pad, 0, w - pad, 0)

        used_gb  = self._used_mb  / 1024
        total_gb = self._total_mb / 1024
        pct      = used_gb / max(total_gb, 1)
        ram_c    = C_CRIT() if pct > 0.90 else (C_WARN() if pct > 0.75 else C_TEXT())

        p.setPen(PEN_DIM())
        p.setFont(F(FONT_LBL, sz))
        p.drawText(QRect(pad, 0, int(w * 0.40), row_h),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   "💾 System")
        p.setPen(QPen(ram_c))
        p.setFont(F(FONT_NUM, sz, True))
        p.drawText(QRect(0, 0, w - pad, row_h),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   f"{used_gb:.1f} / {total_gb:.0f} GB")

        for i, (lbl, temp) in enumerate(self._dimm_temps):
            y   = row_h + i * 24
            rh  = 24
            sz2 = max(6, int(rh * 0.46))
            tc  = C_CRIT() if temp > 85 else (C_WARN() if temp > 70 else C_TEXT())

            p.setPen(PEN_DIM())
            p.setFont(F(FONT_LBL, sz2))
            p.drawText(QRect(pad, y, int(w * 0.55), rh),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       f"🌡 {lbl}")
            p.setPen(QPen(tc))
            p.setFont(F(FONT_NUM, sz2, True))
            p.drawText(QRect(0, y, w - pad, rh),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       f"{temp:.0f}°C")

        if not self._dimm_temps:
            sz2 = max(5, int(20 * 0.40))
            p.setPen(PEN_DIM())
            p.setFont(F(FONT_LBL, sz2))
            p.drawText(QRect(pad, row_h, w - pad * 2, 20),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       "⚠ Start HWiNFO for DIMM temps")
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
        if self._val < 0:
            self._disp = -1.0   # snap immediately, no lerp
        else:
            self._disp += (self._val - self._disp) * 0.18
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

        num_size = max(20, int(h * 0.30))
        p.setPen(PEN_TEXT())
        p.setFont(F(FONT_NUM, num_size, True))
        display = "—" if self._disp < 0 else f"{self._disp:.0f}"
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

        # Apply saved theme before building UI
        global _theme_name
        _theme_name = self._settings.get("theme", "dark")

        self.setWindowTitle("COMMAND CENTER")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        self._is_fullscreen = False
        self._pre_fs_geo    = None   # saved geometry before going fullscreen

        self._build_ui()
        self._apply_monitor()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._apply_data)
        self._timer.start(1000)

        # Hardware reads happen on a background thread — never block the UI
        self._pending_data = None
        self._data_lock    = threading.Lock()
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

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

        # Root: vertical — title bar on top, content below
        root_vbox = QVBoxLayout(root)
        root_vbox.setContentsMargins(0, 0, 0, 0)
        root_vbox.setSpacing(0)

        self._title_bar = TitleBar(
            on_settings   = self._open_settings,
            on_fullscreen = self.toggle_fullscreen,
            on_close      = self.close,
            parent        = root,
        )
        root_vbox.addWidget(self._title_bar)

        self._hwinfo_banner = HWinfoBanner(root)
        if not hwinfo_available():
            root_vbox.addWidget(self._hwinfo_banner)
        else:
            self._hwinfo_banner.setParent(None)   # not in layout at all

        # Content area
        content = QWidget()
        root_vbox.addWidget(content, stretch=1)

        outer = QHBoxLayout(content)
        outer.setContentsMargins(16, 12, 16, 16)
        outer.setSpacing(12)

        # ── Main grid ────────────────────────────────────────────────────────
        main_widget = QWidget()
        grid = QGridLayout(main_widget)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(12)

        # 5 columns: CPU TEMP | CPU LOAD | GPU TEMP | GPU LOAD | FPS
        for c in range(5):
            grid.setColumnStretch(c, 1)
        # 3 rows: gauges | RAM + Net+SSD (span 4 cols)
        grid.setRowStretch(0, 1)
        grid.setRowStretch(1, 1)

        def wrap(w):
            c = Card(); c.add(w, 1); return c

        # Row 0 — all gauges with their strips
        self._cpu_temp        = Gauge("CPU TEMP", "°C", 110,
                                      HW_PROFILE.cpu_temp_warn, HW_PROFILE.cpu_temp_crit)
        self._cpu_load        = Gauge("CPU LOAD", "%",  100, 75, 90)
        self._gpu_temp        = Gauge("GPU TEMP", "°C", 110,
                                      HW_PROFILE.gpu_temp_warn, HW_PROFILE.gpu_temp_crit)
        self._gpu_load        = Gauge("GPU LOAD", "%",  100, 80, 95)
        self._fps             = FpsDisplay()
        self._strip_cpu_info  = StatsStrip(StatsStrip.MODE_CPU_INFO)   # under CPU TEMP
        self._strip_cpu_cores = StatsStrip(StatsStrip.MODE_CPU_CORES)  # under CPU LOAD
        self._strip_gpu_info  = StatsStrip(StatsStrip.MODE_GPU_INFO)   # under GPU TEMP
        self._strip_gpu_mem   = StatsStrip(StatsStrip.MODE_GPU_MEM)    # under GPU LOAD

        def wrap_strip(gauge, strip):
            c = Card()
            c.add(gauge, 1)
            c.add(strip, 0)
            return c

        grid.addWidget(wrap_strip(self._cpu_temp, self._strip_cpu_info),   0, 0)
        grid.addWidget(wrap_strip(self._cpu_load, self._strip_cpu_cores),  0, 1)
        grid.addWidget(wrap_strip(self._gpu_temp, self._strip_gpu_info),   0, 2)
        grid.addWidget(wrap_strip(self._gpu_load, self._strip_gpu_mem),    0, 3)
        grid.addWidget(wrap(self._fps),                                    0, 4)

        # Row 1 — RAM gauge | Network+SSD (span 3 cols) | Disk panel (span 1)
        self._ram_load    = Gauge("RAM LOAD", "%", 100, 75, 90)
        self._ram_info    = RamInfoStrip()
        ram_card = Card()
        ram_card.add(self._ram_load, 1)
        ram_card.add(self._ram_info, 0)
        grid.addWidget(ram_card, 1, 0)
        self._net      = NetGraph()
        self._ssd      = SsdPanel()

        # Network and SSD sit side by side inside one card spanning 3 cols
        net_ssd_card = Card()
        ns_layout = QHBoxLayout()
        ns_layout.setContentsMargins(0, 0, 0, 0)
        ns_layout.setSpacing(10)
        ns_layout.addWidget(self._net, stretch=1)

        # Divider line
        div = QWidget(); div.setFixedWidth(1)
        div.setStyleSheet("background: rgba(0,0,0,0.12);" if _theme_name == "light" else "background: rgba(255,255,255,0.08);")
        ns_layout.addWidget(div)

        ns_layout.addWidget(self._ssd, stretch=1)
        inner = QWidget()
        inner.setLayout(ns_layout)
        net_ssd_card.add(inner, 1)

        # ram_card already added above
        grid.addWidget(net_ssd_card,         1, 1, 1, 4)   # spans cols 1-4

        # ── Right column: Fan panel + Extra Sensors (auto-shown when detected) ──
        self._fan_panel   = FanPanel()
        self._extra_panel = ExtraSensorsPanel()

        right_col = QWidget()
        right_col.setSizePolicy(QSizePolicy.Policy.Preferred,
                                QSizePolicy.Policy.Expanding)
        right_col.setMinimumWidth(220)
        right_col.setMaximumWidth(400)
        right_vbox = QVBoxLayout(right_col)
        right_vbox.setContentsMargins(0, 0, 0, 0)
        right_vbox.setSpacing(12)
        right_vbox.addWidget(self._fan_panel,   stretch=1)
        right_vbox.addWidget(self._extra_panel, stretch=0)

        outer.addWidget(main_widget, stretch=5)
        outer.addWidget(right_col,   stretch=1)

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
        self._ram_info.set_values(d.ram_used, d.ram_total, d.dimm_temps)
        self._fps.set_value(d.fps)
        self._net.push(d.net_tx, d.net_rx, d.net_ping_ms, d.net_packet_loss)
        self._ssd.push(d.disk_io)
        self._fan_panel.set_fans(d.fans)
        self._extra_panel.set_sensors(d.extra_sensors)
        for w in (self._cpu_temp, self._cpu_load, self._gpu_temp,
                  self._gpu_load, self._ram_load, self._fps):
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
            save_settings(self._settings)
            set_startup(vals["startup"])
            self._apply_theme(vals["theme"])
            self._apply_monitor()

    def _apply_theme(self, theme_name: str):
        global _theme_name
        _theme_name = theme_name
        _PC.rebuild()   # rebuild cached QPen/QBrush for new theme
        _FC.clear()     # clear font cache (sizes may differ between themes)
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


def _setup_logging():
    """Redirect stdout/stderr to a rolling log file next to the EXE."""
    import logging
    import traceback

    # Resolve log path — always use %APPDATA%\CommandCenter so it's writable
    # even when the EXE lives in a protected directory (e.g. Program Files).
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
        format     = "%(asctime)s [%(levelname)s] %(message)s",
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
        # Also try to show a message box if Qt is up
        try:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.critical(None, "Command Center — Crash",
                                 f"An unexpected error occurred:\n\n{exc_value}\n\n"
                                 f"See {log_path} for full details.")
        except Exception:
            pass

    sys.excepthook = _excepthook

    logging.info("=" * 60)
    logging.info("Command Center starting")
    logging.info("=" * 60)
    return log_path


if __name__ == "__main__":
    _setup_logging()
    main()
