#!/usr/bin/env python3
"""measure_latency.py — measure a room's audio output latency for msync.

Rooms whose sound reaches the speakers through a heavily-buffered path
(HDMI -> TV/AVR, Bluetooth, ...) arrive *late* compared to rooms on a plain
analog/USB path, so msync must play those rooms that far AHEAD of the sync
timeline. This script measures the difference and prints the value to put
into config.OUTPUT_LATENCY_MS.

How it works
------------
It plays a short up-chirp through the *target* sink (default: HDMI) and a
down-chirp through a *reference* sink (default: this machine's analog/USB
output) while three `parec` captures run at the same time:

  * the target sink's monitor port    -> when the target chirp hit the sink
  * the reference sink's monitor port -> when the reference chirp hit the sink
  * a USB microphone                  -> when each chirp reached the room

Cross-correlating the chirps and subtracting the two "when it was written"
marks from the two "when it was heard" marks leaves exactly the difference in
output-path latency between the two sinks — the value OUTPUT_LATENCY_MS needs.
The per-process start offsets are measured and subtracted, so it all sits on
one timeline.

Requirements
------------
* pairable sinks: the target (HDMI → TV/AVR) + a low-latency reference
  (analog/USB) output on THIS machine.
* a microphone on this machine that can hear BOTH sets of speakers.
* pulseaudio-utils (`parec`, `paplay`) and Python numpy — both already
  present on msync machines (numpy ships with the client).

Usage
-----
    1. Plug a USB microphone into THIS machine so it hears both the target
       speakers (e.g. the TV) and the reference speakers (the analog/USB).
    2. Run:   python3 tools/measure_latency.py --list
       to see every sink/source, then run it for real (no args usually).
    3. If it picks the wrong devices, pass --sink, --ref and/or --mic;
       they match a substring of the device name.
    4. Put the printed value in config.OUTPUT_LATENCY_MS (or run with
       --write-config) and restart the msync client.
"""

import argparse
import os
import re
import signal
import struct
import subprocess
import sys
import time
import wave

import numpy as np

SR = 48000          # everything is recorded/played at this rate
PROBE_LEN = 0.040   # chirp length, seconds
AMP = 0.30          # chirp amplitude (fraction of full scale)

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.normpath(os.path.join(HERE, "..", "config.py"))


# --------------------------------------------------------------------------- #
# pulseaudio/pipewire introspection                                            #
# --------------------------------------------------------------------------- #

def _pactl(args):
    try:
        out = subprocess.run(["pactl"] + args, capture_output=True,
                             text=True, check=True).stdout
        return out
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def _tab_fields(line):
    """pactl 'short' lines are tab-separated; return the non-empty fields."""
    return [f for f in line.split("\t") if f]


def list_sinks():
    """All sink names, in pactl order."""
    names = []
    for line in _pactl(["list", "sinks", "short"]).splitlines():
        fields = _tab_fields(line)
        if len(fields) >= 2:
            names.append(fields[1])
    return names


def list_sources():
    """All source names (real inputs and sink monitors)."""
    names = []
    for line in _pactl(["list", "sources", "short"]).splitlines():
        fields = _tab_fields(line)
        if len(fields) >= 2:
            names.append(fields[1])
    return names


def default_sink():
    return _pactl(["get-default-sink"]).strip() or None


def is_monitor_name(name):
    return name.endswith(".monitor")


def source_channels(name):
    """Parse the channel count of a source from `pactl list sources`."""
    body = _pactl(["list", "sources"])
    block = re.search(r"Source #\d+\n(?:(?!Source #).)*?Name: %s\n"
                      r"(?:(?!Source #).)*" % re.escape(name), body, re.S)
    if block:
        m = re.search(r"(\d+)ch", block.group(0))
        if m:
            return int(m.group(1))
    return 1


# --------------------------------------------------------------------------- #
# probe generation / recording                                                 #
# --------------------------------------------------------------------------- #

def chirp(f0, f1):
    """A 40 ms windowed frequency sweep, normalized to AMP."""
    n = int(PROBE_LEN * SR)
    t = np.arange(n) / SR
    phase = 2 * np.pi * (f0 * t + (f1 - f0) * t ** 2 / (2 * PROBE_LEN))
    return (AMP * np.sin(phase) * np.hanning(n)).astype(np.float64)


def write_chirp_wav(path, sig):
    """Stereo s16 WAV containing a single chirp (a little silence on both
    sides so it survives a paplay that pads or trims a few ms)."""
    pad = int(0.050 * SR)
    mono = np.concatenate([np.zeros(pad), sig, np.zeros(int(0.100 * SR))])
    st = np.repeat(mono[:, None], 2, axis=1)
    pcm = np.clip(st * 32767, -32768, 32767).astype("<i2")
    w = wave.open(path, "wb")
    w.setnchannels(2)
    w.setsampwidth(2)
    w.setframerate(SR)
    w.writeframes(pcm.tobytes())
    w.close()


