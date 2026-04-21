#!/usr/bin/env bash
# GPU Fan Controller – Installer
# Usage: curl -fsSL https://raw.githubusercontent.com/mreschka/fan-controller-backplane/main/install.sh | sudo bash
#
# What this script does:
#   1. Checks prerequisites (python3, pip3, pyserial)
#   2. Installs the Python daemon to /opt/gpu-fan-control/
#   3. Installs the config template to /etc/gpu-fan-control.toml (only if not already present)
#   4. Installs and enables the systemd service
#   5. Adds the calling user to the dialout group

set -euo pipefail

REPO_URL="https://raw.githubusercontent.com/mreschka/fan-controller-backplane/main"
INSTALL_DIR="/opt/gpu-fan-control"
CONFIG_FILE="/etc/gpu-fan-control.toml"
SERVICE_FILE="/etc/systemd/system/gpu-fan-control.service"
SERVICE_NAME="gpu-fan-control.service"

# --- Helper ---
info()  { echo "  [INFO]  $*"; }
warn()  { echo "  [WARN]  $*" >&2; }
error() { echo "  [ERROR] $*" >&2; exit 1; }

# --- Root check ---
if [[ $EUID -ne 0 ]]; then
    error "This installer must be run as root (use sudo)."
fi

# Determine the user who invoked sudo (to add to dialout)
REAL_USER="${SUDO_USER:-}"

echo ""
echo "=== GPU Fan Controller Installer ==="
echo ""

# --- Dependency checks ---
info "Checking dependencies..."

if ! command -v python3 &>/dev/null; then
    info "Installing python3..."
    apt-get install -y python3 python3-pip
fi

PYTHON_VERSION=$(python3 -c "import sys; print(sys.version_info.minor)")
if [[ "$PYTHON_VERSION" -lt 11 ]]; then
    error "Python 3.11 or newer is required (found 3.${PYTHON_VERSION})."
fi

if ! python3 -c "import serial" &>/dev/null; then
    info "Installing pyserial..."
    pip3 install --quiet pyserial
fi

if ! command -v nvidia-smi &>/dev/null; then
    warn "nvidia-smi not found. The daemon will run in fallback mode (fixed fan speed)."
fi

# --- Install daemon ---
info "Installing daemon to ${INSTALL_DIR}..."
mkdir -p "${INSTALL_DIR}"

if command -v curl &>/dev/null; then
    curl -fsSL "${REPO_URL}/gpu_fan_control_advanced.py" -o "${INSTALL_DIR}/gpu_fan_control_advanced.py"
elif command -v wget &>/dev/null; then
    wget -qO "${INSTALL_DIR}/gpu_fan_control_advanced.py" "${REPO_URL}/gpu_fan_control_advanced.py"
else
    error "Neither curl nor wget found. Cannot download files."
fi

chmod 755 "${INSTALL_DIR}/gpu_fan_control_advanced.py"
info "Daemon installed."

# --- Install config (only if not already present) ---
if [[ -f "${CONFIG_FILE}" ]]; then
    warn "Config file ${CONFIG_FILE} already exists – not overwriting. Edit it manually."
else
    info "Installing config template to ${CONFIG_FILE}..."
    if command -v curl &>/dev/null; then
        curl -fsSL "${REPO_URL}/gpu-fan-control.toml" -o "${CONFIG_FILE}"
    else
        wget -qO "${CONFIG_FILE}" "${REPO_URL}/gpu-fan-control.toml"
    fi
    chmod 644 "${CONFIG_FILE}"
    info "Config installed."
    echo ""
    warn "ACTION REQUIRED: Edit ${CONFIG_FILE} before starting the service!"
    warn "  Set 'port' to your Arduino's /dev/serial/by-id/... path."
    warn "  Set 'gpus' to the nvidia-smi indices of your GPUs."
    echo ""
fi

# --- Install systemd service ---
info "Installing systemd service..."

# Determine which user to run the service as
if [[ -n "$REAL_USER" ]]; then
    RUN_AS_USER="$REAL_USER"
else
    RUN_AS_USER="root"
fi

# Download service file and patch User= field
if command -v curl &>/dev/null; then
    curl -fsSL "${REPO_URL}/gpu-fan-control.service" -o "${SERVICE_FILE}"
else
    wget -qO "${SERVICE_FILE}" "${REPO_URL}/gpu-fan-control.service"
fi

sed -i "s/^User=.*/User=${RUN_AS_USER}/" "${SERVICE_FILE}"
chmod 644 "${SERVICE_FILE}"

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
info "Service installed and enabled (will start on next boot)."

# --- dialout group ---
if [[ -n "$REAL_USER" ]]; then
    if ! groups "$REAL_USER" | grep -q dialout; then
        info "Adding ${REAL_USER} to dialout group (required for serial port access)..."
        usermod -aG dialout "$REAL_USER"
        warn "Group change takes effect after ${REAL_USER} logs out and back in."
    else
        info "${REAL_USER} is already in dialout group."
    fi
fi

# --- Summary ---
echo ""
echo "=== Installation complete ==="
echo ""
echo "  Config:   ${CONFIG_FILE}"
echo "  Daemon:   ${INSTALL_DIR}/gpu_fan_control_advanced.py"
echo "  Service:  ${SERVICE_NAME} (enabled, not yet started)"
echo ""
echo "  Next steps:"
echo "  1. Edit ${CONFIG_FILE} – set Arduino port and GPU indices"
echo "  2. Flash the Arduino sketch (see README.md)"
if [[ -n "$REAL_USER" ]]; then
    echo "  3. Log out and back in as ${REAL_USER} (dialout group)"
    echo "  4. sudo systemctl start ${SERVICE_NAME}"
else
    echo "  3. sudo systemctl start ${SERVICE_NAME}"
fi
echo ""
echo "  Monitor:  sudo journalctl -u ${SERVICE_NAME} -f"
echo ""
