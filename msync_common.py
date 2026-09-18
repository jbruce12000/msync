"""
msync_common.py - shared protocol/logic for msync server & client.

Protocol (UDP on configurable port):
  Server broadcasts SYNC packets periodically.
  Client sends NTP request packets; server replies with NTP response.
  Client may send control commands (but typically controls are on the
  server itself).

All timestamps are Unix seconds using time.time() (NTP-friendly).
"""

import config
import json
import logging
import numpy as np
import os
import socket
import sys
import threading
import time

MAGIC = b"MSYN"
PROTO_VERSION = 1

# --------------------------------------------------------------------------- #
# Logging                                                                      #
# --------------------------------------------------------------------------- #
_LOGGER = None


def logger():
    """Return the shared msync logger, configuring it on first use."""
    global _LOGGER
    if _LOGGER is None:
        _LOGGER = logging.getLogger("msync")
    return _LOGGER


def setup_logging(level=logging.INFO):
    """Configure the msync logger to write to stdout by default.

    Called once at startup (server or client). Safe to call more than once.
    """
    lg = logger()
    if not lg.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(
            "[%(levelname)s] %(message)s"))
        lg.addHandler(handler)
        lg.setLevel(level)
        lg.propagate = False
    return lg


# Packet "types" (first payload byte after magic)
TYPE_SYNC      = 1   # server broadcast: current state + song_start timestamp
TYPE_NTP_REQ   = 2   # client -> server: t1 (client tx)
TYPE_NTP_RESP  = 3   # server -> client: t1, t2 (server rx), t3 (server tx)
TYPE_WELCOME   = 4   # server -> client on register
TYPE_REGISTER  = 5   # client -> server handshake
TYPE_PROBE     = 6   # client -> server discovery probe; server answers unicast
TYPE_STATE     = 7   # server broadcast (1 Hz): full state incl. queue list
TYPE_LATENCY   = 8   # server -> client: set output-latency offset (ms)
TYPE_VOLUME    = 9   # server -> client: set this room's volume (0..1.5) + mute
TYPE_START_VOTE = 10 # client -> server: offset vote for the fresh-track-start
                     # quorum (epoch, offset_ms) — each room offers its current
                     # NTP offset estimate when a new track (re)starts
TYPE_START_ACK  = 11 # server -> all: agreed (consensus) clock offset for the
                     # epoch that just started (epoch, offset_ms): the median of
                     # the voters' estimates, used to anchor everyone to the
                     # SAME start time so bang-bang (and the whole suite) hears
                     # a clean song opening instead of per-room start jitter

DEFAULT_PORT = 9770
SYNC_INTERVAL = 0.05        # server sync broadcast period (s) = 20 Hz
STATE_INTERVAL = 1.0        # server state broadcast period (s) for queue info
NTP_INTERVAL  = 1.0         # client NTP resync period (s)
REGISTER_INTERVAL = 10.0    # client re-registration period (s): keeps the
                            # web UI's room list + online status fresh across
                            # server restarts (the server also treats every
                            # NTP request as a heartbeat)

# Idle / paused intervals – broadcast much less when nothing is moving
IDLE_SYNC_INTERVAL  = 10.0  # server sync broadcast period when paused/stopped
IDLE_STATE_INTERVAL = 10.0  # server state broadcast period when paused/stopped
IDLE_NTP_INTERVAL   = 10.0  # client NTP resync period when paused/stopped

# Fresh-track-start quorum. A track (or restart) opens a short round where every
# client reports its NTP offset estimate; the server closes the round with the
# median of the voters' estimates (a consensus start time) once START_QUORUM
# distinct rooms have voted or the deadline passes, then broadcasts
# TYPE_START_ACK. All rooms then anchor the new track to that SAME offset, so
# the initial playhead error right at the beginning of a song is one shared
# bias instead of each room's own (often +-10ms+) estimate — bang-bang stays
# inside its error window instead of slamming the pitch rail on song open.
START_ROUND_SEC    = 0.6    # how long the server collects offset votes (s)
START_QUORUM       = 2      # distinct voters that close the round early
START_VOTE_INTERVAL = 0.1   # client vote cadence while the round is open (s)

# Playback tuning
LATENCY_SEC   = 0.20        # client start latency per song (schedule ahead)
MAX_PITCH     = config.MAX_PITCH  # max drift correction pitch (0.2%), see config.py
PITCH_GAIN    = 0.03        # proportional gain for drift correction (1/s)
PITCH_INT     = 0.02        # integral gain (accumulated error -> pitch)
DRIFT_HYSTERESIS = 0.010    # deadband (s): don't respond to smaller errors
CATCHUP_THRESHOLD = 0.02    # bounded playhead nudge only when gap exceeds this (s)
CATCHUP_STEP      = 0.005   # max nudge per audio block while catching up (s)

