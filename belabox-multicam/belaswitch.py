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
import json
import math
import os
import signal
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
VIDEO_SOCKET = "/tmp/belaswitch.video"
AUDIO_SOCKET = "/tmp/belaswitch.audio"
RETRY_SECONDS = 3
AUDIO_CAPS = "audio/x-raw,format=S16LE,layout=interleaved,rate=48000,channels=2"

DEFAULT_CONFIG = {
    "output": {"width": 1920, "height": 1080, "fps": 30},
    "transition": {"type": "fade", "duration_ms": 600},
    "audio_follows_video": True,
    "previews": True,
    "web": {"host": "0.0.0.0", "port": 8181},
    "belacoder_pipelines_dir": None,
    "audio_bitrate": 128000,
    "cameras": [
        {"name": "Test A", "type": "test", "pattern": "smpte"},
        {"name": "Test B", "type": "test", "pattern": "ball"},
    ],
    "extra_audio": [],
}


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
    gop = int(fps) * 2
    # Element names (ptsfixup, v_delay, a_delay, overlay, venc_bps, appsink)
    # are the ones belacoder looks up; the voaacenc/aacparse fragment is the
    # exact pattern belaUI rewrites when you choose Opus audio.
    return (
        f"shmsrc socket-path={VIDEO_SOCKET} is-live=true do-timestamp=true !\n"
        f"video/x-raw,format=NV12,width={w},height={h},framerate={fps}/1,pixel-aspect-ratio=1/1 !\n"
        "identity name=ptsfixup signal-handoffs=TRUE !\n"
        "identity name=v_delay signal-handoffs=TRUE ! queue max-size-buffers=3 leaky=downstream !\n"
        "textoverlay text='' valignment=top halignment=right font-desc=\"Monospace, 5\" name=overlay ! queue !\n"
        f"mpph265enc zero-copy-pkt=0 qp-max=51 gop={gop} name=venc_bps !\n"
        "h265parse config-interval=-1 ! queue max-size-time=10000000000 max-size-buffers=1000 max-size-bytes=41943040 ! mux.\n"
        f"shmsrc socket-path={AUDIO_SOCKET} is-live=true do-timestamp=true ! {AUDIO_CAPS} !\n"
        "identity name=a_delay signal-handoffs=TRUE ! volume volume=1.0 !\n"
        f"audioconvert ! voaacenc bitrate={int(cfg['audio_bitrate'])} ! aacparse ! queue max-size-time=10000000000 max-size-buffers=1000 ! mux.\n"
        "mpegtsmux name=mux !\n"
        "appsink name=appsink\n"
    )


def find_pipelines_dir(cfg):
    if cfg.get("belacoder_pipelines_dir"):
        return cfg["belacoder_pipelines_dir"]
    candidates = ["/opt/belaUI/setup.json"]
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

    @property
    def device(self):
        return self.spec.get("device")


