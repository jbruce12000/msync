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

# Use the 2-state Kalman error smoother in place of the fixed-gain EMA
# (PLL_ALPHA) for the drift controller's smoothed input. Kalman adapts its
# gain to measurement noise (better under NTP jitter spikes) and estimates
# error velocity, which acts as a clean derivative term. Set to 1 to A/B
# test against the EMA path; leave 0 for the battle-tested default. Read in
# msync_server.py and msync_client.py.
USE_KALMAN = os.environ.get("MSYNC_USE_KALMAN", "0") in ("1", "true", "yes")
