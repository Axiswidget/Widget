#!/bin/sh
# Trim belaUI's pipeline list to the 4K / 1440p HDMI pipelines.
#
# The other pipelines (Cam Link, USB, test pattern, 1080p, 720p, RTMP/SRT
# relays, generic x264...) are moved, not deleted, to a "hidden" folder next
# to BELABOX's pipelines, so they can be brought back at any time. The
# pipeline currently selected in belaUI is always kept.
#
#   sudo ./belaui-pipelines.sh            # keep only the 4K / 1440p HDMI ones
#   sudo ./belaui-pipelines.sh --restore  # bring every pipeline back
#
# A BELABOX update may bring the hidden pipelines back; just run it again.
set -e
if [ "$(id -u)" != 0 ]; then echo "Please run with sudo"; exit 1; fi

UI=$(systemctl show -p WorkingDirectory --value belaUI 2>/dev/null || true)
[ -f "$UI/belaUI.js" ] || UI=/opt/belaUI

rc=0
python3 - "$UI" "${1:-}" <<'PY' || rc=$?
import hashlib, json, os, shutil, sys

ui, arg = sys.argv[1], sys.argv[2]
KEEP = {                      # the pipelines worth showing for 4K / 1440p HDMI
    "h265_hdmi_4k_2160p",     # camera's own frame rate, keyframe every 4 s  <- best
    "h265_hdmi_4k_2160p30",   # forced 30 fps
    "h265_hdmi_1440p",        # 4K camera scaled to 1440p in hardware        <- best 1440p
    "h265_hdmi_1440p30",
}

def load(p):
    try:
        with open(p) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}

setup = load(os.path.join(ui, "setup.json"))
if setup.get("belacoder_path"):
    pdir = os.path.join(setup["belacoder_path"], "pipeline")
else:
    pdir = "/usr/share/belacoder/pipelines"
if not os.path.isdir(pdir):
    sys.exit(f"belacoder pipelines not found at {pdir} - nothing changed")
hidden = os.path.join(os.path.dirname(pdir.rstrip("/")), os.path.basename(pdir.rstrip("/")) + "-hidden")
hw = setup.get("hw", "rk3588")

if arg == "--restore":
    n = 0
    for root, _, files in os.walk(hidden):
        for f in files:
            src = os.path.join(root, f)
            dst = os.path.join(pdir, os.path.relpath(src, hidden))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if not os.path.exists(dst):
                shutil.move(src, dst); n += 1
    print(f"restored {n} pipeline(s)")
    sys.exit(0 if n else 3)

selected = load(os.path.join(ui, "config.json")).get("pipeline")
moved, kept = 0, []
for group in (hw, "generic"):
    gdir = os.path.join(pdir, group)
    if not os.path.isdir(gdir):
        continue
    for f in sorted(os.listdir(gdir)):
        name = f"{group}/{f}"
        pid = hashlib.sha1(name.encode()).hexdigest()   # how belaUI names pipelines
        if (group == hw and f in KEEP) or pid == selected:
            kept.append(name + ("  (currently selected)" if pid == selected else ""))
            continue
        dst = os.path.join(hidden, group, f)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(os.path.join(gdir, f), dst)
        moved += 1
print("pipelines shown in belaUI:")
for k in kept:
    print("   " + k)
custom = os.path.join(pdir, "custom")
for f in sorted(os.listdir(custom)) if os.path.isdir(custom) else []:
    print("   custom/" + f)
print(f"hidden {moved} other pipeline(s) in {hidden}" if moved else "nothing else to hide")
sys.exit(0 if moved else 3)
PY
case $rc in
  0) systemctl restart belaUI 2>/dev/null || true; echo "belaUI restarted - reload its page." ;;
  3) exit 0 ;;
  *) exit "$rc" ;;
esac
