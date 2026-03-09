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
import dataclasses
import collections
import ctypes
import ctypes.wintypes

import psutil

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
    Single sliding-window scan of HWiNFO shared memory.
    Populates: d.cpu_temp, d.fans, d.cpu_power, d.cpu_voltage,
               d.gpu_voltage, d.dimm_temps
    No ctypes structs needed — just mmap + struct.unpack.
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
        print(f"[HWiNFO] open failed: {ex}")
        return

    fans         = []
    cpu_temp     = 0.0
    cpu_power    = 0.0
    cpu_volt     = 0.0
    gpu_volt     = 0.0
    dimm_temps   = []
    seen_dimm    = []
    extra        = []          # extra sensors: (label, value, unit_str)
    extra_labels = set()       # dedup by label

    # ── DIMM temps: exact label search ────────────────────────────────────────
    dimm_search = b'SPD Hub Temperature'
    pos = 0
    while True:
        idx = data.find(dimm_search, pos)
        if idx == -1:
            break
        if not any(abs(idx - s) < 32 for s in seen_dimm):
            end = idx + 160 + 8
            if end <= len(data):
                val = _struct.unpack_from("<d", data, idx + 160)[0]
                if 1.0 < val < 120.0:
                    n = len(dimm_temps) + 1
                    dimm_temps.append((f"DIMM{n}", round(val, 1)))
                    seen_dimm.append(idx)
        pos = idx + 1

    # ── General sensor scan: CPU temp, fans, power, voltages ──────────────────
    # Labels are null-terminated strings in the raw data.
    # We search for each known label type directly rather than parsing the struct.
    # Confirmed offsets from live hex dump:
    #   label  @ byte 12 in each element (128 bytes)
    #   unit   @ byte 268 (16 bytes)
    #   value  @ byte 284 (float64)
    # We find labels by scanning for null-terminated strings and checking neighbours.

    _CPU_PWR  = ("cpu package power", "cpu ppt", "cpu power",
                 "package power", "cpu socket power", "cpu total power")
    _CPU_VOLT = ("cpu core voltage", "cpu vcore", "vcore",
                 "cpu vid", "core voltage", "cpu voltage")
    _GPU_VOLT = ("gpu core voltage", "gpu voltage", "gpu vcore",
                 "gpu vid", "gpu core volt")

    i = 0
    stride = 460   # confirmed element size
    while i < len(data) - stride:
        # Read label at offset 12 within element
        label_raw = data[i+12 : i+140]
        label     = label_raw.split(b'\x00')[0].decode("latin-1", errors="ignore").strip()
        if not label:
            i += 4
            continue

        label_l = label.lower()
        unit    = data[i+268 : i+284].split(b'\x00')[0].decode("latin-1", errors="ignore").strip().lower()
        try:
            val = _struct.unpack_from("<d", data, i + 284)[0]
        except Exception:
            i += 4
            continue

        # CPU temperature — first match wins; everything else that's a valid
        # temp and isn't GPU or DIMM goes to extra_sensors.
        if unit in ("", "c", "\xb0c", "°c") and 0 < val < 150:
            if cpu_temp == 0.0 and any(k in label_l for k in _CPU_TEMP_LABELS):
                cpu_temp = val
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
        elif unit == "w" and cpu_power == 0.0 and 0 < val < 1000:
            if any(label_l == p or label_l.startswith(p) for p in _CPU_PWR):
                cpu_power = val

        # Voltages
        elif unit == "v" and 0.1 < val < 3.0:
            if cpu_volt == 0.0 and any(label_l == p or label_l.startswith(p) for p in _CPU_VOLT):
                cpu_volt = val
            elif gpu_volt == 0.0 and any(label_l == p or label_l.startswith(p) for p in _GPU_VOLT):
                gpu_volt = val

        # Flow rates — water cooling controllers (Aquaero, Octo, mobo headers)
        elif unit in ("l/h", "l/min", "lpm") and 0 < val < 500:
            if label not in extra_labels:
                extra.append((label, round(val, 2), unit.upper()))
                extra_labels.add(label)

        i += stride   # jump a full element at a time

    # ── Write results ──────────────────────────────────────────────────────────
    if cpu_temp  > 0: d.cpu_temp   = cpu_temp
    if cpu_power > 0: d.cpu_power  = cpu_power
    if cpu_volt  > 0: d.cpu_voltage = cpu_volt
    if gpu_volt  > 0: d.gpu_voltage = gpu_volt
    if dimm_temps:    d.dimm_temps  = dimm_temps
    d.extra_sensors = extra
    fans.sort(key=lambda x: x[0].lower())
    d.fans = fans

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
            print("[LHM] DLL not found — CPU temp will use WMI fallback")
            return

        print(f"[LHM] Loading: {dll_path}")
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
        print(f"[LHM] Initialised OK  "
              f"(GPU={comp.IsGpuEnabled}  "
              f"Mobo={comp.IsMotherboardEnabled}  "
              f"Controller={comp.IsControllerEnabled})")
    except Exception as ex:
        print(f"[LHM] Init failed: {ex}")

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
        print("[WMI] Thermal zone fallback ready")
    except Exception as ex:
        print(f"[WMI] Not available: {ex}")

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
    gpu_fan_rpm:  float = 0.0
    gpu_voltage:  float = 0.0   # GPU core voltage in volts
    cpu_voltage:  float = 0.0   # CPU core voltage in volts
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
        print(f"[Profile] CPU detect error: {ex}")

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
            print(f"[Profile] GPU detect error: {ex}")

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
                print(f"[Profile] Non-NVIDIA GPU detected via WMI: {prof.gpu_name}")
                break
        except Exception as ex:
            print(f"[Profile] WMI GPU detect error: {ex}")

    # Fan RPM colour threshold: reasonable for most systems
    prof.fan_max_rpm = 3000

    print(f"[Profile] CPU: {prof.cpu_name} | {prof.cpu_cores}c/{prof.cpu_threads}t | TDP {prof.cpu_tdp}W")
    print(f"[Profile] GPU: {prof.gpu_name} | TDP {prof.gpu_tdp}W | VRAM {prof.gpu_mem_total/1024:.0f}GB")
    return prof


# Detect hardware once at import time
HW_PROFILE: HardwareProfile = _detect_hardware()


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
                print(f"[LHM extra] {ex}")
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
                print(f"[LHM] read error: {ex}")

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
                print(f"[WMI] read error: {ex}")

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
                print(f"[LHM GPU] read error: {ex}")

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
            print(f"[NVML] read error: {ex}")


    # ── Network ───────────────────────────────────────────────────────────────
    def __init__(self):
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
            print(f"[disk_io] {ex}")

    # ── FPS ───────────────────────────────────────────────────────────────────
    def _read_fps(self, d: SensorData):
        """
        Show FPS estimate ONLY when a real fullscreen game is running.
        Uses GPU utilisation to estimate — scales with detected refresh rate.
        """
        if not _is_game_running():
            d.fps = -1.0
            return

        # Try to get the monitor refresh rate dynamically
        try:
            import ctypes
            user32  = ctypes.windll.user32
            dc      = user32.GetDC(0)
            gdi32   = ctypes.windll.gdi32
            refresh = gdi32.GetDeviceCaps(dc, 116)   # VREFRESH = 116
            user32.ReleaseDC(0, dc)
            if refresh < 30 or refresh > 500:
                refresh = 60
        except Exception:
            refresh = 60

        d.fps = max(1.0, refresh * (d.gpu_load / 100.0))


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
