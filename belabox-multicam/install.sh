#!/bin/sh
# Install the multicam switcher on a BELABOX (Rock 5 / RK3588). Run as root:
#   sudo ./install.sh
set -e
cd "$(dirname "$0")"

if [ "$(id -u)" != 0 ]; then echo "Please run with sudo"; exit 1; fi

echo "== Installing Python GStreamer bindings"
apt-get install -y --no-install-recommends python3-gi gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0

for el in compositor audiomixer shmsink shmsrc jpegenc v4l2src alsasrc mpph265enc; do
  if ! gst-inspect-1.0 "$el" >/dev/null 2>&1; then
    echo "WARNING: GStreamer element '$el' not found - see README troubleshooting"
  fi
done

echo "== Copying files to /opt/belaswitch"
mkdir -p /opt/belaswitch/web
cp belaswitch.py /opt/belaswitch/
cp web/index.html /opt/belaswitch/web/
if [ ! -f /etc/belaswitch.json ]; then
  cp config.example.json /etc/belaswitch.json
  echo "   created /etc/belaswitch.json - edit it to list your cameras"
fi

echo "== Writing the belacoder pipeline"
python3 /opt/belaswitch/belaswitch.py --config /etc/belaswitch.json --print-pipeline >/dev/null
cp belaswitch.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now belaswitch
systemctl restart belaswitch
sleep 3

# belaUI reads its pipeline list only at startup
if systemctl list-unit-files | grep -q '^belaUI.service'; then
  echo "== Restarting belaUI so it sees the new pipeline"
  systemctl restart belaUI
fi

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "Done."
echo "  1. Switcher control page:  http://${IP:-<belabox-ip>}:8181/"
echo "  2. In belaUI, choose the pipeline 'custom/multicam_switcher' and start streaming."
echo "  Logs: journalctl -u belaswitch -f"
