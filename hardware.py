"""
hardware.py — Hardware sensor abstraction
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Copyright (c) 2025 Josh Stanley.  All rights reserved.
Unauthorised copying or redistribution is strictly prohibited.

CPU TEMP — tries in order:
  1. LibreHardwareMonitorLib.dll  (most accurate, needs DLL in same folder)
  2. WMI MSAcpi_ThermalZoneTemperature  (built-in Windows, less accurate)
  3. psutil sensors_temperatures  (works on Linux, rarely on Windows)
  4. Returns 0 if all fail

GPU — pynvml direct NVML calls (no extra software needed)
RAM / Disk / Network — psutil
"""

import os
import sys
import time
import logging
import dataclasses
import collections
import ctypes
import ctypes.wintypes
import threading

import psutil

log = logging.getLogger(__name__)

# ── HWiNFO64 Shared Memory ────────────────────────────────────────────────────
HWINFO_SM_NAME = "Global\\HWiNFO_SENS_SM2"
HWINFO_SM_SIZE = 4 * 1024 * 1024

# CPU temp label fragments (case-insensitive)
_CPU_TEMP_LABELS = (
    "cpu (tctl/tdie)", "cpu die (average)", "cpu ccd1 (tdie)",
    "tdie", "tctl", "cpu die", "cpu package",
    "cpu ccd", "cpu average", "core (tdie)", "cpu temperature", "cpu temp",
)

# Water-cooling / controller label keywords (case-insensitive allowlist).
# Only sensors whose label contains at least one of these will appear in
# ExtraSensorsPanel.  Covers: Aquaero, Octo, generic liquid-cooling labels,
# mobo coolant/T_Sensor headers, pump and flow sensors.
_WC_LABELS = (
    "aquaero",          # Aquaero fan/temp controllers
    "octo",             # Aqua Computer Octo
    "water",            # Water In / Water Out / Water Temperature
    "coolant",          # Coolant In/Out
    "liquid",           # Liquid temperature
    "flow",             # Flow rate sensors
    "pump",             # Pump head / pump temperature
    "radiator",         # Radiator inlet/outlet
    "reservoir",        # Reservoir temperature
    "t_sensor",         # ASUS mobo external probe header (typically used for WC)
    "w_pump",           # ASUS W_PUMP header temp
    "fill level",       # Reservoir fill-level sensor
)

def hwinfo_available() -> bool:
    """Quick check — returns True if HWiNFO shared memory is open."""
    try:
        import mmap
        sm = mmap.mmap(-1, 4096, tagname=HWINFO_SM_NAME, access=mmap.ACCESS_READ)
        sm.close()
        return True
    except Exception:
        return False


def _hwinfo_read_all(d: "SensorData"):
    """
    Parse HWiNFO64 shared memory using the proper header-driven layout.
    Populates: d.cpu_temp, d.cpu_power, d.cpu_voltage, d.cpu_freq,
               d.gpu_temp, d.gpu_load, d.gpu_core_clk, d.gpu_mem_clk,
               d.gpu_power, d.gpu_mem_used, d.gpu_voltage,
               d.fans, d.dimm_temps, d.extra_sensors
    HWiNFO values override LHM when available (HWiNFO is more accurate).
    """
    import mmap
    import struct as _struct

    try:
        sm = mmap.mmap(-1, HWINFO_SM_SIZE, tagname=HWINFO_SM_NAME,
                       access=mmap.ACCESS_READ)
        sm.seek(0)
        data = sm.read(HWINFO_SM_SIZE)
        sm.close()
    except FileNotFoundError:
        return
    except Exception as ex:
        log.warning("HWiNFO open failed: %s", ex)
        return

    # ── Parse header to get correct offsets ───────────────────────────────────
    # Header layout (packed):
    #   uint32 dwSignature
    #   uint32 dwVersion
    #   uint32 dwRevision
    #   int64  poll_time
    #   uint32 dwOffsetOfSensorSection
    #   uint32 dwSizeOfSensorElement
    #   uint32 dwNumSensorElements
    #   uint32 dwOffsetOfReadingSection
    #   uint32 dwSizeOfReadingElement
    #   uint32 dwNumReadingElements
    HDR_FMT = "<IIIqIIIIII"
    HDR_SIZE = _struct.calcsize(HDR_FMT)
    if len(data) < HDR_SIZE:
        return

    (sig, _ver, _rev, _poll,
     _off_sensor, _sz_sensor, _n_sensor,
     off_reading, sz_reading, n_reading) = _struct.unpack_from(HDR_FMT, data, 0)

    # Validate signature: "HWiS" = 0x53695748
    if sig != 0x53695748:
        return

    if sz_reading == 0 or n_reading == 0:
        return

    # ── Reading element field offsets (HWiNFO v2 — confirmed from hex dump) ────
    # uint32 dwSensorIndex     @ 0
    # uint32 dwSensorID        @ 4
    # uint32 dwReadingID       @ 8
    # char   szLabelOrig[128]  @ 12
    # char   szLabelUser[128]  @ 140
    # char   szUnit[16]        @ 268
    # double Value             @ 284
    # double ValueMin          @ 292
    # double ValueMax          @ 300
    # double ValueAvg          @ 308
    OFF_LABEL = 12
    OFF_USER  = 140
    OFF_UNIT  = 268
    OFF_VALUE = 284

    fans             = []
    cpu_temp         = 0.0
    cpu_power        = 0.0
    cpu_volt         = 0.0
    gpu_volt         = 0.0
    gpu_load         = 0.0
    gpu_temp         = 0.0
    gpu_core_clk     = 0.0
    gpu_mem_clk      = 0.0
    gpu_power        = 0.0
    gpu_mem_used     = 0.0
    cpu_freq         = 0.0
    cpu_elec_current = 0.0
    cpu_therm_current= 0.0
    dimm_temps       = []
    extra            = []          # extra sensors: (label, value, unit_str)
    extra_labels     = set()       # dedup by label

    _CPU_PWR  = ("cpu package power", "cpu ppt", "cpu power",
                 "package power", "cpu socket power", "cpu total power")
    _CPU_VOLT = ("cpu core voltage", "cpu vcore", "vcore",
                 "cpu vid", "core voltage", "cpu voltage")
    _GPU_VOLT = ("gpu core voltage", "gpu voltage", "gpu vcore",
                 "gpu vid", "gpu core volt")

    for i in range(n_reading):
        base = off_reading + i * sz_reading

        # Bounds check
        if base + OFF_VALUE + 8 > len(data):
            break

        # Read label (user-renamed label takes priority, fall back to original)
        label_user = data[base + OFF_USER : base + OFF_USER + 128].split(b'\x00')[0].decode("latin-1", errors="ignore").strip()
        label_orig = data[base + OFF_LABEL : base + OFF_LABEL + 128].split(b'\x00')[0].decode("latin-1", errors="ignore").strip()
        label = label_user if label_user else label_orig
        if not label:
            continue

        unit = data[base + OFF_UNIT : base + OFF_UNIT + 16].split(b'\x00')[0].decode("latin-1", errors="ignore").strip().lower()
        try:
            val = _struct.unpack_from("<d", data, base + OFF_VALUE)[0]
        except Exception:
            continue

        label_l = label.lower()

        # Temperature sensors
        if unit in ("", "c", "\xb0c", "°c") and 0 < val < 150:
            if cpu_temp == 0.0 and any(k in label_l for k in _CPU_TEMP_LABELS):
                cpu_temp = val
            elif gpu_temp == 0.0 and "gpu" in label_l and any(
                    k in label_l for k in ("gpu temperature", "gpu temp", "gpu core temp",
                                           "gpu diode", "gpu hotspot")):
                gpu_temp = val
            elif any(k in label_l for k in ("spd hub", "dimm", "sodimm",
                                                   "memory temp", "ram temp",
                                                   "tsensor_mem", "ts0_temp", "ts1_temp")):
                # DIMM / memory stick temperatures — NO dedup, same label
                # can appear for each stick (e.g. two "SPD Hub Temperature")
                n = len(dimm_temps) + 1
                dimm_temps.append((f"DIMM {n}", round(val, 1)))
            elif (label not in extra_labels
                  and any(k in label_l for k in _WC_LABELS)):
                extra.append((label, round(val, 1), "°C"))
                extra_labels.add(label)

        # Fan RPM
        elif unit == "rpm" and 0 < val < 10000:
            short = label.replace("#", "").strip()
            if short and not any(f[0] == short for f in fans):
                fans.append((short, val))

        # CPU package power
        elif unit == "w" and 0 < val < 1000:
            if cpu_power == 0.0 and any(label_l == p or label_l.startswith(p) for p in _CPU_PWR):
                cpu_power = val
            elif gpu_power == 0.0 and "gpu" in label_l and any(
                    k in label_l for k in ("gpu power", "gpu total", "gpu board", "total board power")):
                gpu_power = val

        # Voltages
        elif unit == "v" and 0.1 < val < 3.0:
            if cpu_volt == 0.0 and any(label_l == p or label_l.startswith(p) for p in _CPU_VOLT):
                cpu_volt = val
            elif gpu_volt == 0.0 and any(label_l == p or label_l.startswith(p) for p in _GPU_VOLT):
                gpu_volt = val

        # GPU load (%)
        elif unit == "%" and 0 <= val <= 100:
            if gpu_load == 0.0 and "gpu" in label_l and any(
                    k in label_l for k in ("gpu core load", "gpu load", "gpu utilization",
                                           "gpu usage", "3d load", "gpu d3d")):
                gpu_load = val

        # GPU / CPU clocks (MHz)
        elif unit == "mhz" and val > 0:
            if gpu_core_clk == 0.0 and "gpu" in label_l and any(
                    k in label_l for k in ("gpu core clock", "gpu clock", "gpu shader",
                                           "gpu frequency", "gpu engine")):
                if "memory" not in label_l:
                    gpu_core_clk = val
            elif gpu_mem_clk == 0.0 and "gpu" in label_l and "memory" in label_l:
                gpu_mem_clk = val
            elif cpu_freq == 0.0 and any(
                    k in label_l for k in ("cpu package frequency", "cpu frequency",
                                           "cpu clock", "effective clock")):
                cpu_freq = val

        # GPU memory used (MB)
        elif unit == "mb" and val >= 0:
            if gpu_mem_used == 0.0 and "gpu" in label_l and "used" in label_l:
                gpu_mem_used = val

        # CPU current (Amperes) — electrical (EDC) and thermal (TDC)
        elif unit == "a" and 0 < val < 1000:
            if cpu_elec_current == 0.0 and any(
                    k in label_l for k in ("edc", "electrical current", "cpu ia",
                                           "cpu package current", "cpu current",
                                           "ia current", "electrical")):
                cpu_elec_current = val
            elif cpu_therm_current == 0.0 and any(
                    k in label_l for k in ("tdc", "thermal current", "cpu tj",
                                           "thermal design current", "thermal")):
                cpu_therm_current = val

        # Flow rates — water cooling controllers (Aquaero, Octo, mobo headers)
        elif unit in ("l/h", "l/min", "lpm") and 0 < val < 500:
            if label not in extra_labels:
                extra.append((label, round(val, 2), unit.upper()))
                extra_labels.add(label)

    # ── Write results ──────────────────────────────────────────────────────────
    if cpu_temp  > 0: d.cpu_temp   = cpu_temp
    if cpu_power > 0: d.cpu_power  = cpu_power
    if cpu_volt  > 0: d.cpu_voltage = cpu_volt
    if gpu_volt  > 0: d.gpu_voltage = gpu_volt
    # GPU metrics from HWiNFO — override LHM values (HWiNFO is more accurate)
    if gpu_load     > 0: d.gpu_load     = gpu_load
    if gpu_temp     > 0: d.gpu_temp     = gpu_temp
    if gpu_core_clk > 0: d.gpu_core_clk = gpu_core_clk
    if gpu_mem_clk  > 0: d.gpu_mem_clk  = gpu_mem_clk
    if gpu_power    > 0: d.gpu_power    = gpu_power
    if gpu_mem_used > 0: d.gpu_mem_used = gpu_mem_used
    if cpu_freq          > 0: d.cpu_freq              = cpu_freq
    if cpu_elec_current  > 0: d.cpu_electrical_current = cpu_elec_current
    if cpu_therm_current > 0: d.cpu_thermal_current    = cpu_therm_current
    if dimm_temps:            d.dimm_temps             = dimm_temps
    d.extra_sensors = extra
    fans.sort(key=lambda x: x[0].lower())
    d.fans = fans

    # Log first successful read so user logs show what was detected
    global _hwinfo_logged_once
    if not _hwinfo_logged_once:
        _hwinfo_logged_once = True
        log.info("HWiNFO first read: cpu_temp=%.1f  cpu_power=%.1f  fans=%d  dimm=%d  extra=%d",
                 cpu_temp, cpu_power, len(fans), len(dimm_temps), len(extra))
        log.info("HWiNFO currents: elec=%.1fA  therm=%.1fA", cpu_elec_current, cpu_therm_current)
        if fans:
            log.info("HWiNFO fans: %s", ", ".join(f"{n}={rpm:.0f}" for n, rpm in fans))
        if dimm_temps:
            log.info("HWiNFO DIMMs: %s", ", ".join(f"{lbl}={t:.1f}C" for lbl, t in dimm_temps))
        # Log all Ampere-unit sensors so we can see exact HWiNFO label names
        _amp_sensors = []
        for i in range(n_reading):
            base = off_reading + i * sz_reading
            if base + OFF_VALUE + 8 > len(data):
                break
            u = data[base + OFF_UNIT : base + OFF_UNIT + 16].split(b'\x00')[0].decode("latin-1", errors="ignore").strip().lower()
            if u == "a":
                lbl = data[base + OFF_LABEL : base + OFF_LABEL + 128].split(b'\x00')[0].decode("latin-1", errors="ignore").strip()
                try:
                    v = _struct.unpack_from("<d", data, base + OFF_VALUE)[0]
                    _amp_sensors.append(f"{lbl}={v:.1f}A")
                except Exception:
                    pass
        if _amp_sensors:
            log.info("HWiNFO Ampere sensors: %s", ", ".join(_amp_sensors))

