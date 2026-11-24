# belaswitch: locked design (v1.0)

v1.0 is the code as of the commit that last changed this paragraph: run `git log -1 --format=%h -- belabox-multicam/CLAUDE.md` to get its hash.

This is a simple multicam switcher for BELABOX on a Rock 5T. **The owner has approved this design and wants it kept.** Fix bugs and adapt to their hardware, but don't redesign, restyle or add features unless they explicitly ask. If a change would alter anything listed under "Locked", ask them first.

## The owner's setup

- Rock 5T running BELABOX, streaming over bonded cellular with SRTLA. The owner uses **Tailscale** to reach the box.
- Cameras: an **OM System OM-1** and a **DJI Osmo Pocket 3**, both on **USB in webcam mode**. The HDMI input is optional.
- Audio: **one feed for the whole stream**, either the USB audio device or HDMI audio, chosen on the control page. Never two feeds mixed together, and never the cameras' own mics.
- Output: **1080p30**. USB webcam modes deliver at most 1080p.

## Locked

**Architecture.** `belaswitch.py` runs one GStreamer pipeline: each camera feeds a `compositor`, and the audio feeds an `audiomixer`. The result goes through `shmsink` to `/tmp/belaswitch.video` and `/tmp/belaswitch.audio`. belacoder reads those sockets through the generated pipeline `<pipelines>/custom/multicam_switcher`. belacoder keeps doing the hardware H.265 encoding, dynamic bitrate and SRTLA.

* **Never modify belaUI or belacoder.**
* Keep belacoder's element names (`venc_bps`, `overlay`, `ptsfixup`, `a_delay`/`v_delay`, `appsink`).
* Keep the exact `voaacenc bitrate=N ! aacparse !` text, because belaUI's Opus option rewrites that pattern.
* Never put an `alsasrc` in the generated pipeline, so belaUI doesn't show its own audio picker.

**Switching.** Every camera runs all the time. Switching only changes each compositor input's `alpha` and `zorder`.

* **Fade:** the incoming camera sits on top and its alpha is eased with a smoothstep curve. The default is 600 ms.
* **Cut:** instant.
* When idle, the live camera is fully opaque, so the compositor skips the hidden inputs.

**Zero-config install.** The installer writes `"cameras": "auto"` and `"audio_source": "usb"`.

* Auto mode gives every `/dev/v4l/by-id/*-video-index0` device its own button, including cameras plugged in later.
* `"type": "usb"` probes the camera's modes and prefers, in order: the output size and frame rate (29.97 counts as 30), then MJPEG, then raw, then H.264.

**Audio.**

* Exactly one feed (`AUDIO_CHOICES`: `usb` | `hdmi`). Swaps are make-before-break, and the old feed is dropped within `HANDOVER_SECONDS`.
* The choice persists in `/var/lib/belaswitch/state.json`.
* "USB" means the USB sound card that is **not** on the same USB device as a camera. Don't rely on the `usbaudio` name: BELABOX's udev rule can give that name to a camera's mic.
* A new audio source is linked to the mixer **only after its first buffer**.

**Reliability rules (each one fixed a real failure; don't remove them):**

* The pipeline uses the system clock, so unplugging an audio device can't take the clock away.
* A failed source is torn down and retried without interrupting the stream. Retries back off up to 30 s.
* The watchdog restarts a source that has gone silent: audio after 2 s (belacoder restarts the whole stream after 2–4 s without progress) and video after 5 s.
* Bins are set to NULL on a background thread, so a hung USB device can't freeze the main loop or the control page.
* An error from a source that has already been removed is ignored, not treated as fatal.

**Control page** (`web/index.html`, port 8181). Keep the look and layout:

* dark theme with a red **LIVE** border
* programme preview on top
* Fade | Cut, Audio: USB | HDMI, and a fade-length slider
* camera tiles: 2 columns on a phone, numbered 1–9, with keys 1–9 and Shift for a cut

**Tailscale and security.**

* Allowed clients: loopback, Tailscale and private networks (`web.allow`: `local` | `tailscale` | `any`).
* Tailscale is detected by the connection arriving on this machine's `tailscale0` address, **never by the 100.64.0.0/10 address alone**, because carriers use that range for CGNAT.
* Over Tailscale the page goes into low-data mode: state every 1 s, pictures every 2 s, measured at ~0.17 Mbps against ~1.2 Mbps. Every state response must include `remote`.
* Previews can be turned off on each device.

## Testing changes

The Rock 5T hardware (mpp encoder/decoder, RGA, hdmirx) isn't available off-device. Off the device, test with `"type": "test"` or `"custom"` cameras and stand-in audio bins. Check what belacoder receives by reading the shm sockets with `shmsrc`. On the device, use `journalctl -u belaswitch -f`.
