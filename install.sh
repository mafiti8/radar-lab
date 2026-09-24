#!/usr/bin/env bash
# Radar Lab installer. Run from inside a cloned copy of this repo:
#   git clone <repo-url> radar-lab && cd radar-lab && ./install.sh
#
# Idempotent -- safe to re-run (reuses an existing .venv/.env instead of
# clobbering them).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "Radar Lab installer"
echo "===================="
echo ""

# ---------------------------------------------------------------------
# Platform check -- the dependency stack (Py-ART/numpy/scipy/MetPy) ships
# as compiled wheels that are Linux x86_64 only. Fail loudly and early
# instead of letting pip produce a wall of confusing build errors.
# ---------------------------------------------------------------------
if [[ "$(uname -s)" != "Linux" ]]; then
  echo "ERROR: Radar Lab currently only supports Linux." >&2
  echo "Windows: use WSL2 with a Linux distro inside it. macOS: not supported yet." >&2
  exit 1
fi
if [[ "$(uname -m)" != "x86_64" ]]; then
  echo "WARNING: detected architecture $(uname -m), not x86_64." >&2
  echo "Compiled dependency wheels (numpy/scipy/Py-ART) may not have prebuilt" >&2
  echo "packages for this architecture -- pip install below may fail or fall" >&2
  echo "back to a slow from-source build. Continuing anyway." >&2
fi

# ---------------------------------------------------------------------
# Python check
# ---------------------------------------------------------------------
PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "ERROR: $PYTHON_BIN not found. Install Python 3.10+ first." >&2
  exit 1
fi
PY_VERSION="$("$PYTHON_BIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
PY_OK="$("$PYTHON_BIN" -c 'import sys; print(1 if sys.version_info >= (3, 10) else 0)')"
if [[ "$PY_OK" != "1" ]]; then
  echo "ERROR: found Python $PY_VERSION, need 3.10+." >&2
  exit 1
fi
echo "Using Python $PY_VERSION ($PYTHON_BIN)"

if ! "$PYTHON_BIN" -c 'import venv' >/dev/null 2>&1; then
  echo "ERROR: the 'venv' module isn't available for $PYTHON_BIN." >&2
  echo "On Debian/Ubuntu: sudo apt install python3-venv" >&2
  exit 1
fi

# ---------------------------------------------------------------------
# Virtual environment + dependencies
# ---------------------------------------------------------------------
if [[ ! -d .venv ]]; then
  echo "Creating virtual environment..."
  "$PYTHON_BIN" -m venv .venv
else
  echo "Reusing existing .venv"
fi

echo "Installing dependencies (numpy/scipy/Py-ART/MetPy -- can take a few minutes)..."
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r bin/requirements.txt -q
# requirements-mosaic.txt (pygrib, for the national radar mosaic) is kept
# separate from the base requirements because pygrib has no Windows pip
# wheels -- but this script has already gated on Linux above, where pip
# installs it fine, so it's still installed by default here to keep the
# existing default Linux experience (mosaic works out of the box) intact.
.venv/bin/pip install -r bin/requirements-mosaic.txt -q
echo "Dependencies installed."
echo ""

# ---------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------
if [[ ! -f .env ]]; then
  cp .env.example .env
  SITE_INPUT="KVWX"
  if [[ -t 0 ]]; then
    read -rp "NEXRAD site ID to watch (4-letter, e.g. KVWX) [KVWX]: " INPUT || true
    SITE_INPUT="${INPUT:-KVWX}"
  fi
  SITE_INPUT="$(echo "$SITE_INPUT" | tr '[:lower:]' '[:upper:]')"
  sed -i "s/^RADAR_LAB_SITE=.*/RADAR_LAB_SITE=${SITE_INPUT}/" .env
  echo "Configured site: $SITE_INPUT (edit .env any time to change it)"
else
  echo ".env already exists, leaving it as-is."
fi
echo ""

# ---------------------------------------------------------------------
# Optional systemd --user service
# ---------------------------------------------------------------------
INSTALL_SERVICE="n"
if command -v systemctl >/dev/null 2>&1 && [[ -t 0 ]]; then
  read -rp "Install as a systemd --user service (auto-start)? [y/N]: " INSTALL_SERVICE || true
fi
if [[ "$INSTALL_SERVICE" =~ ^[Yy]$ ]]; then
  mkdir -p "$HOME/.config/systemd/user"
  cp radar-lab.service.template "$HOME/.config/systemd/user/radar-lab.service"
  systemctl --user daemon-reload
  systemctl --user enable --now radar-lab
  echo "Installed and started. Check status with: systemctl --user status radar-lab"
else
  echo "Skipped systemd service."
fi

echo ""
echo "Done."
PORT="$(grep -oP '(?<=RADAR_LAB_PORT=).*' .env 2>/dev/null || echo 8297)"
if [[ "$INSTALL_SERVICE" =~ ^[Yy]$ ]]; then
  echo "Running as a service -- open http://localhost:${PORT}/"
else
  echo "Start it with:  .venv/bin/python bin/radar_lab.py"
  echo "Then open:      http://localhost:${PORT}/"
fi