_hwinfo_logged_once = False

# ── NVML ─────────────────────────────────────────────────────────────────────
try:
    try:
        import nvidia_ml_py as pynvml   # preferred: nvidia-ml-py (no deprecation warning)
    except ImportError:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import pynvml                   # fallback: pynvml (deprecated but still works)
    pynvml.nvmlInit()
    _GPU_HANDLE = pynvml.nvmlDeviceGetHandleByIndex(0)
    _NVML_OK    = True
except Exception:
    _NVML_OK    = False
    _GPU_HANDLE = None

# ── LibreHardwareMonitor ──────────────────────────────────────────────────────
_LHM_OK       = False
_lhm_computer = None
_lhm_visitor  = None   # UpdateVisitor instance — reused every read cycle


def _try_init_lhm():
    global _LHM_OK, _lhm_computer, _lhm_visitor
    try:
        import clr
        import atexit

        # Search locations: next to EXE, next to this script, CWD, sys._MEIPASS
        candidates = [
            os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])),
                         "LibreHardwareMonitorLib.dll"),
            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "LibreHardwareMonitorLib.dll"),
            os.path.join(os.getcwd(), "LibreHardwareMonitorLib.dll"),
        ]
        # PyInstaller --onefile unpacks to sys._MEIPASS
        if hasattr(sys, "_MEIPASS"):
            candidates.insert(0, os.path.join(sys._MEIPASS,
                                               "LibreHardwareMonitorLib.dll"))

        dll_path = next((p for p in candidates if os.path.exists(p)), None)
        if dll_path is None:
            log.info("LHM DLL not found -- CPU temp will use WMI fallback")
            return

        log.info("LHM loading: %s", dll_path)
        clr.AddReference(dll_path)
        from LibreHardwareMonitor.Hardware import (  # type: ignore
            Computer, IVisitor
        )

        # ── UpdateVisitor — mirrors the canonical C# pattern ─────────────────
        # computer.Accept(visitor) walks the full hardware tree, calling
        # hardware.Update() on every node before sensors are read.  This is
        # more correct than calling hw.Update() manually per-node.
        class UpdateVisitor(IVisitor):
            def VisitComputer(self, computer):
                computer.Traverse(self)
            def VisitHardware(self, hardware):
                hardware.Update()
                for sub in hardware.SubHardware:
                    sub.Accept(self)
            def VisitSensor(self, sensor):
                pass
            def VisitParameter(self, parameter):
                pass

        _lhm_visitor = UpdateVisitor()

        comp = Computer()
        comp.IsCpuEnabled         = True
        comp.IsGpuEnabled         = not _NVML_OK   # AMD/Intel GPU via LHM
        comp.IsMemoryEnabled      = False           # RAM via psutil
        comp.IsMotherboardEnabled = True            # T_Sensor, W_PUMP, mobo headers
        comp.IsControllerEnabled  = True            # Aquaero, Octo, fan controllers
        comp.IsNetworkEnabled     = False           # Network via psutil
        comp.IsStorageEnabled     = False           # Storage via psutil
        comp.Open()

        # Register clean shutdown — computer.Close() releases the kernel driver
        atexit.register(comp.Close)

        _lhm_computer = comp
        _LHM_OK       = True
        log.info("LHM initialised OK (GPU=%s  Mobo=%s  Controller=%s)",
                 comp.IsGpuEnabled, comp.IsMotherboardEnabled, comp.IsControllerEnabled)
    except Exception as ex:
        log.warning("LHM init failed: %s", ex)

_try_init_lhm()

# ── WMI thermal fallback ──────────────────────────────────────────────────────
_WMI_OK  = False
_wmi_obj = None

def _try_init_wmi():
    global _WMI_OK, _wmi_obj
    if _LHM_OK:
        return   # LHM takes priority
    try:
        import wmi                                        # pip install wmi
        _wmi_obj = wmi.WMI(namespace="root/wmi")
        # Probe once to check it works
        _ = _wmi_obj.MSAcpi_ThermalZoneTemperature()
        _WMI_OK = True
        log.info("WMI thermal zone fallback ready")
    except Exception as ex:
        log.info("WMI not available: %s", ex)

_try_init_wmi()


# ── Data classes ──────────────────────────────────────────────────────────────
@dataclasses.dataclass
class DiskInfo:
    label:     str
    total_str: str
    pct:       float
    detail:    str


