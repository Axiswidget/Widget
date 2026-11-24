# BELABOX Multicam Switcher (Rock 5T / RK3588)

A simple multicam switcher for BELABOX. You can switch between cameras with smooth crossfades or hard cuts from your phone. BELABOX still does the parts it already does well: hardware H.265 encoding, adaptive bitrate and SRTLA bonding.

```
 HDMI-in ─┐
 USB cam ─┼─► belaswitch (crossfade/cut) ──shared memory──► belacoder ──► srtla ──► your server
 USB cam ─┘        ▲
                   └── phone / laptop:  http://<belabox-ip>:8181
```

* **Smooth transitions:** the incoming camera fades in over the outgoing one with an eased curve. The audio does an equal-power crossfade over the same time. You can also choose a hard cut.
* **Instant switching:** every camera keeps running all the time, so a switch never waits for a device to start.
* **Keeps running when a camera drops:** if you unplug a camera or an HDMI source loses signal, the stream continues. The switcher reconnects the camera automatically every 3 s. If the live camera drops, the output shows black until you switch away or it comes back.
* **Simple control page:** it shows a live programme view and a thumbnail for each camera. Tap a camera to take it live, or press keys `1`–`9` (hold `Shift` for a cut).
* **Works inside belaUI:** belaUI lists it as a normal pipeline, `custom/multicam_switcher`. Bitrate control, the bitrate overlay, A/V delay and the AAC/Opus choice all still work.

## Quick start

You need the BELABOX connected to the internet, and a laptop or phone on the same network.

1. **Log in to the BELABOX.** Turn on SSH in belaUI's settings, then on your laptop run `ssh <username>@<belabox-ip>`.
2. **Install** (paste all three lines):
   ```sh
   git clone -b claude/rock5t-multicam-switcher-rpbzrz https://github.com/Axiswidget/Widget
   cd Widget/belabox-multicam
   sudo ./install.sh
   ```
3. **Plug in your USB cameras** and choose **webcam / USB streaming** mode on each. They show up as buttons on their own. **Plug in your USB audio device** too. Its sound is used for the whole stream, whichever camera is live. To use HDMI audio instead, tap **HDMI** next to "Audio" on the control page.
4. **In belaUI**, choose the pipeline **`custom/multicam_switcher`** and press Start.
5. **On your phone**, open **`http://<belabox-ip>:8181`** and tap a camera to switch.

To install exactly the locked **v1.0** version, use `git clone -b v1.0 https://github.com/Axiswidget/Widget` in step 2.

There's no config to edit. Cameras are found automatically. To update later, run `cd Widget && git pull && cd belabox-multicam && sudo ./install.sh`.

## Install

Copy this folder to the BELABOX, for example with `scp -r belabox-multicam user@belabox.local:` or `git clone`. Then run:

```sh
cd belabox-multicam
sudo ./install.sh
```

Then:

1. Edit `/etc/belaswitch.json` to list your cameras (see below), then `sudo systemctl restart belaswitch`.
2. In belaUI, pick the pipeline **`custom/multicam_switcher`** and press Start.
3. On your phone (same network or hotspot), open **`http://<belabox-ip>:8181`**.

## Configuring cameras

By default the installer sets `"cameras": "auto"`. Every USB camera that is plugged in (or plugged in later) gets a button, named after the camera, with its own sound. Buttons are numbered in the order the cameras are found. To choose names or the order yourself, or to add the HDMI input, replace `"auto"` with a list as shown below.

`/etc/belaswitch.json`:

```json
{
  "output": { "width": 1920, "height": 1080, "fps": 30 },
  "transition": { "type": "fade", "duration_ms": 600 },
  "audio_follows_video": true,
  "cameras": [
    { "name": "HDMI", "type": "v4l2", "device": "/dev/hdmirx", "audio": "hw:CARD=rockchiphdmiin" },
    { "name": "USB cam", "type": "mjpeg",
      "device": "/dev/v4l/by-id/usb-MACROSILICON_USB_Video-video-index0",
      "width": 1920, "height": 1080, "fps": 30, "audio": "hw:CARD=MS2109" }
  ]
}
```

