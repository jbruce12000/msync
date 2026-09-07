#!/usr/bin/env python3
"""measure_latency.py — measure a room's audio output latency for msync.

Rooms whose sound reaches the speakers through a heavily-buffered path
(HDMI -> TV/AVR, Bluetooth, ...) arrive *late* compared to rooms on a plain
analog/USB path, so msync must play those rooms that far AHEAD of the sync
timeline. This script measures it and prints the value to put into
config.OUTPUT_LATENCY_MS.

How it works
------------
For each sink it plays a short chirp pattern, then records two things at the
same time:

  * the sink's MONITOR port  -> exactly when the signal hit the sink's output
  * a microphone             -> when it was heard in the room

The difference is that sink's output-path latency (sink buffering + DAC +
amplifier + a few ms of speaker-to-mic air). Measuring both the high-latency
(HDMI/TV) sink and the low-latency (analog/USB) sink the same way and
subtracting them gives OUTPUT_LATENCY_MS — how much earlier this room must
play to line up with a room on the low-latency path.

Because each sink is measured in its own run, the microphone only has to hear
one speaker at a time — put it right in front of the TV for the first run and
in front of the reference speakers for the second.

Requirements / usage
--------------------
  1. Plug a microphone into THIS machine.
  2. Put it in front of the TV speaker, run:  python3 tools/measure_latency.py
  3. It first measures the reference (analog/USB) sink, announces a pause,
     then measures the target (HDMI) sink. Move the mic to face each speaker
     as it is being measured.
  4. Put the printed value in config.OUTPUT_LATENCY_MS (or re-run with
     --write-config) and restart the msync client.

Run `tools/measure_latency.py --list` first to see every sink/source. If the
auto-detection picks wrong devices, pass --sink, --ref and/or --mic (each
matches a substring of the device name).

Dependencies: pulseaudio-utils (parec, paplay), Python numpy — both already
present on msync machines.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import wave

import numpy as np

SR = 48000           # everything is recorded/played at this rate
PROBE_LEN = 0.080    # chirp length, seconds
NPROBES = 10         # chirps per sink (median used) — plenty for a
                     # noise-cancelling mic that may swallow some
PROBE_GAP = 0.300    # between chirps
AMP = 0.55           # chirp amplitude (fraction of full scale)
DEFAULT_NPROBES = NPROBES

DETECT_MIN = 0.15    # minimum (cos) correlation for a chirp to count
AUDIBLE_MIN = 0.35   # below this, warn that the mic placement is weak
MIC_DELAY_MAX = 0.30 # mic arrival can't be more than this after the sink (s);
                     # also keeps the search from grabbing the *next* chirp
                     # (chirps are PROBE_LEN+PROBE_GAP=0.36 s apart)

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.normpath(os.path.join(HERE, "..", "config.py"))


# --------------------------------------------------------------------------- #
# pulseaudio / pipewire introspection                                          #
# --------------------------------------------------------------------------- #

def _pactl(args):
    try:
        return subprocess.run(["pactl"] + args, capture_output=True,
                              text=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def _fields(line):
    return [f for f in line.split("\t") if f]


def list_sinks():
    out = []
    for line in _pactl(["list", "sinks", "short"]).splitlines():
        fields = _fields(line)
        if len(fields) >= 2:
            out.append(fields[1])
    return out


def list_sources():
    out = []
    for line in _pactl(["list", "sources", "short"]).splitlines():
        fields = _fields(line)
        if len(fields) >= 2:
            out.append(fields[1])
    return out


def default_sink():
    return _pactl(["get-default-sink"]).strip() or None


def is_monitor_name(name):
    return name.endswith(".monitor")


def msync_server_http():
    """Send a control request to this machine's msync server (if any)."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        import config
        base = f"http://{config.SERVER}:{config.DEFAULT_PORT + config.HTTP_PORT_OFFSET}"
    except Exception:
        return None

    def call(method, path, params=None):
        if params:
            path += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(base + path, method=method)
        try:
            with urllib.request.urlopen(req, timeout=3) as resp:
                return json.loads(resp.read().decode())
        except Exception:
            return None

    return call


def with_playback_paused(call, fn):
    """Pause the msync server while *fn* runs, then restore the previous state."""
    st = call("GET", "/api/state") if call else None
    was_playing = bool(st and st.get("playing"))
    if was_playing:
        print("pausing playback for cleaner measurements…")
        call("POST", "/api/control/toggle", {"state": 0})
        time.sleep(0.5)
    try:
        return fn()
    finally:
        if was_playing:
            call("POST", "/api/control/toggle", {"state": 1})
            print("playback resumed.")


def device_base(name):
    """'alsa_output.<base>.analog-stereo' and 'alsa_input.<base>.mono-...'
    share <base> for the same physical card, e.g. 'usb-Logi_...-00'."""
    parts = name.split(".")
    return parts[1] if len(parts) >= 3 else name


