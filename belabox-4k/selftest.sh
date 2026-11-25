#!/bin/sh
# Check the belabox-4k setup on the real BELABOX.  Run:  sudo ./selftest.sh
# Best run twice: once before streaming, and once WHILE streaming (a few
# checks only apply while the encoder is running). Changes nothing.
if [ "$(id -u)" != 0 ]; then echo "Please run with sudo"; exit 1; fi

P=0; F=0; W=0
pass() { echo "  PASS  $1"; P=$((P+1)); }
fail() { echo "  FAIL  $1"; F=$((F+1)); }
info() { echo "  info  $1"; }
warn() { echo "  WARN  $1"; W=$((W+1)); }

UI=$(systemctl show -p WorkingDirectory --value belaUI 2>/dev/null)
[ -f "$UI/belaUI.js" ] || UI=/opt/belaUI
echo "belaUI: $UI"

echo "== Guardian"
if systemctl is-active --quiet belabox-guardian; then pass "guardian is running"; else fail "guardian is not running (systemctl status belabox-guardian)"; fi
if systemctl is-enabled --quiet belabox-guardian; then pass "guardian starts at boot"; else fail "guardian is not enabled at boot"; fi
if systemctl show -p StartLimitIntervalUSec --value belabox-guardian 2>/dev/null | grep -qE '^0|infinity'; then
  pass "guardian is restarted forever if it ever stops"
else
  warn "guardian restart limit not removed (re-run install.sh)"
fi
ERR=$(journalctl -u belabox-guardian -n 300 -o cat --no-pager 2>/dev/null | grep -c "error (continuing)")
if [ "$ERR" = 0 ]; then pass "no errors in the guardian's log"; else warn "$ERR error(s) in the guardian's log (journalctl -u belabox-guardian)"; fi
PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)'; then pass "Python $PYV"; else fail "Python $PYV is too old (3.8+ needed)"; fi

echo "== belaUI's log (the guardian reads it to tell drops from camera hiccups)"
if journalctl -u belaUI --since "@$(( $(date +%s) - 3600 ))" -n 1 -o cat --no-pager >/dev/null 2>&1; then
  pass "belaUI's log is readable"
else
  fail "can't read belaUI's log; the guardian will treat every restart as a possible drop"
fi

echo "== Bitrate"
BF=$(python3 -c "import json;print(json.load(open('$UI/setup.json')).get('bitrate_file','/tmp/belacoder_br'))" 2>/dev/null)
MAXBR=$(python3 -c "import json;print(json.load(open('$UI/config.json')).get('max_br',''))" 2>/dev/null)
info "max bitrate set in belaUI: ${MAXBR:-?} kbps"
if grep -q 'maxSrtBr = 30000' "$UI/belaUI.js" && grep -rq 'max: 30000' "$UI/public/"*.js; then pass "belaUI limit is 30 Mbps"; else warn "belaUI limit is not 30 Mbps (a BELABOX update? re-run install.sh)"; fi

echo "== Pipelines"
SEL=$(python3 -c "import json;print(json.load(open('$UI/config.json')).get('pipeline',''))" 2>/dev/null)
PD=$(python3 -c "import json;s=json.load(open('$UI/setup.json'));print((s['belacoder_path']+'/pipeline') if s.get('belacoder_path') else '/usr/share/belacoder/pipelines')" 2>/dev/null)
for f in "$PD"/*/*; do
  [ -f "$f" ] || continue
  n="$(basename "$(dirname "$f")")/$(basename "$f")"
  id=$(printf '%s' "$n" | sha1sum | cut -d' ' -f1)
  [ "$id" = "$SEL" ] && mark="   <- selected" || mark=""
  info "$n$mark"
done
[ -e /dev/hdmirx ] && pass "HDMI input present (/dev/hdmirx)" || warn "no /dev/hdmirx (HDMI pipeline won't be listed)"

echo "== Add-on"
if [ -f "$UI/public/belabox-4k.js" ] && [ "$(grep -c 'belabox-4k.js?v=' "$UI/public/index.html")" = 1 ]; then pass "add-on installed in belaUI's page"; else warn "add-on not installed (a BELABOX update? re-run install.sh)"; fi

echo "== While streaming"
PID=$(pgrep -x belacoder | head -1)
if [ -z "$PID" ]; then
  info "not streaming: press Start in belaUI and run this again to check the live part"
else
  pass "belacoder is running (pid $PID)"
  CGT=$(awk '/^SigCgt:/{print $2}' /proc/$PID/status)
  if [ $(( 0x$CGT & 1 )) = 1 ]; then pass "belacoder accepts bitrate reloads (SIGHUP handler installed)"; else fail "belacoder has no SIGHUP handler; the guardian won't touch it"; fi
  if [ -f "$BF" ]; then
    FMAX=$(sed -n 2p "$BF"); info "encoder's current max: $((FMAX/1000)) kbps (the guardian may hold it lower right after a start or a drop)"
    pass "bitrate file $BF exists"
  else
    fail "bitrate file $BF not found"
  fi
  journalctl -u belabox-guardian -n 5 -o cat --no-pager 2>/dev/null | sed 's/^/        guardian: /'
fi

echo
echo "RESULT: $P passed, $F failed, $W warnings"
