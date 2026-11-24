# belaswitch: locked design (v1.3)

v1.3 (per-camera and output quality menus, 4K/1440p only, belaUI bitrate limit 30 Mbps) is the code as of the commit that last changed this paragraph: run `git log -1 --format=%h -- belabox-multicam/CLAUDE.md` to get its hash.

This is a simple multicam switcher for BELABOX on a Rock 5T. **The owner has approved this design and wants it kept.** Fix bugs and adapt to their hardware, but don't redesign, restyle or add features unless they explicitly ask. If a change would alter anything listed under "Locked", ask them first.

## The owner's setup

- Rock 5T running BELABOX, streaming over bonded cellular with SRTLA. The owner controls it from an iPhone on the same Wi-Fi/hotspot. Tailscale is optional and still supported.
- Cameras: an **OM System OM-1** and a **DJI Osmo Pocket 3**, both on **USB in webcam mode**. The HDMI input is optional.
- Audio: **one feed for the whole stream**, either the USB audio device or HDMI audio, chosen on the control page. Never two feeds mixed together, and never the cameras' own mics.
- Output: **4K 30 normally, sometimes 1440p. Never HD.** The output menu offers only 4K 30/25 and 1440p 30/25, and the default is 4K 30.
- Picture quality matters most: the owner wants **no artifacts added by the switcher**.

## Locked

**Architecture.** `belaswitch.py` runs one GStreamer pipeline: each camera feeds a `compositor`, and the audio feeds an `audiomixer`. The result goes through `shmsink` to `/tmp/belaswitch.<W>x<H>p<fps>.video` and `.audio`. The output size is in the socket name, so a belacoder set up for the old size can never read a picture of the wrong size. belacoder reads those sockets through the generated pipeline `<pipelines>/custom/multicam_switcher`. belacoder keeps doing the hardware H.265 encoding, dynamic bitrate and SRTLA.

* **Never modify belaUI or belacoder,** with one owner-requested exception: `belaui-bitrate.sh` raises belaUI's bitrate cap from 12 to 30 Mbps (belacoder's own `ABS_MAX_BITRATE`). It changes exactly two literals, backs up `.orig` files, refuses unknown belaUI versions, supports `--revert`, and the installer runs it.
* Keep belacoder's element names (`venc_bps`, `overlay`, `ptsfixup`, `a_delay`/`v_delay`, `appsink`).
* Keep the exact `voaacenc bitrate=N ! aacparse !` text, because belaUI's Opus option rewrites that pattern.
* Never put an `alsasrc` in the generated pipeline, so belaUI doesn't show its own audio picker.

**Switching.** Every camera runs all the time. Switching only changes each compositor input's `alpha` and `zorder`.

* **Fade:** the incoming camera sits on top and its alpha is eased with a smoothstep curve. The default is 600 ms.
* **Cut:** instant.
* When idle, the live camera is fully opaque, so the compositor skips the hidden inputs.

**Zero-config install.** The installer writes `"cameras": "auto"` and `"audio_source": "usb"`.

* Auto mode gives the Rock 5's HDMI input (`/dev/hdmirx`, when present) and every `/dev/v4l/by-id/*-video-index0` device their own buttons, including cameras plugged in later. HDMI shows "waiting" until it has a signal. Like BELABOX's stock HDMI pipelines, HDMI frames flagged DROPPABLE (damaged) are dropped.
* `"type": "usb"` lists the camera's modes (`list_modes`). In Auto mode (`pick_from_modes`) it prefers: the output size (or the next size down); then the output frame rate, then an exact multiple (60 for 30), then near rates (29.97); then RAW (uncompressed) over MJPEG over H.264.

**Quality (v1.3).**

* Each USB camera has a capture-mode menu (Auto or a specific mode). The choice is saved per device in `state.json` (`camera_modes`). Changing it restarts only that camera.
* The output menu (`OUTPUT_CHOICES`) saves `output` in `state.json`, rewrites the belacoder pipeline and exits with code 75 so systemd restarts the service. A banner asks the owner to Stop/Start in belaUI until belacoder connects (`output_pending`; connection is tracked with shmsink's client signals).
* Zero added artifacts: a camera that matches the output passes through **bit-exact** (verified: 0 of 8.3 M pixels changed at 4K, live and after fades). Scaling uses `videoscale method=catrom`, measured ~60% less aliasing than bilinear. `videoconvert dither=none`. RGA hardware scaling is used only when enlarging, never when shrinking.

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

* **The owner chose no-Tailscale-needed control (v1.2).** The default `web.allow` is `local`, which allows loopback, private networks (Wi-Fi/hotspot/LAN) and Tailscale; `tailscale` allows loopback and Tailscale only; `any` allows everything. Refused clients get a plain message saying how to connect.
* Tailscale is detected by the connection arriving on one of this machine's own Tailscale addresses (`tailscale ip`, plus addresses on `tailscale*` interfaces), **never by the 100.64.0.0/10 address alone**, because carriers use that range for CGNAT. If Tailscale comes up late, the address cache is refreshed rather than refusing the owner's phone.
* Previews cycle through low-data (the default over Tailscale: state every 1 s, pictures every 2 s), full (the default on the local Wi-Fi) and off. Each device remembers its setting, and every state response must include `remote`.

## Testing changes

The Rock 5T hardware (mpp encoder/decoder, RGA, hdmirx) isn't available off-device. Off the device, test with `"type": "test"` or `"custom"` cameras and stand-in audio bins. Check what belacoder receives by reading the shm sockets with `shmsrc`. On the device, use `journalctl -u belaswitch -f`.
