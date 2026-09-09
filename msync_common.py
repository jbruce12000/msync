"""
msync_common.py - shared protocol/logic for msync server & client.

Protocol (UDP on configurable port):
  Server broadcasts SYNC packets periodically.
  Client sends NTP request packets; server replies with NTP response.
  Client may send control commands (but typically controls are on the
  server itself).

All timestamps are Unix seconds using time.time() (NTP-friendly).
"""

import json
import logging
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

# Playback tuning
LATENCY_SEC   = 0.20        # client start latency per song (schedule ahead)
MAX_PITCH     = 0.002       # max drift correction pitch (0.2%)
PITCH_GAIN    = 0.03        # proportional gain for drift correction (1/s)
PITCH_INT     = 0.02        # integral gain (accumulated error -> pitch)
DRIFT_HYSTERESIS = 0.010    # deadband (s): don't respond to smaller errors
CATCHUP_THRESHOLD = 0.06    # bounded playhead nudge only when gap exceeds this (s)
CATCHUP_STEP      = 0.005   # max nudge per audio block while catching up (s)

# PLL error smoothing. The error seen by the rate controller is the raw
# difference between the (block-quantized) local playhead and the
# NTP-derived reference, which carries tens of ms of reference jitter and a
# +-half-block sawtooth. Fed straight into the PI, that noise saturates the
# +-0.2% pitch clamp on almost every block and the loop limit-cycles at
# +-MAX_PITCH (audible as a slow, constant speed wobble). So the pitch
# controller runs on a low-passed error instead; the EMA weight is per
# callback (~tau 0.6s at 5 Hz blocks).
PLL_ALPHA     = 0.15        # PLL error EMA weight (0..1, higher = faster)
INT_UNWIND    = 0.85        # per-callback integrator unwind when error is tiny
INT_LIMIT     = 0.02        # integrator state clip (x PITCH_INT = <=400ppm)

# PID test mode (bang-bang/PID hybrid) tuning. PID_P_EDGE_FRAC is the fraction
# of the pitch rail the critically-damped PID's P term is designed to use at the
# bang-window edge (see pid_tune.py). Small enough to keep the PID unsaturated
# (genuinely linear) across the +/-window instead of railing a fraction of a
# millisecond into it; 1.0 would pin the P output at the rail at the edge.
PID_P_EDGE_FRAC = 0.6       # P term at window edge, as a fraction of MAX_PITCH


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
# the callback thread real-time scheduling when permitted, (b) tighten the GIL #
# switch interval so the callback grabs the GIL sooner under load, and        #
# (c) raise the whole process's priority when allowed. All failures are       #
# swallowed: unprivileged setups simply keep normal scheduling.                #
# --------------------------------------------------------------------------- #
_AUDIO_BOOST_TRIED = set()
_AUDIO_BOOST_LOCK = threading.Lock()

# Serializes every PortAudio stream open/start/stop/close across the process.
# Test runs frequently have a server stream and a client stream live at the
# same time; concurrent lifecycle calls on the ALSA->PipeWire bridge can
# corrupt its callback tables (observed as a use-after-free crash jumping into
# a freed pipewire memfd), so we never let two streams change state at once.
stream_ops_lock = threading.Lock()


def boost_audio_thread():
    """Best-effort: give the calling thread (PortAudio's callback thread)
    the highest real-time scheduling priority the system permits.

    Tries SCHED_FIFO (then SCHED_RR) from the highest allowed priority
    downward. Requires CAP_SYS_NICE / an RT rlimit; otherwise the thread
    silently keeps its normal priority. Each OS thread is only attempted
    once, so placing this at the top of an audio callback is cheap.
    """
    if not hasattr(os, "gettid") or not hasattr(os, "sched_setscheduler"):
        return
    tid = os.gettid()
    with _AUDIO_BOOST_LOCK:
        if tid in _AUDIO_BOOST_TRIED:
            return
        _AUDIO_BOOST_TRIED.add(tid)
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
                    return
                except (OSError, ValueError):
                    continue
    except Exception:
        pass


def tune_process():
    """Process-wide scheduling smoothing, run once at startup.

    - ``sys.setswitchinterval(0.002)``: the GIL is handed off every 2 ms
      instead of 5 ms, so the audio callback thread waits less behind the
      scan/HTTP/download threads that collectively hold the GIL.
    - ``os.nice(-10)``: raise the whole process's priority (needs root /
      CAP_SYS_NICE; silently ignored otherwise).
    """
    try:
        sys.setswitchinterval(0.002)
    except Exception:
        pass
    if hasattr(os, "nice"):
        try:
            os.nice(-10)
        except OSError:
            pass