# Next-track prefetch. The following queued song is downloaded + decoded in a
# background thread right after each adoption so a normal auto-advance can be
# swapped in with zero HTTP/decode latency, and its front pages are re-warmed
# just before the current song ends so the swap's first callbacks never stall
# on cold mmap reads (the source of the post-switch err excursion).
PREFETCH_PREWARM_LEAD_S = 10.0   # warm the prefetched buffer this many seconds
                                 # before the current track is expected to end
PREFETCH_PREWARM_S      = 2.0    # warm this many seconds of the next track

# PLL error smoothing. The error seen by the rate controller is the raw
# difference between the (block-quantized) local playhead and the
# NTP-derived reference, which carries tens of ms of reference jitter and a
# +-half-block sawtooth. Fed straight into the PI, that noise saturates the
# +-0.2% pitch clamp on almost every block and the loop limit-cycles at
# +-MAX_PITCH (audible as a slow, constant speed wobble). So the pitch
# controller runs on a low-passed error instead; the EMA weight is per
# callback (~tau 0.6s at 5 Hz blocks).
PLL_ALPHA     = 0.15        # PLL error EMA weight (0..1, higher = faster)
INT_UNWIND    = 1.0         # per-callback integrator unwind when error is tiny (1.0 = no decay; lets integral hold its drift correction)
INT_LIMIT     = 0.02        # integrator state clip (x PITCH_INT = <=400ppm)

# PID test mode (bang-bang/PID hybrid) tuning. PID_P_EDGE_FRAC is the fraction
# of the pitch rail the critically-damped PID's P term is designed to use at the
# bang-window edge (see pid_tune.py). Small enough to keep the PID unsaturated
# (genuinely linear) across the +/-window instead of railing a fraction of a
# millisecond into it; 1.0 would pin the P output at the rail at the edge.
PID_P_EDGE_FRAC = 0.6       # P term at window edge, as a fraction of MAX_PITCH

# Loud state transitions (pause/resume, mute/volume changes, track starts/ends)
# are ramped over this many seconds in the audio callback. A hard step from
# full program level to silence (or back) is a click; a few-ms ramp is
# inaudible and removes it.
AUDIO_FADE_SEC = 0.010


def interp_read(data, idx_pre, rate, frames, catchup_s=0.0):
    """Variable-rate interpolated read from stereo audio data.

    Reads *frames* output samples from *data* (N×2 float32) starting at
    fractional content-sample position *idx_pre*, advancing by *rate*
    content samples per output sample.  Linear interpolation eliminates the
    block-boundary sample drops/repeats that produce clicks when *rate* ≠ 1.

    When *catchup_s* is nonzero (a bounded playhead nudge applied in the
    same block), the displacement is ramped linearly across the block so the
    transition is click-free.

    Returns (out, n_valid) — the (frames, 2) float32 output buffer and the
    count of samples that fell inside the valid region of *data*.
    """
    if abs(catchup_s) > 0.5:
        ramp = np.linspace(0.0, 1.0, frames, dtype=np.float64)
        pos = idx_pre + np.arange(frames, dtype=np.float64) * rate + catchup_s * ramp
    else:
        pos = idx_pre + np.arange(frames, dtype=np.float64) * rate
    n = len(data)
    i = pos.astype(np.int64)
    f = (pos - i).astype(np.float32)
    valid_mask = (i >= 0) & (i < n)
    i_safe = np.clip(i, 0, n - 1)
    i1_safe = np.minimum(i_safe + 1, n - 1)
    out = np.empty((frames, 2), dtype=np.float32)
    f2 = f[:, None]
    out[:, 0] = data[i_safe, 0] * (1 - f2.ravel()) + data[i1_safe, 0] * f2.ravel()
    out[:, 1] = data[i_safe, 1] * (1 - f2.ravel()) + data[i1_safe, 1] * f2.ravel()
    out[~valid_mask] = 0.0
    n_valid = int(valid_mask.sum())
    return out, n_valid


def ts():
    return time.time()


def make_packet(ptype: int, **fields) -> bytes:
    body = {**fields, "type": ptype}
    return MAGIC + bytes([ptype]) + json.dumps(body).encode()


def parse_packet(data: bytes):
    if not data.startswith(MAGIC) or len(data) <= len(MAGIC):
        return None
    ptype = data[len(MAGIC)]
    try:
        body = json.loads(data[len(MAGIC) + 1:].decode())
    except Exception:
        return None
    return ptype, body


