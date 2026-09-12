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

# Pandora radio (server only). DISABLED by default: set PANDORA_ENABLED = True
# (or MSYNC_PANDORA_ENABLED=1) to switch it on, then either set your account
# credentials below for your real stations, or leave both blank to run in mock
# mode (fabricated stations/tracks) for development. When enabled, the server's
# Pandora thread (msync_pandora.PandoraService) logs in, fetches station
# playlists, and downloads the streams into MUSIC_DIR/"Pandora - <Station>"/,
# queuing each one as it finishes. The web UI's Pandora tab appears only while
# this is enabled.
#
# To use your real account, type your credentials directly into the two lines
# below — the repo's git clean filter (tools/setup-msync-secret-filter.sh)
# scrubs them from every commit automatically, so they never leave this
# machine. Alternatively, leave the defaults untouched and set
# MSYNC_PANDORA_USERNAME / MSYNC_PANDORA_PASSWORD env vars in the service
# environment instead. Blank credentials (the default) runs mock mode.
#
# Caveat: `git checkout -- config.py` replaces your local credentials with the
# scrubbed version from git — re-type them after any checkout or stash.
#
#PANDORA_ENABLED = os.environ.get("MSYNC_PANDORA_ENABLED", "0") in ("1", "true", "yes", "on")
PANDORA_ENABLED = True
PANDORA_USERNAME = os.environ.get("MSYNC_PANDORA_USERNAME", "")
PANDORA_PASSWORD = os.environ.get("MSYNC_PANDORA_PASSWORD", "")

# Pandora radio batch size: how many songs a station queues in one go. When
# you start a station (or click the Pandora tab's "Queue N more" button) the
# radio downloads exactly this many tracks into the queue and then stops —
# it never auto-refills. Env override: MSYNC_PANDORA_QUEUE_SIZE.
PANDORA_QUEUE_SIZE = int(os.environ.get("MSYNC_PANDORA_QUEUE_SIZE", "10"))

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

# Drift-controller hybrid, ON BY DEFAULT. When enabled, any smoothed sync
# error OUTSIDE a +/-BANG_BANG_WINDOW_MS window drives the actuator at FULL
# pitch power (+/-MAX_PITCH = +/-2000ppm) bang-bang; INSIDE the window it runs
# a critically-damped PID (see pid_tune.py). The audio paths realize pitch by
# dropping/repeating raw samples at block boundaries (no interpolation), so
# large pitches are AUDIBLY CLICKY — which is why the fresh-track-start quorum
# matters: every room anchors a new song to the SAME consensus start time, so
# the opening error is one small shared bias inside the window and bang-bang
# never engages on song start. Set to 0 to fall back to the gentle PI
# controller for the whole song. Env-overridable:
#
#   MSYNC_BANG_BANG=0               disable the hybrid (gentle PI instead)
#   MSYNC_BANG_BANG_WINDOW_MS=10    error window in milliseconds (default 10)
BANG_BANG = os.environ.get("MSYNC_BANG_BANG", "1") in ("1", "true", "yes", "on")
BANG_BANG_WINDOW_MS = float(os.environ.get("MSYNC_BANG_BANG_WINDOW_MS", "10"))

# Critically-damped PID tuning (used inside the window in test mode). The gains
# are computed once at start-up from the bang window and the host's audio block
# period, and held in memory — nothing is persisted (see pid_tune.py).
#   PID_SETTLE_MS        target 95% settling time (ms); 0 = auto, derived from
#                        the bang window so the PID stays unsaturated inside it
#   PID_OMEGA_MAX_FRAC   cap on wn as a fraction of the host's block/loop rate
PID_SETTLE_MS = float(os.environ.get("MSYNC_PID_SETTLE_MS", "0"))
PID_OMEGA_MAX_FRAC = float(os.environ.get("MSYNC_PID_OMEGA_MAX_FRAC", "0.5"))
