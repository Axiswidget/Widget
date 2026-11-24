#!/bin/sh
# BELABOX 4K tweaks for a Rock 5T streaming a 4K / 1440p HDMI camera:
#   1. removes the multicam switcher, if it was installed
#   2. raises belaUI's bitrate limit from 12 to 30 Mbps
#   3. trims belaUI's pipeline list to the 4K / 1440p HDMI pipelines
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

systemctl restart belaUI 2>/dev/null || true
echo
echo "Done. Reload belaUI, then pick:"
echo "   rk3588/h265_hdmi_4k_2160p   for 4K   (camera's own frame rate, best quality)"
echo "   rk3588/h265_hdmi_1440p      for 1440p"