def source_channels(name):
    body = _pactl(["list", "sources"])
    block = re.search(r"Source #\d+\n(?:(?!Source #).)*?Name: %s\n"
                      r"(?:(?!Source #).)*" % re.escape(name), body, re.S)
    if block:
        m = re.search(r"(\d+)ch", block.group(0))
        if m:
            return int(m.group(1))
    return 1


# --------------------------------------------------------------------------- #
# probes                                                                       #
# --------------------------------------------------------------------------- #

def chirp(f0, f1):
    n = int(PROBE_LEN * SR)
    t = np.arange(n) / SR
    phase = 2 * np.pi * (f0 * t + (f1 - f0) * t ** 2 / (2 * PROBE_LEN))
    return (AMP * np.sin(phase) * np.hanning(n)).astype(np.float64)


def write_chirps_wav(path, sig):
    """A WAV with NPROBES chirps spread across the WHOLE capture window, so
    the sink never runs dry (an idle PipeWire sink suspends and stops feeding
    the monitor/mic after a fraction of a second). Returns the frame count."""
    pad = int(0.100 * SR)
    total = int((PROBE_LEN + PROBE_GAP) * SR)
    nframes = total * NPROBES + int(0.150 * SR)
    buf = np.zeros(nframes)
    for i in range(NPROBES):
        a = i * total + pad
        buf[a:a + len(sig)] = sig
    st = np.repeat(buf[:, None], 2, axis=1)
    pcm = np.clip(st * 32767, -32768, 32767).astype("<i2")
    w = wave.open(path, "wb")
    w.setnchannels(2)
    w.setsampwidth(2)
    w.setframerate(SR)
    w.writeframes(pcm.tobytes())
    w.close()
    return nframes


def parec(path, source, channels):
    # stdbuf -o0: parec buffers its stdout in 64 KiB chunks and would lose
    # everything past the last flush when we kill it — unbuffered writes go
    # straight into the file, so nothing gets thrown away.
    return subprocess.Popen(
        ["stdbuf", "-o0", "parec", "--device=" + source,
         f"--channels={channels}", "--format=s16le", "--rate=48000", "--raw"],
        stdout=open(path, "wb"), stderr=subprocess.DEVNULL)


