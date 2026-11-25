#!/usr/bin/env python3
"""
belabox-guardian: keep a BELABOX stream up on its own, for hours.

BELABOX's encoder (belacoder) restarts the stream whenever the SRT
connection fails, and each restart is a blank moment for viewers. The usual
cause is pushing more than the connections can carry: belacoder starts every
stream, and every restart, at the max bitrate set in belaUI, and if the
connections got worse since you set it, it overloads them again and again.

The guardian fixes that automatically, without restarting anything:

1. Soft start. Each stream and each restart begins at START_KBPS, then the
   max is put back after RAMP_DELAY seconds and belacoder climbs by itself,
   only while the connection stays stable (its normal behaviour).
2. Learns from drops. When belacoder restarts on its own because the SRT
   connection failed (not you pressing Start, not a camera hiccup, not
   "couldn't connect at all" - belaUI's log says which), the guardian caps
   the bitrate at 80% of what the connections were actually carrying just
   before, so it stops overloading them.
3. Recovers. After every RAISE_AFTER seconds (3 min) without a drop, the
   cap is raised by RAISE_STEP (25%) until it is back at your max, with no
   restart.
4. Your choice wins. Moving belaUI's bitrate slider (or pressing Start)
   clears the cap.

It only uses belacoder's bitrate file and the reload signal (SIGHUP), the
same way belaUI's slider does, and only once belacoder is ready for the
signal. Your max is always restored when the guardian stops; if it isn't
running, BELABOX behaves exactly as stock. Status goes to belaUI's web
folder so the belabox-4k add-on can show it.
"""
import json
import os
import signal
import subprocess
import sys
import time

E = os.environ.get
START_KBPS = int(E("GUARDIAN_START_KBPS", "4000"))
RAMP_DELAY = float(E("GUARDIAN_RAMP_DELAY", "6"))
MIN_CAP_KBPS = int(E("GUARDIAN_MIN_CAP_KBPS", "2000"))
CAP_SHARE = 0.8                                          # cap = 80% of what the links carried
RAISE_AFTER = float(E("GUARDIAN_RAISE_AFTER", "180"))    # 3 min without a drop (walking: signal changes fast)...
RAISE_STEP = float(E("GUARDIAN_RAISE_STEP", "1.25"))    # ...raises the cap by 25%
RESTART_GAP = float(E("GUARDIAN_RESTART_GAP", "15"))     # belaUI restarts belacoder ~2 s later
FORGET_AFTER = float(E("GUARDIAN_FORGET_AFTER", "600"))  # idle this long: a new stream, new place
SYSFS_NET = E("GUARDIAN_SYSFS_NET", "/sys/class/net")
POLL = 0.2
MIN_CARRIED_KBPS = 1000      # below this no real stream was flowing: nothing to learn
JOURNAL_CMD = E("GUARDIAN_JOURNAL_CMD", "")   # tests only


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def belaui_dir():
    d = E("BELAUI_DIR")
    if d:
        return d
    try:
        d = subprocess.run(["systemctl", "show", "-p", "WorkingDirectory", "--value", "belaUI"],
                           capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        d = ""
    return d if d and os.path.exists(os.path.join(d, "belaUI.js")) else "/opt/belaUI"


UI = belaui_dir()
BITRATE_FILE = load_json(os.path.join(UI, "setup.json")).get("bitrate_file", "/tmp/belacoder_br")
STATUS_FILE = os.path.join(UI, "public", "belabox-guardian.json")


# ------------------------------------------------------------ belaUI / belacoder

def user_max_kbps():
    v = load_json(os.path.join(UI, "config.json")).get("max_br")
    return int(v) if isinstance(v, (int, float)) and v > 0 else None


def read_file():
    try:
        with open(BITRATE_FILE) as f:
            a = f.read().split()
        return int(a[0]), int(a[1])
    except (OSError, ValueError, IndexError):
        return None


def file_mtime():
    try:
        return os.stat(BITRATE_FILE).st_mtime
    except OSError:
        return 0.0


_comm_cache = {}   # pid -> (is_belacoder, first_seen)


def belacoder_pids():
    """PIDs of belacoder. Each process is read once; processes younger than
    3 s are re-read, because belaUI forks and then execs belacoder."""
    now = time.monotonic()
    out, alive = [], set()
    for p in os.listdir("/proc"):
        if not p.isdigit():
            continue
        alive.add(p)
        hit = _comm_cache.get(p)
        if hit and (hit[0] or now - hit[1] > 3):
            if hit[0]:
                out.append(int(p))
            continue
        try:
            with open(f"/proc/{p}/comm") as f:
                is_bc = f.read().strip() == "belacoder"
        except OSError:
            continue
        _comm_cache[p] = (is_bc, hit[1] if hit else now)
        if is_bc:
            out.append(int(p))
    for p in list(_comm_cache):
        if p not in alive:
            del _comm_cache[p]
    return out


def exit_reason(since):
    """Why did belacoder stop? From belaUI's log (belacoder's messages):
    'link'    - the SRT connection failed or timed out (connections overloaded / lost)
    'connect' - it never connected (no network / server unreachable)
    'camera'  - the video source stalled (e.g. HDMI hiccup)
    None      - unknown"""
    cmd = (JOURNAL_CMD.split() if JOURNAL_CMD else
           ["journalctl", "-u", "belaUI", "--since", f"@{int(since)}", "-o", "cat", "--no-pager"])
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=4).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    if "SRT connection failed" in out or "SRT connection timed out" in out:
        return "link"
    if "Failed to establish an SRT connection" in out:
        return "connect"
    if "Pipeline stall" in out or "gstreamer error" in out.lower():
        return "camera"
    return None