class Switcher:
    def __init__(self, cfg):
        self.cfg = cfg
        o = cfg["output"]
        self.w, self.h, self.fps = int(o["width"]), int(o["height"]), int(o["fps"])
        self.pw = 320
        self.transition = cfg["transition"].get("type", "fade")
        self.duration_ms = int(cfg["transition"].get("duration_ms", 600))
        self.follow = bool(cfg.get("audio_follows_video", True))
        self.previews = bool(cfg.get("previews", True))
        self.sources = [Source(i, s) for i, s in enumerate(cfg["cameras"])]
        self.extra_audio = [Source(100 + i, dict(s, name=s.get("name", f"Audio {i + 1}")))
                            for i, s in enumerate(cfg.get("extra_audio", []))]
        self.active = 0
        self.previous = None
        self.fade = None          # (start_time, duration_s, from_idx, to_idx)
        self.fade_timer = None
        self.program_preview = None
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
            f"shmsink socket-path={VIDEO_SOCKET} shm-size={self.w * self.h * 3 // 2 * 8} "
            "wait-for-connection=false sync=false async=false "
        )
        if self.previews:
            desc += "vt. ! " + self._preview_tail("progpreview", 640)
        desc += (
            f" audiotestsrc wave=silence is-live=true ! {AUDIO_CAPS} ! queue ! "
            f"audiomixer name=amix latency=20000000 ! {AUDIO_CAPS} ! queue max-size-time=200000000 leaky=downstream ! "
            f"shmsink socket-path={AUDIO_SOCKET} shm-size=2000000 wait-for-connection=false sync=false async=false"
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
            f"{self.jpegenc} quality=70 ! appsink name={sink_name} emit-signals=true "
            "max-buffers=1 drop=true sync=false async=false "
        )

    def _hook_preview(self, appsink, src):
        def on_sample(sink):
            sample = sink.emit("pull-sample")
            if sample:
                buf = sample.get_buffer()
                ok, info = buf.map(Gst.MapFlags.READ)
                if ok:
                    data = bytes(info.data)
                    buf.unmap(info)
                    if src is None:
                        self.program_preview = data
                    else:
                        src.preview = data
            return Gst.FlowReturn.OK
        appsink.connect("new-sample", on_sample)

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

        if kind == "test":
            pattern = spec.get("pattern", "smpte")
            return (f"videotestsrc is-live=true pattern={pattern} ! "
                    f"video/x-raw,width={self.w},height={self.h},framerate={self.fps}/1 ! "
                    f"textoverlay text={gst_quote(s.name)} font-desc=\"Sans Bold 48\" "
                    "valignment=bottom halignment=left shaded-background=true")
        if kind == "mjpeg":
            if has_element("mppjpegdec"):
                dec = "mppjpegdec"
                # The Rockchip decoder can scale and convert with the RGA
                # hardware block, which keeps a 1080p camera in a 4K mix
                # off the CPU.
                if all(has_prop("mppjpegdec", p) for p in ("width", "height", "format")):
                    dec += f" width={self.w} height={self.h} format=NV12"
            else:
                dec = "jpegdec"
            return (f"v4l2src device={gst_quote(dev)} do-timestamp=true ! image/jpeg{caps_extra} ! "
                    f"queue max-size-buffers=2 leaky=downstream ! {dec}")
        if kind == "v4l2":
            return (f"v4l2src device={gst_quote(dev)} do-timestamp=true ! video/x-raw{caps_extra} ! "
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
        desc = (
            f"{self._video_source_desc(s)} ! videorate name=rate ! "
            f"videoscale add-borders=true{threads if self.scale_threads else ''} ! "
            f"videoconvert{threads if self.convert_threads else ''} ! {vcaps} ! tee name=t "
            "t. ! queue name=out max-size-buffers=3 leaky=downstream "
        )
        if self.previews:
            desc += "t. ! " + self._preview_tail("preview")
        b = Gst.parse_bin_from_description(desc, False)
        b.set_name(f"cam{s.index}")
        out = b.get_by_name("out")
        b.add_pad(Gst.GhostPad.new("src", out.get_static_pad("src")))
        if self.previews:
            self._hook_preview(b.get_by_name("preview"), s)
        b.get_by_name("rate").get_static_pad("sink").add_probe(
            Gst.PadProbeType.EVENT_DOWNSTREAM, self._on_input_caps, s)
        return b

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
        if s.spec.get("type", "v4l2") in ("v4l2", "mjpeg") and dev and not os.path.exists(dev):
            if not s.failing["video"]:
                log(f"[{s.name}] waiting for {dev}")
            s.failing["video"] = True
            s.error = f"{dev} not found"
            self._schedule_retry(s, "video")
            return False
        try:
            b = self._make_video_bin(s)
        except (GLib.Error, ValueError, KeyError) as e:
            s.error = f"bad camera config: {e}"
            log(f"[{s.name}] {s.error}")
            return False
        self.pipeline.add(b)
        pad = self.comp.request_pad_simple("sink_%u")
        self._apply_video_props(s, pad)
        b.get_static_pad("src").link(pad)
        s.vbin, s.vpad = b, pad
        b.sync_state_with_parent()
        s.video_ok = True
        self._announce(s, "video", b)
        return True

    def _attach_audio(self, s, tag="aud"):
        s.retry_pending["audio"] = False
        device = s.spec.get("audio") if tag == "aud" else s.spec.get("device")
        if not device:
            return False
        try:
            b = self._make_audio_bin(s, device, tag)
        except GLib.Error as e:
            log(f"[{s.name}] bad audio config: {e}")
            return False
        self.pipeline.add(b)
        pad = self.amix.request_pad_simple("sink_%u")
        pad.set_property("volume", self._audio_volume(s, tag))
        b.get_static_pad("src").link(pad)
        s.abin, s.apad = b, pad
        b.sync_state_with_parent()
        s.audio_ok = True
        self._announce(s, "audio", b, f" ({device})")
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
                if kind == "video":
                    s.error = ""
                else:
                    s.audio_error = ""
                log(f"[{s.name}] {kind} back{extra}")
            return False
        GLib.timeout_add_seconds(2, check)

    def _detach(self, s, kind):
        b, pad, mixer = ((s.vbin, s.vpad, self.comp) if kind == "video"
                         else (s.abin, s.apad, self.amix))
        if b is None:
            return
        src = b.get_static_pad("src")
        if pad is not None:
            src.unlink(pad)
            mixer.release_request_pad(pad)
        b.set_state(Gst.State.NULL)
        self.pipeline.remove(b)
        if kind == "video":
            s.vbin = s.vpad = None
            s.video_ok = False
            s.preview = None
        else:
            s.abin = s.apad = None
            s.audio_ok = False

    def _schedule_retry(self, s, kind):
        if s.retry_pending[kind]:
            return
        s.retry_pending[kind] = True

        def retry():
            if kind == "video":
                self._attach_video(s)
            else:
                self._attach_audio(s, "aud" if s.index < 100 else "extra")
            return False
        GLib.timeout_add_seconds(RETRY_SECONDS, retry)

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
            obj = obj.get_parent()
        return None, None

    def _on_message(self, bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            s, kind = self._owner(msg.src)
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
            "cameras": [
                {"index": s.index, "name": s.name, "online": s.video_ok,
                 "audio": bool(s.spec.get("audio")), "audio_online": s.audio_ok,
                 "error": s.error, "audio_error": s.audio_error,
                 "input": s.input, "cpu_work": s.cpu_work}
                for s in self.sources
            ],
        }

    # ---------------- run ----------------

    def run(self):
        for p in (VIDEO_SOCKET, AUDIO_SOCKET):
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass
        self.loop = GLib.MainLoop()
        self.pipeline.set_state(Gst.State.PLAYING)
        for s in self.sources:
            self._attach_video(s)
            self._attach_audio(s, "aud")
        for s in self.extra_audio:
            self._attach_audio(s, "extra")
        log(f"programme {self.w}x{self.h}@{self.fps} -> {VIDEO_SOCKET}, {AUDIO_SOCKET}")
        try:
            self.loop.run()
        finally:
            self.pipeline.set_state(Gst.State.NULL)

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

def make_handler(sw):
    index_path = os.path.join(HERE, "web", "index.html")

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
            path, p = self._params()
            if path in ("/", "/index.html"):
                with open(index_path, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if path == "/api/state":
                return self._send(200, sw.state())
            if path in ("/api/switch", "/api/cut", "/api/fade"):
                try:
                    cam = int(p["cam"]) - 1   # 1-based in the API, like the buttons
                except (KeyError, ValueError):
                    return self._send(400, {"error": "cam (1..N) required"})
                tr = {"/api/cut": "cut", "/api/fade": "fade"}.get(path, p.get("transition"))
                ok = sw.call_in_main(sw.switch, cam, tr, p.get("duration_ms"))
                return self._send(200 if ok else 400, sw.state())
            if path == "/api/settings":
                def apply():
                    if p.get("transition") in ("fade", "cut"):
                        sw.transition = p["transition"]
                    if "duration_ms" in p:
                        sw.duration_ms = max(0, min(5000, int(p["duration_ms"])))
                sw.call_in_main(apply)
                return self._send(200, sw.state())
            if path.startswith("/preview/") and path.endswith(".jpg"):
                key = path[len("/preview/"):-4]
                data = sw.program_preview if key == "program" else None
                if key.isdigit() and 1 <= int(key) <= len(sw.sources):
                    data = sw.sources[int(key) - 1].preview
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
    args = ap.parse_args()

    cfg = load_config(args.config)
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
    sw.run()
    httpd.shutdown()


if __name__ == "__main__":
    sys.exit(main())
