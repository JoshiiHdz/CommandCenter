================================================================================
  Command Center  —  System Monitor
  Copyright (c) 2025 Josh Stanley.  All rights reserved.
================================================================================

REQUIREMENTS
------------
The EXE is self-contained.  No Python installation needed.

  REQUIRED (always):
    • Windows 10 / 11 (64-bit)
    • Run as Administrator
      → Windows will prompt automatically (UAC embedded in EXE)

  REQUIRED for CPU temperatures + AMD/Intel GPU metrics:
    • Microsoft .NET 6 Desktop Runtime (x64)
      Download: https://dotnet.microsoft.com/en-us/download/dotnet/6.0
      (Choose ".NET Desktop Runtime 6.x.x" — Windows x64 installer)

  OPTIONAL — real-time water-cooling / controller sensors:
    • HWiNFO64 running in the background with Shared Memory Support enabled
      Download: https://www.hwinfo.com/download/
      Enable:   HWiNFO64 → Settings → Shared Memory Support ✓

  OPTIONAL — NVIDIA GPU (full metrics):
    • NVIDIA GPU with a current driver installed
      (pynvml is bundled; driver auto-detected)

  NOT required:
    • Python, pip, or any other runtime
    • Visual C++ Redistributables
    • Any manual DLL installation

--------------------------------------------------------------------------------

FILE LOCATIONS  (created automatically on first run)
-----------------------------------------------------
All user data is stored in your AppData folder so the app works even
when installed in protected directories (e.g. C:\Program Files\).

  Settings file:
    %APPDATA%\CommandCenter\settings.ini

  Fan custom names:
    %APPDATA%\CommandCenter\fan_names.ini

  Log file:
    %APPDATA%\CommandCenter\CommandCenter.log

  Full path example (replace <YourUsername>):
    C:\Users\<YourUsername>\AppData\Roaming\CommandCenter\

  Tip: Paste  %APPDATA%\CommandCenter  directly into Windows Explorer
       address bar to open the folder.

--------------------------------------------------------------------------------

WHAT WORKS WITHOUT .NET 6
--------------------------
  ✓  CPU usage & frequency
  ✓  RAM usage
  ✓  Disk usage & I/O
  ✓  Fan speeds (via HWiNFO or direct)
  ✓  NVIDIA GPU (load, temp, VRAM, power, clocks)
  ✗  CPU temperatures          ← needs .NET 6
  ✗  AMD / Intel GPU metrics   ← needs .NET 6

--------------------------------------------------------------------------------

TROUBLESHOOTING
---------------
  • App won't start      → Make sure you are running as Administrator
  • CPU temp shows 0     → Install .NET 6 Desktop Runtime (see above)
  • AMD GPU shows 0      → Install .NET 6 Desktop Runtime (see above)
  • Water-cooling blank  → Start HWiNFO64 with Shared Memory Support enabled
  • Check the log for details:
      %APPDATA%\CommandCenter\CommandCenter.log

--------------------------------------------------------------------------------

UNINSTALL
---------
  1. Delete CommandCenter.exe (wherever you placed it)
  2. Delete the data folder:  %APPDATA%\CommandCenter\

  No registry entries are created by the application.

================================================================================
