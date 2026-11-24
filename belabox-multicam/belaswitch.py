#!/usr/bin/env python3
"""
belaswitch - a simple multicam switcher for BELABOX (Rock 5 / RK3588).

It captures every configured camera, mixes them with a GStreamer
compositor (smooth crossfades or hard cuts) and hands the finished
programme to belacoder through shared memory. belacoder still does the
hardware H.265 encode, adaptive bitrate and SRTLA bonding as usual: in
belaUI you simply pick the "custom/multicam_switcher" pipeline.

    cameras ──► compositor (fade/cut) ──► shmsink ──► belacoder pipeline
    camera audio ──► audiomixer (follows video) ──► shmsink ──┘

A small web page (default http://<belabox-ip>:8181) shows a live preview
of each camera and lets you switch with a tap or with keys 1-9.
"""

import argparse
import glob
import ipaddress
import json
import re
import math
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PIPELINE_NAME = "multicam_switcher"
# The shared-memory sockets carry the output size in their name: after the
# output is changed, a belacoder still set up for the old size can't connect
# to the new picture (which would be garbage at the wrong size). It fails
# cleanly until the stream is restarted in belaUI with the new pipeline.
OUTPUT_CHOICES = {          # control page "Output" menu (the owner streams 4K or 1440p)
    "3840x2160@30": "4K 30",
    "3840x2160@25": "4K 25",
    "2560x1440@30": "1440p 30",
    "2560x1440@25": "1440p 25",
}


def output_key(o):
    return f"{int(o['width'])}x{int(o['height'])}@{int(o['fps'])}"


def parse_output_key(key):
    wh, fps = key.split("@")
    w, h = wh.split("x")
    return {"width": int(w), "height": int(h), "fps": int(fps)}


def socket_paths(cfg):
    o = cfg["output"]
    tag = f"{int(o['width'])}x{int(o['height'])}p{int(o['fps'])}"
    return f"/tmp/belaswitch.{tag}.video", f"/tmp/belaswitch.{tag}.audio"
RETRY_SECONDS = 3
MAX_RETRY_SECONDS = 30
# A source that delivers nothing for this long is restarted. Audio must be
# short: belacoder restarts the whole stream after ~2-4 s without progress.
STALL_SECONDS = {"audio": 2.0, "video": 5.0}
STARTUP_GRACE_SECONDS = 8.0
STATE_FILE = "/var/lib/belaswitch/state.json"   # remembers the audio choice
# The audio feeds you can pick between on the control page (one at a time)
AUDIO_CHOICES = {
    "usb": "USB audio",     # the USB sound card that isn't part of a camera
    "hdmi": "HDMI audio",   # the Rock 5's HDMI input
}
HANDOVER_SECONDS = 1.5      # how long the old feed may overlap the new one
AUDIO_CAPS = "audio/x-raw,format=S16LE,layout=interleaved,rate=48000,channels=2"

DEFAULT_CONFIG = {
    "output": {"width": 3840, "height": 2160, "fps": 30},
    "transition": {"type": "fade", "duration_ms": 600},
    "audio_follows_video": True,
    "previews": True,
    # allow: "local" = this box, devices on the same Wi-Fi/hotspot/LAN, and
    #        your Tailscale devices (default);
    #        "tailscale" = this box and Tailscale only;
    #        "any" = no restriction
    "web": {"host": "0.0.0.0", "port": 8181, "allow": "local"},
    "belacoder_pipelines_dir": None,
    "audio_bitrate": 128000,
    "cameras": [
        {"name": "Test A", "type": "test", "pattern": "smpte"},
        {"name": "Test B", "type": "test", "pattern": "ball"},
    ],
    "extra_audio": [],
    # "camera": each camera's own sound, following the picture
    # "usb":    one standalone USB audio device (not a camera), always on
    # or an ALSA device such as "hw:CARD=usbaudio", always on
    "audio_source": "camera",
    "hdmi_audio_device": "hw:CARD=rockchiphdmiin",
}


TAILSCALE_NETS = [ipaddress.ip_network("100.64.0.0/10"),
                  ipaddress.ip_network("fd7a:115c:a1e0::/48")]


_ts_cache = {"at": 0.0, "addrs": set()}


