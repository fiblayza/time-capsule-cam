#!/usr/bin/env bash
# install.sh — time-capsule-cam
#
# One-liner install (run on the Pi after flashing the upstream image):
#   curl -fsSL https://raw.githubusercontent.com/fiblayza/time-capsule-cam/main/install.sh | bash
#
# Or after cloning manually:
#   bash /home/admin/time-capsule-cam/install.sh

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
REPO_URL="https://github.com/fiblayza/time-capsule-cam"
INSTALL_DIR="/home/admin/time-capsule-cam"
UPSTREAM_DIR="/home/admin/rotary-phone-audio-guestbook"
CONFIG_YAML="${UPSTREAM_DIR}/config.yaml"
SERVICE_NAME="video_recorder"

# ── Colours ───────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
log()  { echo -e "${GREEN}▶${NC} $*"; }
warn() { echo -e "${YELLOW}⚠${NC}  $*"; }
die()  { echo -e "${RED}✗${NC}  $*" >&2; exit 1; }
ok()   { echo -e "${GREEN}✓${NC} $*"; }

echo ""
echo "  time-capsule-cam installer"
echo "  ──────────────────────────"
echo ""

# ── 1. Check upstream image is present ───────────────────────────────────────
log "Checking upstream rotary-phone-audio-guestbook..."
[ -d "$UPSTREAM_DIR" ] \
    || die "Upstream repo not found at ${UPSTREAM_DIR}.\nFlash the image first: https://github.com/nickpourazima/rotary-phone-audio-guestbook/releases"
[ -f "$CONFIG_YAML" ] \
    || die "config.yaml not found at ${CONFIG_YAML}"
ok "Upstream found"

# ── 2. Clone or update our extension ─────────────────────────────────────────
if [ -d "${INSTALL_DIR}/.git" ]; then
    log "Updating existing installation..."
    git -C "$INSTALL_DIR" pull --ff-only
    ok "Updated to $(git -C "$INSTALL_DIR" rev-parse --short HEAD)"
else
    log "Cloning time-capsule-cam → ${INSTALL_DIR} ..."
    git clone "$REPO_URL" "$INSTALL_DIR"
    ok "Cloned"
fi

# ── 3. System dependencies ────────────────────────────────────────────────────
log "Installing ffmpeg (this may take a minute)..."
sudo apt-get install -y --no-install-recommends ffmpeg > /dev/null
ok "ffmpeg ready"

# ── 4. Python dependencies ────────────────────────────────────────────────────
log "Installing Python dependencies..."
pip3 install --break-system-packages --quiet -r "${INSTALL_DIR}/requirements.txt"
ok "Python deps ready"

# ── 5. Add video: section to config.yaml (idempotent) ─────────────────────────
if grep -q "^video:" "$CONFIG_YAML"; then
    warn "video: section already present in config.yaml — skipping"
else
    log "Adding video: section to config.yaml..."
    cat >> "$CONFIG_YAML" << 'EOF'

# ── time-capsule-cam additions ────────────────────────────────────────────────
video:
  enabled: true
  backend: usb           # "usb" or "picamera"
  device: /dev/video0    # only for usb backend
  resolution: "1280x720"
  fps: 25
  min_duration_seconds: 2
  codec: libx264
  preset: ultrafast
  led_gpio: 17             # BCM pin for recording LED; 0 to disable
EOF
    ok "config.yaml updated"
fi

# ── 6. Create status.json if it doesn't exist ────────────────────────────────
STATUS_JSON="${INSTALL_DIR}/status.json"
if [ ! -f "$STATUS_JSON" ]; then
    log "Creating status.json..."
    echo '{"status": "idle"}' > "$STATUS_JSON"
    ok "status.json created"
fi

# ── 8. Patch upstream webserver ───────────────────────────────────────────────
log "Patching upstream webserver/server.py..."
python3 "${INSTALL_DIR}/webserver_patch/apply_patch.py"
ok "Webserver patched"

# ── 9. Install and start systemd service ──────────────────────────────────────
log "Installing systemd service..."
sudo cp "${INSTALL_DIR}/video_recorder.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}.service"
sudo systemctl restart "${SERVICE_NAME}.service"
ok "video_recorder.service active"

# ── 10. Restart upstream webserver ────────────────────────────────────────────
log "Restarting audioGuestBookWebServer..."
sudo systemctl restart audioGuestBookWebServer.service
ok "Webserver restarted"

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "  ✅  Installation complete!"
echo ""
echo "  Check status:   systemctl status ${SERVICE_NAME}.service"
echo "  Live logs:      journalctl -fu ${SERVICE_NAME}.service"
echo "  Current state:  cat ${INSTALL_DIR}/status.json"
echo ""
