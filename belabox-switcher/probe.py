#!/usr/bin/env python3
"""
Hardware check for the multicam switcher, run ON the BELABOX:

    sudo python3 probe.py

Stop the stream in belaUI first (the cameras must be free). Keep the HDMI
camera on and the Pocket 3 plugged in, in webcam mode. Takes ~3 minutes,
changes nothing, and prints a table to paste back.

Each test runs in its own process with a time limit, so a stuck camera
can't hang the check.
"""
import json
import os
import subprocess
import sys
import time

V4K = "video/x-raw,format=NV12,width=3840,height=2160,framerate=30/1"
V4K_ANYFPS = "video/x-raw,format=NV12,width=3840,height=2160"
HDMI = "v4l2src device=/dev/hdmirx ! queue max-size-buffers=2 leaky=downstream"
USB4K = ("libuvch26xsrc index=0 ! video/x-h264,width=3840,height=2160,framerate=30/1;"
         "video/x-h265,width=3840,height=2160,framerate=30/1 ! queue ! mppvideodec")
TO_4K = f"videoconvert n-threads=4 ! videoscale n-threads=4 ! {V4K_ANYFPS}"
SINK = "fakesink name=sink sync=false async=false"
BG = f"videotestsrc is-live=true pattern=black ! {V4K} ! queue"

TESTS = [
    ("cpu_mix", "software 4K mixing (2 test pictures, 50% fade)",
     f"videotestsrc is-live=true pattern=ball ! {V4K} ! queue ! compositor name=c sink_1::alpha=0.5 ! {V4K} ! {SINK} "
     f"videotestsrc is-live=true ! {V4K} ! queue ! c."),
    ("hdmi_raw", "HDMI input as it comes",
     f"{HDMI} ! {SINK}"),
    ("hdmi_nv12", "HDMI input converted for mixing",
     f"{HDMI} ! {TO_4K} ! {SINK}"),
    ("hdmi_mix", "HDMI into the mixer",
     f"{BG} ! compositor name=c ! {V4K} ! {SINK} {HDMI} ! {TO_4K} ! queue ! c."),
    ("usb_4k", "Pocket 3 4K via BELABOX's USB source + hardware decode",
     f"{USB4K} ! {SINK}"),
    ("usb_any", "Pocket 3, any mode it sends",
     f"libuvch26xsrc index=0 ! queue ! mppvideodec ! {SINK}"),
    ("usb_mix", "Pocket 3 into the mixer",
     f"{BG} ! compositor name=c ! {V4K} ! {SINK} {USB4K} ! {TO_4K} ! queue ! c."),
    ("both_fade", "HDMI + Pocket 3 mixed, mid-fade (the real switcher load)",
     f"{BG} ! compositor name=c sink_2::alpha=0.5 ! {V4K} ! {SINK} "
     f"{HDMI} ! {TO_4K} ! queue ! c. {USB4K} ! {TO_4K} ! queue ! c."),
    ("both_fade_enc", "...the same, through the hardware 4K encoder",
     f"{BG} ! compositor name=c sink_2::alpha=0.5 ! {V4K} ! queue ! "
     f"mpph265enc zero-copy-pkt=0 qp-max=51 gop=60 ! {SINK} "
     f"{HDMI} ! {TO_4K} ! queue ! c. {USB4K} ! {TO_4K} ! queue ! c."),
    ("hdmi_audio", "HDMI audio",
     f"alsasrc device=hw:CARD=rockchiphdmiin ! {SINK}"),
]