def parec(path, source, channels):
    return subprocess.Popen(
        ["parec", "--device=" + source,
         f"--channels={channels}", "--format=s16le",
         "--rate=48000", "--raw"],
        stdout=open(path, "wb"), stderr=subprocess.DEVNULL)


def load_raw(path, channels):
    d = np.fromfile(path, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1 and d.size:
        d = d.reshape(-1, channels).mean(axis=1)
    return d


def find_peak(cap, template):
    """Position (samples from the start of *cap*) of the best match for
    *template*, plus a correlation-strength sanity number (~1+ = audible
    match, well under 1 = the mic probably did not hear that chirp)."""
    if len(cap) < len(template):
        return 0, 0.0
    corr = np.correlate(cap, template, mode="valid")
    k = int(np.argmax(np.abs(corr)))
    peak = float(np.max(np.abs(corr)))
    rms = float(np.sqrt(np.mean(cap ** 2)) + 1e-9)
    return k, peak / (rms * len(template) + 1e-9)


# --------------------------------------------------------------------------- #
# the experiment                                                               #
# --------------------------------------------------------------------------- #

def choose_sinks(target_sub, ref_sub):
    sinks = list_sinks()
    if not sinks:
        print("no output sinks found — is PipeWire/Pulse running?"); sys.exit(1)

    def first_match(sub, pool, words=()):
        if sub:
            for n in pool:
                if sub.lower() in n.lower():
                    return n
        for w in words:
            for n in pool:
                if w in n.lower():
                    return n
        return pool[0] if pool else None

    target = first_match(target_sub, sinks, words=("hdmi",))
    if target_sub:
        target = first_match(target_sub, sinks)
    if not target:
        target = default_sink() or sinks[0]
    rest = [s for s in sinks if s != target]
    ref = first_match(ref_sub, rest, words=("analog", "usb"))
    if ref_sub:
        ref = first_match(ref_sub, rest)
    return target, ref


def choose_mic(mic_sub):
    sources = [s for s in list_sources() if s.startswith("alsa_input")
               and not is_monitor_name(s)]
    if mic_sub:
        for s in sources:
            if mic_sub.lower() in s.lower():
                return s
    for w in ("usb", "mic"):
        for s in sources:
            if w in s.lower():
                return s
    return sources[0] if sources else None


def wait_until(t0, sec):
    while time.monotonic() - t0 < sec:
        time.sleep(0.01)


def run(target, ref, mic, write_config):
    print(f"\nPlaying probes …\n  target sink   : {target}\n  reference sink: {ref}")
    print("  (quiet room + mic near both speakers help the most)")

    t0 = time.monotonic()
    offsets, procs, paths = {}, {}, {}

    def launch(name, popen):
        offsets[name] = time.monotonic() - t0
        procs[name] = popen

    mon_tgt = target + ".monitor"
    mon_ref = ref + ".monitor"
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp")
    os.makedirs(base, exist_ok=True)
    paths.update({
        "mic": os.path.join(base, "lat_mic.raw"),
        "mon_ref": os.path.join(base, "lat_mon_ref.raw"),
        "mon_tgt": os.path.join(base, "lat_mon_tgt.raw"),
        "ref": os.path.join(base, "lat_ref.wav"),
        "tgt": os.path.join(base, "lat_tgt.wav"),
    })

    try:
        launch("mic", parec(paths["mic"], mic, source_channels(mic)))
        launch("mon_ref", parec(paths["mon_ref"], mon_ref, 2))
        launch("mon_tgt", parec(paths["mon_tgt"], mon_tgt, 2))

        write_chirp_wav(paths["ref"], chirp(300, 4000))     # up-chirp
        write_chirp_wav(paths["tgt"], chirp(4000, 300))     # down-chirp

        wait_until(t0, 0.4)
        launch("play_ref", subprocess.Popen(
            ["paplay", "--device=" + ref, paths["ref"]],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        wait_until(t0, 0.8)
        launch("play_tgt", subprocess.Popen(
            ["paplay", "--device=" + target, paths["tgt"]],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        wait_until(t0, 3.2)
    finally:
        for name in ("play_ref", "play_tgt"):
            p = procs.get(name)
            if p:
                p.terminate()
        for name in ("mic", "mon_ref", "mon_tgt"):
            p = procs.get(name)
            if p:
                p.terminate()
        time.sleep(0.2)
        for p in procs.values():
            if p.poll() is None:
                p.kill()

    mic_cap = load_raw(paths["mic"], source_channels(mic))
    mon_ref_cap = load_raw(paths["mon_ref"], 2)
    mon_tgt_cap = load_raw(paths["mon_tgt"], 2)

    up = chirp(300, 4000)
    down = chirp(4000, 300)

    p_ref_mon, s_ref_mon = find_peak(mon_ref_cap, up)
    p_tgt_mon, s_tgt_mon = find_peak(mon_tgt_cap, down)
    p_ref_mic, s_ref_mic = find_peak(mic_cap, up)
    p_tgt_mic, s_tgt_mic = find_peak(mic_cap, down)

    # put every recording on the shared timeline: start offset + sample pos
    t_ref_mon = offsets["mon_ref"] + p_ref_mon / SR
    t_tgt_mon = offsets["mon_tgt"] + p_tgt_mon / SR
    t_ref_mic = offsets["mic"] + p_ref_mic / SR
    t_tgt_mic = offsets["mic"] + p_tgt_mic / SR

    dt_mon = t_tgt_mon - t_ref_mon      # as written to each sink
    dt_mic = t_tgt_mic - t_ref_mic      # as heard by the microphone
    latency = dt_mic - dt_mon           # target path minus reference path

    print(f"\n  written-to-sink delta (target - ref): {dt_mon * 1000:7.1f} ms")
    print(f"  heard-by-mic delta     (target - ref): {dt_mic * 1000:7.1f} ms")
    print("  correlation sanity (mon ref/tgt, mic ref/tgt): "
          f"{s_ref_mon:.1f} {s_tgt_mon:.1f} / {s_ref_mic:.1f} {s_tgt_mic:.1f}")
    if s_ref_mic < 2.0 or s_tgt_mic < 2.0:
        print("\n  NOTE: the microphone barely heard one of the chirps. Move it")
        print("  so it hears BOTH speakers (TV + the reference output) and re-run.")

    latency_ms = round(latency * 1000)
    print(f"\n  ==> OUTPUT_LATENCY_MS = {latency_ms}"
          f"   (target path {latency * 1000:+.1f} ms slower than reference)")
    print("\nNext steps:")
    print("  * set OUTPUT_LATENCY_MS = %d in config.py on this machine" % latency_ms)
    print("    (or re-run with --write-config), then restart the msync client")
    print("  * fine-tune by ear from the client console with: latency <ms>")
    print("  * remaining error is ~speaker-to-mic air distance; you can split")
    print("    the difference if the two speakers are different distances away")
    if write_config:
        write_config_value(latency_ms)
    return latency_ms


# --------------------------------------------------------------------------- #
# optional config.py write                                                     #
# --------------------------------------------------------------------------- #

def write_config_value(ms):
    try:
        with open(CONFIG) as fh:
            src = fh.read()
    except OSError as e:
        print(f"could not read {CONFIG}: {e}")
        return
    if re.search(r"^OUTPUT_LATENCY_MS\s*=", src, re.M):
        src = re.sub(r"^OUTPUT_LATENCY_MS\s*=.*$",
                     f"OUTPUT_LATENCY_MS = {ms}", src, flags=re.M)
        with open(CONFIG, "w") as fh:
            fh.write(src)
        print(f"updated OUTPUT_LATENCY_MS = {ms} in {CONFIG}")
    else:
        with open(CONFIG, "a") as fh:
            fh.write(f"\n# Set by tools/measure_latency.py on "
                     f"{time.strftime('%Y-%m-%d')}\nOUTPUT_LATENCY_MS = {ms}\n")
        print(f"appended OUTPUT_LATENCY_MS = {ms} to {CONFIG}")


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(
        description="Measure the output-latency difference between a "
                    "high-latency sink (HDMI -> TV/AVR...) and this machine's "
                    "low-latency reference sink, for msync's OUTPUT_LATENCY_MS.")
    ap.add_argument("--sink", default=None,
                    help="substring of the TARGET (high-latency) sink name; "
                         "default: the HDMI sink")
    ap.add_argument("--ref", default=None,
                    help="substring of the REFERENCE (low-latency) sink name; "
                         "default: the analog/USB sink")
    ap.add_argument("--mic", default=None,
                    help="substring of the microphone source name")
    ap.add_argument("--list", action="store_true",
                    help="list all sinks and sources, then exit")
    ap.add_argument("--write-config", action="store_true",
                    help="write the measured value into config.py")
    args = ap.parse_args()

    if args.list:
        print("sinks:")
        for n in list_sinks():
            print(f"  {n}")
        print("sources:")
        for n in list_sources():
            print(f"  {n}   {'(monitor)' if is_monitor_name(n) else ''}")
        return

    target, ref = choose_sinks(args.sink, args.ref)
    if ref is None:
        print("no low-latency reference sink found — plug in an analog/USB "
              "output as well (or use --ref), so the measurement can compare.")
        return
    mic = choose_mic(args.mic)
    if mic is None:
        print("\nNo microphone found. Plug a USB mic into this machine so it\n"
              "can hear both the target and the reference speakers, then\n"
              "run this script again.")
        return

    run(target, ref, mic, args.write_config)


if __name__ == "__main__":
    main()