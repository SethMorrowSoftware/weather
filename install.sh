#!/usr/bin/env bash
#
# install.sh -- one-command setup for the Precipitation -> MQTT controller.
#
# On a fresh Debian/Ubuntu box this:
#   1. installs Python + Mosquitto (the MQTT broker),
#   2. creates a service account and an isolated virtualenv,
#   3. runs an interactive wizard to write config.yaml (first run only),
#   4. points Mosquitto at a local listener the controller can use,
#   5. installs + starts the monitor and web-UI as systemd services.
#
# Re-running is safe: it never clobbers an existing config and only creates
# things that are missing.
#
# Usage:
#   git clone <repo> && cd weather && sudo ./install.sh
#
# Override defaults with env vars, e.g.:
#   sudo INSTALL_DIR=/srv/weather SERVICE_USER=weatherbot ./install.sh
#
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/weather-mqtt}"
SERVICE_USER="${SERVICE_USER:-weather}"
SETUP_MOSQUITTO="${SETUP_MOSQUITTO:-1}"   # set 0 to skip broker install/config
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

c_g() { printf '\033[1;32m%s\033[0m\n' "$*"; }   # green
c_y() { printf '\033[1;33m%s\033[0m\n' "$*"; }   # yellow
c_r() { printf '\033[1;31m%s\033[0m\n' "$*"; }   # red
step() { printf '\n\033[1;36m==>\033[0m \033[1m%s\033[0m\n' "$*"; }

if [ "$(id -u)" -ne 0 ]; then
  c_r "Please run with sudo:  sudo ./install.sh"
  exit 1
fi

if ! command -v apt-get >/dev/null 2>&1; then
  c_r "This installer targets Debian/Ubuntu (apt). For other systems, follow the"
  c_r "manual steps in README.md."
  exit 1
fi

# ---------------------------------------------------------------------------
step "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
PKGS="python3 python3-venv python3-pip"
[ "$SETUP_MOSQUITTO" = "1" ] && PKGS="$PKGS mosquitto mosquitto-clients"
apt-get update -qq
apt-get install -y -qq $PKGS
c_g "Packages installed."

# ---------------------------------------------------------------------------
step "Creating service account '$SERVICE_USER'"
if id "$SERVICE_USER" >/dev/null 2>&1; then
  echo "User already exists; skipping."
else
  useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
  c_g "Created system user '$SERVICE_USER'."
fi

# ---------------------------------------------------------------------------
step "Installing application to $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
for f in weather_mqtt.py webui.py setup_wizard.py requirements.txt; do
  install -m 0644 "$SRC_DIR/$f" "$INSTALL_DIR/$f"
done
# Ship the demo and example config too (handy reference); never overwrite a live config.
cp -r "$SRC_DIR/demo" "$INSTALL_DIR/" 2>/dev/null || true
[ -f "$SRC_DIR/config.yaml" ] && install -m 0644 "$SRC_DIR/config.yaml" "$INSTALL_DIR/config.yaml.example"
c_g "Files copied."

step "Creating Python virtualenv + installing dependencies"
if [ ! -x "$INSTALL_DIR/venv/bin/python" ]; then
  python3 -m venv "$INSTALL_DIR/venv"
fi
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
c_g "Dependencies installed."

# ---------------------------------------------------------------------------
step "Configuration"
CONFIG="$INSTALL_DIR/config.yaml"
if [ -f "$CONFIG" ]; then
  c_g "Existing config.yaml found -- keeping it (not overwriting)."
elif [ -t 0 ]; then
  # Interactive terminal: run the wizard, writing straight to the install dir.
  "$INSTALL_DIR/venv/bin/python" "$INSTALL_DIR/setup_wizard.py" -o "$CONFIG"
else
  cp "$INSTALL_DIR/config.yaml.example" "$CONFIG"
  c_y "No terminal for the setup wizard -- copied the example config."
  c_y "EDIT $CONFIG (set your latitude/longitude and contact) before relying on it."
fi
# config.yaml can hold secrets (MQTT/web passwords, Slack + status tokens); keep
# it owner-only rather than the default world-readable 0644.
chmod 600 "$CONFIG" 2>/dev/null || true

# ---------------------------------------------------------------------------
if [ "$SETUP_MOSQUITTO" = "1" ]; then
  step "Configuring Mosquitto (local listener)"
  MOSQ_CONF="/etc/mosquitto/conf.d/weather-mqtt.conf"
  if [ -d /etc/mosquitto/conf.d ] && [ ! -f "$MOSQ_CONF" ]; then
    cat > "$MOSQ_CONF" <<'EOF'
# Added by weather-mqtt install.sh: a local listener the controller connects to.
# Anonymous access is fine because it only listens on localhost. To accept
# connections from other machines, add another `listener 1883 0.0.0.0` and set
# up authentication -- see the Mosquitto docs.
listener 1883 localhost
allow_anonymous true
EOF
    c_g "Wrote $MOSQ_CONF (localhost:1883, anonymous)."
  else
    echo "Leaving existing Mosquitto config untouched."
  fi
  systemctl enable --now mosquitto >/dev/null 2>&1 || true
  systemctl restart mosquitto || c_y "Could not restart mosquitto; check 'systemctl status mosquitto'."
  if command -v mosquitto_pub >/dev/null 2>&1; then
    if mosquitto_pub -h localhost -t weather-mqtt/installtest -m ok >/dev/null 2>&1; then
      c_g "Broker reachable on localhost:1883."
    else
      c_y "Could not publish a test message to localhost:1883 yet -- the monitor will retry."
    fi
  fi
fi

# ---------------------------------------------------------------------------
step "Installing systemd services"
# Render units from the repo's templates, substituting the chosen paths/user.
render_unit() {
  sed -e "s#/opt/weather-mqtt#${INSTALL_DIR}#g" \
      -e "s/^User=weather/User=${SERVICE_USER}/" "$1"
}
render_unit "$SRC_DIR/weather-mqtt.service"  > /etc/systemd/system/weather-mqtt.service
render_unit "$SRC_DIR/weather-webui.service" > /etc/systemd/system/weather-webui.service

# The service account needs to read everything and write cache/state files.
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

systemctl daemon-reload
systemctl enable --now weather-mqtt.service
systemctl enable --now weather-webui.service
c_g "Services installed and started."

# ---------------------------------------------------------------------------
PORT="$("$INSTALL_DIR/venv/bin/python" - "$CONFIG" <<'PY' 2>/dev/null || echo 8080
import sys, yaml
print((yaml.safe_load(open(sys.argv[1])).get("web") or {}).get("port", 8080))
PY
)"
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"; [ -z "$IP" ] && IP="<server-ip>"

step "Done!"
c_g "The controller and web UI are running as systemd services."
echo
echo "  Dashboard:   http://${IP}:${PORT}"
echo "  Monitor log: journalctl -u weather-mqtt -f"
echo "  Web UI log:  journalctl -u weather-webui -f"
echo "  Config:      ${CONFIG}   (edits to rules/thresholds apply next poll)"
echo
echo "  Restart after editing location/MQTT/web settings:"
echo "    sudo systemctl restart weather-mqtt weather-webui"
echo
