#!/usr/bin/env bash
# Installer for ktrackball — run with: sudo ./install.sh
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="/opt/ktrackball"
CONF_DIR="/etc/ktrackball"
UNIT="/etc/systemd/system/ktrackball.service"

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root:  sudo ./install.sh" >&2
  exit 1
fi

echo "==> Installing dependencies (if missing)"
NEED=()
python3 -c "import evdev" 2>/dev/null || NEED+=(python3-evdev)
python3 -c "import gi; gi.require_version('Gtk','3.0')" 2>/dev/null || NEED+=(python3-gi gir1.2-gtk-3.0)
if [[ ${#NEED[@]} -gt 0 ]]; then
  apt-get update -qq
  apt-get install -y "${NEED[@]}"
fi

echo "==> Ensuring uinput is loaded and persistent"
modprobe uinput || true
echo uinput >/etc/modules-load.d/uinput.conf

echo "==> Installing application to $APP_DIR"
install -d "$APP_DIR"
install -m 0755 "$SRC_DIR/trackball_mapper.py" "$APP_DIR/trackball_mapper.py"
install -m 0755 "$SRC_DIR/ktrackball_gui.py" "$APP_DIR/ktrackball_gui.py"

echo "==> Installing GUI launcher (app menu entry)"
install -m 0644 "$SRC_DIR/ktrackball.desktop" /usr/share/applications/ktrackball.desktop

echo "==> Installing config to $CONF_DIR (existing config is preserved)"
install -d "$CONF_DIR"
if [[ -f "$CONF_DIR/config.toml" ]]; then
  echo "    $CONF_DIR/config.toml already exists — leaving it untouched."
  install -m 0644 "$SRC_DIR/config.toml" "$CONF_DIR/config.toml.default"
else
  install -m 0644 "$SRC_DIR/config.toml" "$CONF_DIR/config.toml"
fi

echo "==> Installing systemd unit"
install -m 0644 "$SRC_DIR/ktrackball.service" "$UNIT"
systemctl daemon-reload
systemctl enable --now ktrackball.service

echo
echo "Done. Service status:"
systemctl --no-pager --full status ktrackball.service | head -n 12 || true
echo
echo "Next steps:"
echo "  • Confirm which physical buttons emit which codes:"
echo "      sudo systemctl stop ktrackball       # release the device first"
echo "      sudo python3 $APP_DIR/trackball_mapper.py learn --config $CONF_DIR/config.toml"
echo "  • Edit the [buttons] map:   sudoedit $CONF_DIR/config.toml"
echo "  • Apply changes:            sudo systemctl restart ktrackball"
echo "  • Watch logs:               journalctl -u ktrackball -f"
