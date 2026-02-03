#!/bin/bash
set -e

# Configuration
DISPLAY=${DISPLAY:-:99}
VNC_ENABLED=${VNC_ENABLED:-true}
NOVNC_ENABLED=${NOVNC_ENABLED:-true}
VNC_PASSWORD=${VNC_PASSWORD:-}
RECIPES_PATH=${RECIPES_PATH:-/recipes}
CONFIG_HOME=${CONFIG_HOME:-/config}

# Export XDG directories to use persistent config volume
export XDG_CONFIG_HOME="${CONFIG_HOME}/.config"
export XDG_DATA_HOME="${CONFIG_HOME}/.local/share"
export HOME="${CONFIG_HOME}"

# Create required directories
mkdir -p "${XDG_CONFIG_HOME}" "${XDG_DATA_HOME}"

echo "Starting Cook Desktop Sync Agent..."
echo "  Display: ${DISPLAY}"
echo "  VNC Enabled: ${VNC_ENABLED}"
echo "  Recipes Path: ${RECIPES_PATH}"
echo "  Config Home: ${CONFIG_HOME}"

# Start Xvfb (virtual framebuffer)
echo "Starting Xvfb..."
Xvfb ${DISPLAY} -screen 0 1024x768x24 &
XVFB_PID=$!
sleep 2

# Verify Xvfb is running
if ! kill -0 $XVFB_PID 2>/dev/null; then
    echo "ERROR: Xvfb failed to start"
    exit 1
fi
echo "Xvfb started (PID: $XVFB_PID)"

# Start D-Bus session (required for GTK apps)
echo "Starting D-Bus session..."
eval $(dbus-launch --sh-syntax)
export DBUS_SESSION_BUS_ADDRESS

# Start VNC server if enabled (for initial OTP login setup)
if [ "${VNC_ENABLED}" = "true" ]; then
    echo "Starting x11vnc server on port 5900..."
    if [ -n "${VNC_PASSWORD}" ]; then
        x11vnc -display ${DISPLAY} -forever -shared -rfbport 5900 -passwd "${VNC_PASSWORD}" &
    else
        echo "WARNING: VNC running without password (use for initial setup only)"
        x11vnc -display ${DISPLAY} -forever -shared -rfbport 5900 -nopw &
    fi
    VNC_PID=$!
    sleep 1
    echo "VNC server started (PID: $VNC_PID)"
    
    # Start noVNC web interface if enabled
    if [ "${NOVNC_ENABLED}" = "true" ]; then
        echo "Starting noVNC web interface on port 6080..."
        websockify --web=/usr/share/novnc 6080 localhost:5900 &
        NOVNC_PID=$!
        sleep 1
        echo "noVNC started (PID: $NOVNC_PID)"
        echo ""
        echo "=========================================="
        echo "WEB VNC ACCESS FOR INITIAL SETUP:"
        echo "  kubectl port-forward svc/cooklang-novnc 6080:6080"
        echo "  Then open in browser: http://localhost:6080/vnc.html"
        echo "=========================================="
        echo ""
    else
        echo ""
        echo "=========================================="
        echo "VNC ACCESS FOR INITIAL SETUP:"
        echo "  kubectl port-forward svc/cooklang-vnc 5900:5900"
        echo "  Then connect with VNC client to localhost:5900"
        echo "=========================================="
        echo ""
    fi
fi

# Wait for recipes directory to be ready
echo "Waiting for recipes directory..."
while [ ! -d "${RECIPES_PATH}" ]; do
    sleep 1
done
echo "Recipes directory ready: ${RECIPES_PATH}"

# Start Cook Desktop
echo "Starting Cook Desktop..."
echo "  If this is first run, connect via VNC to complete OTP login"

# Find the cook-desktop binary
COOK_DESKTOP_BIN="/usr/bin/cook-desktop"

if [ ! -x "${COOK_DESKTOP_BIN}" ]; then
    # Try which as fallback
    COOK_DESKTOP_BIN=$(which cook-desktop 2>/dev/null || echo "")
    if [ -z "${COOK_DESKTOP_BIN}" ] || [ ! -x "${COOK_DESKTOP_BIN}" ]; then
        echo "ERROR: cook-desktop binary not found"
        echo "Checking /usr/bin contents:"
        ls -la /usr/bin/cook* 2>/dev/null || echo "No cook* binaries found"
        exit 1
    fi
fi

echo "Using cook-desktop at: ${COOK_DESKTOP_BIN}"

# Run Cook Desktop
# The app should pick up RECIPES_PATH or be configured via its settings
exec "${COOK_DESKTOP_BIN}"