def handles_sighup(pid):
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("SigCgt:"):
                    return bool(int(line.split()[1], 16) & (1 << (signal.SIGHUP - 1)))
    except OSError:
        pass
    return False


def tx_bytes():
    """Bytes sent on the interfaces belaUI bonds (as belaUI counts them)."""
    total = 0
    try:
        names = os.listdir(SYSFS_NET)
    except OSError:
        return 0
    for n in names:
        if n == "lo" or n.startswith(("docker", "l4tbr", "tailscale", "veth")):
            continue
        try:
            with open(os.path.join(SYSFS_NET, n, "statistics", "tx_bytes")) as f:
                total += int(f.read())
        except (OSError, ValueError):
            pass
    return total


# ------------------------------------------------------------------ guardian

class Guardian:
    def __init__(self):
        self.cap = None                # kbps, or None: no cap
        self.cap_reason = ""
        self.last_drop = 0.0
        self.last_raise = 0.0
        self.drops = 0
        self.seen = set()
        self.ramp = None               # (pid, deadline)
        self.last_exit = 0.0           # when belacoder was last seen stopping
        self.run_start = 0.0           # when the current belacoder run started
        self.exited_run_start = 0.0    # start of the run that last stopped
        self.had_belacoder = False
        self.our_write = 0.0           # mtime of our own last write
        self.user_seen = user_max_kbps()
        self.samples = []              # (time, tx bytes), last ~40 s
        self.last_sample = 0.0
        self.status = {}

    # -- bitrate file
    def write_max(self, kbps):
        cur = read_file()
        min_bps = cur[0] if cur else 300000
        max_bps = max(min_bps, int(kbps) * 1000)
        if cur and cur[1] == max_bps:
            return False
        tmp = BITRATE_FILE + ".guardian"
        with open(tmp, "w") as f:
            f.write(f"{min_bps}\n{max_bps}\n")
        os.replace(tmp, BITRATE_FILE)
        self.our_write = file_mtime()
        return True

    def effective_max(self):
        u = user_max_kbps()
        if not u:
            return None
        return min(u, self.cap) if self.cap else u

    def apply(self, pids, why=""):
        """Write the effective max and tell a running belacoder."""
        eff = self.effective_max()
        if eff and self.write_max(eff):
            for pid in pids:
                if handles_sighup(pid):
                    os.kill(pid, signal.SIGHUP)
            if why:
                log(f"max bitrate now {eff} kbps ({why})")

    # -- measurements
    def sample(self, now):
        if now - self.last_sample >= 1.0:
            self.last_sample = now
            self.samples.append((now, tx_bytes()))
            self.samples = [s for s in self.samples if now - s[0] <= 40]

    def recent_kbps(self, before, span=20):
        """Average send rate over `span` seconds before `before` (kbps)."""
        pts = [s for s in self.samples if before - span <= s[0] <= before + 0.5]
        if len(pts) < 2 or pts[-1][0] - pts[0][0] < 3:
            return None
        return (pts[-1][1] - pts[0][1]) * 8 / 1000 / (pts[-1][0] - pts[0][0])

    # -- status for the add-on
    def write_status(self, streaming):
        st = {
            "running": True,
            "streaming": streaming,
            "user_max_kbps": user_max_kbps(),
            "cap_kbps": self.cap,
            "cap_reason": self.cap_reason,
            "drops": self.drops,
            "last_drop": time.strftime("%H:%M", time.localtime(self.last_drop)) if self.last_drop else None,
            "next_raise_in_s": (int(max(0, RAISE_AFTER - (time.time() - max(self.last_drop, self.last_raise))))
                                if self.cap else None),
        }
        if st != self.status:
            self.status = st
            try:
                tmp = STATUS_FILE + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(st, f)
                os.replace(tmp, STATUS_FILE)
            except OSError:
                pass

    # -- main loop step
    def step(self):
        now = time.time()
        mono = time.monotonic()
        self.sample(now)
        pids = belacoder_pids()
        streaming = bool(pids)

        # your slider wins: a new max in belaUI clears the cap
        u = user_max_kbps()
        if u != self.user_seen:
            self.user_seen = u
            if self.cap:
                log(f"belaUI max changed to {u} kbps: cap cleared")
            self.cap, self.cap_reason = None, ""
            self.apply(pids)

        if self.had_belacoder and not pids:
            self.last_exit = now
            self.exited_run_start = self.run_start
        self.had_belacoder = bool(pids)

        # forget the cap after a long break (new stream, probably new place)
        if not pids and self.cap and now - self.last_exit > FORGET_AFTER:
            log("stream idle for a while: cap cleared")
            self.cap, self.cap_reason = None, ""
            self.apply(pids)

        self.seen &= set(pids)
        for pid in pids:
            if pid in self.seen or not handles_sighup(pid):
                continue
            self.seen.add(pid)
            new_run_start = now
            # a restart by belaUI after a drop, or you pressing Start?
            # (belaUI rewrites the bitrate file on Start, not on a restart)
            user_start = (file_mtime() != self.our_write and now - file_mtime() < 30)
            if not user_start and self.last_exit and now - self.last_exit < RESTART_GAP:
                self.on_drop(now)
            elif user_start and self.cap:
                log("stream started from belaUI: cap cleared")
                self.cap, self.cap_reason = None, ""
            self.run_start = new_run_start
            eff = self.effective_max()
            if eff and eff > START_KBPS:
                self.write_max(START_KBPS)
                os.kill(pid, signal.SIGHUP)
                self.ramp = (pid, mono + RAMP_DELAY)
                log(f"belacoder started (pid {pid}): soft start at {START_KBPS} kbps, "
                    f"{eff} kbps in {RAMP_DELAY:.0f} s")
            else:
                self.apply([pid])

        if self.ramp and (mono >= self.ramp[1] or self.ramp[0] not in pids):
            self.ramp = None
            self.apply(pids, "after soft start")

        # recover: raise the cap step by step while there are no drops
        if self.cap and pids and not self.ramp:
            since = now - max(self.last_drop, self.last_raise)
            if since >= RAISE_AFTER:
                self.last_raise = now
                u = user_max_kbps() or self.cap
                new = int(self.cap * RAISE_STEP) // 250 * 250
                if new >= u:
                    self.cap, self.cap_reason = None, ""
                    self.apply(pids, f"no drops for {RAISE_AFTER/60:.0f} min: cap removed")
                else:
                    self.cap = new
                    self.cap_reason = "raising again while stable"
                    self.apply(pids, f"no drops for {RAISE_AFTER/60:.0f} min: cap raised")

        self.write_status(streaming)

    def on_drop(self, now):
        # only what the run that just failed carried (not an earlier run)
        span = min(20.0, self.last_exit - self.exited_run_start) if self.exited_run_start else 20.0
        carried = self.recent_kbps(self.last_exit, span) if span >= 3 else None
        reason = exit_reason(self.last_exit - 30)
        if reason in ("camera", "connect"):
            log(f"belacoder restarted ({'camera/video source' if reason == 'camera' else 'could not connect'}): "
                "not a bandwidth problem, bitrate left as is")
            return
        if not carried or carried < MIN_CARRIED_KBPS:
            log("belacoder restarted before any real data flowed: nothing to learn, bitrate left as is")
            return
        self.drops += 1
        self.last_drop = now
        current = self.cap or user_max_kbps() or START_KBPS
        new = int(carried * CAP_SHARE)
        new = max(MIN_CAP_KBPS, min(new, current))
        new = new // 250 * 250
        self.cap = new
        self.cap_reason = f"drop at {time.strftime('%H:%M')}: connections carried {carried/1000:.1f} Mbps"
        log(f"stream dropped (#{self.drops}, {reason or 'reason unknown'}); "
            f"connections carried {carried:.0f} kbps; capping at {new} kbps")

    def restore(self):
        u = user_max_kbps()
        if u and os.path.exists(BITRATE_FILE) and self.write_max(u):
            for pid in belacoder_pids():
                if handles_sighup(pid):
                    os.kill(pid, signal.SIGHUP)
        try:
            with open(STATUS_FILE, "w") as f:
                json.dump({"running": False}, f)
        except OSError:
            pass


def main():
    g = Guardian()
    log(f"belabox-guardian: bitrate file {BITRATE_FILE}; soft start {START_KBPS} kbps for "
        f"{RAMP_DELAY:.0f} s; after a drop cap at {int(CAP_SHARE*100)}% of what the links carried; "
        f"raise {round((RAISE_STEP-1)*100)}% per {RAISE_AFTER/60:.0f} min without drops")
    g.restore()

    def stop(*_):
        g.restore()
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while True:
        try:
            g.step()
        except Exception as e:      # never die on one bad read; keep guarding
            log(f"error (continuing): {e!r}")
        time.sleep(POLL)


if __name__ == "__main__":
    main()
