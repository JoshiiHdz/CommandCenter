# Command Center

A real-time system monitor for Windows — CPU, GPU, RAM, fans, disk I/O, and water-cooling sensors, all in one always-on-top panel.

> **Copyright © 2025 Josh Stanley. All rights reserved.**

---

## Features

- CPU usage, frequency, and temperature (per-core)
- NVIDIA GPU — load, temp, VRAM, power draw, clocks (via NVML)
- AMD / Intel GPU — load, temp, VRAM, power draw, clocks (via LibreHardwareMonitor)
- RAM usage with slot breakdown
- Disk usage and real-time I/O
- Fan speeds with custom rename support
- Water-cooling sensors — Aquaero, Octo, coolant headers (via HWiNFO64 shared memory or LHM)
- Resizable UI with auto-scaling fonts
- Always-on-top compact panel

---

## Requirements

### Always required
- Windows 10 / 11 (64-bit)
- Run as **Administrator** — UAC prompt is embedded in the EXE

### Required for CPU temps + AMD/Intel GPU metrics
- [.NET 6 Desktop Runtime (x64)](https://dotnet.microsoft.com/en-us/download/dotnet/6.0)

### Optional — water-cooling / controller sensors
- [HWiNFO64](https://www.hwinfo.com/download/) running with **Shared Memory Support** enabled
  `HWiNFO64 → Settings → Shared Memory Support ✓`

### Optional — NVIDIA GPU
- Any NVIDIA GPU with a current driver (NVML is bundled)

---

## Download

Go to the [Releases](../../releases) page and download `CommandCenter.exe`.
No Python or pip install needed — the EXE is fully self-contained.

---

## File Locations

All user data is stored in `%APPDATA%\CommandCenter\` (created automatically on first run).

| File | Path |
|---|---|
| Settings | `%APPDATA%\CommandCenter\settings.ini` |
| Fan names | `%APPDATA%\CommandCenter\fan_names.ini` |
| Log file | `%APPDATA%\CommandCenter\CommandCenter.log` |

Paste `%APPDATA%\CommandCenter` directly into Windows Explorer to open the folder.

---

## What Works Without .NET 6

| Feature | Without .NET 6 |
|---|---|
| CPU usage & frequency | Works |
| RAM, disk, I/O | Works |
| Fan speeds | Works |
| NVIDIA GPU (full) | Works |
| **CPU temperatures** | **Shows 0** |
| **AMD / Intel GPU metrics** | **Shows 0** |

---

## Building from Source

\`\`\`bash
pip install PyQt6 psutil pynvml pythonnet pyinstaller pillow pywin32
python build.py
# Output: dist/CommandCenter.exe
\`\`\`

`LibreHardwareMonitorLib.dll` must be in the project root before building to enable CPU temps and AMD GPU support. Download it from the [LibreHardwareMonitor releases](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases).

---

## Troubleshooting

| Problem | Fix |
|---|---|
| App won't start | Run as Administrator |
| CPU temp shows 0 | Install .NET 6 Desktop Runtime |
| AMD GPU shows 0 | Install .NET 6 Desktop Runtime |
| Water-cooling blank | Start HWiNFO64 with Shared Memory Support enabled |
| Other issues | Check `%APPDATA%\CommandCenter\CommandCenter.log` |

---

## Uninstall

1. Delete `CommandCenter.exe`
2. Delete `%APPDATA%\CommandCenter\`

No registry entries are created.
