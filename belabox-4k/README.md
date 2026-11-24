# BELABOX 4K tweaks (Rock 5T, HDMI camera)

Small, reversible tweaks for streaming a **4K or 1440p HDMI camera** with BELABOX:

1. **Bitrate limit 12 → 30 Mbps.** belaUI caps the bitrate slider at 12 Mbps. BELABOX's encoder (belacoder) already accepts up to 30 Mbps, so only belaUI is changed: exactly two numbers, with backups.
2. **A tidy pipeline list.** belaUI shows only the 4K and 1440p HDMI pipelines. Cam Link, USB, test pattern, 1080p, 720p and the rest are moved to a `pipelines-hidden` folder, not deleted. The pipeline you currently have selected is always kept.
3. **Named connections.** belaUI's connection list shows names like **Ethernet**, **iPhone hotspot** or **EE 5G**, with the signal strength underneath. Tap a name to set your own; each phone or browser remembers its own names.
4. **"What can my connections carry?"** Under the connection list, while streaming, it shows what your connections are sending, the best they managed in the last 2 minutes, a **suggested max bitrate** (80% of that best) with a **Use** button, and the **best resolution** for it (4K / 1440p / 1080p). If your connections keep up with your max, it tells you that instead. The suggestion needs about 20 s of streaming, and it only changes anything when you press **Use**.
5. **Removes the old multicam switcher** if it was installed.

## Install (Windows laptop)

1. In belaUI, **Stop** the stream and turn on **SSH** (Advanced / developer). Note the password.
2. In PowerShell, run `ssh user@belabox.local` and paste the password with a right-click.
3. Paste this line:
   ```
   rm -rf ~/Widget; git clone -b claude/rock5t-multicam-switcher-rpbzrz https://github.com/Axiswidget/Widget && cd Widget/belabox-4k && sudo ./install.sh
   ```
4. Reload belaUI. The bitrate slider now goes to 30 Mbps, and the pipeline list is short.

## Best 4K quality: settings that actually help

| Setting | Use | Why |
|---|---|---|
| **Pipeline** | `rk3588/h265_hdmi_4k_2160p` (no number), or `rk3588/h265_hdmi_1440p` | Passes the camera's frames through **untouched** and sends a full keyframe every **4 s** instead of every 2 s. That leaves more bits for picture detail at the same bitrate. The `…p30` pipelines force 30 fps; with a camera that sends 29.97 fps, that repeats a frame about every 33 s, a tiny stutter |
| **Bitrate (max)** | as high as your links can carry: **15–25 Mbps** for 4K | 4K30 H.265 usually needs roughly 15–25 Mbps to look clean in motion (rule of thumb, scene dependent). belacoder lowers it automatically when the links can't keep up, so a high maximum is safe |
| **1440p instead of 4K** | when the links can't sustain about 15 Mbps | At the same bitrate, 1440p usually shows fewer compression artifacts than 4K. The Rock 5T scales the 4K camera down in hardware |
| **SRT latency** | higher, e.g. **2000 ms or more** | Gives SRT more time to re-send lost packets, so you get fewer glitches on cellular. The trade-off is more delay |
| **Camera HDMI output** | 4K 29.97/30p, **clean HDMI** (no on-screen overlays), YCbCr | Overlays end up in the stream, and RGB costs extra conversion |
| **In-camera sharpening / noise** | sharpening **low**, noise reduction **on** | Noise and over-sharpening waste bitrate and turn into blocky artifacts in motion |
| **Audio codec** | Opus | Better quality than AAC at the same bitrate |

## Undo

```
cd ~/Widget/belabox-4k
sudo ./belaui-pipelines.sh --restore   # show all pipelines again
sudo ./belaui-bitrate.sh --revert      # back to the 12 Mbps limit
sudo ./belaui-addon.sh --revert        # remove the names / bitrate suggestion
```

## After a BELABOX update

An update can put the 12 Mbps limit and the full pipeline list back, and remove the add-on. Run step 3 of *Install* again. Your choices in belaUI are kept.