@dataclasses.dataclass
class SensorData:
    cpu_temp:     float = 0.0
    cpu_load:     float = 0.0
    cpu_cores:    list  = dataclasses.field(default_factory=list)   # per-core load %
    cpu_freqs:    list  = dataclasses.field(default_factory=list)   # per-core freq MHz
    cpu_power:    float = 0.0
    cpu_freq:     float = 0.0
    cpu_ccd_temp: str   = "—"

    gpu_load:     float = 0.0
    gpu_temp:     float = 0.0
    gpu_mem_used: float = 0.0
    gpu_mem_total:float = 0.0
    gpu_core_clk: float = 0.0
    gpu_mem_clk:  float = 0.0
    gpu_power:    float = 0.0
    gpu_fan_rpm:         float = 0.0
    gpu_voltage:         float = 0.0   # GPU core voltage in volts
    cpu_voltage:         float = 0.0   # CPU core voltage in volts
    cpu_electrical_current: float = 0.0  # CPU electrical current (A)
    cpu_thermal_current:    float = 0.0  # CPU thermal current (A)
    fps:          float = 0.0

    net_tx:         float = 0.0
    net_rx:         float = 0.0
    net_tx_total:   float = 0.0
    net_rx_total:   float = 0.0
    net_ping_ms:    float = -1.0   # -1 = no connection / timeout
    net_packet_loss:float = 0.0    # 0–100 %

    ram_used:     float = 0.0
    ram_total:    float = 0.0

    disks:        list  = dataclasses.field(default_factory=list)

    # Per-disk I/O: list of (label, read_bps, write_bps)
    disk_io:       list  = dataclasses.field(default_factory=list)

    # Fan speeds from HWiNFO — list of (label, rpm) tuples
    fans:          list  = dataclasses.field(default_factory=list)

    # DIMM temperatures — list of (label, °C) e.g. [("DIMM1", 39.5), ("DIMM2", 39.25)]
    dimm_temps:    list  = dataclasses.field(default_factory=list)

    # Extra HWiNFO sensors — anything not already shown elsewhere:
    # mobo temps, water cooling, VRM, chipset, Aquaero/Octo channels, flow rates.
    # list of (label, value, unit_str) e.g. [("Water In", 28.5, "°C"), ("Flow", 120.0, "L/H")]
    extra_sensors: list  = dataclasses.field(default_factory=list)


# ══════════════════════════════════════════════════════════════════════════════
#  HARDWARE PROFILE — detected once at startup, drives all dynamic thresholds
# ══════════════════════════════════════════════════════════════════════════════
@dataclasses.dataclass
class HardwareProfile:
    cpu_name:       str   = "Unknown CPU"
    cpu_cores:      int   = 4
    cpu_threads:    int   = 4
    cpu_tdp:        float = 65.0    # watts — used for warn threshold
    cpu_temp_warn:  float = 75.0
    cpu_temp_crit:  float = 90.0
    cpu_volt_warn:  float = 1.35

    gpu_name:       str   = "Unknown GPU"
    gpu_count:      int   = 0
    gpu_tdp:        float = 200.0
    gpu_temp_warn:  float = 80.0
    gpu_temp_crit:  float = 95.0
    gpu_mem_total:  float = 0.0     # MB
    gpu_max_rpm:    int   = 3000    # used to scale fan bar colours

    fan_max_rpm:    int   = 3000    # global fan max for colour thresholds

    has_nvidia:     bool  = False
    has_amd_gpu:    bool  = False

    @property
    def cpu_power_warn(self):
        return self.cpu_tdp * 0.85

    @property
    def gpu_power_warn(self):
        return self.gpu_tdp * 0.85


def _detect_hardware() -> HardwareProfile:
    """Probe the system once and return a fully populated HardwareProfile."""
    prof = HardwareProfile()

    # ── CPU ──────────────────────────────────────────────────────────────────
    try:
        import platform
        # platform.processor() returns CPUID string on AMD (e.g. "AMD64 Family 26...")
        # Use WMI Win32_Processor for the actual marketing name
        cpu_name_raw = "Unknown CPU"
        try:
            import wmi as _wmi
            for proc in _wmi.WMI().Win32_Processor():
                cpu_name_raw = proc.Name.strip()
                break
        except Exception:
            cpu_name_raw = platform.processor() or "Unknown CPU"

        prof.cpu_name    = cpu_name_raw
        prof.cpu_threads = psutil.cpu_count(logical=True)  or 4
        prof.cpu_cores   = psutil.cpu_count(logical=False) or prof.cpu_threads // 2

        name_l = prof.cpu_name.lower()

        # TDP table: broad matches ordered most-specific → least
        _TDP_TABLE = [
            # Threadripper / HEDT
            ("threadripper", 280), ("w-", 250),
            # AMD desktop high-end
            ("9950x",  170), ("9900x",  120), ("9800x3d", 120), ("9700x",  65),
            ("9600x",   65), ("7950x",  170), ("7900x",  170), ("7800x3d",120),
            ("7700x",  105), ("7600x",   105),
            # AMD mobile
            ("hx",      55), ("hs",      35), ("h",       45), ("u",       15),
            # Intel desktop high-end
            ("i9-14",  253), ("i9-13",  253), ("i9-12",  241),
            ("i7-14",  253), ("i7-13",  253), ("i7-12",  125),
            ("i5-14",  125), ("i5-13",  125), ("i5-12",   65),
            # Intel mobile (k/kf are desktop unlocked)
            ("k",      125), ("kf",     125),
            # Generic AMD / Intel fallback
            ("ryzen 9", 105), ("ryzen 7", 65), ("ryzen 5", 65),
            ("core i9",  65), ("core i7", 65), ("core i5", 65),
            ("xeon",   150),
        ]
        for fragment, tdp in _TDP_TABLE:
            if fragment in name_l:
                prof.cpu_tdp = float(tdp)
                break

        # Temp thresholds: AMD runs hotter than Intel at stock
        if "amd" in name_l or "ryzen" in name_l or "epyc" in name_l:
            prof.cpu_temp_warn = 75.0
            prof.cpu_temp_crit = 90.0
        else:
            prof.cpu_temp_warn = 80.0
            prof.cpu_temp_crit = 95.0

    except Exception as ex:
        log.error("CPU detect error: %s", ex)

    # ── GPU ──────────────────────────────────────────────────────────────────
    if _NVML_OK:
        try:
            count = pynvml.nvmlDeviceGetCount()
            prof.gpu_count    = count
            prof.has_nvidia   = count > 0
            if count > 0:
                h = pynvml.nvmlDeviceGetHandleByIndex(0)
                prof.gpu_name      = pynvml.nvmlDeviceGetName(h)
                if isinstance(prof.gpu_name, bytes):
                    prof.gpu_name = prof.gpu_name.decode()
                mem = pynvml.nvmlDeviceGetMemoryInfo(h)
                prof.gpu_mem_total = mem.total / 1024**2

                # TDP from enforced power limit
                try:
                    prof.gpu_tdp = pynvml.nvmlDeviceGetPowerManagementDefaultLimit(h) / 1000.0
                except Exception:
                    # Fallback: infer from name
                    gn = prof.gpu_name.lower()
                    _GPU_TDP = [
                        ("5090", 575), ("5080", 360), ("5070 ti", 300), ("5070", 250),
                        ("5060 ti",180), ("5060", 150),
                        ("4090", 450), ("4080", 320), ("4070 ti", 285), ("4070", 200),
                        ("4060 ti",165), ("4060", 115),
                        ("3090", 350), ("3080", 320), ("3070", 220), ("3060", 170),
                        ("a100", 400), ("h100", 700),
                    ]
                    for frag, tdp in _GPU_TDP:
                        if frag in gn:
                            prof.gpu_tdp = float(tdp)
                            break

                # GPU temp thresholds: most NVIDIA cards throttle at 83-87°C
                prof.gpu_temp_warn = 80.0
                prof.gpu_temp_crit = 90.0

                # Max fan RPM: probe if possible
                try:
                    prof.gpu_max_rpm = pynvml.nvmlDeviceGetMaxClockInfo(
                        h, pynvml.NVML_CLOCK_SM)   # not RPM but available
                except Exception:
                    pass

        except Exception as ex:
            log.error("GPU detect error: %s", ex)

    # ── Non-NVIDIA GPU: detect via WMI Win32_VideoController ─────────────────
    if not _NVML_OK:
        try:
            import wmi as _wmi
            for gpu in _wmi.WMI().Win32_VideoController():
                name = (gpu.Name or "").strip()
                # Skip Microsoft software renderers and remote display adapters
                if not name or any(s in name for s in ("Microsoft", "Remote", "Virtual")):
                    continue
                prof.gpu_name  = name
                prof.gpu_count = 1
                name_l = name.lower()
                if "amd" in name_l or "radeon" in name_l:
                    prof.has_amd_gpu = True
                # Infer TDP from name fragments for AMD cards
                _AMD_TDP = [
                    ("9070 xt", 304), ("9070", 220), ("9060 xt", 150),
                    ("7900 xtx", 355), ("7900 xt", 315), ("7900 gre", 260),
                    ("7800 xt", 263), ("7700 xt", 245), ("7600 xt", 165),
                    ("7600", 165), ("6950 xt", 335), ("6900 xt", 300),
                    ("6800 xt", 300), ("6800", 250), ("6750 xt", 250),
                    ("6700 xt", 230), ("6650 xt", 180), ("6600 xt", 160),
                    ("6600", 132),
                ]
                for frag, tdp in _AMD_TDP:
                    if frag in name_l:
                        prof.gpu_tdp = float(tdp)
                        break
                # VRAM from WMI (AdapterRAM is in bytes, but often wrong for > 4 GB cards;
                # use as a rough fallback only)
                try:
                    vram = int(gpu.AdapterRAM or 0)
                    if vram > 0:
                        prof.gpu_mem_total = vram / 1024**2
                except Exception:
                    pass
                prof.gpu_temp_warn = 85.0
                prof.gpu_temp_crit = 95.0
                log.info("Non-NVIDIA GPU detected via WMI: %s", prof.gpu_name)
                break
        except Exception as ex:
            log.error("WMI GPU detect error: %s", ex)

    # Fan RPM colour threshold: reasonable for most systems
    prof.fan_max_rpm = 3000

    log.info("CPU: %s | %dc/%dt | TDP %.0fW", prof.cpu_name, prof.cpu_cores, prof.cpu_threads, prof.cpu_tdp)
    log.info("GPU: %s | TDP %.0fW | VRAM %.0fGB", prof.gpu_name, prof.gpu_tdp, prof.gpu_mem_total / 1024)
    return prof


