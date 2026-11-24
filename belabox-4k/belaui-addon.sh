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

# The file's checksum goes in the link, so browsers fetch a new version of
# the add-on automatically on the normal belabox.local page (no stale copy).
VER=$(cksum web/belabox-4k.js | cut -d' ' -f1)
TAG="<script type=\"text/javascript\" src=\"/belabox-4k.js?v=$VER\"></script>"

if [ "${1:-}" = "--revert" ]; then
  rm -f "$PUB/belabox-4k.js"
  grep -v 'src="/belabox-4k.js' "$PUB/index.html" > "$PUB/index.html.tmp" && mv "$PUB/index.html.tmp" "$PUB/index.html"
  echo "add-on removed - reload the belaUI page"
  exit 0
fi

if ! grep -q 'src="/script.js"' "$PUB/index.html"; then
  echo "this belaUI version looks different (script.js not found in index.html) - nothing changed"; exit 1
fi
cp web/belabox-4k.js "$PUB/belabox-4k.js"
[ -f "$PUB/index.html.orig" ] || cp "$PUB/index.html" "$PUB/index.html.orig"
# (re)write the link right after belaUI's own script, with the current version
awk -v tag="$TAG" '/src="\/belabox-4k\.js/ {next} {print} /src="\/script\.js"/ {print "    " tag}' \
  "$PUB/index.html" > "$PUB/index.html.tmp"
mv "$PUB/index.html.tmp" "$PUB/index.html"
echo "add-on installed - reload the belaUI page"
