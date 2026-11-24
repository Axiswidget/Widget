# BELABOX Multicam Switcher (Rock 5T / RK3588)

A simple multicam switcher for BELABOX. You can switch between cameras with smooth crossfades or hard cuts from your phone. BELABOX still does the parts it already does well: hardware H.265 encoding, adaptive bitrate and SRTLA bonding.

```
 HDMI-in ─┐
 USB cam ─┼─► belaswitch (crossfade/cut) ──shared memory──► belacoder ──► srtla ──► your server
 USB cam ─┘        ▲
                   └── phone on the same Wi-Fi / hotspot (or Tailscale):  http://<belabox-ip>:8181
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
5. **On your phone, on the same Wi-Fi or BELABOX hotspot as the box**, open the address the installer printed. It's the same address you use for belaUI, plus **`:8181`**, e.g. `http://192.168.4.1:8181`. Tap a camera to switch. (It also works from anywhere over Tailscale, if the box is on your tailnet.)

**Locked version (v1.3).** The design is frozen (see `CLAUDE.md`). To find the locked commit, run `git log -1 --format=%h -- belabox-multicam/CLAUDE.md`. If later changes ever need undoing, `git checkout <that hash>` followed by `sudo ./install.sh` puts it back.

If the BELABOX says `git: command not found`, download it this way instead (same files):
```sh
curl -fsSL https://github.com/Axiswidget/Widget/archive/refs/heads/claude/rock5t-multicam-switcher-rpbzrz.tar.gz | tar xz
cd Widget-claude-rock5t-multicam-switcher-rpbzrz/belabox-multicam
sudo ./install.sh
```

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
3. On your phone, on the same Wi-Fi or hotspot as the box, open **`http://<belabox-ip>:8181`**. Over Tailscale, use the box's Tailscale address instead.

## Configuring cameras

By default the installer sets `"cameras": "auto"`: **every connected camera is detected automatically**. The Rock 5's **HDMI input** is always button 1 (it shows "waiting" until an HDMI camera with a signal is connected). Every USB camera or capture card gets a button, including cheap cards without a serial number, cameras plugged in later, and cameras unplugged and plugged back in, which return to the same button, named after the camera, with its own sound. Buttons are numbered in the order the cameras are found. To choose names or the order yourself, or to add the HDMI input, replace `"auto"` with a list as shown below.

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
4. The output starts at **4K 30**; change it with the **Output** menu. Open each camera's menu to see the modes it really offers over USB. If a camera tops out at 1080p in webcam mode, it's upscaled cleanly but can't show true 4K detail (see *Picture quality*).
5. If you don't use the HDMI input, delete its entry.

Check the log (`journalctl -u belaswitch -f`). For each camera you should see a line like `USB mode: MJPEG width=1920,height=1080,framerate=30/1`, followed by `input ...`.

**USB power and bandwidth:** plug the cameras into the Rock 5T's USB 3 ports, or a powered hub. Long streams in webcam mode drain camera batteries, so use USB power or a dummy battery if your camera supports it.

## Picture quality: output and camera menus

**Output menu** (on the control page): **4K 30** (default), **4K 25**, **1440p 30** or **1440p 25**. Changing it restarts the switcher for a few seconds. Then press **Stop** and **Start** in belaUI so the stream uses the new size. A yellow banner reminds you until belaUI has picked it up, and the **● belaUI streaming** pill shows when it's connected.

**Camera menus** (under each camera): **Auto**, or a specific mode the camera offers, e.g. `2160p30 MJPEG`, `1080p60 MJPEG`, `1080p30 RAW`. Auto picks:
1. the output size, or the largest size below it;
2. the output frame rate, or an exact multiple (60 for 30, halved cleanly), before near rates such as 29.97;
3. **RAW** (uncompressed) over **MJPEG** over H.264.

Your choice is remembered for each camera. Changing a live camera blacks it out for about a second, so the page asks first.

### Artifacts: what is guaranteed and what isn't