# Detect hardware once at import time
HW_PROFILE: HardwareProfile = _detect_hardware()

# ── Real FPS singletons — initialised lazily on first HardwareMonitor() ───────
_FPS_UPDATE_INTERVAL = 0.25   # seconds between cache refreshes


class _FpsSource:
    """Single owner of ETW + RTSS FPS acquisition.

    A daemon thread calls _resolve() every 250 ms and writes the result to
    self._cached.  All callers — UI fast timer and hardware reader — read
    only that cached float.  No process lookups ever run on the UI thread.
    """

    def __init__(self):
        self._cached: float            = -1.0
        self._etw:   '_EtwFpsCounter | None' = None
        self._rtss:  '_RtssFpsReader | None' = None

    def start(self) -> None:
        self._rtss = _RtssFpsReader()
        t = threading.Thread(target=self._update_loop,
                             daemon=True, name="fps-cache")
        t.start()

    @property
    def fps(self) -> float:
        """Cached present rate. -1.0 = no game detected.
        Single-object attribute read is GIL-atomic in CPython."""
        return self._cached

    # ── internal ──────────────────────────────────────────────────────────────
    def _update_loop(self) -> None:
        while True:
            try:
                self._cached = self._resolve()
            except Exception:
                self._cached = -1.0
            time.sleep(_FPS_UPDATE_INTERVAL)

    def _resolve(self) -> float:
        """Determine the foreground process and return its present rate.

        Returns DXGI present rate, not display-confirmed FPS. The two values
        diverge when vsync queue depth > 1, or when driver-level frame
        generation (DLSS-FG, FSR3) inserts additional presents that inflate
        the count above the rendered frame rate. RTSS is preferred when
        available as it carries additional display-correlation metadata.
        """
        user32 = ctypes.windll.user32
        hwnd   = user32.GetForegroundWindow()
        if not hwnd:
            return -1.0
        pid_c = ctypes.c_ulong(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_c))
        pid = pid_c.value
        if not pid:
            return -1.0
        try:
            if psutil.Process(pid).name().lower() in _NON_GAME_PROCS:
                return -1.0
        except Exception:
            return -1.0

        if self._rtss is not None and self._rtss.available():
            fps = self._rtss.fps_for_pid(pid)
            if fps >= 1.0:
                return fps

        return -1.0


_fps_source:    '_FpsSource | None' = None
_fps_init_done: bool                = False


def _init_fps_sources() -> None:
    global _fps_source, _fps_init_done
    if _fps_init_done:
        return
    _fps_init_done = True
    src = _FpsSource()
    src.start()
    _fps_source = src


def get_foreground_fps() -> float:
    """Return the cached present rate for the foreground process.

    -1.0 means no game active or acquisition not yet started.
    Updated every 250 ms by the fps-cache daemon thread.
    """
    return _fps_source.fps if _fps_source is not None else -1.0