def load_raw(path, channels):
    d = np.fromfile(path, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1 and d.size:
        d = d.reshape(-1, channels).mean(axis=1)
    return d


def find_peaks(cap, template, count):
    """Positions (samples) of the `count` best matches for `template` in
    *cap*, found one at a time (each match's surroundings zeroed afterwards).
    Strength is the normalized (cosine) correlation at the peak: 0 = no match,
    1.0 = exact. The monitor recording contains only the chirps, so this gets
    every probe."""
    if len(cap) < len(template):
        return [], []
    peaks, strengths = [], []
    work = cap.copy()
    for _ in range(count):
        corr = np.correlate(work, template, mode="valid")
        k = int(np.argmax(np.abs(corr)))
        w = work[k:k + len(template)]
        cos = float(np.max(np.abs(corr))) / (
            np.linalg.norm(w) * np.linalg.norm(template) + 1e-12)
        if cos < DETECT_MIN:
            break
        peaks.append(k)
        strengths.append(cos)
        a = max(0, k - len(template))
        b = min(len(work), k + len(template) * 2)
        work[a:b] = 0.0
    return peaks, strengths


def find_mic_arrivals(mic_cap, template, mon_peaks):
    """For each monitor-detected chirp, find the strongest template match in
    the mic recording within the plausible delay range after it (a chirp the
    mic 'hears' can never arrive before it hits the sink, and not wildly late
    either). Returns a (sample, cos) pair per chirp, or None if not found."""
    out = []
    for k_mon in mon_peaks:
        lo = k_mon + int(0.008 * SR)             # sink write, then air path
        hi = lo + int(MIC_DELAY_MAX * SR)
        hi = min(hi, len(mic_cap) - len(template))
        if hi <= lo:
            out.append(None)
            continue
        corr = np.correlate(mic_cap[lo:hi], template, mode="valid")
        k = int(np.argmax(np.abs(corr)))
        w = mic_cap[lo + k:lo + k + len(template)]
        cos = float(np.max(np.abs(corr))) / (
            np.linalg.norm(w) * np.linalg.norm(template) + 1e-12)
        out.append((lo + k, cos) if cos >= DETECT_MIN else None)
    return out


# --------------------------------------------------------------------------- #
# per-sink absolute measurement                                                #
# --------------------------------------------------------------------------- #

def measure_sink(sink, mic, wav, template, label):
    mon = sink + ".monitor"
    mic_ch = source_channels(mic)
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp")
    os.makedirs(base, exist_ok=True)
    mon_path = os.path.join(base, "mon.raw")
    mic_path = os.path.join(base, "mic.raw")
    for p in (mon_path, mic_path):
        if os.path.exists(p):
            os.remove(p)

    t0 = time.monotonic()
    off = {}
    procs = {}

    def launch(name, popen):
        off[name] = time.monotonic() - t0
        procs[name] = popen

    launch("mic", parec(mic_path, mic, mic_ch))
    launch("mon", parec(mon_path, mon, 2))
    try:
        time.sleep(0.20)
        launch("play", subprocess.Popen(
            ["paplay", "--device=" + sink, wav],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        # wait until the whole chirp WAV has played (plus margin) so the sink
        # stays fed for the entire capture — an idle PipeWire sink suspends
        # and stops feeding the monitor/mic after a fraction of a second
        with wave.open(wav, "rb") as wf:
            wav_nframes = wf.getnframes()
        time.sleep(wav_nframes / SR + 0.8)
    finally:
        for key in ("play", "mic", "mon"):
            p = procs.get(key)
            if p and p.poll() is None:
                p.terminate()
        time.sleep(0.2)
        for p in procs.values():
            if p.poll() is None:
                p.kill()
        time.sleep(0.3)          # let the recording files flush
    mon_cap = load_raw(mon_path, 2)
    mic_cap = load_raw(mic_path, mic_ch)

    p_mon, s_mon = find_peaks(mon_cap, template, NPROBES)
    arrivals = find_mic_arrivals(mic_cap, template, p_mon)
    mic_rms = float(np.sqrt(np.mean(mic_cap ** 2) + 1e-12))

    if not p_mon:
        print(f"  {label}: no chirps reached the sink's monitor port"
              f" (mic RMS {mic_rms * 1000:.1f}) — cannot measure.")
        return None

    pairs = [(p_mon[i], arrivals[i]) for i in range(len(p_mon))
             if arrivals[i] is not None]
    if not pairs:
        print(f"  {label}: chirps reached the sink (monitor) but the mic did"
              f" not hear them (mic RMS {mic_rms * 1000:.1f}). Move the mic"
              f" right in front of the speaker and re-run.")
        return None

    latencies = []
    print(f"  {label} — per-chirp arrivals (mic path minus monitor path):")
    for i, (k_mon, (k_mic, cos)) in enumerate(pairs, 1):
        t_mon = off["mon"] + k_mon / SR
        t_mic = off["mic"] + k_mic / SR
        lat = t_mic - t_mon
        latencies.append(lat)
        print(f"    chirp {i}: mon {t_mon:.3f}s  mic {t_mic:.3f}s"
              f"  -> {lat * 1000:6.1f} ms  (mic fit {cos:.2f})")
    lat = float(np.median(latencies))
    best_cos = max(p[1][1] for p in pairs)

    print(f"    output-path latency     : {lat * 1000:7.1f} ms"
          f" (median of {len(latencies)})")
    if best_cos < AUDIBLE_MIN:
        print("    WARNING: the microphone barely heard this speaker"
              f" (best fit {best_cos:.2f} < {AUDIBLE_MIN:.2f}) — hold the mic")
        print("             right against the speaker and re-run for reliability.")
    elif len(latencies) < NPROBES:
        print(f"    note: only {len(latencies)} of {NPROBES} chirps detected;")
        print("          the result is based on fewer samples.")
    return lat


# --------------------------------------------------------------------------- #
# device selection                                                             #
# --------------------------------------------------------------------------- #

def choose_sinks(target_sub, ref_sub):
    sinks = list_sinks()
    if not sinks:
        print("no output sinks found — is PipeWire/Pulse running?")
        sys.exit(1)

    def first(sub, pool, words=()):
        if sub:
            for n in pool:
                if sub.lower() in n.lower():
                    return n
        for w in words:
            for n in pool:
                if w in n.lower():
                    return n
        return pool[0] if pool else None

    target = first(target_sub, sinks, words=("hdmi",))
    if not target:
        target = default_sink() or sinks[0]
    rest = [s for s in sinks if s != target]
    ref = first(ref_sub, rest, words=("analog", "usb"))
    return target, ref


def choose_mic(mic_sub, ref_sink=None):
    sources = [s for s in list_sources() if s.startswith("alsa_input")
               and not is_monitor_name(s)]
    if ref_sink is not None:
        keep = [s for s in sources if device_base(s) != device_base(ref_sink)]
        if keep:
            sources = keep
    if mic_sub:
        for s in sources:
            if mic_sub.lower() in s.lower():
                return s
    for w in ("usb", "mic", "headset"):
        for s in sources:
            if w in s.lower():
                return s
    return sources[0] if sources else None


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


def main():
    ap = argparse.ArgumentParser(
        description="Measure the output-path latency of a high-latency sink "
                    "(HDMI -> TV/AVR) vs. this machine's low-latency "
                    "reference sink, for msync's OUTPUT_LATENCY_MS.")
    ap.add_argument("--sink", default=None,
                    help="substring of the TARGET (high-latency) sink name; "
                         "default: the HDMI sink")
    ap.add_argument("--ref", default=None,
                    help="substring of the REFERENCE (low-latency) sink name; "
                         "default: the analog/USB sink")
    ap.add_argument("--mic", default=None,
                    help="substring of the microphone source name")
    ap.add_argument("--probes", type=int, default=DEFAULT_NPROBES,
                    help=f"number of chirps per sink (default: {DEFAULT_NPROBES})")
    ap.add_argument("--list", action="store_true",
                    help="list all sinks and sources, then exit")
    ap.add_argument("--ref-only", action="store_true",
                    help="measure only the REFERENCE sink (hold the mic near it)")
    ap.add_argument("--target-only", action="store_true",
                    help="measure only the TARGET sink (hold the mic near it)")
    ap.add_argument("--no-pause", action="store_true",
                    help="leave playback running during the measurement "
                         "(worse chirp detection); default pauses via the "
                         "msync HTTP API")
    ap.add_argument("--write-config", action="store_true",
                    help="write the measured value into config.py")
    args = ap.parse_args()

    global NPROBES
    NPROBES = args.probes

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
              "output as well (or use --ref) so the measurement can compare.")
        return
    mic = choose_mic(args.mic, ref)
    if mic is None:
        print("\nNo microphone found. Plug a USB mic (or headset) into this\n"
              "machine — it just needs to hear each speaker for a few seconds.")
        return

    print(f"mic: {mic}")

    template = chirp(300, 4000)
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp")
    os.makedirs(base, exist_ok=True)
    wav = os.path.join(base, "probe.wav")
    write_chirps_wav(wav, template)

    call = None if args.no_pause else msync_server_http()
    if args.no_pause:
        print("(--no-pause: leaving playback running; expect weaker chirp detection)")

    def measure_ref():
        print(f"\nHOLD THE MIC AGAINST/NEAR THE REFERENCE SPEAKERS. {NPROBES} chirps\n"
              "are about to play there... Measuring in 2s.")
        time.sleep(2.0)
        return measure_sink(ref, mic, wav, template, "reference sink")

    def measure_target():
        print(f"\nHOLD THE MIC AGAINST/NEAR THE TARGET SPEAKER (TV/AVR). {NPROBES} chirps\n"
              "are about to play there... Measuring in 2s.")
        time.sleep(2.0)
        return measure_sink(target, mic, wav, template, "target sink")

    if args.ref_only:
        lat_ref = with_playback_paused(call, measure_ref)
        if lat_ref is not None:
            print(f"\nreference sink path latency: {lat_ref * 1000:.1f} ms\n"
                  "Now run:  python3 tools/measure_latency.py --target-only\n"
                  "(mic near the TV) and subtract this from the target value.")
        return
    if args.target_only:
        lat_tgt = with_playback_paused(call, measure_target)
        if lat_tgt is not None:
            print(f"\ntarget sink path latency: {lat_tgt * 1000:.1f} ms\n"
                  "To get OUTPUT_LATENCY_MS, also run --ref-only and subtract\n"
                  "the reference value from this one.")
        return

    def measure_all():
        lat_ref = measure_ref()
        print("\nSTEP 2/2 — the TARGET sink (TV/AVR).")
        print("  Move the mic in front of the TARGET speaker now.")
        time.sleep(4.0)
        lat_tgt = measure_target()
        return lat_ref, lat_tgt

    lat_ref, lat_tgt = with_playback_paused(call, measure_all)

    if lat_ref is None or lat_tgt is None:
        print("\nmeasurement failed — see the messages above.")
        return

    delta = lat_tgt - lat_ref
    ms = round(delta * 1000)
    print("\n" + "=" * 60)
    print(f"  reference sink path latency : {lat_ref * 1000:7.1f} ms")
    print(f"  target sink path latency    : {lat_tgt * 1000:7.1f} ms")
    print(f"  ==> OUTPUT_LATENCY_MS = {ms}")
    print("=" * 60)
    print("\nNext steps:")
    print(f"  * set OUTPUT_LATENCY_MS = {ms} in config.py on this machine")
    print("    (or re-run with --write-config), then restart the msync client")
    print("  * fine-tune by ear from the client console with: latency <ms>")
    print("  * each measurement includes ~1-3 ms of speaker-to-mic air; with\n"
          "    the mic close to each speaker those roughly cancel.")
    if args.write_config:
        write_config_value(ms)


if __name__ == "__main__":
    main()