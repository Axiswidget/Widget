#!/bin/sh
# BELABOX 4K tweaks for a Rock 5T streaming 4K (HDMI camera or DJI Pocket 3 on USB):
#   1. removes the multicam switcher, if it was installed
#   2. raises belaUI's bitrate limit from 12 to 30 Mbps
#   3. trims belaUI's pipeline list to HDMI 4K and USB 4K
#   4. adds friendly connection names and a "what can my connections
#      carry?" bitrate suggestion to belaUI's page
#   5. guardian: each stream / reconnect starts gently; after a drop it caps
#      the bitrate at what the connections carried, then raises it again
# Every step can be undone; see README.md.   Run:  sudo ./install.sh
set -e
cd "$(dirname "$0")"
if [ "$(id -u)" != 0 ]; then echo "Please run with sudo"; exit 1; fi

echo "== 1. Removing the multicam switcher (if installed)"
if [ -f /etc/systemd/system/belaswitch.service ] || [ -d /opt/belaswitch ]; then
  systemctl disable --now belaswitch 2>/dev/null || true
  rm -rf /opt/belaswitch /etc/belaswitch.json /etc/belaswitch.json.bak \
         /etc/systemd/system/belaswitch.service /var/lib/belaswitch
  rm -f /tmp/belaswitch*
  systemctl daemon-reload
  echo "   removed"
else
  echo "   not installed"
fi
# its pipeline file, wherever belacoder keeps pipelines
find / -xdev -path '*/custom/multicam_switcher' -delete 2>/dev/null || true

echo "== 2. belaUI bitrate limit"
./belaui-bitrate.sh 30000 || echo "   (skipped - see the message above)"

echo "== 3. belaUI pipeline list"
./belaui-pipelines.sh || echo "   (skipped - see the message above)"

echo "== 4. belaUI add-on: connection names + 'What can my connections carry?'"
./belaui-addon.sh || echo "   (skipped - see the message above)"

echo "== 5. Guardian: soft start, learns from drops, recovers on its own"
# replaces the earlier soft start helper
if [ -f /etc/systemd/system/belabox-softstart.service ]; then
  systemctl disable --now belabox-softstart 2>/dev/null || true
  rm -rf /etc/systemd/system/belabox-softstart.service /opt/belabox-softstart
fi
mkdir -p /opt/belabox-guardian
cp guardian/belabox-guardian.py /opt/belabox-guardian/
cp guardian/belabox-guardian.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable belabox-guardian >/dev/null 2>&1 || true
systemctl restart belabox-guardian
echo "   running"

systemctl restart belaUI 2>/dev/null || true
echo
echo "Done. Reload belaUI, then pick:"
echo "   rk3588/h265_hdmi_4k_2160p          HDMI camera, 4K"
echo "   rk3588/h265_libuvch264_4k_2160p30  DJI Pocket 3 on USB, 4K"
