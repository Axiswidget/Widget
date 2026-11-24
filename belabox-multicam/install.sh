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
# Default: find USB cameras automatically. A config written by an earlier
# version of this installer and never edited is upgraded too (with a backup).
OLD_V11='{
  "output": { "width": 1920, "height": 1080, "fps": 30 },
  "transition": { "type": "fade", "duration_ms": 600 },
  "cameras": "auto",
  "audio_source": "usb",
  "web": { "port": 8181, "allow": "tailscale" }
}'
if [ ! -f /etc/belaswitch.json ] || grep -q "REPLACE-with" /etc/belaswitch.json \
   || { grep -q '"cameras": "auto"' /etc/belaswitch.json && ! grep -q '"allow"' /etc/belaswitch.json; } \
   || [ "$(cat /etc/belaswitch.json)" = "$OLD_V11" ]; then
  [ -f /etc/belaswitch.json ] && cp /etc/belaswitch.json /etc/belaswitch.json.bak
  cat > /etc/belaswitch.json <<'JSON'
{
  "output": { "width": 1920, "height": 1080, "fps": 30 },
  "transition": { "type": "fade", "duration_ms": 600 },
  "cameras": "auto",
  "audio_source": "usb",
  "web": { "port": 8181, "allow": "local" }
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

echo
echo "Done."
TSIP=""; TSNAME=""
if command -v tailscale >/dev/null 2>&1; then
  TSIP=$(tailscale ip -4 2>/dev/null | head -n1) || TSIP=""
  TSNAME=$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null) || TSNAME=""
fi
# This box's Wi-Fi / hotspot / LAN addresses (private ranges only)
LANIPS=$(hostname -I 2>/dev/null | tr ' ' '\n' | python3 -c '
import ipaddress, sys
for a in sys.stdin.read().split():
    try: ip = ipaddress.ip_address(a)
    except ValueError: continue
    if ip.version == 4 and ip.is_private and ip not in ipaddress.ip_network("100.64.0.0/10") \
       and not a.startswith("172.17."):
        print(a)' 2>/dev/null) || LANIPS=""
echo "  1. Control page - open it on a phone on the same Wi-Fi or BELABOX hotspot:"
if [ -n "$LANIPS" ]; then
  for a in $LANIPS; do echo "       http://${a}:8181/"; done
else
  echo "       http://<the address you use for belaUI>:8181/"
fi
if [ -n "$TSIP" ]; then
  echo "     or from anywhere over Tailscale:"
  if [ -n "$TSNAME" ]; then echo "       http://${TSNAME}:8181/"; fi
  echo "       http://${TSIP}:8181/"
fi
echo "  2. Plug in your USB cameras and put them in webcam mode - they appear as buttons."
echo "  3. In belaUI, choose the pipeline 'custom/multicam_switcher' and start streaming."
echo "  Logs: journalctl -u belaswitch -f"
