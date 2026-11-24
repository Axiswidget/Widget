#!/bin/sh
# Raise belaUI's maximum bitrate from 12 Mbps to 30 Mbps.
#
# belaUI caps the bitrate slider (and its server-side check) at 12000 kbps.
# belacoder itself already accepts up to 30 Mbps (ABS_MAX_BITRATE), so only
# belaUI needs changing. Safe to run again; a BELABOX update that replaces
# belaUI puts the 12 Mbps limit back, so just run it again afterwards.
#
#   sudo ./belaui-bitrate.sh            # set the limit to 30 Mbps
#   sudo ./belaui-bitrate.sh 25000      # or another limit in kbps (12000-30000)
#   sudo ./belaui-bitrate.sh --revert   # put the original files back
set -e

if [ "$(id -u)" != 0 ]; then echo "Please run with sudo"; exit 1; fi

DIR=$(systemctl show -p WorkingDirectory --value belaUI 2>/dev/null || true)
[ -f "$DIR/belaUI.js" ] || DIR=/opt/belaUI
if [ ! -f "$DIR/belaUI.js" ]; then
  echo "belaUI not found (looked in its systemd WorkingDirectory and /opt/belaUI)"; exit 1
fi

rc=0
python3 - "$DIR" "${1:-30000}" <<'PY' || rc=$?
import glob, os, re, shutil, sys

d, arg = sys.argv[1], sys.argv[2]
server = os.path.join(d, "belaUI.js")
clients = [f for f in glob.glob(os.path.join(d, "public", "**", "*.js"), recursive=True)
           if "bitrateSlider" in open(f, encoding="utf-8", errors="replace").read()]
files = [server] + clients

if arg == "--revert":
    n = 0
    for f in files:
        if os.path.exists(f + ".orig"):
            shutil.copy2(f + ".orig", f); n += 1
    print(f"restored {n} original file(s)" if n else "nothing to revert")
    sys.exit(0)

new = int(arg)
if not 12000 <= new <= 30000:
    sys.exit("the limit must be between 12000 and 30000 kbps (belacoder's maximum is 30 Mbps)")

# Accept the readable and minified spellings, at the original or any value
# this script set before.
srv_re = re.compile(r"(\bmaxSrtBr\s*=\s*)(\d+(?:e3)?)")
cli_re = re.compile(r"(bitrateSlider[\s\S]{0,600}?\bmax\s*:\s*)(\d+(?:e3)?)")

def kbps(tok):
    return int(float(tok))

plan = []
txt = open(server, encoding="utf-8").read()
m = srv_re.search(txt)
if not m or not 12000 <= kbps(m.group(2)) <= 30000:
    sys.exit("this belaUI version looks different (server limit not found) - nothing changed")
plan.append((server, srv_re, txt))
if not clients:
    sys.exit("this belaUI version looks different (bitrate slider not found) - nothing changed")
for f in clients:
    t = open(f, encoding="utf-8").read()
    mm = cli_re.search(t)
    if not mm or not 12000 <= kbps(mm.group(2)) <= 30000:
        sys.exit(f"this belaUI version looks different (slider limit not found in {f}) - nothing changed")
    plan.append((f, cli_re, t))

changed = 0
for f, rx, t in plan:
    out = rx.sub(lambda m: m.group(1) + str(new), t, count=1)
    if out != t:
        if not os.path.exists(f + ".orig"):
            shutil.copy2(f, f + ".orig")
        with open(f, "w", encoding="utf-8") as fh:
            fh.write(out)
        changed += 1
print(f"belaUI bitrate limit: {new} kbps" + ("" if changed else " (already set)"))
sys.exit(0 if changed else 3)
PY
case $rc in
  0) systemctl restart belaUI 2>/dev/null || true
     echo "belaUI restarted. Reload the belaUI page in your browser to see the new slider limit." ;;
  3) exit 0 ;;          # already set: nothing to do
  *) exit "$rc" ;;
esac