# ══════════════════════════════════════════════════════════════════════════════
class HardwareMonitor:
    def read(self) -> SensorData:
        d = SensorData()
        # Traverse the full LHM hardware tree once — all readers share the result
        _lhm_update()
        self._read_cpu(d)
        self._read_gpu(d)
        self._read_network(d)
        self._read_memory(d)
        self._read_disks(d)
        self._read_ssd_io(d)
        self._read_fps(d)
        _hwinfo_read_all(d)   # CPU temp, fans, power, voltages, DIMM temps
        # Merge LHM controller/mobo sensors (Aquaero, Octo, T_Sensor) into
        # extra_sensors — deduplicated by label against HWiNFO results
        if _LHM_OK and _lhm_computer is not None:
            try:
                lhm_extra  = _lhm_read_extra(_lhm_computer)
                existing   = {s[0] for s in d.extra_sensors}
                for entry in lhm_extra:
                    if entry[0] not in existing:
                        d.extra_sensors.append(entry)
                        existing.add(entry[0])
            except Exception as ex:
                log.debug("LHM extra read: %s", ex)
        return d

    # ── CPU ───────────────────────────────────────────────────────────────────
    def _read_cpu(self, d: SensorData):
        d.cpu_cores = psutil.cpu_percent(percpu=True)
        d.cpu_load  = sum(d.cpu_cores) / max(len(d.cpu_cores), 1)
        freq = psutil.cpu_freq()
        if freq:
            d.cpu_freq = freq.current
        # Per-core frequencies — throttled to every 2 ticks (expensive on many-core)
        if not hasattr(self, "_freq_tick"):
            self._freq_tick = 0
            self._freq_cache = []
        self._freq_tick += 1
        if self._freq_tick % 2 == 0 or not self._freq_cache:
            try:
                per_freqs = psutil.cpu_freq(percpu=True)
                if per_freqs:
                    self._freq_cache = [f.current for f in per_freqs]
            except Exception:
                self._freq_cache = []
        d.cpu_freqs = self._freq_cache

        # Temp — METHOD 1: HWiNFO64 shared memory (handled in _hwinfo_read_all)
        # Fall through to LHM/WMI/psutil if HWiNFO not running

        # Temp — METHOD 2: LibreHardwareMonitor DLL
        if _LHM_OK and _lhm_computer:
            try:
                temp, power, ccd = _lhm_read_cpu(_lhm_computer)
                if temp > 0:
                    d.cpu_temp     = temp
                    d.cpu_power    = power
                    d.cpu_ccd_temp = ccd
                    return
            except Exception as ex:
                log.debug("LHM CPU read error: %s", ex)

        # Temp — METHOD 3: WMI thermal zone
        if _WMI_OK and _wmi_obj:
            try:
                zones = _wmi_obj.MSAcpi_ThermalZoneTemperature()
                if zones:
                    temps_c = [(z.CurrentTemperature / 10.0) - 273.15
                               for z in zones]
                    valid = [t for t in temps_c if 0 < t < 150]
                    if valid:
                        d.cpu_temp = max(valid)
                        return
            except Exception as ex:
                log.debug("WMI CPU read error: %s", ex)

        # Temp — METHOD 4: psutil sensors
        try:
            temps = psutil.sensors_temperatures()
            if temps:
                for key in ("k10temp", "coretemp", "cpu_thermal", "acpitz"):
                    if key in temps:
                        vals = [e.current for e in temps[key] if e.current > 0]
                        if vals:
                            d.cpu_temp = max(vals)
                            return
        except Exception:
            pass

    # ── GPU ───────────────────────────────────────────────────────────────────
    def _read_gpu(self, d: SensorData):
        # ── Path A: NVIDIA via NVML (preferred) ──────────────────────────────
        if _NVML_OK and _GPU_HANDLE is not None:
            self._read_gpu_nvml(d)
            return
        # ── Path B: AMD / Intel via LibreHardwareMonitor ──────────────────────
        if _LHM_OK and _lhm_computer is not None:
            try:
                load, temp, vram_used, vram_total, power, core_mhz, mem_mhz = \
                    _lhm_read_gpu(_lhm_computer)
                if load > 0 or temp > 0:
                    d.gpu_load      = load
                    d.gpu_temp      = temp
                    d.gpu_mem_used  = vram_used
                    d.gpu_mem_total = vram_total if vram_total > 0 else HW_PROFILE.gpu_mem_total
                    d.gpu_power     = power
                    d.gpu_core_clk  = core_mhz
                    d.gpu_mem_clk   = mem_mhz
            except Exception as ex:
                log.debug("LHM GPU read error: %s", ex)

    def _read_gpu_nvml(self, d: SensorData):
        try:
            util            = pynvml.nvmlDeviceGetUtilizationRates(_GPU_HANDLE)
            d.gpu_load      = util.gpu
            d.gpu_temp      = pynvml.nvmlDeviceGetTemperature(
                                _GPU_HANDLE, pynvml.NVML_TEMPERATURE_GPU)
            mem             = pynvml.nvmlDeviceGetMemoryInfo(_GPU_HANDLE)
            d.gpu_mem_used  = mem.used  / 1024**2
            d.gpu_mem_total = mem.total / 1024**2
            d.gpu_power     = pynvml.nvmlDeviceGetPowerUsage(_GPU_HANDLE) / 1000.0
            d.gpu_core_clk  = pynvml.nvmlDeviceGetClockInfo(_GPU_HANDLE,
                                pynvml.NVML_CLOCK_SM)
            d.gpu_mem_clk   = pynvml.nvmlDeviceGetClockInfo(_GPU_HANDLE,
                                pynvml.NVML_CLOCK_MEM)
            # Fan speed: try per-fan RPM query first, fallback to percent * max
            try:
                fan_count = pynvml.nvmlDeviceGetNumFans(_GPU_HANDLE)
                total_rpm = 0
                for fi in range(fan_count):
                    total_rpm += pynvml.nvmlDeviceGetFanSpeedRPM(_GPU_HANDLE, fi)
                d.gpu_fan_rpm = total_rpm / max(fan_count, 1)
            except Exception:
                try:
                    fan_pct       = pynvml.nvmlDeviceGetFanSpeed(_GPU_HANDLE)
                    # Scale percent to RPM using profile max (default 3000 if unknown)
                    d.gpu_fan_rpm = fan_pct / 100.0 * HW_PROFILE.fan_max_rpm
                except Exception:
                    d.gpu_fan_rpm = 0.0
        except Exception as ex:
            log.debug("NVML read error: %s", ex)


    # ── Network ───────────────────────────────────────────────────────────────
    def __init__(self):
        _init_fps_sources()
        self._last_net       = psutil.net_io_counters()
        self._last_time      = time.monotonic()
        try:
            self._last_disk = psutil.disk_io_counters(perdisk=True)
        except Exception:
            self._last_disk = {}
        self._last_disk_time = time.monotonic()
        psutil.cpu_percent(percpu=True)   # seed
        # Ping state — updated in background thread
        self._ping_ms         = -1.0
        self._packet_loss_pct = 0.0
        self._ping_history    = collections.deque([None] * 10, maxlen=10)
        self._start_ping_thread()

    def _start_ping_thread(self):
        import threading
        t = threading.Thread(target=self._ping_loop, daemon=True)
        t.start()

    def _ping_loop(self):
        """Background thread: ping 8.8.8.8 every second using raw socket."""
        import socket, struct, select, random
        HOST = "8.8.8.8"
        while True:
            try:
                start = time.monotonic()
                # Use TCP connect to port 53 as fallback (no raw socket needed)
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(1.0)
                err = s.connect_ex((HOST, 53))
                elapsed = (time.monotonic() - start) * 1000
                s.close()
                if err == 0:
                    self._ping_history.append(elapsed)
                else:
                    self._ping_history.append(None)
            except Exception:
                self._ping_history.append(None)
            # Compute stats from history window
            hist = list(self._ping_history)
            valid = [v for v in hist if v is not None]
            if valid:
                self._ping_ms         = sum(valid) / len(valid)
                self._packet_loss_pct = (hist.count(None) / len(hist)) * 100
            else:
                self._ping_ms         = -1.0
                self._packet_loss_pct = 100.0
            time.sleep(1.0)

    def _read_network(self, d: SensorData):
        now     = time.monotonic()
        cur     = psutil.net_io_counters()
        elapsed = max(now - self._last_time, 0.001)
        d.net_tx            = (cur.bytes_sent - self._last_net.bytes_sent) / elapsed
        d.net_rx            = (cur.bytes_recv - self._last_net.bytes_recv) / elapsed
        d.net_tx_total      = cur.bytes_sent
        d.net_rx_total      = cur.bytes_recv
        d.net_ping_ms       = self._ping_ms
        d.net_packet_loss   = self._packet_loss_pct
        self._last_net  = cur
        self._last_time = now

    # ── Memory ────────────────────────────────────────────────────────────────
    def _read_memory(self, d: SensorData):
        vm          = psutil.virtual_memory()
        d.ram_used  = vm.used  / 1024**2
        d.ram_total = vm.total / 1024**2

    # ── Disks ─────────────────────────────────────────────────────────────────
    def _read_disks(self, d: SensorData):
        disks = []
        try:
            for part in psutil.disk_partitions(all=False):
                if "cdrom" in part.opts or part.fstype == "":
                    continue
                try:
                    usage    = psutil.disk_usage(part.mountpoint)
                    total_gb = usage.total / 1024**3
                    used_gb  = usage.used  / 1024**3
                    disks.append(DiskInfo(
                        label     = part.device.replace("\\", "").rstrip(":"),
                        total_str = f"{total_gb:.0f} GB",
                        pct       = usage.percent,
                        detail    = (f"{used_gb:.1f} / {total_gb:.0f} GB"
                                     f"  ({usage.percent:.0f}%)")
                    ))
                except PermissionError:
                    continue
        except Exception:
            pass
        d.disks = disks[:4]

    # ── Disk I/O (per disk) ───────────────────────────────────────────────────
    def _read_ssd_io(self, d: SensorData):
        try:
            counters = psutil.disk_io_counters(perdisk=True)
            now      = time.monotonic()
            elapsed  = max(now - self._last_disk_time, 0.001)

            # Build mapping: physical drive number → list of (letter, volume_label)
            phys_map = _get_drive_map()

            result = []
            for disk_name, c in counters.items():
                if c.read_bytes == 0 and c.write_bytes == 0:
                    continue
                prev = self._last_disk.get(disk_name)
                if prev:
                    rbps = max(0.0, (c.read_bytes  - prev.read_bytes)  / elapsed)
                    wbps = max(0.0, (c.write_bytes - prev.write_bytes) / elapsed)
                else:
                    rbps = wbps = 0.0

                label = _disk_label(disk_name, phys_map)
                result.append((label, rbps, wbps))

            self._last_disk      = counters
            self._last_disk_time = now
            d.disk_io = result
        except Exception as ex:
            log.debug("disk_io error: %s", ex)

    # ── FPS ───────────────────────────────────────────────────────────────────
    def _read_fps(self, d: SensorData):
        """FPS from RTSS shared memory; falls back to GPU-load × refresh-rate estimate."""
        fps = get_foreground_fps()
        if fps > 0:
            d.fps = fps
        elif _is_game_running():
            # GPU-load × refresh-rate estimate — bounded and clearly approximate
            refresh = _get_primary_refresh_rate()
            d.fps = max(1.0, refresh * (d.gpu_load / 100.0))
        else:
            d.fps = -1.0


# ── Drive map: physical drive number → [(letter, volume_label), ...] ─────────
def _build_drive_map() -> dict:
    """
    Uses Windows WMI to map PhysicalDisk indices to drive letters and volume labels.
    Returns dict like {0: [("C", "Windows"), ("D", "Data")], 1: [("E", "Games")]}
    Falls back to empty dict on non-Windows or WMI error.
    """
    result = {}
    try:
        import wmi
        w = wmi.WMI()
        for disk in w.Win32_DiskDrive():
            idx = int(disk.Index)
            letters = []
            try:
                for partition in disk.associators("Win32_DiskDriveToDiskPartition"):
                    for logical in partition.associators("Win32_LogicalDiskToPartition"):
                        letter = logical.DeviceID.rstrip(":\\")
                        vol    = (logical.VolumeName or "").strip()
                        letters.append((letter, vol))
            except Exception:
                pass
            result[idx] = letters
    except Exception:
        pass
    return result

_drive_map_cache      = {}
_drive_map_cache_time = 0.0

def _get_drive_map():
    """Cache the WMI drive map for 30 s to avoid hammering WMI every 500 ms."""
    global _drive_map_cache, _drive_map_cache_time
    import time as _time
    if _time.monotonic() - _drive_map_cache_time > 30.0:
        _drive_map_cache      = _build_drive_map()
        _drive_map_cache_time = _time.monotonic()
    return _drive_map_cache


def _disk_label(raw: str, phys_map: dict = None) -> str:
    """
    Convert a psutil perdisk key to a friendly label.
    On Windows:  '\\\\.\\ PhysicalDrive0' → 'C: Windows / D: Data'
    Fallback:    'PhysicalDrive0' → 'Drive 0'
    """
    import re
    r   = raw.strip().replace("\\\\.\\", "").replace("\\", "")
    rl  = r.lower()
    m   = re.search(r'(\d+)$', r)
    num = int(m.group(1)) if m else -1

    # Try to resolve via the pre-built map
    if phys_map is None:
        phys_map = {}

    if "physicaldrive" in rl and num >= 0 and num in phys_map:
        entries = phys_map[num]
        if entries:
            parts = []
            for letter, vol in entries:
                if vol:
                    parts.append(f"{letter}: {vol}")
                else:
                    parts.append(f"{letter}:")
            return "  ".join(parts)

    # Fallback labels
    if "physicaldrive" in rl: return f"Drive {num}" if num >= 0 else "Drive"
    if "nvme"          in rl: return f"NVMe {num}"  if num >= 0 else "NVMe"
    if "cdrom"         in rl: return f"DVD"
    if rl.startswith("sd"):   return r.upper()
    if rl.startswith("hd"):   return f"HDD {num}"   if num >= 0 else "HDD"
    return r[:14]


