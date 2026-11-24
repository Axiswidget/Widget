#!/bin/sh
# Install the multicam switcher on a BELABOX (Rock 5 / RK3588). Run as root:
#   sudo ./install.sh
set -e
cd "$(dirname "$0")"

if [ "$(id -u)" != 0 ]; then echo "Please run with sudo"; exit 1; fi

echo "== Checking Python GStreamer bindings"
if ! python3 -c "import gi; gi.require_version('Gst', '1.0'); from gi.repository import Gst" 2>/dev/null; then
  apt-get install -y --no-install-recommends python3-gi gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 || {
    echo "ERROR: could not install python3-gi / gir1.2-gstreamer-1.0. Connect the BELABOX to the internet and re-run."
    exit 1
  }
fi

for el in compositor audiomixer shmsink shmsrc jpegenc v4l2src alsasrc mpph265enc; do
  if ! gst-inspect-1.0 "$el" >/dev/null 2>&1; then
    echo "WARNING: GStreamer element '$el' not found - see README troubleshooting"
  fi
done

echo "== Copying files to /opt/belaswitch"
mkdir -p /opt/belaswitch/web
cp belaswitch.py /opt/belaswitch/
cp web/index.html /opt/belaswitch/web/
# Default: find USB cameras automatically. An unedited config from an
# earlier install (still containing REPLACE placeholders) is upgraded too.
if [ ! -f /etc/belaswitch.json ] || grep -q "REPLACE-with" /etc/belaswitch.json \
   || { grep -q '"cameras": "auto"' /etc/belaswitch.json && ! grep -q audio_source /etc/belaswitch.json; }; then
  [ -f /etc/belaswitch.json ] && cp /etc/belaswitch.json /etc/belaswitch.json.bak
  cat > /etc/belaswitch.json <<'JSON'
{
  "output": { "width": 1920, "height": 1080, "fps": 30 },
  "transition": { "type": "fade", "duration_ms": 600 },
  "cameras": "auto",
  "audio_source": "usb"
}
JSON
  echo "   /etc/belaswitch.json: cameras found automatically; sound from your USB audio device"
else
  echo "   keeping your existing /etc/belaswitch.json"
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
TSIP=$(command -v tailscale >/dev/null 2>&1 && tailscale ip -4 2>/dev/null | head -n1)
echo "  1. Switcher control page:  http://${IP:-<belabox-ip>}:8181/"
[ -n "$TSIP" ] && echo "     over Tailscale:         http://${TSIP}:8181/   (low-data previews)"
echo "  2. Plug in your USB cameras and put them in webcam mode - they appear as buttons."
echo "  3. In belaUI, choose the pipeline 'custom/multicam_switcher' and start streaming."
echo "  Logs: journalctl -u belaswitch -f"
