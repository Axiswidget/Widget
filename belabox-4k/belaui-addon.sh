#!/bin/sh
# Add (or remove) the belabox-4k add-on in belaUI's page:
#   * friendly connection names (tap to rename)
#   * "What can my connections carry?" with a suggested max bitrate
#
#   sudo ./belaui-addon.sh            # add / update
#   sudo ./belaui-addon.sh --revert   # remove
#
# Only belaUI's web page is touched (one added line in index.html plus one
# file). A BELABOX update that replaces belaUI removes it; run this again.
set -e
cd "$(dirname "$0")"
if [ "$(id -u)" != 0 ]; then echo "Please run with sudo"; exit 1; fi

UI=$(systemctl show -p WorkingDirectory --value belaUI 2>/dev/null || true)
[ -f "$UI/belaUI.js" ] || UI=/opt/belaUI
PUB="$UI/public"
if [ ! -f "$PUB/index.html" ]; then echo "belaUI's page not found in $PUB - nothing changed"; exit 1; fi

TAG='<script type="text/javascript" src="/belabox-4k.js"></script>'

if [ "${1:-}" = "--revert" ]; then
  rm -f "$PUB/belabox-4k.js"
  grep -v 'src="/belabox-4k.js"' "$PUB/index.html" > "$PUB/index.html.tmp" && mv "$PUB/index.html.tmp" "$PUB/index.html"
  echo "add-on removed - reload the belaUI page"
  exit 0
fi

if ! grep -q 'src="/script.js"' "$PUB/index.html"; then
  echo "this belaUI version looks different (script.js not found in index.html) - nothing changed"; exit 1
fi
cp web/belabox-4k.js "$PUB/belabox-4k.js"
if ! grep -q 'src="/belabox-4k.js"' "$PUB/index.html"; then
  [ -f "$PUB/index.html.orig" ] || cp "$PUB/index.html" "$PUB/index.html.orig"
  # load it right after belaUI's own script
  awk -v tag="$TAG" '{print} /src="\/script\.js"/ {print "    " tag}' "$PUB/index.html" > "$PUB/index.html.tmp"
  mv "$PUB/index.html.tmp" "$PUB/index.html"
fi
echo "add-on installed - reload the belaUI page"
