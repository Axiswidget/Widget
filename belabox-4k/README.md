# BELABOX 4K tweaks (Rock 5T, HDMI camera)

Small, reversible tweaks for streaming **4K** with BELABOX (HDMI camera or DJI Pocket 3 on USB):

1. **Bitrate limit 12 → 30 Mbps.** belaUI caps the bitrate slider at 12 Mbps. BELABOX's encoder (belacoder) already accepts up to 30 Mbps, so only belaUI is changed: exactly two numbers, with backups.
2. **A tidy pipeline list.** belaUI shows just two pipelines: **HDMI 4K** and **USB 4K** (USB = DJI Osmo Pocket 3). Cam Link, test pattern, 1080p, 720p and the rest are moved to a `pipelines-hidden` folder, not deleted. The pipeline you currently have selected is always kept.
3. **Named connections.** belaUI's connection list shows names like **Ethernet**, **iPhone hotspot** or **EE 5G**, with the signal strength underneath. Tap a name to set your own; each phone or browser remembers its own names.
4. **"What can my connections carry?"** Under the connection list, while streaming, it shows what your connections are sending, the best they managed in the last 2 minutes, a **suggested max bitrate** (80% of that best) with a **Use** button, and how good **4K** will look at that bitrate (Excellent 18+ Mbps · Good 12–18 · Fair 8–12 · Low under 8). If your connections keep up with your max, it tells you that instead. The suggestion needs about 20 s of streaming, and it only changes anything when you press **Use**.
5. **Guardian (reliability, automatic).** Runs on its own, with nothing to set:
   * **Soft start:** every stream, and every automatic reconnect, starts at 4 Mbps and climbs to your max over 6 s. It never starts by overloading the connections. BELABOX's encoder normally starts at the max, which is what caused repeated blank screens.
   * **Learns from drops:** if the **connection** drops anyway, it caps the bitrate at 80% of what your connections were actually carrying in that run, so it stops overloading them. It checks belaUI's log for *why* the encoder restarted, and leaves the bitrate alone for a camera/HDMI hiccup, for "couldn't connect at all", or when no real data had flowed yet.
   * **Recovers:** after every 3 minutes without a drop it raises the cap by 25%, back up to your max (e.g. 8 → 20 Mbps in about 12 minutes), with no restart. This suits walking, where signal changes every few minutes.
   * **Your choice wins:** moving the bitrate slider, or pressing Start, clears the cap.
   * The add-on panel shows it as **Auto-protect**. It uses the same bitrate file and reload signal as belaUI's slider, always restores your max, and if it isn't running BELABOX behaves exactly as stock.
6. **Removes the old multicam switcher** if it was installed.

## Install (Windows laptop)

1. In belaUI, **Stop** the stream and turn on **SSH** (Advanced / developer). Note the password.
2. In PowerShell, run `ssh user@belabox.local` and paste the password with a right-click.
3. Paste this line:
   ```
   rm -rf ~/Widget; git clone -b claude/rock5t-multicam-switcher-rpbzrz https://github.com/Axiswidget/Widget && cd Widget/belabox-4k && sudo ./install.sh
   ```
4. Reload belaUI. The bitrate slider now goes to 30 Mbps, and the pipeline list is short.

## Which pipeline?

| Camera | Pipeline |
|---|---|
| **HDMI 4K** | `rk3588/h265_hdmi_4k_2160p` |
| **USB 4K** (DJI Pocket 3) | `rk3588/h265_libuvch264_4k_2160p30` |

Only these two are shown (plus whichever pipeline you had selected, so it never disappears). `sudo ./belaui-pipelines.sh --restore` brings back all the others.

For the Pocket 3, put it in **webcam (USB streaming) mode** when you plug it in. Its microphone appears in belaUI's audio source list as **DJIPocket3**.

## Best 4K quality: settings that actually help

