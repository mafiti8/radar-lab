# PyInstaller spec for the standalone desktop build (bin/radar_lab_app.py).
# Build with:  pyinstaller bin/radar_lab.spec --distpath dist --workpath build
#
# Written 2026-09-25 from a Linux dev box that can't actually run PyInstaller
# for a Windows target (it has to run ON Windows -- cross-compiling a build
# this dependency-heavy isn't realistic). This is a well-informed first
# draft, not a verified-working build -- real iteration on the actual
# Windows machine should be expected, not treated as something going wrong.
# collect_all() is used liberally here (favors a bigger bundle over a
# missing-module crash at runtime) for exactly the packages independently
# confirmed to need it for PyInstaller bundling: netCDF4 (needs its C
# libraries), pyproj (needs its proj.db data file, a real documented
# PyInstaller gotcha for anything MetPy-adjacent), scipy (MetPy dependency,
# has known hidden-import issues), pyart and metpy themselves (real
# scientific packages with lazy/plugin-style imports PyInstaller's static
# analysis can miss), and webview (pywebview -- has its own PyInstaller
# hook, but collect_all is cheap insurance here too).
#
# If the build fails with a missing-module ImportError at runtime (not at
# build time -- PyInstaller often doesn't complain until you actually run
# the .exe and hit the import), that's the normal failure mode for this
# kind of stack: add the missing package to the COLLECT_ALL list below, or
# a specific missing submodule to hiddenimports, and rebuild.

from PyInstaller.utils.hooks import collect_all

COLLECT_ALL = ["pyart", "metpy", "netCDF4", "pyproj", "scipy", "webview", "PIL"]

datas = [("web", "web")]
binaries = []
hiddenimports = []

for pkg in COLLECT_ALL:
    pkg_datas, pkg_binaries, pkg_hiddenimports = collect_all(pkg)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hiddenimports

a = Analysis(
    ["radar_lab_app.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="RadarLab",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    # True for the first real build -- keeps a console window open so
    # real startup errors (missing imports, etc.) are actually visible
    # instead of the app just silently failing to open. Flip to False
    # once a build is confirmed working end-to-end, for the real
    # double-click-no-terminal experience this is ultimately meant to have.
    console=True,
    icon=None,
)