| Camera field | Meaning |
|---|---|
| `name` | Label on the button |
| `type` | **`usb` for USB cameras in webcam mode** (OM-1, DJI Pocket 3, webcams, capture cards). It asks the camera which modes it supports and picks the best one: output size and frame rate if offered, MJPEG over raw, and H.264 as a fallback. The choice is logged. `v4l2` for raw capture such as the Rock 5 HDMI-in. `mjpeg` for most cheap USB capture cards and webcams, decoded in hardware with `mppjpegdec` when it is available. `test` for a test pattern. `custom` for any GStreamer source fragment in `"pipeline"` |
| `device` | Video device. **For USB devices, use the `/dev/v4l/by-id/...` path.** `/dev/videoN` numbers change between boots |
| `width`/`height`/`fps` | Mode to request from the camera (optional). The switcher scales every camera to `output` |
| `audio` | Sound for this camera (optional). `"auto"` finds the USB camera's own microphone or audio input. Otherwise give an ALSA device: run `arecord -l`, then use `hw:CARD=<name>` with the name in brackets |
| `audio_volume` | 0.0–2.0, default 1.0 |

**Audio: one feed for every camera (the installer's default).** The control page has an **Audio: USB | HDMI** switch. Exactly one of them is used for the whole stream, whichever camera is live, and your choice is remembered after a restart. Swapping audio is gapless: the new feed takes over as soon as it delivers sound.

* **USB** is your USB audio device (mic, receiver or interface). The switcher picks the USB sound card that *isn't* part of a camera, so the cameras' own microphones are never used by mistake.
* **HDMI** is the sound on the Rock 5's HDMI input (`hdmi_audio_device`, default `hw:CARD=rockchiphdmiin`).

A 🎙 pill shows the feed is delivering sound. It turns red ("no sound") if not, for example HDMI selected with nothing plugged in. You never get two feeds mixed together. In the config, `"audio_source"` sets the starting choice (`"usb"` or `"hdmi"`). You can also give a fixed ALSA device such as `"hw:CARD=usbaudio"` (no switch is shown then), or `"camera"` to have each camera's own sound follow the picture.

**Audio: each camera's own sound.** With `"audio_source": "camera"` and `"audio_follows_video": true`, you hear the audio of whichever camera is live, crossfaded with the picture. If you use a single mic for everything instead, leave `audio` off the cameras and add it as an always-on source:

```json
"extra_audio": [ { "name": "Lav mic", "device": "hw:CARD=usbaudio", "volume": 1.0 } ]
```

**Changing the output resolution or frame rate.** Edit `output` and restart the switcher. It rewrites the belacoder pipeline to match, and the change applies the next time you start the stream in belaUI.

**Phone or network camera (optional).** Use the `custom` type with any GStreamer chain that ends in raw video. For example, an SRT listener on port 4001:

```json
{ "name": "Phone", "type": "custom",
  "pipeline": "srtsrc uri=srt://:4001?mode=listener ! tsdemux ! h264parse ! mppvideodec" }
```

## Example: OM-1 + DJI Pocket 3 over USB (+ optional HDMI)

`config.example.json` is set up for this. Each camera becomes a button: 1 = OM-1, 2 = Pocket 3, 3 = HDMI. Any camera that isn't connected just shows as offline until it is.

1. Put both cameras in **USB webcam / USB streaming mode**. When you plug in the USB cable, each camera asks which USB mode to use; pick the webcam or streaming option (the wording varies by model and firmware). Mass-storage or charging-only modes don't provide video.
2. On the BELABOX, run `ls /dev/v4l/by-id/`. Each camera has an entry ending in `-video-index0`. Copy those paths into `device`. Unlike `/dev/videoN`, they don't change when you replug.
3. Leave `"audio": "auto"`. Each camera's sound is then matched to it automatically and follows the picture when you switch.
4. Keep the output at **1080p30**. Cameras in USB webcam mode generally deliver at most 1080p, so a 4K output would only upscale them and use more CPU and bitrate. Use 4K only if your main camera comes in over HDMI at 4K.
5. If you don't use the HDMI input, delete its entry.

Check the log (`journalctl -u belaswitch -f`). For each camera you should see a line like `USB mode: MJPEG width=1920,height=1080,framerate=30/1`, followed by `input ...`.

**USB power and bandwidth:** plug the cameras into the Rock 5T's USB 3 ports, or a powered hub. Long streams in webcam mode drain camera batteries, so use USB power or a dummy battery if your camera supports it.

## 4K 30 fps

The switcher can mix and stream in 4K. Set `"output": { "width": 3840, "height": 2160, "fps": 30 }`. belacoder then hardware-encodes the 4K mix, just like BELABOX's stock `h265_hdmi_4k_2160p30` pipeline.

At 4K, the important difference from stock BELABOX is where the work happens. BELABOX on its own sends the HDMI feed straight to the hardware encoder. The switcher has to mix the cameras *before* the encoder, so every camera is processed by the CPU. That work is nearly free when a camera already delivers **4K NV12 at 30 fps**. It gets expensive when a camera needs converting or scaling.

Estimates per camera at 4K30. They were measured on a cloud server, and the Rock 5T's cores are roughly 2–4× slower, so treat them as a guide. The 33 ms frame budget at 30 fps is shared by all cameras:

| Camera delivers | CPU work each frame | Verdict |
|---|---|---|
| 4K NV12 | none (passthrough) | ✅ ideal |
| 4K NV16 / NV24 (YUV 4:2:2 / 4:4:4) | chroma convert, ~1 ms | ✅ fine |
| 4K RGB/BGR | colour convert, ~11 ms here (≈25–40 ms on the Rock 5T) | ❌ likely drops frames |
| 1080p MJPEG USB card | scaled by the RGA hardware when `mppjpegdec` supports it | ✅ fine |
| 1080p raw USB/HDMI | software upscale to 4K, ~8 ms here | ⚠️ one is OK, several are not |
| mixing and fades | ~3–4 ms here | ✅ fine |

**Check what your camera actually delivers.** The log prints one line per camera, and the control page shows each camera's resolution. If a camera costs CPU work, its label turns amber and hovering over it shows what it costs:

```
[HDMI] input 3840x2160 NV12 @30                    <- good
[USB]  input 1920x1080 BGR @30 - CPU scaling ..., converting BGR->NV12 every frame (heavy)
```

To see what the HDMI input receives, run `v4l2-ctl -d /dev/hdmirx --get-fmt-video`. If it reports RGB, set your camera's HDMI output colour format to **YCbCr / YUV** (often called "HDMI output → YCbCr 4:2:2" or similar) instead of RGB.

**Tips for 4K multicam**

* Keep the extra cameras on **MJPEG USB capture cards**. They are decoded and upscaled in hardware.
* Set `"previews": false` if you need every last bit of CPU.
* 4K needs a lot of bitrate. Over bonded cellular at around 6–10 Mbps, 1080p often looks better than 4K. You can still feed in a 4K camera and set the output to 1080p. That costs one software downscale of the 4K camera (~3 ms here) and makes the mixing 4× cheaper.

## HTTP API (for a Stream Deck, Bitfocus Companion, etc.)

Camera numbers start at 1.

| Request | Effect |
|---|---|
| `GET /api/switch?cam=2` | Switch to camera 2 using the current transition |
| `GET /api/cut?cam=2` / `GET /api/fade?cam=2` | Force a cut or a fade |
| `GET /api/switch?cam=2&duration_ms=1500` | Use a one-off fade length |
| `POST /api/settings` `{"transition":"cut","duration_ms":800}` | Change the defaults |
| `POST /api/audio?source=usb` / `?source=hdmi` | Choose the audio feed |
| `GET /api/state` | Live camera, camera status and settings (JSON) |
| `GET /preview/program.jpg`, `/preview/1.jpg` … | Preview stills |

## How it works

* `belaswitch.py` runs one GStreamer pipeline. Each camera is decoded and scaled to the output size, then fed into a `compositor`. Switching changes each input's `alpha` and `zorder`. A fade raises the new camera's alpha frame by frame. When no fade is running, the live camera is fully opaque, so the compositor skips the hidden inputs and uses almost no CPU on them.
* Audio from the cameras goes into an `audiomixer`, and each input's `volume` follows the video.
* The finished raw video (NV12) and audio go through `shmsink` to `/tmp/belaswitch.video` and `/tmp/belaswitch.audio`.
* The switcher writes `<belacoder pipelines>/custom/multicam_switcher`. It reads both sockets with `shmsrc` and uses the same element names as the stock rk3588 pipelines (`venc_bps`, `overlay`, `ptsfixup`, `a_delay`/`v_delay`, `appsink`). Because of that, belacoder's dynamic bitrate and belaUI's audio-codec option work unchanged.
* Each camera runs in its own sub-pipeline. When one fails, it is removed and retried without interrupting the stream.
* A watchdog restarts any source that stops sending without reporting an error, which USB audio devices are known to do. A new audio source joins the mix only once it is actually delivering sound. So a frozen device can never silence the stream or make belaUI restart it: the audio just goes quiet until the device recovers.

Nothing in belaUI or belacoder is modified, so BELABOX updates won't break it. If an update ever wipes the `custom/` pipeline folder, restarting `belaswitch` puts the file back.

## Troubleshooting

* `journalctl -u belaswitch -f` shows camera connect and disconnect events.
* **"multicam_switcher" is missing in belaUI.** Run `sudo systemctl restart belaUI`. It only reads the pipeline list at startup.
* **belaUI says "input source has stalled".** The switcher isn't running. Check `systemctl status belaswitch`.
* **High CPU or dropped frames.** Match the camera mode to the output (`width`/`height`/`fps`) so no scaling is needed. Use `mjpeg` mode for USB cards, which decodes in hardware. Set `"previews": false` to save a little more.
* **A GStreamer element is missing at install time.** `compositor`/`audiomixer` come from `gstreamer1.0-plugins-base`, `shmsink`/`shmsrc` from `gstreamer1.0-plugins-bad`, and `jpegenc`/`v4l2src` from `gstreamer1.0-plugins-good`. The BELABOX image normally includes all of them.
* **No sound.** Run `arecord -l` and check that the `hw:CARD=` names match. The camera tile shows "audio offline" when the device can't be opened.

## Tailscale

The control page works over Tailscale, so you can switch cameras from anywhere: `http://<belabox-tailscale-name-or-100.x-ip>:8181`. The installer prints the address at the end.

Over Tailscale, the page's traffic leaves the BELABOX through the same cellular modems as your stream. So when you open it over Tailscale, it switches itself to **low-data mode**: pictures refresh every 2 s instead of about 3 times a second. Tap **Previews** at the top to turn pictures off completely. That setting is remembered on that phone. Measured with detailed (hard-to-compress) camera images:

| Control page | Upload used on the BELABOX |
|---|---|
| On local Wi-Fi / hotspot, previews on | ~1.2 Mbps (doesn't touch the cellular links) |
| Over Tailscale, low-data previews | ~0.17 Mbps |
| Over Tailscale, previews off | ~0.005 Mbps |

Switching itself uses almost no data, and when you aren't looking at the page (tab in the background) it barely polls.

## Security

The control page has no password, so it only answers devices you control: **this BELABOX, your Tailscale devices, and private networks** (your Wi-Fi, the BELABOX hotspot, your LAN). Anything coming from the internet or the mobile carrier's network is refused. Tailscale is recognised by the interface the connection arrives on, not by the `100.x` address alone, because carriers use that same range for cellular addresses.

To allow **only** Tailscale (for example when the BELABOX is on venue Wi-Fi shared with strangers), set `"web": {"allow": "tailscale"}` in `/etc/belaswitch.json` and restart. `"allow": "any"` removes the restriction.