# ── Game detection ────────────────────────────────────────────────────────────
# Processes that own GPU load but are NOT games
_NON_GAME_PROCS = frozenset({
    "dwm.exe", "explorer.exe", "searchhost.exe", "shellexperiencehost.exe",
    "startmenuexperiencehost.exe", "runtimebroker.exe", "svchost.exe",
    "taskhostw.exe", "sihost.exe", "ctfmon.exe", "textinputhost.exe",
    "systemsettings.exe", "applicationframehost.exe", "winstore.app.exe",
    "lockapp.exe", "logonui.exe", "fontdrvhost.exe", "csrss.exe",
    "wininit.exe", "services.exe", "lsass.exe", "smss.exe",
    # monitoring / overlay tools
    "hwinfo64.exe", "hwinfo32.exe", "msiafterburner.exe", "rtss.exe",
    "riva statistics server.exe", "gpuz.exe", "cpuz.exe", "aida64.exe",
    "commandcenter.exe", "python.exe", "python3.exe",
    # browsers (GPU-accelerated but not games)
    "chrome.exe", "firefox.exe", "msedge.exe", "opera.exe", "brave.exe",
    # common creative / productivity apps
    "photoshop.exe", "premiere.exe", "resolve.exe", "blender.exe",
    "code.exe", "devenv.exe", "idea64.exe",
})

def _get_primary_refresh_rate() -> float:
    """Return the primary monitor refresh rate in Hz (defaults to 60.0 on error)."""
    try:
        user32 = ctypes.windll.user32
        hdc    = ctypes.windll.gdi32.CreateDCW("DISPLAY", None, None, None)
        if hdc:
            hz  = ctypes.windll.gdi32.GetDeviceCaps(hdc, 116)  # VREFRESH = 116
            ctypes.windll.gdi32.DeleteDC(hdc)
            if hz > 0:
                return float(hz)
    except Exception:
        pass
    return 60.0


def _is_game_running() -> bool:
    """
    Return True if a game (fullscreen or high-GPU-load non-system process)
    appears to be running. Uses two signals:
      1. A fullscreen exclusive window exists owned by a non-system process
      2. GPU load > 40% AND the foreground window belongs to a non-system proc
    """
    try:
        import ctypes
        user32 = ctypes.windll.user32

        # Get the foreground window and its process
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False

        pid = ctypes.c_ulong(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        pid = pid.value

        # Get process name for that PID
        try:
            proc = psutil.Process(pid)
            proc_name = proc.name().lower()
        except Exception:
            return False

        if proc_name in _NON_GAME_PROCS:
            return False

        # The foreground window belongs to a non-system process.
        # Now check if it's fullscreen (covers the whole screen).
        rect = ctypes.wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        win_w = rect.right  - rect.left
        win_h = rect.bottom - rect.top

        # Get screen dimensions
        screen_w = user32.GetSystemMetrics(0)
        screen_h = user32.GetSystemMetrics(1)

        is_fullscreen = (win_w >= screen_w and win_h >= screen_h)
        return is_fullscreen

    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════════════════
#  REAL FPS — RTSS shared memory (RivaTuner Statistics Server)
# ══════════════════════════════════════════════════════════════════════════════
class _RtssFpsReader:
    """Reads per-process FPS from RivaTuner / MSI Afterburner shared memory.

    Maintains a ring buffer of (time, frame_count) samples per PID so the
    rolling-window (N-1)/span calculation matches _EtwFpsCounter.
    """

    _SM_NAME  = "Global\\RTSSSharedMemoryV2"
    _MAX_HIST = 16    # samples per PID

    def __init__(self):
        self._history: dict = {}   # pid -> deque of (mono_time, frame_count)

    def available(self) -> bool:
        import mmap
        try:
            sm = mmap.mmap(-1, 8, tagname=self._SM_NAME, access=mmap.ACCESS_READ)
            sm.close()
            return True
        except Exception:
            return False

    def fps_for_pid(self, pid: int, min_window: float = 0.25) -> float:
        import mmap
        import struct as _s
        try:
            sm = mmap.mmap(-1, 256 * 1024, tagname=self._SM_NAME,
                           access=mmap.ACCESS_READ)
        except Exception:
            return 0.0
        try:
            if bytes(sm[0:4]) != b'RTSS':
                return 0.0
            _ver, entry_size, arr_offset, arr_size = _s.unpack_from('<IIII', sm, 4)
            for i in range(min(arr_size, 256)):
                off  = arr_offset + i * entry_size
                epid = _s.unpack_from('<I', sm, off)[0]
                if epid != pid:
                    continue
                frames = _s.unpack_from('<I', sm, off + 16)[0]
                now = time.monotonic()
                dq  = self._history.setdefault(
                    pid, collections.deque(maxlen=self._MAX_HIST))
                dq.append((now, frames))
                if len(dq) < 2:
                    return 0.0
                # Walk back to find the oldest sample with enough span
                for old_t, old_f in dq:
                    span = dq[-1][0] - old_t
                    if span >= min_window:
                        df = (dq[-1][1] - old_f) & 0xFFFF_FFFF
                        return df / span
                return 0.0
            return 0.0
        except Exception:
            return 0.0
        finally:
            sm.close()


# ══════════════════════════════════════════════════════════════════════════════
#  REAL FPS — ETW DXGI Present event counter
#  Counts IDXGISwapChain::Present() calls per process in real time.
#  Same technique used by NVIDIA FrameView / PresentMon.
# ══════════════════════════════════════════════════════════════════════════════
import ctypes.wintypes as _wt   # noqa: E402  (already imported above, alias for clarity)


class _GUID(ctypes.Structure):
    _fields_ = [('Data1', _wt.ULONG), ('Data2', _wt.USHORT),
                ('Data3', _wt.USHORT), ('Data4', ctypes.c_ubyte * 8)]


class _WNODE_HEADER(ctypes.Structure):
    _fields_ = [
        ('BufferSize',        _wt.ULONG),
        ('ProviderId',        _wt.ULONG),
        ('HistoricalContext', ctypes.c_ulonglong),
        ('TimeStamp',         ctypes.c_longlong),
        ('Guid',              _GUID),
        ('ClientContext',     _wt.ULONG),
        ('Flags',             _wt.ULONG),
    ]


class _EVENT_TRACE_PROPERTIES(ctypes.Structure):
    _fields_ = [
        ('Wnode',                _WNODE_HEADER),
        ('BufferSize',           _wt.ULONG),
        ('MinimumBuffers',       _wt.ULONG),
        ('MaximumBuffers',       _wt.ULONG),
        ('MaximumFileSize',      _wt.ULONG),
        ('LogFileMode',          _wt.ULONG),
        ('FlushTimer',           _wt.ULONG),
        ('EnableFlags',          _wt.ULONG),
        ('AgeLimit',             _wt.LONG),
        ('NumberOfBuffers',      _wt.ULONG),
        ('FreeBuffers',          _wt.ULONG),
        ('EventsLost',           _wt.ULONG),
        ('BuffersWritten',       _wt.ULONG),
        ('LogBuffersLost',       _wt.ULONG),
        ('RealTimeBuffersLost',  _wt.ULONG),
        ('LoggerThreadId',       ctypes.c_void_p),
        ('LogFileNameOffset',    _wt.ULONG),
        ('LoggerNameOffset',     _wt.ULONG),
    ]


class _EVENT_DESCRIPTOR(ctypes.Structure):
    _fields_ = [
        ('Id',      _wt.USHORT),
        ('Version', ctypes.c_ubyte),
        ('Channel', ctypes.c_ubyte),
        ('Level',   ctypes.c_ubyte),
        ('Opcode',  ctypes.c_ubyte),
        ('Task',    _wt.USHORT),
        ('Keyword', ctypes.c_ulonglong),
    ]


class _EVENT_HEADER(ctypes.Structure):
    _fields_ = [
        ('Size',            _wt.USHORT),
        ('HeaderType',      _wt.USHORT),
        ('Flags',           _wt.USHORT),
        ('EventProperty',   _wt.USHORT),
        ('ThreadId',        _wt.ULONG),
        ('ProcessId',       _wt.ULONG),
        ('TimeStamp',       ctypes.c_longlong),
        ('ProviderId',      _GUID),
        ('EventDescriptor', _EVENT_DESCRIPTOR),
        ('ProcessorTime',   ctypes.c_ulonglong),
        ('ActivityId',      _GUID),
    ]


class _ETW_BUFFER_CONTEXT(ctypes.Structure):
    _fields_ = [('ProcessorNumber', ctypes.c_ubyte),
                ('Alignment',       ctypes.c_ubyte),
                ('LoggerId',        _wt.USHORT)]


class _EVENT_RECORD(ctypes.Structure):
    _fields_ = [
        ('EventHeader',       _EVENT_HEADER),
        ('BufferContext',      _ETW_BUFFER_CONTEXT),
        ('ExtendedDataCount',  _wt.USHORT),
        ('UserDataLength',     _wt.USHORT),
        ('ExtendedData',       ctypes.c_void_p),
        ('UserData',           ctypes.c_void_p),
        ('UserContext',        ctypes.c_void_p),
    ]


class _SYSTEMTIME(ctypes.Structure):
    _fields_ = [('wYear', _wt.WORD), ('wMonth', _wt.WORD),
                ('wDayOfWeek', _wt.WORD), ('wDay', _wt.WORD),
                ('wHour', _wt.WORD), ('wMinute', _wt.WORD),
                ('wSecond', _wt.WORD), ('wMilliseconds', _wt.WORD)]


class _TIME_ZONE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ('Bias',          _wt.LONG),
        ('StandardName',  ctypes.c_wchar * 32),
        ('StandardDate',  _SYSTEMTIME),
        ('StandardBias',  _wt.LONG),
        ('DaylightName',  ctypes.c_wchar * 32),
        ('DaylightDate',  _SYSTEMTIME),
        ('DaylightBias',  _wt.LONG),
    ]


