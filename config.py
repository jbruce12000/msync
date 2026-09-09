"""msync config — shared by server and clients; env vars override every value.

Server holds the tracks; clients only need the network settings.
Edit values here or override with the MSYNC_* env vars named below."""

import os
import tempfile


def _here(name):
    """Absolute path to a file next to this one."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)


# --------------------------------------------------------------------------- #
# Music library                                                               #
# --------------------------------------------------------------------------- #

# Server's music folder (audio files here get played and scanned). Defaults
# to the "music" folder next to this file (see the README/project layout);
# point MSYNC_MUSIC_DIR (or edit this line) at a larger library elsewhere.
#MUSIC_DIR = os.environ.get("MSYNC_MUSIC_DIR", _here("music"))
MUSIC_DIR = "/home/jbruce/music"

# Sub-folder inside MUSIC_DIR: drop audio files here to queue them.
QUEUE_SUBDIR = ".queue"

# Client's local cache for downloaded tracks.
CACHE_DIR = os.environ.get(
    "MSYNC_CACHE_DIR", os.path.join(tempfile.gettempdir(), "msync-cache"))

# Server's catalog DB (albums/tags/queue), SQLite.
DB_PATH = os.environ.get("MSYNC_DB_PATH", _here("msync.db"))


# --------------------------------------------------------------------------- #
# Networking                                                                  #
# --------------------------------------------------------------------------- #

# UDP sync/state/clock port (server binds it, clients listen).
DEFAULT_PORT = int(os.environ.get("MSYNC_DEFAULT_PORT", "9770"))

# Web UI / HTTP API port is the UDP port plus this offset.
HTTP_PORT_OFFSET = 1000

# How long (seconds) a room can go without a heartbeat before the server
# forgets it: its row is deleted from the catalog DB and it disappears from
# the Configure tab. Clients re-register every few seconds, so this only
# affects rooms that have genuinely gone away.
CLIENT_STALE_AFTER = int(os.environ.get("MSYNC_CLIENT_STALE_AFTER", "86400"))


# --------------------------------------------------------------------------- #
# Room tuning                                                                  #
# --------------------------------------------------------------------------- #

# Extra latency of THIS machine's audio output path, in milliseconds (0 =
# none). Rooms whose sound reaches the speakers through a heavily-buffered
# path (HDMI -> TV/AVR, Bluetooth, etc.) arrive late relative to rooms on
# a low-latency path, so the sync must play that far AHEAD of the timeline
# to line the sounds up.
#
# Measure it with tools/measure_latency.py (plug a USB mic into the room,
# run the script, it prints the value), then set that value here. 0 means
# the normal amount of buffering is fine for this room.
OUTPUT_LATENCY_MS = float(os.environ.get("MSYNC_OUTPUT_LATENCY_MS", "0"))


# --------------------------------------------------------------------------- #
# PID test mode (bang-bang outside the error window)                          #
# --------------------------------------------------------------------------- #

# EXPERIMENTAL drift-controller hybrid. PID mode is ENABLED by default in this
# repo (current test cycle): whenever the smoothed sync error is OUTSIDE a
# +/-BANG_BANG_WINDOW_MS window the controller drives the actuator at FULL pitch
# power (+/-MAX_PITCH = +/-2000ppm) bang-bang, snapping the playhead back
# quickly; INSIDE the window it runs the host's autodetected, critically-damped
# PID (see pid_tune.py). Aggressive and audibly fast -- not for production.
# Every value is env-overridable:
#
#   MSYNC_BANG_BANG=0               disable the hybrid (back to the gentle PI)
#   MSYNC_BANG_BANG_WINDOW_MS=10    error window in milliseconds (default 10)
BANG_BANG = os.environ.get("MSYNC_BANG_BANG", "1") in ("1", "true", "yes", "on")
BANG_BANG_WINDOW_MS = float(os.environ.get("MSYNC_BANG_BANG_WINDOW_MS", "10"))

# Critically-damped PID tuning (used inside the window in test mode). The gains
# are autodetected per host from its detected audio block period and the bang
# window, then persisted (see pid_tune.py) and never changed afterward.
#   PID_SETTLE_MS        target 95% settling time (ms); 0 = auto, derived from
#                        the bang window so the PID stays unsaturated inside it
#   PID_OMEGA_MAX_FRAC   cap on wn as a fraction of the host's block/loop rate
#   MSYNC_PID_FILE       optional override path for this host's PID values file
PID_SETTLE_MS = float(os.environ.get("MSYNC_PID_SETTLE_MS", "0"))
PID_OMEGA_MAX_FRAC = float(os.environ.get("MSYNC_PID_OMEGA_MAX_FRAC", "0.5"))