| Setting | Use | Why |
|---|---|---|
| **Pipeline** | `rk3588/h265_hdmi_4k_2160p` (no number), or USB 4K for the Pocket 3 | Passes the camera's frames through **untouched** and sends a full keyframe every **4 s** instead of every 2 s. That leaves more bits for picture detail at the same bitrate. The `…p30` pipelines force 30 fps; with a camera that sends 29.97 fps, that repeats a frame about every 33 s, a tiny stutter |
| **Bitrate (max)** | as high as your links can carry: **15–25 Mbps** for 4K | 4K30 H.265 usually needs roughly 15–25 Mbps to look clean in motion (rule of thumb, scene dependent). belacoder lowers it automatically when the links can't keep up, so a high maximum is safe |
| **More connections** | 2–3 SIMs on different networks | 4K looks best at 18+ Mbps. More connections is the way to get there, and to keep it when one network is weak |
| **SRT latency** | higher, e.g. **2000 ms or more** | Gives SRT more time to re-send lost packets, so you get fewer glitches on cellular. The trade-off is more delay |
| **Camera HDMI output** | 4K 29.97/30p, **clean HDMI** (no on-screen overlays), YCbCr | Overlays end up in the stream, and RGB costs extra conversion |
| **In-camera sharpening / noise** | sharpening **low**, noise reduction **on** | Noise and over-sharpening waste bitrate and turn into blocky artifacts in motion |
| **Audio codec** | Opus | Better quality than AAC at the same bitrate |

## Walking with patchy signal: simple setup

| Setting | Set to |
|---|---|
| Pipeline | **HDMI 4K** `h265_hdmi_4k_2160p`, or **USB 4K** `h265_libuvch264_4k_2160p30` for the Pocket 3 |
| Max bitrate | **20000**. The guardian lowers it automatically when the signal can't carry it, and raises it again when it can |
| SRT latency | **3000 ms**, so short dips don't show |
| Connections | **two or more, on different networks** |
| OBS | a **"Be right back" scene** that switches on automatically (e.g. NOALBS), for when the signal is truly gone |

The panel's **4K quality** line tells you how good the picture is at what your connections carry: **Excellent** at 18+ Mbps, **Good** at 12–18, **Fair** at 8–12, **Low** under 8. If it often says Fair or Low, add a connection.

## Reliability checklist

| | Do this | Why |
|---|---|---|
| 1 | **Two or three modems / SIMs on different networks** (e.g. EE + Vodafone + Three) | When one network is weak in an area, the others carry the stream. This is the biggest reliability gain |
| 2 | **External antennas**, modems high up, not inside a bag against your body | Better signal means more bitrate and fewer drops |
| 3 | **SRT latency 3000 ms** in patchy areas (2000 ms in good ones) | More time to re-send lost packets, so short dips don't show |
| 4 | **Max bitrate about 80% of what the connections carry.** The add-on's *Use* button does this | Leaves headroom for dips |
| 5 | **Powered USB hub** for modems, and a good power supply for the Rock 5T | Modems drop out when USB power sags |
| 6 | **Keep the Rock 5T cool** (a fan or heatsink, and not in direct sun) | Hot chips slow down. Watch *SoC temperature* in belaUI; under about 70 °C is comfortable |
| 7 | iPhone hotspot: keep the phone **charging and unlocked**, or use a real modem | iOS can slow or pause the hotspot to save power |
| 8 | **A "Be right back" scene in OBS** (e.g. with NOALBS) | When the signal really is gone, viewers see a BRB screen instead of a frozen or blank one |

## Check it on the box (self-test)

```
cd ~/Widget/belabox-4k && sudo ./selftest.sh
```
Run it once before streaming, and once **while streaming**, which adds the live checks. It changes nothing and prints PASS / FAIL / WARN for:
* the guardian: running, starting at boot, restarted forever, no errors;
* belaUI's log being readable;
* the 30 Mbps limit;
* the pipeline list and the HDMI input;
* the add-on;
* while streaming, that the encoder accepts bitrate reloads.

The panel also shows **Data used this stream** (mobile data sent since you pressed Start), which helps on long streams with limited SIM data.

## Undo

```
cd ~/Widget/belabox-4k
sudo ./belaui-pipelines.sh --restore   # show all pipelines again
sudo ./belaui-bitrate.sh --revert      # back to the 12 Mbps limit
sudo ./belaui-addon.sh --revert        # remove the names / bitrate suggestion
sudo systemctl disable --now belabox-guardian    # turn the guardian off
```

## After a BELABOX update

An update can put the 12 Mbps limit and the full pipeline list back, and remove the add-on. Run step 3 of *Install* again. Your choices in belaUI are kept.