class _EVENT_TRACE_HEADER(ctypes.Structure):
    _fields_ = [
        ('Size',          _wt.USHORT),
        ('FieldTypeFlags', _wt.USHORT),
        ('Version',       _wt.ULONG),
        ('ThreadId',      _wt.ULONG),
        ('ProcessId',     _wt.ULONG),
        ('TimeStamp',     ctypes.c_longlong),
        ('Guid',          _GUID),
        ('ProcessorTime', ctypes.c_ulonglong),
    ]


class _EVENT_TRACE(ctypes.Structure):
    _fields_ = [
        ('Header',           _EVENT_TRACE_HEADER),
        ('InstanceId',       _wt.ULONG),
        ('ParentInstanceId', _wt.ULONG),
        ('ParentGuid',       _GUID),
        ('MofData',          ctypes.c_void_p),
        ('MofLength',        _wt.ULONG),
        ('ClientContext',    _wt.ULONG),
    ]


class _TRACE_LOGFILE_HEADER(ctypes.Structure):
    _fields_ = [
        ('BufferSize',         _wt.ULONG),
        ('Version',            _wt.ULONG),
        ('ProviderVersion',    _wt.ULONG),
        ('NumberOfProcessors', _wt.ULONG),
        ('EndTime',            ctypes.c_longlong),
        ('TimerResolution',    _wt.ULONG),
        ('MaximumFileSize',    _wt.ULONG),
        ('LogFileMode',        _wt.ULONG),
        ('BuffersWritten',     _wt.ULONG),
        ('LogInstanceGuid',    _GUID),
        ('LoggerName',         ctypes.c_wchar_p),
        ('LogFileName',        ctypes.c_wchar_p),
        ('TimeZone',           _TIME_ZONE_INFORMATION),
        ('BootTime',           ctypes.c_longlong),
        ('PerfFreq',           ctypes.c_longlong),
        ('StartTime',          ctypes.c_longlong),
        ('ReservedFlags',      _wt.ULONG),
        ('BuffersLost',        _wt.ULONG),
    ]


class _EVENT_TRACE_LOGFILEW(ctypes.Structure):
    _fields_ = [
        ('LogFileName',         ctypes.c_wchar_p),
        ('LoggerName',          ctypes.c_wchar_p),
        ('CurrentTime',         ctypes.c_longlong),
        ('BuffersRead',         _wt.ULONG),
        ('ProcessTraceMode',    _wt.ULONG),       # union with LogFileMode
        ('CurrentEvent',        _EVENT_TRACE),
        ('LogfileHeader',       _TRACE_LOGFILE_HEADER),
        ('BufferCallback',      ctypes.c_void_p),
        ('BufferSize',          _wt.ULONG),
        ('Filled',              _wt.ULONG),
        ('EventsLost',          _wt.ULONG),
        ('EventRecordCallback', ctypes.c_void_p),  # union with EventCallback
        ('IsKernelTrace',       _wt.ULONG),
        ('Context',             ctypes.c_void_p),
    ]


# Microsoft-Windows-DXGI provider — fires on every IDXGISwapChain::Present call
_DXGI_PROVIDER = _GUID(
    0xCA11C036, 0x0102, 0x4A2D,
    (ctypes.c_ubyte * 8)(0xA6, 0xAD, 0xF0, 0x3C, 0xFE, 0xD5, 0xD3, 0xC9),
)

_ETW_SESSION_NAME  = "CommandCenter-FPS-v1"
_INVALID_THANDLE   = ctypes.c_uint64(-1).value
_WNODE_TRACED_GUID = 0x00020000
_ETW_REALTIME      = 0x00000100
_PTM_REALTIME      = 0x00000100
_PTM_EVENT_RECORD  = 0x10000000
_ETC_STOP          = 1


def _setup_etw_argtypes() -> None:
    adv = ctypes.windll.advapi32
    adv.StartTraceW.restype    = _wt.ULONG
    adv.StartTraceW.argtypes   = [ctypes.POINTER(ctypes.c_uint64),
                                   ctypes.c_wchar_p, ctypes.c_void_p]
    adv.ControlTraceW.restype  = _wt.ULONG
    adv.ControlTraceW.argtypes = [ctypes.c_uint64, ctypes.c_wchar_p,
                                   ctypes.c_void_p, _wt.ULONG]
    adv.EnableTraceEx2.restype = _wt.ULONG
    adv.OpenTraceW.restype     = ctypes.c_uint64
    adv.OpenTraceW.argtypes    = [ctypes.c_void_p]
    adv.ProcessTrace.restype   = _wt.ULONG
    adv.ProcessTrace.argtypes  = [ctypes.POINTER(ctypes.c_uint64),
                                   _wt.ULONG,
                                   ctypes.c_void_p, ctypes.c_void_p]
    adv.CloseTrace.restype     = _wt.ULONG
    adv.CloseTrace.argtypes    = [ctypes.c_uint64]


try:
    _setup_etw_argtypes()
except Exception:
    pass


class _EtwFpsCounter:
    """ETW DXGI Present_Start counter with per-swap-chain tracking.

    The first 8 bytes of Present_Start UserData contain the IDXGISwapChain*
    pointer.  Modern games (UE5, Genshin, etc.) own multiple swap chains per
    PID (3D scene, UI, driver overlays).  Tracking per-chain lets us isolate
    the primary render chain rather than summing all chains blindly.

    fps_for_pid() returns the minimum present-rate among swap chains that
    are actively presenting at ≥ 5 Hz — the main rendering chain runs at the
    game's target frame rate; auxiliary chains tend to be faster or slower.
    """

    def __init__(self):
        self._lock         = threading.Lock()
        # key: (pid, swapchain_ptr)  value: deque of monotonic timestamps
        self._chains: dict = collections.defaultdict(collections.deque)
        self._session      = ctypes.c_uint64(0)
        self._trace        = ctypes.c_uint64(_INVALID_THANDLE)
        self._callback_ref = None
        self.active        = False

    def start(self) -> None:
        t = threading.Thread(target=self._run, daemon=True, name="etw-fps")
        t.start()

    def fps_for_pid(self, pid: int,
                    min_window: float = 0.25,
                    min_rate:   float = 5.0) -> float:
        """Present rate of the primary render chain for pid.

        Chains presenting at < min_rate Hz are excluded (idle/debug chains).
        Returns the minimum fps among the remaining chains: the main renderer
        targets the game's frame rate; overlay/UI chains are typically faster.

        Measures DXGI present rate, not display-confirmed FPS. Driver-level
        frame generation (DLSS-FG, FSR3) inflates this above rendered fps.
        """
        now    = time.monotonic()
        cutoff = now - 2.0

        with self._lock:
            # Snapshot all swap chains belonging to this pid
            chains_snap = {
                sc: list(dq)
                for (p, sc), dq in self._chains.items()
                if p == pid
            }

        fps_values = []
        for sc, timestamps in chains_snap.items():
            window = [t for t in timestamps if t >= cutoff]
            if len(window) < 2:
                continue
            span = window[-1] - window[0]
            if span < min_window:
                continue
            fps = (len(window) - 1) / span
            # Exclude chains below the minimum rate (idle/infrequent)
            if fps < min_rate:
                continue
            fps_values.append(fps)

        if not fps_values:
            return 0.0

        # The primary render chain presents at the game's target frame rate.
        # Auxiliary chains (overlays, UI, deferred passes) tend to be faster.
        return min(fps_values)

    # ── internal ──────────────────────────────────────────────────────────────
    def _run(self) -> None:
        try:
            self._start_session()
            self.active = True
            self._process_events()
        except Exception as exc:
            log.debug("ETW FPS thread error: %s", exc)
        finally:
            self._stop_session()
            self.active = False

    def _start_session(self) -> None:
        adv  = ctypes.windll.advapi32
        name = _ETW_SESSION_NAME
        xtra = (len(name) + 1) * 2
        bsz  = ctypes.sizeof(_EVENT_TRACE_PROPERTIES) + xtra
        buf  = (ctypes.c_byte * bsz)()

        def _fill(b):
            p = ctypes.cast(b, ctypes.POINTER(_EVENT_TRACE_PROPERTIES)).contents
            p.Wnode.BufferSize = bsz
            p.Wnode.Flags      = _WNODE_TRACED_GUID
            p.LogFileMode      = _ETW_REALTIME
            p.LoggerNameOffset = ctypes.sizeof(_EVENT_TRACE_PROPERTIES)
            p.BufferSize       = 256     # larger buffers reduce event loss at high fps
            p.MinimumBuffers   = 8

        # Kill any leftover session from a previous crash
        _fill(buf)
        adv.ControlTraceW(ctypes.c_uint64(0), name,
                          ctypes.cast(buf, ctypes.c_void_p), _ETC_STOP)
        ctypes.memset(buf, 0, bsz)
        _fill(buf)
        ret = adv.StartTraceW(ctypes.byref(self._session), name,
                              ctypes.cast(buf, ctypes.c_void_p))
        if ret not in (0, 183):
            raise OSError(f"StartTraceW failed: {ret}")

        adv.EnableTraceEx2(
            self._session, ctypes.byref(_DXGI_PROVIDER),
            1, 5,
            ctypes.c_uint64(0xFFFF_FFFF_FFFF_FFFF),
            ctypes.c_uint64(0), ctypes.c_uint32(0), None,
        )

    def _process_events(self) -> None:
        adv = ctypes.windll.advapi32
        _CB = ctypes.WINFUNCTYPE(None, ctypes.POINTER(_EVENT_RECORD))

        @_CB
        def _on_event(rec_ptr):
            try:
                rec    = rec_ptr.contents
                opcode = rec.EventHeader.EventDescriptor.Opcode
                if opcode != 1:      # only Present_Start
                    return
                pid = rec.EventHeader.ProcessId
                ts  = time.monotonic()

                # Extract IDXGISwapChain* from first 8 bytes of UserData
                sc_ptr = 0
                if rec.UserDataLength >= 8 and rec.UserData:
                    sc_ptr = ctypes.cast(
                        rec.UserData, ctypes.POINTER(ctypes.c_uint64)
                    ).contents.value

                key = (pid, sc_ptr)
                with self._lock:
                    dq = self._chains[key]
                    dq.append(ts)
                    # Trim timestamps older than 2 s
                    cutoff = ts - 2.0
                    while dq and dq[0] < cutoff:
                        dq.popleft()
            except Exception:
                pass

        self._callback_ref = _on_event

        logfile = _EVENT_TRACE_LOGFILEW()
        logfile.LoggerName          = _ETW_SESSION_NAME
        logfile.ProcessTraceMode    = _PTM_REALTIME | _PTM_EVENT_RECORD
        logfile.EventRecordCallback = ctypes.cast(_on_event, ctypes.c_void_p)

        handle = adv.OpenTraceW(ctypes.byref(logfile))
        if handle == _INVALID_THANDLE:
            raise OSError("OpenTraceW failed")
        self._trace = ctypes.c_uint64(handle)

        h_arr = (ctypes.c_uint64 * 1)(handle)
        adv.ProcessTrace(h_arr, 1, None, None)   # blocks until CloseTrace

    def _stop_session(self) -> None:
        adv = ctypes.windll.advapi32
        if self._trace.value != _INVALID_THANDLE:
            adv.CloseTrace(self._trace)
        if self._session.value:
            xtra = (len(_ETW_SESSION_NAME) + 1) * 2
            bsz  = ctypes.sizeof(_EVENT_TRACE_PROPERTIES) + xtra
            buf  = (ctypes.c_byte * bsz)()
            p    = ctypes.cast(buf, ctypes.POINTER(_EVENT_TRACE_PROPERTIES)).contents
            p.Wnode.BufferSize = bsz
            adv.ControlTraceW(self._session, None,
                              ctypes.cast(buf, ctypes.c_void_p), _ETC_STOP)


