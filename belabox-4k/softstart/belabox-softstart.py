#!/usr/bin/env python3
"""
belabox-softstart: start every stream (and every reconnect) gently.

BELABOX's encoder (belacoder) starts each stream at the *maximum* bitrate set
in belaUI and only lowers it once the connection struggles. If the max is
above what the connections can carry, the start overloads them, the SRT
connection fails, belacoder restarts - at the max again - and the stream
keeps going blank.

This helper watches for belacoder starting. As soon as it is running, it
lowers belacoder's max to START_KBPS (via the same bitrate file and SIGHUP
that belaUI's own slider uses), then after RAMP_DELAY seconds puts the
real max back. belacoder then raises the bitrate by itself, step by step,
only while the connection stays stable (its normal behaviour).

Safe by design:
  * it only touches belacoder's bitrate file and sends SIGHUP, like belaUI;
  * it never signals belacoder before belacoder has installed its SIGHUP
    handler (the default action of SIGHUP would kill it);
  * the real max always comes from belaUI's config.json and is written back
    after every ramp, on exit, and when this helper starts;
  * if the helper isn't running, BELABOX behaves exactly as stock.
"""
import json
import os
import signal
import subprocess
import sys
import time

START_KBPS = int(os.environ.get("SOFTSTART_KBPS", "4000"))
RAMP_DELAY = float(os.environ.get("SOFTSTART_DELAY", "6"))
POLL = 0.1


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def belaui_dir():
    d = os.environ.get("BELAUI_DIR")
    if d:
        return d
    try:
        d = subprocess.run(["systemctl", "show", "-p", "WorkingDirectory", "--value", "belaUI"],
                           capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        d = ""
    return d if d and os.path.exists(os.path.join(d, "belaUI.js")) else "/opt/belaUI"


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


UI = belaui_dir()
SETUP = load_json(os.path.join(UI, "setup.json"))
BITRATE_FILE = SETUP.get("bitrate_file", "/tmp/belacoder_br")


def user_max_kbps():
    """The max bitrate chosen in belaUI (kbps), or None."""
    v = load_json(os.path.join(UI, "config.json")).get("max_br")
    return int(v) if isinstance(v, (int, float)) and v > 0 else None


def read_file():
    try:
        with open(BITRATE_FILE) as f:
            lines = f.read().split()
        return int(lines[0]), int(lines[1])
    except (OSError, ValueError, IndexError):
        return None


def write_max(max_kbps):
    """Rewrite belacoder's bitrate file with a new max, keeping belaUI's min."""
    cur = read_file()
    min_bps = cur[0] if cur else 300000
    max_bps = max(min_bps, int(max_kbps) * 1000)
    tmp = BITRATE_FILE + ".softstart"
    with open(tmp, "w") as f:
        f.write(f"{min_bps}\n{max_bps}\n")
    os.replace(tmp, BITRATE_FILE)


def belacoder_pids():
    pids = []
    for p in os.listdir("/proc"):
        if p.isdigit():
            try:
                with open(f"/proc/{p}/comm") as f:
                    if f.read().strip() == "belacoder":
                        pids.append(int(p))
            except OSError:
                pass
    return pids


def handles_sighup(pid):
    """True once belacoder has installed its SIGHUP handler."""
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("SigCgt:"):
                    return bool(int(line.split()[1], 16) & (1 << (signal.SIGHUP - 1)))
    except OSError:
        pass
    return False


def reload(pid):
    try:
        os.kill(pid, signal.SIGHUP)
    except ProcessLookupError:
        pass


def restore():
    m = user_max_kbps()
    if m and os.path.exists(BITRATE_FILE):
        cur = read_file()
        if not cur or cur[1] != m * 1000:
            write_max(m)
            return True
    return False


def main():
    log(f"belabox-softstart: bitrate file {BITRATE_FILE}, start at {START_KBPS} kbps, "
        f"real max after {RAMP_DELAY:.0f} s")
    if restore():
        log("restored the max bitrate from belaUI's settings")

    def stop(*_):
        # put the real max back, and tell a running belacoder about it
        if restore():
            for pid in belacoder_pids():
                if handles_sighup(pid):
                    reload(pid)
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    seen = set()
    ramp = None            # (pid, deadline) while a ramp is in progress
    while True:
        pids = belacoder_pids()
        seen &= set(pids)
        for pid in pids:
            if pid in seen or not handles_sighup(pid):
                continue
            seen.add(pid)
            real = user_max_kbps()
            if not real or real <= START_KBPS:
                continue   # nothing to soften
            write_max(START_KBPS)
            reload(pid)
            ramp = (pid, time.monotonic() + RAMP_DELAY)
            log(f"belacoder started (pid {pid}): starting at {START_KBPS} kbps, "
                f"{real} kbps in {RAMP_DELAY:.0f} s")
        if ramp and (time.monotonic() >= ramp[1] or ramp[0] not in pids):
            pid = ramp[0]
            ramp = None
            real = user_max_kbps()
            if real:
                write_max(real)
                if pid in pids:
                    reload(pid)
                    log(f"max back to {real} kbps; belacoder climbs to it while the connection holds")
        time.sleep(POLL)


if __name__ == "__main__":
    main()
