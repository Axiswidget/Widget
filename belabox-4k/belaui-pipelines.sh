#!/bin/sh
# Trim belaUI's pipeline list to the 4K / 1440p HDMI pipelines and the
# USB-camera pipelines for the DJI Osmo Pocket 3.
#
# The other pipelines (Cam Link, USB, test pattern, 1080p, 720p, RTMP/SRT
# relays, generic x264...) are moved, not deleted, to a "hidden" folder next
# to BELABOX's pipelines, so they can be brought back at any time. The
# pipeline currently selected in belaUI is always kept.
#
#   sudo ./belaui-pipelines.sh            # keep only HDMI 4K/1440p and USB 4K/1440p
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
KEEP = {                      # just four: HDMI and USB, 4K and 1440p
    "h265_hdmi_4k_2160p",           # HDMI 4K  (camera's own frame rate, keyframe every 4 s)
    "h265_hdmi_1440p",              # HDMI 1440p (4K camera scaled in hardware)
    "h265_libuvch264_4k_2160p30",   # USB 4K   (DJI Pocket 3 in webcam mode)
    "h265_libuvch264_1440p60",      # USB 1440p (scaled in hardware)
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

# bring back any kept pipeline hidden by an earlier run (the keep list grows)
brought = 0
for f in KEEP:
    src = os.path.join(hidden, hw, f)
    dst = os.path.join(pdir, hw, f)
    if os.path.exists(src) and not os.path.exists(dst):
        shutil.move(src, dst)
        print(f"brought back {hw}/{f}")
        brought += 1

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
sys.exit(0 if (moved or brought) else 3)   # 0 = list changed: restart belaUI
PY
case $rc in
  0) systemctl restart belaUI 2>/dev/null || true; echo "belaUI restarted - reload its page." ;;
  3) exit 0 ;;
  *) exit "$rc" ;;
esac