# ── HWiNFO full scan — fans ───────────────────────────────────────────────────
_FAN_SKIP_LABELS = frozenset({
    "cpu fan", "chassis fan", "pump",    # add custom skips here if needed
})



# ── LHM shared update — call once per read cycle before any reader ────────────
def _lhm_update():
    """Traverse the full hardware tree via UpdateVisitor (canonical LHM pattern).
    Must be called once before _lhm_read_cpu / _lhm_read_gpu / _lhm_read_extra."""
    if _lhm_computer is not None and _lhm_visitor is not None:
        _lhm_computer.Accept(_lhm_visitor)


# ── LHM CPU reader ────────────────────────────────────────────────────────────
def _lhm_read_cpu(computer):
    """Walk LHM hardware tree → (tdie_temp, package_power, ccd_string).
    Assumes _lhm_update() has already been called this cycle."""
    temp  = 0.0
    power = 0.0
    ccds  = []

    for hw in computer.Hardware:
        if "Cpu" not in str(hw.HardwareType):
            continue

        for sensor in hw.Sensors:
            name = sensor.Name.lower()
            st   = str(sensor.SensorType)
            val  = float(sensor.Value) if sensor.Value is not None else 0.0

            if "Temperature" in st:
                if any(k in name for k in ("tdie", "tctl", "core (tdie)", "package")):
                    if val > temp:
                        temp = val
                elif "ccd" in name and val > 0:
                    ccds.append(f"CCD{name[-1] if name[-1].isdigit() else ''}: {val:.0f}°C")
                elif "core #" in name and val > 0:
                    if temp == 0:
                        temp = val
                    else:
                        temp = max(temp, val)

            elif "Power" in st and "package" in name:
                power = val

        for sub in hw.SubHardware:
            for sensor in sub.Sensors:
                name = sensor.Name.lower()
                st   = str(sensor.SensorType)
                val  = float(sensor.Value) if sensor.Value is not None else 0.0
                if "Temperature" in st and val > 0:
                    if "ccd" in name:
                        ccds.append(f"CCD: {val:.0f}°C")
                    elif temp == 0:
                        temp = val

    return temp, power, ("  ".join(ccds) if ccds else "—")


# ── LHM GPU reader (AMD / Intel fallback when NVML unavailable) ───────────────
def _lhm_read_gpu(computer):
    """Walk LHM hardware tree → (load%, temp°C, vram_used_MB, vram_total_MB, power_W, core_mhz, mem_mhz).
    Assumes _lhm_update() has already been called this cycle."""
    load = 0.0; temp = 0.0; vram_used = 0.0; vram_total = 0.0
    power = 0.0; core_mhz = 0.0; mem_mhz = 0.0

    for hw in computer.Hardware:
        ht = str(hw.HardwareType)
        if "Gpu" not in ht and "Amd" not in ht and "Nvidia" not in ht and "Intel" not in ht:
            continue
        if "Cpu" in ht:
            continue   # skip integrated CPU entries that share the type name

        for sensor in hw.Sensors:
            name_l = sensor.Name.lower()
            st     = str(sensor.SensorType)
            val    = float(sensor.Value) if sensor.Value is not None else 0.0

            if "Temperature" in st and val > 0:
                if temp == 0 or "core" in name_l or "gpu" in name_l:
                    temp = val

            elif "Load" in st and val >= 0:
                # Prefer "GPU Core" / "D3D 3D" over memory/video-engine loads
                if "core" in name_l or "3d" in name_l:
                    if load == 0:
                        load = val
                elif load == 0 and "gpu" in name_l:
                    load = val

            elif "Clock" in st and val > 0:
                if ("core" in name_l or "shader" in name_l) and core_mhz == 0:
                    core_mhz = val
                elif "memory" in name_l and mem_mhz == 0:
                    mem_mhz = val

            elif "Power" in st and val > 0 and power == 0:
                power = val

            elif ("SmallData" in st or "Data" in st) and val >= 0:
                if "used" in name_l and vram_used == 0:
                    vram_used = val * 1024   # LHM reports GB → convert to MB
                elif ("total" in name_l or "size" in name_l) and vram_total == 0:
                    vram_total = val * 1024

        # Sub-hardware (some AMD cards expose sensors here)
        # No sub.Update() needed — visitor already traversed sub-hardware
        for sub in hw.SubHardware:
            for sensor in sub.Sensors:
                name_l = sensor.Name.lower()
                st     = str(sensor.SensorType)
                val    = float(sensor.Value) if sensor.Value is not None else 0.0
                if "Temperature" in st and val > 0 and temp == 0:
                    temp = val
                elif "Load" in st and val >= 0 and load == 0:
                    if "core" in name_l or "3d" in name_l or "gpu" in name_l:
                        load = val

    return load, temp, vram_used, vram_total, power, core_mhz, mem_mhz


# ── LHM extra-sensor reader — controllers & mobo (Aquaero, Octo, T_Sensor) ───
def _lhm_read_extra(computer) -> list:
    """Return [(label, value, unit_str), ...] for water-cooling / controller
    sensors found in the LHM hardware tree (IsControllerEnabled +
    IsMotherboardEnabled must be True).
    Assumes _lhm_update() has already been called this cycle."""
    results  = []
    seen     = set()

    for hw in computer.Hardware:
        ht     = str(hw.HardwareType)
        hw_lbl = hw.Name.lower()

        # Only process controller and motherboard hardware
        is_ctrl = "Controller" in ht
        is_mobo = "Motherboard" in ht or "SuperIO" in ht

        if not (is_ctrl or is_mobo):
            continue

        nodes = [hw] + list(hw.SubHardware)
        for node in nodes:
            for sensor in node.Sensors:
                name_l = sensor.Name.lower()
                full_l = f"{hw_lbl} {name_l}"
                st     = str(sensor.SensorType)
                val    = float(sensor.Value) if sensor.Value is not None else None

                if val is None:
                    continue

                # Temperature sensors matching the WC allowlist
                if "Temperature" in st and 0 < val < 150:
                    if any(k in full_l or k in name_l for k in _WC_LABELS):
                        lbl = f"{hw.Name} – {sensor.Name}"
                        if lbl not in seen:
                            results.append((lbl, round(val, 1), "°C"))
                            seen.add(lbl)

                # Flow rate sensors
                elif "Flow" in st and val > 0:
                    lbl = f"{hw.Name} – {sensor.Name}"
                    if lbl not in seen:
                        results.append((lbl, round(val, 1), "L/H"))
                        seen.add(lbl)

    return results