def tailscale_addresses(refresh=False):
    """This machine's own Tailscale addresses (cached for 30 s).

    Asks Tailscale itself (`tailscale ip`) and also reads the addresses on
    any tailscale* network interface, so either source is enough.
    """
    now = time.monotonic()
    if not refresh and now - _ts_cache["at"] < 30:
        return _ts_cache["addrs"]
    addrs = set()
    for cmd in (["tailscale", "ip"], ["ip", "-o", "addr", "show"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        if cmd[0] == "tailscale":
            lines = out.split()
        else:   # "3: tailscale0    inet 100.x.y.z/32 ..." -> only tailscale* interfaces
            lines = [m.group(1) for m in re.finditer(
                r"^\d+:\s+tailscale\S*\s+inet6? ([0-9a-fA-F:.]+)/", out, re.M)]
        for a in lines:
            try:
                ip = ipaddress.ip_address(a.strip())
            except ValueError:
                continue
            if any(ip in n for n in TAILSCALE_NETS):
                addrs.add(ip)
    _ts_cache.update(at=now, addrs=addrs)
    return addrs


def _ip(addr):
    ip = ipaddress.ip_address(addr.split("%")[0])
    return ip.ipv4_mapped if getattr(ip, "ipv4_mapped", None) else ip


def client_kind(addr, local_addr=None, ts_addrs=None):
    """'loopback', 'tailscale', 'private' or 'public' for a client.

    Tailscale is recognised by the connection arriving on this machine's
    Tailscale address, not by the client's IP alone: mobile carriers use
    the same 100.64.0.0/10 range for cellular (CGNAT) addresses.
    """
    try:
        ip = _ip(addr)
    except ValueError:
        return "public"
    if ip.is_loopback:
        return "loopback"
    if local_addr is not None:
        try:
            local = _ip(local_addr)
            ts = tailscale_addresses() if ts_addrs is None else ts_addrs
            # Tailscale may have come up after our last look: check again
            # rather than refuse the owner's phone for up to 30 s.
            if (ts_addrs is None and local not in ts
                    and any(local in n for n in TAILSCALE_NETS)):
                ts = tailscale_addresses(refresh=True)
            if local in ts:
                return "tailscale"
        except ValueError:
            pass
    if any(ip in n for n in TAILSCALE_NETS):
        return "public"          # 100.64/10 not via Tailscale = carrier NAT
    if ip.is_private or ip.is_link_local:
        return "private"
    return "public"


def client_allowed(kind, allow):
    if allow == "any":
        return True
    if allow == "tailscale":
        return kind in ("loopback", "tailscale")
    return kind in ("loopback", "tailscale", "private")


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def update_state(**changes):
    """Merge changes into the saved state (audio choice, output, camera modes)."""
    st = load_state()
    st.update(changes)
    save_state(st)


def save_state(state):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        log(f"could not save {STATE_FILE}: {e}")


def log(*args):
    print(time.strftime("%H:%M:%S"), *args, flush=True)


def load_config(path):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if path and os.path.exists(path):
        with open(path) as f:
            user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    elif path:
        log(f"config {path} not found, using built-in test cameras")
    return cfg


def has_element(name):
    return Gst.ElementFactory.find(name) is not None


def has_prop(factory, prop):
    """True if a GStreamer element type exists and has the given property."""
    el = Gst.ElementFactory.make(factory, None)
    return el is not None and el.find_property(prop) is not None


def describe_caps(caps):
    st = caps.get_structure(0)
    w, h = st.get_value("width"), st.get_value("height")
    ok, num, den = st.get_fraction("framerate")
    fps = f"{num / den:.4g}" if ok and den else "?"
    return w, h, st.get_value("format"), f"{w}x{h} {st.get_value('format')} @{fps}"


BY_ID_DIR = "/dev/v4l/by-id"
HDMI_DEVICE = "/dev/hdmirx"      # the Rock 5's HDMI input (BELABOX's udev name)
SYSFS_ROOT, DEV_ROOT = "/sys", "/dev"   # overridable for tests


def friendly_camera_name(by_id_path):
    """'usb-OM_Digital_Solutions_OM-1_0123ABCD-video-index0' -> 'OM Digital Solutions OM-1'."""
    n = os.path.basename(by_id_path)
    n = re.sub(r"^usb-", "", n)
    n = re.sub(r"-video-index\d+$", "", n)
    parts = [p for p in n.split("_") if p]
    # drop a trailing serial number (long token with digits)
    if len(parts) > 1 and len(parts[-1]) >= 6 and re.search(r"\d", parts[-1]):
        parts = parts[:-1]
    name = " ".join(parts) or "USB camera"
    return name if len(name) <= 28 else name[:27] + "…"


def find_usb_cameras(by_id_dir=BY_ID_DIR):
    """Every USB camera's main video node, in a stable order."""
    return sorted(glob.glob(os.path.join(by_id_dir, "*-video-index0")))


def _read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def find_cameras(sysfs="/sys", dev="/dev"):
    """Every connected camera, whatever it is: [{"path", "kind", "name"}].

    Looks at *all* video devices, not just the ones with a /dev/v4l/by-id
    name, so capture cards without a serial number are found too:

    * "hdmi": the board's HDMI input (BELABOX's /dev/hdmirx, or any non-USB
      video device whose driver calls itself HDMI).
    * "usb":  each USB video device's main node (V4L2 index 0; the other
      nodes of a UVC camera are metadata). Named by its stable
      /dev/v4l/by-id path if it has one, else by-path (the USB port), else
      /dev/videoN.

    The SoC's own video blocks (ISP, RGA scaler, hardware codecs...) are
    platform devices, not USB or HDMI, so they never become buttons.
    """
    nodes = sorted(glob.glob(os.path.join(sysfs, "class/video4linux/video*")),
                   key=lambda p: int(re.sub(r"\D", "", os.path.basename(p)) or 0))
    links = {}
    for d in ("v4l/by-path", "v4l/by-id"):          # by-id wins: set last
        for l in glob.glob(os.path.join(dev, d, "*")):
            links[os.path.realpath(l)] = l
    found, hdmi_nodes = [], set()
    hdmirx = os.path.join(dev, "hdmirx")
    if os.path.exists(hdmirx):
        found.append({"path": hdmirx, "kind": "hdmi", "name": "HDMI"})
        hdmi_nodes.add(os.path.realpath(hdmirx))
    for node in nodes:
        n = os.path.basename(node)
        devnode = os.path.join(dev, n)
        name = _read(os.path.join(node, "name"))
        index = _read(os.path.join(node, "index"), "0")
        bus = os.path.realpath(os.path.join(node, "device"))
        if "/usb" in bus:
            if index != "0":
                continue                         # metadata node of a UVC camera
            path = links.get(os.path.realpath(devnode), devnode)
            label = (friendly_camera_name(path) if "/by-id/" in path
                     else (name.split(":")[0].strip() or "USB camera"))
            found.append({"path": path, "kind": "usb", "name": label})
        elif (re.search(r"hdmi", name, re.I) and index == "0"
              and os.path.realpath(devnode) not in hdmi_nodes and not hdmi_nodes):
            found.append({"path": devnode, "kind": "hdmi", "name": "HDMI"})
            hdmi_nodes.add(os.path.realpath(devnode))
    return found


class NotReady(Exception):
    """A camera is plugged in but can't be used yet; keep retrying."""


KIND_MEDIA = {"v4l2": "video/x-raw", "mjpeg": "image/jpeg", "h264": "video/x-h264"}
KIND_LABEL = {"v4l2": "RAW", "mjpeg": "MJPEG", "h264": "H.264"}   # RAW = uncompressed
# For picture quality: uncompressed beats MJPEG (no camera-side JPEG
# blocking) beats H.264.
KIND_RANK = {"v4l2": 3, "mjpeg": 2, "h264": 1}
STANDARD_FPS = (60, 50, 30, 25, 24)


def _fps_values(txt):
    """'(fraction)30/1' | '(fraction){ 30/1, 25/1 }' | '(fraction)[ 1/1, 30/1 ]' -> [(n, d)]"""
    fr = [tuple(map(int, m)) for m in re.findall(r"(\d+)/(\d+)", txt)]
    if "[" in txt and len(fr) == 2:          # a range: offer the standard rates inside it
        lo, hi = fr[0][0] / fr[0][1], fr[1][0] / fr[1][1]
        fr = [(f, 1) for f in STANDARD_FPS if lo <= f <= hi] or [fr[1]]
    return [f for f in fr if f[0] > 0]


def list_modes(caps):
    """Every discrete capture mode a camera offers, best first.

    Each mode: {"id", "kind", "w", "h", "fps": (n, d), "format", "label"}.
    """
    modes, seen = [], set()
    for struct in caps.to_string().split(";"):
        media = struct.strip().split(",")[0].strip()
        kind = {v: k for k, v in KIND_MEDIA.items()}.get(media)
        w = re.search(r"width=\(int\)(\d+)", struct)
        h = re.search(r"height=\(int\)(\d+)", struct)
        fr = re.search(r"framerate=(\(fraction\)(?:\{[^}]*\}|\[[^\]]*\]|[\d/]+))", struct)
        if not (kind and w and h and fr):
            continue                     # ranges of sizes etc. are left to "Auto"
        fmt = re.search(r"format=\(string\)(\w+)", struct)
        fmt = fmt.group(1) if (fmt and kind == "v4l2") else None
        for n, d in _fps_values(fr.group(1)):
            w_, h_ = int(w.group(1)), int(h.group(1))
            mid = f"{kind}:{w_}x{h_}@{n}/{d}" + (f":{fmt}" if fmt else "")
            if mid in seen:
                continue
            seen.add(mid)
            fps = n / d
            label = f"{h_}p{fps:.4g} {KIND_LABEL[kind]}"        # e.g. "2160p30 MJPEG"
            modes.append({"id": mid, "kind": kind, "w": w_, "h": h_, "fps": (n, d),
                          "format": fmt, "label": label})
    modes.sort(key=lambda m: (m["w"] * m["h"], m["fps"][0] / m["fps"][1], KIND_RANK[m["kind"]]),
               reverse=True)
    return modes


def mode_caps_extra(m):
    extra = f",width={m['w']},height={m['h']},framerate={m['fps'][0]}/{m['fps'][1]}"
    if m["format"]:
        extra += f",format={m['format']}"
    return extra


def pick_from_modes(modes, out_w, out_h, fps):
    """Auto choice: the output size (or the next size down), the output frame
    rate (or an exact multiple, which videorate halves cleanly), then the
    least compressed format."""
    if not modes:
        return None
    sizes = sorted({(m["w"], m["h"]) for m in modes}, key=lambda wh: wh[0] * wh[1], reverse=True)
    fitting = [wh for wh in sizes if wh[0] <= out_w and wh[1] <= out_h] or [sizes[-1]]
    w, h = fitting[0]

    def score(m):
        f = m["fps"][0] / m["fps"][1]
        if abs(f - fps) < 0.01:
            cls = 4
        elif abs(f / fps - round(f / fps)) < 0.01 and f > fps:
            cls = 3                      # 60 for 30: drop every other frame, exact
        elif abs(f - fps) <= 1:
            cls = 2                      # 29.97 for 30: a repeated frame every ~33 s
        else:
            cls = 1 if f >= fps else 0
        return (cls, f if cls <= 1 else 0, KIND_RANK[m["kind"]])
    return max((m for m in modes if (m["w"], m["h"]) == (w, h)), key=score)


def pick_usb_mode(device_caps, out_w, out_h, fps):
    """Choose the best capture mode a USB camera offers.

    Prefers the output size (or the largest standard size below it), the
    output frame rate, and MJPEG over raw (raw 1080p doesn't fit through
    USB 2, and MJPEG is decoded in hardware). Returns (kind, caps_extra)
    where kind is "mjpeg", "v4l2" or "h264", or None if nothing usable is offered.
    """
    sizes = [(out_w, out_h)] + [wh for wh in ((3840, 2160), (2560, 1440), (1920, 1080),
                                             (1280, 720), (640, 360), (640, 480))
                                if wh[0] * wh[1] < out_w * out_h]
    kinds = (("v4l2", "video/x-raw"), ("mjpeg", "image/jpeg"), ("h264", "video/x-h264"))
    for w, h in sizes:
        # exact rate, then near it (29.97 for 30), then whatever is offered
        for rate in (f",framerate={fps}/1", f",framerate=[{fps - 1}/1,{fps + 1}/1]", ""):
            for kind, media in kinds:
                extra = f",width={w},height={h}{rate}"
                if device_caps.can_intersect(Gst.Caps.from_string(media + extra)):
                    return kind, extra
    for kind, media in kinds:
        if device_caps.can_intersect(Gst.Caps.from_string(media)):
            return kind, ""
    return None


def usb_audio_for_video(video_device, sysfs="/sys"):
    """Find the ALSA card on the same USB device as a V4L2 camera.

    A USB webcam shows up as one USB device with a video interface and an
    audio interface; both hang off the same parent in sysfs.
    Returns "hw:CARD=<id>" or None.
    """
    try:
        node = os.path.basename(os.path.realpath(video_device))        # videoN
        vif = os.path.realpath(os.path.join(sysfs, "class/video4linux", node, "device"))
        usb_dev = os.path.dirname(vif)
        sound = os.path.join(sysfs, "class/sound")
        for card in sorted(os.listdir(sound)):
            if not (card.startswith("card") and card[4:].isdigit()):
                continue
            aif = os.path.realpath(os.path.join(sound, card, "device"))
            if os.path.dirname(aif) == usb_dev:
                with open(os.path.join(sound, card, "id")) as f:
                    return f"hw:CARD={f.read().strip()}"
    except OSError:
        pass
    return None


def standalone_usb_audio(sysfs="/sys"):
    """The ALSA card of a USB audio device that is not part of a camera.

    USB cameras expose their microphone as a sound card too, so a plain
    "first USB sound card" could pick a camera. Here a card only counts if
    its USB device has no video interface. Returns "hw:CARD=<id>" or None.
    """
    sound = os.path.join(sysfs, "class/sound")
    found = []
    for card in sorted(_listdir(sound)):
        if not (card.startswith("card") and card[4:].isdigit()):
            continue
        try:
            usb_dev = os.path.dirname(os.path.realpath(os.path.join(sound, card, "device")))
            if "/usb" not in usb_dev:
                continue                       # built-in audio, HDMI-in, ...
            if glob.glob(os.path.join(usb_dev, "*", "video4linux")):
                continue                       # this sound card belongs to a camera
            with open(os.path.join(sound, card, "id")) as f:
                found.append(f.read().strip())
        except OSError:
            continue
    if not found:
        return None
    # BELABOX names the USB audio card "usbaudio"; prefer it if present
    return "hw:CARD=" + ("usbaudio" if "usbaudio" in found else found[0])


def probe_device_caps(device):
    src = Gst.ElementFactory.make("v4l2src", None)
    if src is None:
        return None
    src.set_property("device", device)
    try:
        if src.set_state(Gst.State.READY) == Gst.StateChangeReturn.FAILURE:
            return None
        return src.get_static_pad("src").query_caps(None)
    finally:
        src.set_state(Gst.State.NULL)


def gst_quote(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def smoothstep(t):
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


# --------------------------------------------------------------------------
# belacoder pipeline file
# --------------------------------------------------------------------------

def belacoder_pipeline(cfg):
    o = cfg["output"]
    w, h, fps = o["width"], o["height"], o["fps"]
    vsock, asock = socket_paths(cfg)
    gop = int(fps) * 2
    # Element names (ptsfixup, v_delay, a_delay, overlay, venc_bps, appsink)
    # are the ones belacoder looks up; the voaacenc/aacparse fragment is the
    # exact pattern belaUI rewrites when you choose Opus audio.
    return (
        f"shmsrc socket-path={vsock} is-live=true do-timestamp=true !\n"
        f"video/x-raw,format=NV12,width={w},height={h},framerate={fps}/1,pixel-aspect-ratio=1/1 !\n"
        "identity name=ptsfixup signal-handoffs=TRUE !\n"
        "identity name=v_delay signal-handoffs=TRUE ! queue max-size-buffers=3 leaky=downstream !\n"
        "textoverlay text='' valignment=top halignment=right font-desc=\"Monospace, 5\" name=overlay ! queue !\n"
        f"mpph265enc zero-copy-pkt=0 qp-max=51 gop={gop} name=venc_bps !\n"
        "h265parse config-interval=-1 ! queue max-size-time=10000000000 max-size-buffers=1000 max-size-bytes=41943040 ! mux.\n"
        f"shmsrc socket-path={asock} is-live=true do-timestamp=true ! {AUDIO_CAPS} !\n"
        "identity name=a_delay signal-handoffs=TRUE ! volume volume=1.0 !\n"
        f"audioconvert ! voaacenc bitrate={int(cfg['audio_bitrate'])} ! aacparse ! queue max-size-time=10000000000 max-size-buffers=1000 ! mux.\n"
        "mpegtsmux name=mux !\n"
        "appsink name=appsink\n"
    )


def find_pipelines_dir(cfg):
    if cfg.get("belacoder_pipelines_dir"):
        return cfg["belacoder_pipelines_dir"]
    candidates = []
    # belaUI reads setup.json from its systemd WorkingDirectory; ask systemd
    # where that is rather than assuming the default install location.
    try:
        wd = subprocess.run(["systemctl", "show", "-p", "WorkingDirectory", "--value", "belaUI"],
                            capture_output=True, text=True, timeout=5).stdout.strip()
        if wd:
            candidates.append(os.path.join(wd, "setup.json"))
    except (OSError, subprocess.SubprocessError):
        pass
    candidates += ["/opt/belaUI/setup.json", "/root/belaUI/setup.json"]
    candidates += [f"/home/{u}/belaUI/setup.json" for u in _listdir("/home")]
    for setup_path in candidates:
        try:
            with open(setup_path) as f:
                setup = json.load(f)
            if setup.get("belacoder_path"):
                return os.path.join(setup["belacoder_path"], "pipeline")
            return "/usr/share/belacoder/pipelines"
        except (OSError, ValueError):
            continue
    if os.path.isdir("/usr/share/belacoder/pipelines"):
        return "/usr/share/belacoder/pipelines"
    return None


def _listdir(d):
    try:
        return os.listdir(d)
    except OSError:
        return []


def write_pipeline_file(cfg):
    d = find_pipelines_dir(cfg)
    if not d:
        log("belacoder pipelines directory not found; not writing the pipeline file")
        return None
    path = os.path.join(d, "custom", PIPELINE_NAME)
    text = belacoder_pipeline(cfg)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        old = None
        if os.path.exists(path):
            with open(path) as f:
                old = f.read()
        if old != text:
            with open(path, "w") as f:
                f.write(text)
            log(f"wrote belacoder pipeline {path}")
        return path
    except OSError as e:
        log(f"could not write {path}: {e}")
        return None


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------

class Source:
    """A camera's video (and optionally its audio)."""

    def __init__(self, index, spec):
        self.index = index
        self.spec = spec
        self.name = spec.get("name") or f"Camera {index + 1}"
        self.vbin = None
        self.vpad = None          # compositor sink pad
        self.abin = None
        self.apad = None          # audiomixer sink pad
        self.video_ok = False
        self.audio_ok = False
        self.preview = None       # latest JPEG bytes
        self.input = ""           # what the camera actually delivers
        self.cpu_work = ""        # software scaling/conversion it costs
        self.error = ""           # video problem, shown on the camera tile
        self.audio_error = ""
        self.retry_pending = {"video": False, "audio": False}
        self.failing = {"video": False, "audio": False}   # quiet retries while true
        self.retry_delay = {"video": RETRY_SECONDS, "audio": RETRY_SECONDS}
        self.last_data = {"video": 0.0, "audio": 0.0}       # monotonic time of last buffer
        self.attached_at = {"video": 0.0, "audio": 0.0}

    @property
    def device(self):
        return self.spec.get("device")


class Switcher:
    def __init__(self, cfg):
        self.cfg = cfg
        o = cfg["output"]
        self.w, self.h, self.fps = int(o["width"]), int(o["height"]), int(o["fps"])
        self.vsock, self.asock = socket_paths(cfg)
        self.encoder_clients = 0       # belacoder connected to our video socket
        self.exit_code = 0
        self.camera_modes = load_state().get("camera_modes", {})   # camera key -> mode id
        self.pw = 320
        self.transition = cfg["transition"].get("type", "fade")
        self.duration_ms = int(cfg["transition"].get("duration_ms", 600))
        self.follow = bool(cfg.get("audio_follows_video", True))
        self.previews = bool(cfg.get("previews", True))
        # "cameras": "auto" -> use every USB camera that is (or gets) plugged in
        self.auto = cfg["cameras"] == "auto"
        self.sources = [] if self.auto else [Source(i, s) for i, s in enumerate(cfg["cameras"])]
        self.extra_audio = [Source(100 + i, dict(s, name=s.get("name", f"Audio {i + 1}")))
                            for i, s in enumerate(cfg.get("extra_audio", []))]
        # One audio feed for every camera instead of camera sound. If the
        # config picks "usb" or "hdmi", the control page can swap between
        # them, and the last choice is remembered across restarts.
        self.audio_source = cfg.get("audio_source", "camera")
        if self.audio_source in AUDIO_CHOICES:
            saved = load_state().get("audio_source")
            if saved in AUDIO_CHOICES:
                self.audio_source = saved
        self.audio_selectable = self.audio_source in AUDIO_CHOICES
        self.feed = None
        self._next_audio_index = 100 + len(self.extra_audio)
        if self.audio_source != "camera":
            self.feed = self._new_feed(self.audio_source)
            self.extra_audio.append(self.feed)
            for src in self.sources:
                src.spec.pop("audio", None)
        self.active = 0
        self.previous = None
        self.fade = None          # (start_time, duration_s, from_idx, to_idx)
        self.fade_timer = None
        self.program_preview = None
        self.program_sink = None
        self.lock = threading.Lock()
        self.jpegenc = "jpegenc"
        self.threads = min(4, os.cpu_count() or 1)
        self.scale_threads = has_prop("videoscale", "n-threads")
        self.convert_threads = has_prop("videoconvert", "n-threads")
        self._build()

    # ---------------- pipeline construction ----------------

    def _build(self):
        vcaps = (f"video/x-raw,format=NV12,width={self.w},height={self.h},"
                 f"framerate={self.fps}/1,pixel-aspect-ratio=1/1")

        # Programme video: black background + cameras -> compositor -> shm
        desc = (
            f"videotestsrc pattern=black is-live=true ! {vcaps} ! queue ! "
            f"compositor name=comp background=black latency=20000000 ! {vcaps} ! tee name=vt "
            "vt. ! queue max-size-buffers=2 leaky=downstream ! "
            f"shmsink name=vshm socket-path={self.vsock} shm-size={self.w * self.h * 3 // 2 * 8} "
            "wait-for-connection=false sync=false async=false "
        )
        if self.previews:
            desc += "vt. ! " + self._preview_tail("progpreview", 640)
        desc += (
            f" audiotestsrc wave=silence is-live=true ! {AUDIO_CAPS} ! queue ! "
            f"audiomixer name=amix latency=20000000 ! {AUDIO_CAPS} ! queue max-size-time=200000000 leaky=downstream ! "
            f"shmsink socket-path={self.asock} shm-size=2000000 wait-for-connection=false sync=false async=false"
        )
        self.pipeline = Gst.parse_launch(desc)
        self.pipeline.set_name("belaswitch")
        # Never let an unplugged USB audio device take the clock away.
        self.pipeline.use_clock(Gst.SystemClock.obtain())
        bin_ = self.pipeline
        self.comp = bin_.get_by_name("comp")
        self.amix = bin_.get_by_name("amix")
        for el in (self.comp, self.amix):
            if el.find_property("ignore-inactive-pads"):
                el.set_property("ignore-inactive-pads", True)
        if self.previews:
            self._hook_preview(bin_.get_by_name("progpreview"), None)
        vshm = bin_.get_by_name("vshm")

        def on_client(_el, _fd, delta):
            self.encoder_clients = max(0, self.encoder_clients + delta)
            if delta > 0 and load_state().get("output_pending"):
                update_state(output_pending=False)
            log("belacoder " + ("connected" if delta > 0 else "disconnected"))
        vshm.connect("client-connected", on_client, 1)
        vshm.connect("client-disconnected", on_client, -1)

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_message)

    def _preview_tail(self, sink_name, width=None):
        pw = width or self.pw
        ph = int(round(pw * self.h / self.w / 2)) * 2
        return (
            "queue max-size-buffers=1 leaky=downstream ! "
            "videorate drop-only=true max-rate=4 ! videoscale ! videoconvert ! "
            f"video/x-raw,width={pw},height={ph},pixel-aspect-ratio=1/1 ! "
            f"{self.jpegenc} quality=70 ! appsink name={sink_name} emit-signals=false "
            "max-buffers=1 drop=true sync=false async=false "
        )

    def _hook_preview(self, appsink, src):
        """Remember a preview appsink; pictures are pulled when the page asks.

        No Python runs in GStreamer's streaming threads for previews: with
        GStreamer 1.20 (BELABOX) a Python "new-sample" callback there
        stalled the whole audio mix (found by testing against Ubuntu 22.04's
        GStreamer 1.20.3), so belacoder never got audio and sent nothing.
        """
        if src is None:
            self.program_sink = appsink
        else:
            src.preview_sink = appsink

    def get_preview(self, src=None):
        """Latest JPEG for a camera (or the programme when src is None)."""
        sink = self.program_sink if src is None else getattr(src, "preview_sink", None)
        if sink is not None:
            sample = sink.emit("try-pull-sample", 0)
            if sample is not None:
                buf = sample.get_buffer()
                ok, info = buf.map(Gst.MapFlags.READ)
                if ok:
                    data = bytes(info.data)
                    buf.unmap(info)
                    if src is None:
                        self.program_preview = data
                    else:
                        src.preview = data
        return self.program_preview if src is None else getattr(src, "preview", None)

    def _video_source_desc(self, s):
        spec = s.spec
        kind = spec.get("type", "v4l2")
        dev = spec.get("device", "/dev/video0")
        sw, sh, sfps = spec.get("width"), spec.get("height"), spec.get("fps")
        caps_extra = ""
        if sw and sh:
            caps_extra += f",width={int(sw)},height={int(sh)}"
        if sfps:
            caps_extra += f",framerate={int(sfps)}/1"

        s.capture_size = None
        if kind == "usb":
            # Ask the camera what it can do. Use the mode chosen on the
            # control page if the camera still offers it, else pick the best.
            caps = probe_device_caps(dev)
            s.modes = list_modes(caps) if caps else []
            wanted = self.camera_modes.get(self._camera_key(s), "auto")
            mode = next((m for m in s.modes if m["id"] == wanted), None)
            if mode is None:
                mode = pick_from_modes(s.modes, self.w, self.h, self.fps)
            if mode is not None:
                kind, caps_extra = mode["kind"], mode_caps_extra(mode)
                s.capture_size = (mode["w"], mode["h"])
                label = mode["label"]
            else:                # camera only describes ranges: fall back
                choice = pick_usb_mode(caps, self.w, self.h, self.fps) if caps else None
                if choice is None:
                    raise NotReady("no video mode offered - is the camera in webcam/USB streaming mode?")
                kind, caps_extra = choice
                label = f"{KIND_LABEL[kind]} {caps_extra.lstrip(',')}"
            s.mode_label = label + ("" if wanted == "auto" else " (chosen)")
            if s.mode_label != getattr(s, "logged_mode", None):
                s.logged_mode = s.mode_label
                log(f"[{s.name}] capture mode: {s.mode_label}")

        if kind == "test":
            pattern = spec.get("pattern", "smpte")
            return (f"videotestsrc is-live=true pattern={pattern} ! "
                    f"video/x-raw,width={self.w},height={self.h},framerate={self.fps}/1 ! "
                    f"textoverlay text={gst_quote(s.name)} font-desc=\"Sans Bold 48\" "
                    "valignment=bottom halignment=left shaded-background=true")
        if kind == "mjpeg":
            # mppjpegdec is the Rockchip hardware decoder; its RGA scaling is
            # switched on in _tune_decoder() once the bin exists.
            dec = "mppjpegdec name=dec" if has_element("mppjpegdec") else "jpegdec"
            return (f"v4l2src device={gst_quote(dev)} do-timestamp=true ! image/jpeg{caps_extra} ! "
                    f"queue max-size-buffers=2 leaky=downstream ! {dec}")
        if kind == "h264":
            dec = ("mppvideodec" if has_element("mppvideodec")
                   else "avdec_h264" if has_element("avdec_h264") else "decodebin")
            return (f"v4l2src device={gst_quote(dev)} do-timestamp=true ! video/x-h264{caps_extra} ! "
                    f"h264parse ! queue max-size-buffers=4 leaky=downstream ! {dec}")
        if kind == "v4l2":
            # Like BELABOX's own HDMI pipelines: drop frames the HDMI
            # receiver marks as damaged (they show up as glitches).
            drop = (" ! identity drop-buffer-flags=GST_BUFFER_FLAG_DROPPABLE"
                    if os.path.realpath(dev) == os.path.realpath(HDMI_DEVICE) else "")
            return (f"v4l2src device={gst_quote(dev)} do-timestamp=true ! video/x-raw{caps_extra}{drop} ! "
                    "queue max-size-buffers=2 leaky=downstream")
        if kind == "custom":
            return spec["pipeline"]
        raise ValueError(f"unknown camera type {kind!r}")

    def _make_video_bin(self, s):
        vcaps = (f"video/x-raw,format=NV12,width={self.w},height={self.h},"
                 f"framerate={self.fps}/1,pixel-aspect-ratio=1/1")
        # videorate first so a 60 fps camera is thinned before we scale it.
        # Both are passthrough (free) when the camera already delivers
        # NV12 at the output size; otherwise spread the work over cores.
        threads = f" n-threads={self.threads}" if self.threads > 1 else ""
        # Picture quality: Catmull-Rom scaling (measured ~60% less aliasing
        # than the default bilinear when shrinking 4K, and sharper when
        # enlarging) and no dithering. Both are skipped entirely (bit-exact
        # passthrough) when the camera already delivers NV12 at the output size.
        desc = (
            f"{self._video_source_desc(s)} ! videorate name=rate ! "
            f"videoscale method=catrom add-borders=true{threads if self.scale_threads else ''} ! "
            f"videoconvert dither=none{threads if self.convert_threads else ''} ! {vcaps} ! tee name=t "
            "t. ! queue name=out max-size-buffers=3 leaky=downstream "
        )
        if self.previews:
            desc += "t. ! " + self._preview_tail("preview")
        b = Gst.parse_bin_from_description(desc, False)
        b.set_name(f"cam{s.index}")
        self._tune_decoder(b.get_by_name("dec"), s)
        out = b.get_by_name("out")
        b.add_pad(Gst.GhostPad.new("src", out.get_static_pad("src")))
        if self.previews:
            self._hook_preview(b.get_by_name("preview"), s)
        b.get_by_name("rate").get_static_pad("sink").add_probe(
            Gst.PadProbeType.EVENT_DOWNSTREAM, self._on_input_caps, s)
        return b

    def _tune_decoder(self, dec, s):
        """Let the Rockchip MJPEG decoder scale to the output size in hardware.

        Done property by property and never fatal: if this BELABOX build's
        decoder lacks an option, the camera still works and the CPU scales.
        """
        if dec is None:
            return
        cap = getattr(s, "capture_size", None)
        shrinking = cap is not None and (cap[0] > self.w or cap[1] > self.h)
        try:
            if dec.find_property("width") and dec.find_property("height") and not shrinking:
                dec.set_property("width", self.w)
                dec.set_property("height", self.h)
            if dec.find_property("format"):
                Gst.util_set_object_arg(dec, "format", "NV12")
        except (TypeError, ValueError) as e:
            log(f"[{s.name}] hardware scaling not available ({e}); using CPU")

    def _on_input_caps(self, pad, info, s):
        ev = info.get_event()
        if ev.type != Gst.EventType.CAPS:
            return Gst.PadProbeReturn.OK
        w, h, fmt, text = describe_caps(ev.parse_caps())
        work = []
        if (w, h) != (self.w, self.h):
            work.append(f"scaling {w}x{h}->{self.w}x{self.h}")
        if fmt != "NV12":
            work.append(f"converting {fmt}->NV12")
        if text != s.input:
            s.input, s.cpu_work = text, ", ".join(work)
            msg = f"[{s.name}] input {text}"
            if work:
                msg += f" - CPU {s.cpu_work} every frame"
                if self.w * self.h >= 3840 * 2160 or fmt in ("BGR", "RGB", "BGRx", "RGBx"):
                    msg += " (heavy: see README '4K')"
            log(msg)
        return Gst.PadProbeReturn.OK

    def _make_audio_bin(self, s, device, tag):
        desc = (f"alsasrc device={gst_quote(device)} ! audioconvert ! audioresample ! "
                f"{AUDIO_CAPS} ! queue name=out max-size-time=200000000 leaky=downstream")
        b = Gst.parse_bin_from_description(desc, False)
        b.set_name(f"{tag}{s.index}")
        b.add_pad(Gst.GhostPad.new("src", b.get_by_name("out").get_static_pad("src")))
        return b

    # ---------------- attaching / detaching sources ----------------

    def _attach_video(self, s):
        s.retry_pending["video"] = False
        dev = s.device
        if s.spec.get("type", "v4l2") in ("v4l2", "mjpeg", "usb") and dev and not os.path.exists(dev):
            if not s.failing["video"]:
                log(f"[{s.name}] waiting for {dev}")
            s.failing["video"] = True
            s.error = f"{dev} not found"
            self._schedule_retry(s, "video")
            return False
        try:
            b = self._make_video_bin(s)
        except NotReady as e:
            if not s.failing["video"]:
                log(f"[{s.name}] waiting: {e}")
            s.failing["video"] = True
            s.error = str(e)
            self._schedule_retry(s, "video")
            return False
        except (GLib.Error, ValueError, KeyError) as e:
            s.error = f"bad camera config: {e}"
            log(f"[{s.name}] {s.error}")
            return False
        self.pipeline.add(b)
        pad = self.comp.request_pad_simple("sink_%u")
        self._apply_video_props(s, pad)
        ret = b.get_static_pad("src").link(pad)
        if ret != Gst.PadLinkReturn.OK:
            # e.g. a format the mixer can't take: report it and retry later
            # instead of taking the whole switcher down
            log(f"[{s.name}] could not connect to the mixer ({ret.value_nick})")
            self.comp.release_request_pad(pad)
            self.pipeline.remove(b)
            b.set_state(Gst.State.NULL)
            s.error = f"mixer refused this camera's format ({ret.value_nick})"
            s.failing["video"] = True
            self._schedule_retry(s, "video")
            return False
        s.vbin, s.vpad = b, pad
        self._watch_data(s, "video", b.get_static_pad("src"))
        b.sync_state_with_parent()
        for ms in (500, 2000):
            GLib.timeout_add(ms, self._relatency)
        s.video_ok = True
        self._announce(s, "video", b)
        return True

    def _attach_audio(self, s, tag="aud"):
        s.retry_pending["audio"] = False
        device = s.spec.get("audio") if tag == "aud" else s.spec.get("device")
        if not device:
            return False
        if device == "usb":
            device = standalone_usb_audio()
            if not device:
                if not s.failing["audio"]:
                    log(f"[{s.name}] waiting for a USB audio device (not a camera)")
                s.failing["audio"] = True
                s.audio_error = "no USB audio device plugged in"
                self._schedule_retry(s, "audio")
                return False
        if device == "auto":
            device = usb_audio_for_video(s.device or "")
            if not device:
                if not s.failing["audio"]:
                    log(f"[{s.name}] waiting for the camera's USB audio")
                s.failing["audio"] = True
                s.audio_error = "no USB audio found for this camera"
                self._schedule_retry(s, "audio")
                return False
        try:
            b = self._make_audio_bin(s, device, tag)
        except GLib.Error as e:
            log(f"[{s.name}] bad audio config: {e}")
            return False
        # The source joins the mixer only after its first buffer: a device
        # that opens but never delivers must not hold up the whole mix.
        self.pipeline.add(b)
        s.abin, s.apad = b, None
        ghost = b.get_static_pad("src")
        state = {"scheduled": False}

        def link_now():
            if s.abin is not b:
                return False            # replaced or removed meanwhile
            pad = self.amix.request_pad_simple("sink_%u")
            pad.set_property("volume", self._audio_volume(s, tag))
            ghost.link(pad)
            s.apad = pad
            ghost.remove_probe(block_id)
            s.audio_ok = True
            GLib.timeout_add(300, self._relatency)
            self._announce(s, "audio", b, f" ({device})")
            old = getattr(s, "replaces", None)
            if old is not None:          # new feed is live: drop the old one now
                s.replaces = None
                self._retire(old)
            return False

        def on_first_data(pad, info):
            if not state["scheduled"]:
                state["scheduled"] = True
                GLib.idle_add(link_now)
            return Gst.PadProbeReturn.OK          # stay blocked until linked

        block_id = ghost.add_probe(
            Gst.PadProbeType.BLOCK | Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST,
            on_first_data)
        self._watch_data(s, "audio", ghost)
        b.sync_state_with_parent()
        return True

    def _announce(self, s, kind, b, extra=""):
        """Log a start, or (after failures) a recovery once it has stayed up."""
        if not s.failing[kind]:
            log(f"[{s.name}] {kind} started{extra}")
            return

        def check():
            current = s.vbin if kind == "video" else s.abin
            if current is b and s.failing[kind]:
                s.failing[kind] = False
                s.retry_delay[kind] = RETRY_SECONDS
                if kind == "video":
                    s.error = ""
                else:
                    s.audio_error = ""
                log(f"[{s.name}] {kind} back{extra}")
            return False
        GLib.timeout_add_seconds(2, check)

    def _relatency(self):
        self.pipeline.recalculate_latency()
        return False

    def _watch_data(self, s, kind, pad):
        now = time.monotonic()
        s.attached_at[kind] = now
        s.last_data[kind] = 0.0

        def on_data(_pad, _info):
            s.last_data[kind] = time.monotonic()
            return Gst.PadProbeReturn.OK
        pad.add_probe(Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST, on_data)

    def _watchdog(self):
        """Restart any source that has stopped delivering without an error."""
        now = time.monotonic()
        for s in self.sources + self.extra_audio:
            for kind in ("video", "audio"):
                b = s.vbin if kind == "video" else s.abin
                if b is None or getattr(b, "_dying", False):
                    continue
                last = s.last_data[kind]
                if last == 0.0:
                    silent_for = now - s.attached_at[kind] - STARTUP_GRACE_SECONDS
                    reason = "never started sending"
                else:
                    silent_for = now - last
                    reason = "stopped sending"
                if silent_for <= STALL_SECONDS[kind]:
                    continue
                if kind == "video":
                    s.error = f"camera {reason} - restarting"
                else:
                    s.audio_error = f"audio {reason} - restarting"
                if not s.failing[kind]:
                    log(f"[{s.name}] {kind} {reason} - restarting it")
                s.failing[kind] = True
                s.retry_delay[kind] = min(MAX_RETRY_SECONDS, s.retry_delay[kind] * 2)
                self._detach(s, kind)
                self._schedule_retry(s, kind)
        return True

    def _detach(self, s, kind):
        b, pad, mixer = ((s.vbin, s.vpad, self.comp) if kind == "video"
                         else (s.abin, s.apad, self.amix))
        if b is None:
            return
        src = b.get_static_pad("src")
        if pad is not None:
            src.unlink(pad)
            mixer.release_request_pad(pad)
        b._dying = True
        self.pipeline.remove(b)
        # A hung USB device can block a state change; do it off the main
        # loop so switching and the control page never freeze.
        threading.Thread(target=b.set_state, args=(Gst.State.NULL,), daemon=True).start()
        if kind == "video":
            s.vbin = s.vpad = None
            s.video_ok = False
            s.preview = None
            s.preview_sink = None
        else:
            s.abin = s.apad = None
            s.audio_ok = False

    def _schedule_retry(self, s, kind):
        if s.retry_pending[kind]:
            return
        s.retry_pending[kind] = True

        def retry():
            if getattr(s, "retired", False):
                return False
            if kind == "video":
                self._attach_video(s)
            else:
                self._attach_audio(s, "aud" if s.index < 100 else "extra")
            return False
        GLib.timeout_add_seconds(int(s.retry_delay[kind]), retry)

    def _owner(self, obj):
        while obj is not None:
            name = obj.get_name() if hasattr(obj, "get_name") else ""
            for prefix, kind, pool in (("cam", "video", self.sources),
                                       ("aud", "audio", self.sources),
                                       ("extra", "audio", self.extra_audio)):
                if name.startswith(prefix) and name[len(prefix):].isdigit():
                    idx = int(name[len(prefix):])
                    for s in pool:
                        if s.index == idx:
                            return s, kind
                    return "stale", kind    # from a source we already removed
            obj = obj.get_parent()
        return None, None

    def _on_message(self, bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            s, kind = self._owner(msg.src)
            if s == "stale":
                return
            if s is None:
                log(f"fatal pipeline error: {err.message} ({dbg})")
                self.loop.quit()
                return
            current = s.vbin if kind == "video" else s.abin
            if current is None or getattr(current, "_dying", False):
                return  # a follow-up error from a bin we're already removing
            current._dying = True
            if kind == "video":
                s.error = err.message
            else:
                s.audio_error = err.message
            if not s.failing[kind]:
                s.failing[kind] = True
                log(f"[{s.name}] {kind} lost: {err.message} - retrying every {RETRY_SECONDS}s")

            def teardown():
                if (s.vbin if kind == "video" else s.abin) is not current:
                    return False
                self._detach(s, kind)
                self._schedule_retry(s, kind)
                return False
            GLib.idle_add(teardown)
        elif msg.type == Gst.MessageType.LATENCY:
            self.pipeline.recalculate_latency()
        elif msg.type == Gst.MessageType.WARNING:
            w, _ = msg.parse_warning()
            log(f"warning from {msg.src.get_name()}: {w.message}")

    # ---------------- switching ----------------

    def _audio_volume(self, s, tag="aud"):
        base = float(s.spec.get("audio_volume", s.spec.get("volume", 1.0)))
        if tag != "aud" or not self.follow:
            return base
        return base if s.index == self.active else 0.0

    def _apply_video_props(self, s, pad):
        """Set alpha/zorder for a (re)attached camera from the current state."""
        live = s.index == self.active
        pad.set_property("zorder", 3 if live else 1)
        pad.set_property("alpha", 1.0 if live else 0.0)

    def _set(self, idx, alpha=None, zorder=None, vol=None):
        s = self.sources[idx]
        if s.vpad is not None:
            if zorder is not None:
                s.vpad.set_property("zorder", zorder)
            if alpha is not None:
                s.vpad.set_property("alpha", alpha)
        if s.apad is not None and vol is not None and self.follow:
            base = float(s.spec.get("audio_volume", 1.0))
            s.apad.set_property("volume", base * vol)

    def _finish_fade(self):
        if self.fade_timer:
            GLib.source_remove(self.fade_timer)
            self.fade_timer = None
        if self.fade:
            _, _, a, b = self.fade
            self._set(a, alpha=0.0, zorder=1, vol=0.0)
            self._set(b, alpha=1.0, zorder=3, vol=1.0)
            self.fade = None

    def switch(self, idx, transition=None, duration_ms=None):
        """Must run on the GLib main loop."""
        if not 0 <= idx < len(self.sources):
            return False
        transition = transition or self.transition
        duration_ms = self.duration_ms if duration_ms is None else int(duration_ms)
        self._finish_fade()
        if idx == self.active:
            return True
        prev, self.active, self.previous = self.active, idx, self.active
        for s in self.sources:
            if s.index not in (prev, idx):
                self._set(s.index, alpha=0.0, zorder=1, vol=0.0)
        log(f"{transition} -> {self.sources[idx].name}")

        if transition == "cut" or duration_ms <= 0:
            self._set(idx, alpha=1.0, zorder=3, vol=1.0)
            self._set(prev, alpha=0.0, zorder=1, vol=0.0)
            return True

        # Crossfade: the incoming camera is layered on top of the outgoing
        # one and its opacity is eased from 0 to 1. Audio does an
        # equal-power crossfade over the same time.
        self._set(prev, alpha=1.0, zorder=2)
        self._set(idx, alpha=0.0, zorder=3, vol=0.0)
        self.fade = (time.monotonic(), duration_ms / 1000.0, prev, idx)

        def tick():
            start, dur, a, b = self.fade
            t = (time.monotonic() - start) / dur
            if t >= 1.0:
                self.fade_timer = None
                self._finish_fade()
                return False
            e = smoothstep(t)
            self._set(b, alpha=e, vol=math.sin(e * math.pi / 2))
            self._set(a, vol=math.cos(e * math.pi / 2))
            return True
        self.fade_timer = GLib.timeout_add(max(5, 500 // self.fps), tick)
        return True

    def state(self):
        return {
            "active": self.active,
            "transition": self.transition,
            "duration_ms": self.duration_ms,
            "fading": self.fade is not None,
            "previews": self.previews,
            "output": f"{self.w}x{self.h}@{self.fps}",
            "output_key": output_key(self.cfg["output"]),
            "output_choices": OUTPUT_CHOICES,
            "output_pending": bool(load_state().get("output_pending")),
            "encoder_connected": self.encoder_clients > 0,
            "audio_selectable": self.audio_selectable,
            "audio_source": self.audio_source,
            "audio_choices": AUDIO_CHOICES if self.audio_selectable else {},
            "audio_feed": (None if self.feed is None else
                           {"name": self.feed.name, "online": self.feed.audio_ok,
                            "error": self.feed.audio_error}),
            "audio_feeds": [
                {"name": a.name, "online": a.audio_ok, "error": a.audio_error}
                for a in self.extra_audio
            ],
            "cameras": [
                {"index": s.index, "name": s.name, "online": s.video_ok,
                 "audio": bool(s.spec.get("audio")), "audio_online": s.audio_ok,
                 "error": s.error, "audio_error": s.audio_error,
                 "input": s.input, "cpu_work": s.cpu_work,
                 "modes": [{"id": m["id"], "label": m["label"]} for m in getattr(s, "modes", [])],
                 "mode": self.camera_modes.get(self._camera_key(s), "auto"),
                 "mode_label": getattr(s, "mode_label", "")}
                for s in self.sources
            ],
        }

    # ---------------- run ----------------

    def run(self):
        # remove sockets left by earlier runs, for any output size
        for p in glob.glob("/tmp/belaswitch*.video") + glob.glob("/tmp/belaswitch*.audio"):
            try:
                os.unlink(p)
            except OSError:
                pass
        self.loop = GLib.MainLoop()
        ret = self.pipeline.set_state(Gst.State.PLAYING)
        log(f"pipeline start: {ret.value_nick} (GStreamer {Gst.version_string()})")
        # GStreamer 1.20 (Ubuntu 22.04, BELABOX): the mixers ask their live
        # inputs for their latency before those inputs have negotiated, the
        # question fails, and the mixer then waits for *every* input forever.
        # Newer GStreamer re-asks by itself; 1.20 must be told to.
        for ms in (300, 1000, 2500):
            GLib.timeout_add(ms, self._relatency)
        GLib.timeout_add(500, self._watchdog)
        for s in self.sources:
            self._attach_video(s)
            self._attach_audio(s, "aud")
        for s in self.extra_audio:
            self._attach_audio(s, "extra")
        if self.auto:
            log("cameras: auto - every USB camera you plug in gets a button")
            self._scan_cameras()
            GLib.timeout_add_seconds(RETRY_SECONDS, self._scan_cameras)
        log(f"programme {self.w}x{self.h}@{self.fps} -> {self.vsock}, {self.asock}")
        try:
            self.loop.run()
        finally:
            self.pipeline.set_state(Gst.State.NULL)

    def _new_feed(self, choice):
        device = {"hdmi": self.cfg.get("hdmi_audio_device", "hw:CARD=rockchiphdmiin")}.get(choice, choice)
        name = AUDIO_CHOICES.get(choice, choice)
        src = Source(self._next_audio_index, {"name": name, "device": device})
        self._next_audio_index += 1
        src.choice = choice
        return src

    def select_audio(self, choice):
        """Swap the single audio feed (must run on the GLib main loop).

        Make-before-break: the new feed is brought up and the old one is
        dropped as soon as the new one delivers sound (or after
        HANDOVER_SECONDS), so there is never more than one feed for long
        and normally no gap.
        """
        if not self.audio_selectable or choice not in AUDIO_CHOICES:
            return False
        if self.feed is not None and getattr(self.feed, "choice", None) == choice:
            return True
        old, new = self.feed, self._new_feed(choice)
        self.feed, self.audio_source = new, choice
        new.replaces = old
        self.extra_audio.append(new)
        update_state(audio_source=choice)
        log(f"audio -> {new.name}")
        self._attach_audio(new, "extra")
        if old is not None:
            GLib.timeout_add(int(HANDOVER_SECONDS * 1000), lambda: (self._retire(old), False)[1])
        return True

    def _retire(self, src):
        """Remove an audio feed for good (it was replaced)."""
        if getattr(src, "retired", False):
            return
        src.retired = True
        self._detach(src, "audio")
        if src in self.extra_audio:
            self.extra_audio.remove(src)

    def _camera_key(self, s):
        """Stable key for remembering a camera's chosen mode."""
        return s.device or s.name

    def set_camera_mode(self, idx, mode_id):
        """Choose a camera's capture mode ("auto" = best). Main loop only.

        The camera restarts in the new mode; while it does, its tile (and the
        programme, if it is live) is black for about a second.
        """
        if not 0 <= idx < len(self.sources):
            return False
        s = self.sources[idx]
        if mode_id != "auto" and mode_id not in {m["id"] for m in getattr(s, "modes", [])}:
            return False
        key = self._camera_key(s)
        if mode_id == "auto":
            self.camera_modes.pop(key, None)
        else:
            self.camera_modes[key] = mode_id
        update_state(camera_modes=self.camera_modes)
        log(f"[{s.name}] mode -> {mode_id}")
        if s.vbin is not None:
            self._detach(s, "video")
        s.failing["video"] = False
        # give the old capture a moment to release the device
        GLib.timeout_add(800, lambda: (self._attach_video(s), False)[1])
        return True

    def set_output(self, key):
        """Change the output size/rate. Main loop only.

        Writes the new belacoder pipeline and restarts this service (systemd
        brings it back in ~2 s). The stream must then be stopped and started
        in belaUI so belacoder picks up the new size.
        """
        if key not in OUTPUT_CHOICES:
            return False
        if key == output_key(self.cfg["output"]):
            return True
        self.cfg["output"] = parse_output_key(key)
        update_state(output=key, output_pending=True)
        write_pipeline_file(self.cfg)
        log(f"output -> {key}; restarting the switcher")
        self.exit_code = 75          # non-zero: systemd restarts us

        def restart():
            self.loop.quit()
            return False
        GLib.timeout_add(500, restart)  # let the HTTP reply go out first
        return True

    def _scan_cameras(self):
        """Give every connected camera a button: HDMI, USB, anything (auto mode).

        Runs every few seconds, so cameras plugged in later appear by
        themselves. A camera that is unplugged keeps its button ("waiting")
        and comes back on the same button when it is plugged in again.
        """
        known = {s.device for s in self.sources}
        found = []
        for cam in find_cameras(SYSFS_ROOT, DEV_ROOT):
            path = cam["path"]
            if cam["kind"] == "hdmi":
                # The HDMI input shows "waiting" until it has a signal.
                spec = {"name": "HDMI", "type": "v4l2", "device": path}
                if self.audio_source == "camera":
                    spec["audio"] = self.cfg.get("hdmi_audio_device", "hw:CARD=rockchiphdmiin")
            else:
                spec = {"name": cam["name"], "type": "usb", "device": path}
                if self.audio_source == "camera":
                    spec["audio"] = "auto"
            found.append((path, spec, cam["kind"]))
        # Physical devices already on a button (only ones present right now:
        # an unplugged camera's old /dev/videoN may be reused by a new one).
        live_real = {os.path.realpath(s.device) for s in self.sources
                     if s.device and os.path.exists(s.device)}
        for path, spec, kind in found:
            if path in known or os.path.realpath(path) in live_real:
                continue
            # A USB camera seen only as a bare /dev/videoN may have been
            # plugged in a moment ago, before udev made its stable by-id
            # name: wait for it. (The HDMI input never needs to wait.)
            if kind == "usb" and re.fullmatch(r"video\d+", os.path.basename(path)):
                try:
                    if time.time() - os.stat(path).st_ctime < 5:
                        continue
                except OSError:
                    continue
            s = Source(len(self.sources), spec)
            self.sources.append(s)
            log(f"[{s.name}] found camera {len(self.sources)}: {path}")
            self._attach_video(s)
            self._attach_audio(s, "aud")
        return True

    def call_in_main(self, fn, *args, **kwargs):
        """Run fn on the GLib thread and wait for its result."""
        done = threading.Event()
        box = {}

        def wrapper():
            try:
                box["r"] = fn(*args, **kwargs)
            except Exception as e:  # noqa: BLE001
                box["e"] = e
            done.set()
            return False
        GLib.idle_add(wrapper)
        done.wait(5)
        if "e" in box:
            raise box["e"]
        return box.get("r")


# --------------------------------------------------------------------------
# Web control
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Diagnostics (--diagnose): where does video stop?
# --------------------------------------------------------------------------

_diag = {"counts": {}, "t0": 0.0}


def _all_elements(bin_):
    it = bin_.iterate_recurse()
    out = []
    while True:
        ok, el = it.next()
        if ok != Gst.IteratorResult.OK:
            break
        out.append(el)
    return out


def _pads(el):
    it = el.iterate_pads()
    out = []
    while True:
        ok, p = it.next()
        if ok != Gst.IteratorResult.OK:
            break
        out.append(p)
    return out


def _path(obj):
    parts = []
    while obj is not None and obj.get_parent() is not None:
        parts.append(obj.get_name())
        obj = obj.get_parent()
    return "/".join(reversed(parts))


def diagnose_start(sw):
    _diag["t0"] = time.monotonic()
    for el in _all_elements(sw.pipeline):
        for p in _pads(el):
            key = _path(p)
            _diag["counts"][key] = 0

            def count(_pad, _info, key=key):
                _diag["counts"][key] += 1
                return Gst.PadProbeReturn.OK
            p.add_probe(Gst.PadProbeType.BUFFER, count)
    log(f"diagnose: counting buffers on {len(_diag['counts'])} pads...")


def diagnose_report(sw):
    secs = time.monotonic() - _diag["t0"]
    out = [f"===== belaswitch diagnose ({secs:.0f} s) =====",
           f"GStreamer {Gst.version_string()} | Python {sys.version.split()[0]} | PyGObject {gi.__version__}"]
    r, cur, pend = sw.pipeline.get_state(0)
    out.append(f"pipeline: {cur.value_nick} (pending {pend.value_nick}, last change {r.value_nick})")
    bad = []
    for el in _all_elements(sw.pipeline):
        r, cur, pend = el.get_state(0)
        if cur != Gst.State.PLAYING or pend != Gst.State.VOID_PENDING:
            bad.append(f"  {_path(el)} [{el.get_factory().get_name() if el.get_factory() else 'bin'}]: "
                       f"{cur.value_nick} (pending {pend.value_nick}, {r.value_nick})")
    out.append("elements not PLAYING: " + ("none" if not bad else ""))
    out += bad
    out.append("buffers per pad (pad: count | caps):")
    for el in _all_elements(sw.pipeline):
        for p in _pads(el):
            key = _path(p)
            if key not in _diag["counts"] or p.get_direction() != Gst.PadDirection.SRC:
                continue
            caps = p.get_current_caps()
            cs = caps.to_string() if caps else "-"
            cs = re.sub(r"\(\w+\)", "", cs)[:110]
            flag = "" if _diag["counts"][key] else "   <-- nothing"
            out.append(f"  {key}: {_diag['counts'][key]} | {cs}{flag}")
    out.append("cameras: " + "; ".join(
        f"{c['name']}={'online' if c['online'] else 'offline'}" + (f" ({c['error']})" if c['error'] else "")
        for c in sw.state()["cameras"]))
    out.append("===== end =====")
    print("\n".join(out), flush=True)


def make_handler(sw):
    index_path = os.path.join(HERE, "web", "index.html")
    allow = sw.cfg.get("web", {}).get("allow", "local")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _params(self):
            u = urlparse(self.path)
            p = {k: v[0] for k, v in parse_qs(u.query).items()}
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                try:
                    p.update(json.loads(self.rfile.read(n) or b"{}"))
                except ValueError:
                    pass
            return u.path, p

        def do_GET(self):
            self._route()

        def do_POST(self):
            self._route()

        def _route(self):
            kind = client_kind(self.client_address[0], self.connection.getsockname()[0])
            if not client_allowed(kind, allow):
                hint = ("Open it from a device on your tailnet: http://<belabox-tailscale-name>:8181"
                        if allow == "tailscale" else
                        "Connect this phone to the same Wi-Fi or BELABOX hotspot as the box, "
                        "or use Tailscale.")
                return self._send(403, "Not allowed from this network. " + hint + "\n",
                                  "text/plain; charset=utf-8")
            path, p = self._params()
            if path in ("/", "/index.html"):
                with open(index_path, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            def state():
                st = sw.state()
                # Over Tailscale the page's traffic shares the cellular
                # uplink with the stream, so the page goes into low-data mode.
                st["remote"] = kind == "tailscale"
                return st
            if path == "/api/state":
                return self._send(200, state())
            if path in ("/api/switch", "/api/cut", "/api/fade"):
                try:
                    cam = int(p["cam"]) - 1   # 1-based in the API, like the buttons
                except (KeyError, ValueError):
                    return self._send(400, {"error": "cam (1..N) required"})
                tr = {"/api/cut": "cut", "/api/fade": "fade"}.get(path, p.get("transition"))
                ok = sw.call_in_main(sw.switch, cam, tr, p.get("duration_ms"))
                return self._send(200 if ok else 400, state())
            if path == "/api/camera_mode":
                try:
                    cam = int(p["cam"]) - 1
                except (KeyError, ValueError):
                    return self._send(400, {"error": "cam (1..N) required"})
                ok = sw.call_in_main(sw.set_camera_mode, cam, str(p.get("mode", "auto")))
                return self._send(200 if ok else 400, state())
            if path == "/api/output":
                ok = sw.call_in_main(sw.set_output, str(p.get("output", "")))
                return self._send(200 if ok else 400, state())
            if path == "/api/audio":
                ok = sw.call_in_main(sw.select_audio, p.get("source", ""))
                return self._send(200 if ok else 400, state())
            if path == "/api/settings":
                def apply():
                    if p.get("transition") in ("fade", "cut"):
                        sw.transition = p["transition"]
                    if "duration_ms" in p:
                        sw.duration_ms = max(0, min(5000, int(p["duration_ms"])))
                sw.call_in_main(apply)
                return self._send(200, state())
            if path.startswith("/preview/") and path.endswith(".jpg"):
                key = path[len("/preview/"):-4]
                data = sw.get_preview(None) if key == "program" else None
                if key.isdigit() and 1 <= int(key) <= len(sw.sources):
                    data = sw.get_preview(sw.sources[int(key) - 1])
                if data:
                    return self._send(200, data, "image/jpeg")
                return self._send(404, {"error": "no preview"})
            return self._send(404, {"error": "not found"})

    return Handler


def main():
    ap = argparse.ArgumentParser(description="Simple multicam switcher for BELABOX")
    ap.add_argument("-c", "--config", default="/etc/belaswitch.json")
    ap.add_argument("--print-pipeline", action="store_true",
                    help="print the belacoder pipeline for this config and exit")
    ap.add_argument("--no-pipeline-file", action="store_true",
                    help="don't write the pipeline into belacoder's custom/ directory")
    ap.add_argument("--diagnose", type=int, nargs="?", const=15, metavar="SECONDS",
                    help="run for a while, then print where video is (or isn't) flowing, and exit")
    args = ap.parse_args()

    cfg = load_config(args.config)
    saved_output = load_state().get("output")      # chosen on the control page
    if saved_output in OUTPUT_CHOICES:
        cfg["output"] = parse_output_key(saved_output)
    if args.print_pipeline:
        print(belacoder_pipeline(cfg), end="")
        return

    Gst.init(None)
    if not args.no_pipeline_file:
        write_pipeline_file(cfg)

    sw = Switcher(cfg)
    web = cfg["web"]
    httpd = ThreadingHTTPServer((web.get("host", "0.0.0.0"), int(web.get("port", 8181))),
                                make_handler(sw))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    log(f"control page on http://{web.get('host', '0.0.0.0')}:{web.get('port', 8181)}/")

    def stop(*_):
        sw.loop.quit()
        return False
    for sig in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, stop)
    if args.diagnose:
        GLib.timeout_add_seconds(3, lambda: (diagnose_start(sw), False)[1])
        GLib.timeout_add_seconds(3 + args.diagnose, lambda: (diagnose_report(sw), sw.loop.quit(), False)[2])
    sw.run()
    httpd.shutdown()
    return sw.exit_code


if __name__ == "__main__":
    sys.exit(main())