* **The switcher adds no artifacts.** When a camera's mode matches the output, its picture reaches the encoder **bit-exact**. This was tested at 4K: 0 of 8.3 million pixels changed, live and after fades. Fades blend two clean pictures, and the moment a fade ends the picture is bit-exact again.
* **When scaling is needed** (e.g. a 1080p camera in a 4K output), the switcher uses Catmull-Rom. Measured against the default scaler, it has about 60% less shimmer/moiré when shrinking and is sharper when enlarging. Enlarging can't add detail, though: a 1080p camera looks like 1080p, softly upscaled, and the tile's label says so. For the cleanest 4K, give it a camera mode of **2160p**.
* **MJPEG** modes are compressed by the camera before the switcher sees them. Choose a **RAW** mode where the camera offers one at the size and rate you want (usually only over USB 3).
* **The encoder:** belacoder compresses the stream to fit what your modems can carry, and that is where most visible artifacts in a live stream come from. No stream over cellular can promise zero. In motion, 4K30 H.265 usually needs roughly **15–25 Mbps** to look clean (rule of thumb, scene dependent), well above belaUI's normal 12 Mbps cap. That's why the installer raises it to 30 Mbps (below). belacoder still lowers the bitrate automatically whenever the links can't carry it. If your links often can't sustain about 15 Mbps, 1440p will usually look cleaner than 4K at the same bitrate.

### belaUI bitrate limit: 12 → 30 Mbps

belaUI caps the bitrate slider at 12 Mbps, but belacoder, BELABOX's encoder, accepts up to 30 Mbps. The installer runs `belaui-bitrate.sh`, which raises belaUI's cap to 30 Mbps:
* It changes exactly two numbers in belaUI and keeps backups (`*.orig`).
* If your belaUI version looks different, it changes nothing.
* Run `sudo ./belaui-bitrate.sh --revert` to undo it.

**A BELABOX update that replaces belaUI puts the 12 Mbps cap back.** Just run `sudo ./belaui-bitrate.sh` again, or re-run the installer, then reload the belaUI page.

### Rock 5T CPU at 4K

A camera that already delivers NV12 at the output size costs almost nothing. Converting or scaling is real work at 4K: the log and the tile tooltip show which cameras need it. Measured per 4K frame on a cloud server (the Rock 5T is roughly 2–4× slower): Catmull-Rom scaling ~5 ms, YUY2/I420→NV12 ~1–2 ms, mixing and fades ~3–4 ms. MJPEG cameras are decoded and enlarged by the Rock 5T's hardware where available.

## HTTP API (for a Stream Deck, Bitfocus Companion, etc.)

Camera numbers start at 1.

| Request | Effect |
|---|---|
| `GET /api/switch?cam=2` | Switch to camera 2 using the current transition |
| `GET /api/cut?cam=2` / `GET /api/fade?cam=2` | Force a cut or a fade |
| `GET /api/switch?cam=2&duration_ms=1500` | Use a one-off fade length |
| `POST /api/settings` `{"transition":"cut","duration_ms":800}` | Change the defaults |
| `POST /api/audio?source=usb` / `?source=hdmi` | Choose the audio feed |
| `POST /api/output?output=3840x2160@30` | Choose the output (`3840x2160@30`, `3840x2160@25`, `2560x1440@30`, `2560x1440@25`) |
| `POST /api/camera_mode?cam=1&mode=auto` | Choose a camera's capture mode (`auto` or an id from `/api/state`) |
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

## Tailscale (optional: control from anywhere)

You don't need Tailscale: a phone on the same Wi-Fi or BELABOX hotspot can use the control page. If the BELABOX is on your tailnet, you can also open `http://<belabox-tailscale-name>:8181` (or its `100.x` address) from anywhere. The installer prints it when Tailscale is running on the box. If you add Tailscale, switch off `tailscale0` in belaUI's network list so your stream isn't bonded over it.

Over Tailscale, the page's traffic can leave the BELABOX through the same cellular modems as your stream, so it uses **low-data mode** there. On the local Wi-Fi it shows full previews. The **Previews** button at the top cycles through these settings, and each phone remembers its choice:

| Previews | Upload used on the BELABOX | When |
|---|---|---|
| low-data (default over Tailscale) | ~0.1–0.2 Mbps | away from the box |
| full (default on Wi-Fi/hotspot) | ~1.2 Mbps | phone on the box's Wi-Fi/hotspot (doesn't touch cellular) |
| off | ~0.01 Mbps | maximum bandwidth for the stream |

These were measured with detailed, hard-to-compress images. Switching cameras itself uses almost no data.

## Security

The control page has no password, so it only answers **the BELABOX itself, devices on the same Wi-Fi / hotspot / LAN, and your Tailscale devices** (`"allow": "local"`, the default). Anything from the internet or the mobile carrier's network is refused. Tailscale is recognised by the connection arriving on the box's own Tailscale address, not by the `100.x` address alone, because carriers use that same range for cellular addresses.

Anyone on the same Wi-Fi can switch your cameras. To lock it to your Tailscale devices only (for example on venue Wi-Fi shared with strangers), set `"allow": "tailscale"` in the `web` section of `/etc/belaswitch.json` and run `sudo systemctl restart belaswitch`. `"allow": "any"` removes the restriction entirely.