# --------------------------------------------------------------------------- #
# Audio scheduling priority                                                   #
#                                                                             #
# Music is produced by PortAudio's callback thread. It must never be starved  #
# by a busy server: heaviest GIL users are the catalog scan (tinytag), HTTP    #
# client downloads, the inotify watcher, and NTP/status threads. We (a) give  #
# the callback thread real-time scheduling when permitted and (b) tighten the #
# GIL switch interval so the callback grabs the GIL sooner under load. The    #
# process's nice value is deliberately left at 0 (the system default). All    #
# failures are swallowed: unprivileged setups simply keep normal scheduling.  #
# --------------------------------------------------------------------------- #
_AUDIO_BOOST_TRIED = set()
_AUDIO_BOOST_LOCK = threading.Lock()

# Serializes every PortAudio stream open/start/stop/close across the process.
# Test runs frequently have a server stream and a client stream live at the
# same time; concurrent lifecycle calls on the ALSA->PipeWire bridge can
# corrupt its callback tables (observed as a use-after-free crash jumping into
# a freed pipewire memfd), so we never let two streams change state at once.
stream_ops_lock = threading.Lock()


def _current_tid():
    """Native OS thread id of the calling thread (works even on Pythons where
    ``os.gettid`` is absent; ``threading.get_native_id`` is 3.8+)."""
    if hasattr(os, "gettid"):
        try:
            return os.gettid()
        except OSError:
            pass
    try:
        return threading.get_native_id()
    except (AttributeError, OSError):
        return None


def boost_audio_thread():
    """Best-effort: give the calling thread (PortAudio's callback thread)
    the highest real-time scheduling priority the system permits.

    Tries SCHED_FIFO (then SCHED_RR) from the highest allowed priority
    downward. Requires CAP_SYS_NICE / an RT rlimit (see the installed units'
    ``LimitRTPRIO=99``); otherwise the thread keeps its normal priority and a
    one-time warning is logged so the journal shows the boost's outcome.
    Each OS thread is only attempted once, so placing this at the top of an
    audio callback is cheap.
    """
    if not hasattr(os, "sched_setscheduler"):
        return
    tid = _current_tid()
    if tid is None:
        return
    with _AUDIO_BOOST_LOCK:
        if tid in _AUDIO_BOOST_TRIED:
            return
        _AUDIO_BOOST_TRIED.add(tid)
    outcome = None
    try:
        for policy_name in ("SCHED_FIFO", "SCHED_RR"):
            policy = getattr(os, policy_name, None)
            if policy is None:
                continue
            pmax = os.sched_get_priority_max(policy)
            pmin = os.sched_get_priority_min(policy)
            # highest permitted first; the rlimit may only allow the bottom.
            for prio in (pmax, pmin):
                try:
                    os.sched_setscheduler(tid, policy, os.sched_param(prio))
                    outcome = "%s @ priority %d" % (policy_name, prio)
                    break
                except (OSError, ValueError):
                    continue
            if outcome is not None:
                break
    except Exception:
        pass
    if outcome is not None:
        logger().info("audio callback thread tid=%d: scheduling %s",
                      tid, outcome)
    else:
        logger().warning(
            "audio callback thread tid=%d: could NOT get real-time "
            "scheduling (need LimitRTPRIO in the unit, or root)", tid)


def _rt_rlimit():
    """Current RLIMIT_RTPRIO (the cap on sched_setscheduler priority), or None
    if the platform doesn't expose it."""
    try:
        import resource
        cur, _ = resource.getrlimit(resource.RLIMIT_RTPRIO)
        return cur
    except (ImportError, AttributeError, OSError, ValueError):
        return None


def tune_process():
    """Process-wide scheduling smoothing, run once at startup.

    - ``sys.setswitchinterval(0.002)``: the GIL is handed off every 2 ms
      instead of 5 ms, so the audio callback thread waits less behind the
      scan/HTTP/download threads that collectively hold the GIL.
    - Leaves the process's nice value at 0 (the default): no priority boost.
    - Logs the resulting nice value and real-time rlimit, so `journalctl`
      can confirm whether the audio RT boost (``boost_audio_thread``) will
      apply — an rlimit of 0 means the unit needs ``LimitRTPRIO=99``.
    """
    try:
        sys.setswitchinterval(0.002)
    except Exception:
        pass
    # Read back and report what actually took effect.
    try:
        nice = os.getpriority(os.PRIO_PROCESS, 0)
    except (AttributeError, OSError):
        nice = None
    rt = _rt_rlimit()
    if nice is None:
        logger().info("priority: nice readback unavailable")
    elif rt is None:
        logger().info("priority: nice=%d (RT rlimit not exposed)", nice)
    else:
        logger().info("priority: nice=%d, realtime rlimit=%d%s",
                      nice, rt, "" if rt > 0 else
                      "  -> audio RT boost will NOT apply")