def run_one(name, desc, seconds=8, warmup=2):
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import GLib, Gst
    Gst.init(None)
    res = {"name": name, "frames": 0, "fps": 0.0, "caps": "", "error": ""}
    try:
        pipe = Gst.parse_launch(desc)
    except GLib.Error as e:
        res["error"] = f"can't build: {e.message}"
        print(json.dumps(res)); return
    sink = pipe.get_by_name("sink")
    count = {"n": 0, "t0": None}

    def on_buf(pad, info):
        if count["t0"] is not None:
            count["n"] += 1
        return Gst.PadProbeReturn.OK
    sink.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, on_buf)
    loop = GLib.MainLoop()

    def on_msg(bus, msg):
        if msg.type == Gst.MessageType.ERROR and not res["error"]:
            err, _ = msg.parse_error()
            res["error"] = f"{msg.src.get_name()}: {err.message}"
            loop.quit()
    bus = pipe.get_bus(); bus.add_signal_watch(); bus.connect("message", on_msg)
    pipe.set_state(Gst.State.PLAYING)

    def start_count():
        count["t0"] = time.monotonic()
        count["cpu0"] = sum(os.times()[:2])
        return False
    GLib.timeout_add(int(warmup * 1000), start_count)
    GLib.timeout_add(int((warmup + seconds) * 1000), loop.quit)
    loop.run()
    if count["t0"] is not None:
        dt = time.monotonic() - count["t0"]
        res["frames"] = count["n"]
        res["fps"] = round(count["n"] / dt, 1) if dt > 0 else 0
        res["cpu"] = round(100 * (sum(os.times()[:2]) - count["cpu0"]) / dt)
    caps = sink.get_static_pad("sink").get_current_caps()
    if caps:
        st = caps.get_structure(0)
        ok, n, d = st.get_fraction("framerate")
        res["caps"] = (f"{st.get_name().split('/')[-1]} {st.get_value('format') or ''} "
                       f"{st.get_value('width') or ''}x{st.get_value('height') or ''}"
                       + (f" @{n/d:.2f}" if ok and d else "")).strip()
    print(json.dumps(res), flush=True)
    os._exit(0)          # don't risk a hang in teardown


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--run":
        name = sys.argv[2]
        desc = next(t[2] for t in TESTS if t[0] == name)
        run_one(name, desc)
        return

    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    print(f"GStreamer {Gst.version_string()} | Python {sys.version.split()[0]} | "
          f"cores {os.cpu_count()} | {open('/proc/device-tree/model').read().strip(chr(0)) if os.path.exists('/proc/device-tree/model') else ''}")
    need = ["compositor", "videoconvert", "videoscale", "v4l2src", "libuvch26xsrc", "mppvideodec",
            "mppjpegdec", "mpph265enc", "shmsink", "shmsrc", "alsasrc", "audiomixer", "jpegenc", "input-selector"]
    print("elements: " + "  ".join(f"{e}:{'yes' if Gst.ElementFactory.find(e) else 'NO'}" for e in need))
    extra = sorted({f.get_name() for f in Gst.Registry.get().get_feature_list(Gst.ElementFactory)
                    if any(k in f.get_name() for k in ("rga", "rk", "mpp", "uvc", "gl", "kms"))})
    print("rockchip/gl elements: " + " ".join(extra))
    if subprocess.run(["pgrep", "-x", "belacoder"], capture_output=True).returncode == 0:
        print("WARNING: belaUI is streaming. Press Stop in belaUI, then run this again.")
        return
    print()
    print(f"{'test':15s} {'fps':>6s} {'cpu%':>5s}  {'format':32s} result")
    for name, label, _ in TESTS:
        try:
            out = subprocess.run([sys.executable, __file__, "--run", name], capture_output=True,
                                 text=True, timeout=25).stdout.strip().splitlines()
            r = json.loads(out[-1]) if out else {"error": "no result"}
        except subprocess.TimeoutExpired:
            r = {"error": "timed out (stuck)"}
        except (ValueError, IndexError):
            r = {"error": "bad result"}
        verdict = r.get("error") or ("OK" if r.get("fps", 0) >= 27 else
                                      "SLOW" if r.get("fps", 0) > 0 else "NO FRAMES")
        print(f"{name:15s} {r.get('fps', 0):6.1f} {str(r.get('cpu', '')):>5s}  {r.get('caps', '')[:32]:32s} {verdict}")
        print(f"{'':15s} ({label})")
        time.sleep(1)          # let the camera settle between tests


if __name__ == "__main__":
    main()
