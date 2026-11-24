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
| `type` | `v4l2` for raw capture such as the Rock 5 HDMI-in. `mjpeg` for most cheap USB capture cards and webcams, decoded in hardware with `mppjpegdec` when it is available. `test` for a test pattern. `custom` for any GStreamer source fragment in `"pipeline"` |
| `device` | Video device. **For USB devices, use the `/dev/v4l/by-id/...` path.** `/dev/videoN` numbers change between boots |
| `width`/`height`/`fps` | Mode to request from the camera (optional). The switcher scales every camera to `output` |
| `audio` | ALSA device for this camera's sound (optional). List devices with `arecord -l`; `hw:CARD=<name>` uses the name in brackets |
| `audio_volume` | 0.0–2.0, default 1.0 |

**Audio.** With `"audio_follows_video": true`, you hear the audio of whichever camera is live, crossfaded with the picture. If you use a single mic for everything instead, leave `audio` off the cameras and add it as an always-on source:

```json
"extra_audio": [ { "name": "Lav mic", "device": "hw:CARD=usbaudio", "volume": 1.0 } ]
```

**Changing the output resolution or frame rate.** Edit `output` and restart the switcher. It rewrites the belacoder pipeline to match, and the change applies the next time you start the stream in belaUI.

**Phone or network camera (optional).** Use the `custom` type with any GStreamer chain that ends in raw video. For example, an SRT listener on port 4001:

```json
{ "name": "Phone", "type": "custom",
  "pipeline": "srtsrc uri=srt://:4001?mode=listener ! tsdemux ! h264parse ! mppvideodec" }
```

## HTTP API (for a Stream Deck, Bitfocus Companion, etc.)

Camera numbers start at 1.

| Request | Effect |
|---|---|
| `GET /api/switch?cam=2` | Switch to camera 2 using the current transition |
| `GET /api/cut?cam=2` / `GET /api/fade?cam=2` | Force a cut or a fade |
| `GET /api/switch?cam=2&duration_ms=1500` | Use a one-off fade length |
| `POST /api/settings` `{"transition":"cut","duration_ms":800}` | Change the defaults |
| `GET /api/state` | Live camera, camera status and settings (JSON) |
| `GET /preview/program.jpg`, `/preview/1.jpg` … | Preview stills |

## How it works

* `belaswitch.py` runs one GStreamer pipeline. Each camera is decoded and scaled to the output size, then fed into a `compositor`. Switching changes each input's `alpha` and `zorder`. A fade raises the new camera's alpha frame by frame. When no fade is running, the live camera is fully opaque, so the compositor skips the hidden inputs and uses almost no CPU on them.
* Audio from the cameras goes into an `audiomixer`, and each input's `volume` follows the video.
* The finished raw video (NV12) and audio go through `shmsink` to `/tmp/belaswitch.video` and `/tmp/belaswitch.audio`.
* The switcher writes `<belacoder pipelines>/custom/multicam_switcher`. It reads both sockets with `shmsrc` and uses the same element names as the stock rk3588 pipelines (`venc_bps`, `overlay`, `ptsfixup`, `a_delay`/`v_delay`, `appsink`). Because of that, belacoder's dynamic bitrate and belaUI's audio-codec option work unchanged.
* Each camera runs in its own sub-pipeline. When one fails, it is removed and retried without interrupting the stream.

Nothing in belaUI or belacoder is modified, so BELABOX updates won't break it. If an update ever wipes the `custom/` pipeline folder, restarting `belaswitch` puts the file back.

## Troubleshooting

* `journalctl -u belaswitch -f` shows camera connect and disconnect events.
* **"multicam_switcher" is missing in belaUI.** Run `sudo systemctl restart belaUI`. It only reads the pipeline list at startup.
* **belaUI says "input source has stalled".** The switcher isn't running. Check `systemctl status belaswitch`.
* **High CPU or dropped frames.** Match the camera mode to the output (`width`/`height`/`fps`) so no scaling is needed. Use `mjpeg` mode for USB cards, which decodes in hardware. Set `"previews": false` to save a little more.
* **A GStreamer element is missing at install time.** `compositor`/`audiomixer` come from `gstreamer1.0-plugins-base`, `shmsink`/`shmsrc` from `gstreamer1.0-plugins-bad`, and `jpegenc`/`v4l2src` from `gstreamer1.0-plugins-good`. The BELABOX image normally includes all of them.
* **No sound.** Run `arecord -l` and check that the `hw:CARD=` names match. The camera tile shows "audio offline" when the device can't be opened.

## Security

The control page has no password and listens on every network interface, so anyone who can reach port 8181 can switch cameras. Cellular modems are usually behind carrier NAT, so in practice this normally means your local Wi-Fi or hotspot. To lock it down, set `"web": {"host": "127.0.0.1"}` and reach it through an SSH tunnel, or firewall port 8181.
