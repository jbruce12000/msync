"""
msync_youtube.py - YouTube audio for msync via yt-dlp, as a separate daemon thread.

YouTubeService owns every YouTube-facing concern:

  * a "source" list — configured search queries and/or playlist/channel URLs
    (the radio analog of a Pandora station)
  * harvesting: for each source, yt-dlp lists candidate videos (metadata only),
    filters out live streams / Shorts / very long videos, downloads the chosen
    tracks' audio (bestaudio), and transcodes whatever miniaudio can't decode
    (webm/opus/m4a) to FLAC with ffmpeg. Candidates are cached in a deep pool
    (SEARCH_POOL_SIZE) so repeated batches keep finding fresh tracks, and
    long "mix"/compilation videos that carry per-song chapters are split into
    one FLAC per chapter instead of being queued as a single giant track
  * a lookahead loop that keeps the server's play queue topped up.

It shares NOTHING with the server's audio/sync/catalog threads, exactly like
PandoraService — the only coupling is two callbacks the server injects:

  * ``queued()``     -> int, how many of THIS source's tracks are still
                        sitting in the server's play queue (read-only)
  * ``submit(path)`` -> called from the YouTube thread whenever a download
                        has finished and is ready to be queued

``queued()`` must be cheap and must not block on YouTube; ``submit()`` is
expected to hand the path to the server's own queue under the server's lock.
Neither callback may call back into YouTubeService (no re-entrancy).

With no yt-dlp installed (or ``mock=True``) the service serves fabricated
sources and writes tiny generated WAV files, so the whole thread, lookahead
and queue pipeline runs end-to-end without network.

The server drives it in BATCH mode: starting a source (or the YouTube tab's
"Queue more" button) queues ``batch`` tracks and then stops — a finite
harvest, never an auto-refilling radio. The CLI's ``--serve`` still runs the
old continuous top-up mode for standalone listening.

CLI (standalone, no server needed):

    python3 msync_youtube.py --mock --list-sources
    python3 msync_youtube.py --mock --harvest "Lofi Beats" 4
    python3 msync_youtube.py --mock --serve "Synthwave" --duration 30

    # or for real (needs: pip install yt-dlp):
    python3 msync_youtube.py --harvest "Lofi Beats" 4
"""

import argparse
import array
import json
import logging
import math
import os
import re
import shutil
import subprocess
import threading
import time
import wave
import zlib

import msync_common as C

# Downloaded YouTube tracks land in a per-source "album" folder so they show
# up in the library/album UI:  <music_dir>/YouTube - <Source>/Artist - Title.ext
SOURCE_FOLDER_PREFIX = "YouTube - "

# Formats the server's decoder (miniaudio) can play natively. yt-dlp's
# bestaudio picks are usually webm (opus) or m4a (aac), which miniaudio cannot
# decode, so those get transcoded to FLAC with ffmpeg (if present) the moment
# they're downloaded — the same rule PandoraService uses for its AAC streams.
_PLAYABLE_EXTS = frozenset({".wav", ".flac", ".mp3", ".ogg"})

# How deep each source's candidate pool is. A search query lists this many
# videos at a time and the results are consumed lazily across batches, so
# "queue more" keeps finding fresh tracks instead of recycling the same top
# handful (which is why batches of 5 used to only add 3). Playlist / channel
# URLs run the whole list regardless of this number.
SEARCH_POOL_SIZE = 20

# Shortest chapter worth splitting out of a mix/compilation — intro/outro
# clicks and "thanks for watching" stubs are dropped, not queued as songs.
MIN_CHAPTER_S = 30.0


# --------------------------------------------------------------------------- #
# Sources ("stations" for YouTube). A source is a search query or a playlist /
# channel URL. The built-in ones are search queries; add your own in config.py
# via YOUTUBE_SOURCES = "name|url, name|query, ...".
# --------------------------------------------------------------------------- #
DEFAULT_SOURCES = [
    {"sourceToken": "src-lofi", "sourceName": "Lofi Beats",
     "query": "lofi hip hop beats to study to"},
    {"sourceToken": "src-synthwave", "sourceName": "Synthwave",
     "query": "synthwave mix extended"},
    {"sourceToken": "src-deepdub", "sourceName": "Deep Dub",
     "query": "deep dub techno mix"},
    {"sourceToken": "src-jazzhop", "sourceName": "Jazzhop Chill",
     "query": "jazz hop chill mix"},
]

# Mock sources/tracks (same shape) used when mock=True, so the whole pipeline
# runs offline. The tokens double as the "sourceToken" you pass to
# play_source() in mock mode.
MOCK_SOURCES = [
    {"sourceToken": "src-lofi", "sourceName": "Lofi Beats",
     "query": "lofi hip hop beats to study to"},
    {"sourceToken": "src-synthwave", "sourceName": "Synthwave",
     "query": "synthwave mix extended"},
    {"sourceToken": "src-deepdub", "sourceName": "Deep Dub",
     "query": "deep dub techno mix"},
]

MOCK_TRACKS = {
    "src-lofi": [
        ("Golden Hour", "Maeve Ellis"),
        ("Neon Bloom", "The Velvet Tide"),
        ("Paper Planes", "Harlow James"),
        ("Glass Garden", "Iris Vale"),
        ("Slow Bright", "Cedar & Smoke"),
        ("Sunlight Static", "June Marlow"),
    ],
    "src-synthwave": [
        ("Rattlesnake Reel", "The Broken Mics"),
        ("Highway Static", "Rita Kane"),
        ("Copper Line", "Dead Elk Union"),
        ("Thunder Porch", "Sam Blackwood"),
        ("Last Transmission", "The Wire Owls"),
        ("Dynamite Sunday", "Lila Cross"),
    ],
    "src-deepdub": [
        ("Porch Light", "Amos Reed"),
        ("River Stones", "Fern Holiday"),
        ("Oak & Ember", "Tiller Bay"),
        ("Morning Fog", "The Quiet Hours"),
        ("Willow Lane", "Juniper Fox"),
        ("Old Maps", "Bellweather"),
    ],
}


def ytdlp_available():
    """True when the yt_dlp package can be imported (best-effort, no import
    side effects — the real import stays lazy inside the download paths)."""
    try:
        import importlib.util
        return importlib.util.find_spec("yt_dlp") is not None
    except Exception:
        return False


def _slug(name):
    """Dedupe-friendly token from a display name: 'Lofi Beats' -> 'lofi-beats'."""
    return re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-") or "source"


def parse_sources(text):
    """'Name|query, Name|url, ...' -> [{sourceToken, sourceName, query}].

    Bare entries (no '|') use the same string for both the display name and
    the query/URL. Used by the server to turn config.YOUTUBE_SOURCES into the
    service's source list.
    """
    out, seen = [], set()
    for part in str(text or "").split(","):
        part = part.strip()
        if not part:
            continue
        name, sep, query = part.partition("|")
        name = name.strip()
        if not sep:
            # bare entry (no '|'): the same string is the name AND the query
            query = name
        else:
            query = query.strip()
        if not name or not query:
            continue
        tok = "src-" + _slug(name)
        if tok in seen:
            continue
        seen.add(tok)
        out.append({"sourceToken": tok, "sourceName": name, "query": query})
    return out


def _sanitize(name):
    """Filename-safe version of a song/source name."""
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name or "untitled"


def _write_mock_wav(path, freq, dur=1.2, sr=44100):
    """Write a tiny stereo sine-wave WAV (stdlib only) for mock mode."""
    n = int(sr * dur)
    fade = int(sr * 0.05)
    samples = array.array("h")
    for i in range(n):
        env = 1.0
        if i < fade:
            env = i / fade
        elif i > n - fade:
            env = (n - i) / fade
        v = int(0.28 * 32767 * math.sin(2 * math.pi * freq * i / sr) * env)
        samples.append(v)   # left
        samples.append(v)   # right
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(samples.tobytes())


class YouTubeService(threading.Thread):
    """Fully independent YouTube radio thread.

    Start it with ``svc.start()`` (like the server's other background
    threads), or drive it synchronously with ``run_until_done()`` for
    finite harvests in the CLI. Pick one mode; don't call both.

    Callbacks (all called from the YouTube thread):
      queued()     -> int   remaining tracks of this source in the server queue
      submit(path) -> None  a finished download to append to the server queue

    ``sources`` may be None (defaults), a ``parse_sources()``-style string,
    or a list of {"sourceToken", "sourceName", "query"} dicts.
    """

    def __init__(self, music_dir, *, mock=False, lookahead=4, topup_when=1,
                 batch=10, sources=None, download_timeout=60, max_duration=None,
                 single_limit=None,
                 queued=None, submit=None, log=None):
        super().__init__(name="youtube", daemon=True)
        self.music_dir = os.path.abspath(music_dir)
        self.mock = bool(mock)
        self._ytdlp_ok = ytdlp_available()
        if isinstance(sources, str):
            sources = parse_sources(sources)
        self._sources = list(sources or (MOCK_SOURCES if self.mock else DEFAULT_SOURCES))
        self._lookahead = max(1, int(lookahead))
        self._topup_when = max(1, int(topup_when))
        self._batch = max(1, int(batch))        # songs per "queue more" batch
        self._download_timeout = download_timeout
        self._max_duration = max_duration       # seconds; None = no cap
        # Longest video allowed to be a single track. Longer videos are fine
        # only when they can be split into per-chapter songs (see
        # ``_post_download``); chapterless ones are mixes/ambient blobs and
        # get skipped rather than queued as one giant "track".
        self._single_limit = int(single_limit) if single_limit else 10 * 60
        self._queued = queued if callable(queued) else self._local_queued
        self._submit = submit if callable(submit) else self._local_submit
        self.log = log or C.logger()

        # Sources created at runtime (web tab's search box) are persisted to a
        # JSON file next to the music dir so they survive restarts. Mock mode
        # is ephemeral by design (fabricated sources, fresh test dirs).
        self._sources_file = os.path.join(self.music_dir, "youtube_sources.json")
        if not self.mock:
            self._load_persisted_sources()

        self._lock = threading.RLock()
        self._stop_event = threading.Event()

        self._connected = False
        self._connect_error = None
        self._station = None       # {"sourceToken", "sourceName", "query"}
        self._station_dir = None   # absolute album folder for downloads
        self._seen = set()         # video ids downloaded this session
        self._wanted = 0           # 0 = continuous radio, >0 = finite harvest
        self._downloaded = 0
        self._done = False
        self._last_error = None
        self._mock_cursor = {}     # per-source playlist cursor for mock mode

        # Deep cached candidate pool for the active source. ``_harvest`` pops
        # from it and refills via ``_list_tracks(token, need=self._pool_size)``
        # when empty. ``_exhausted`` snaps True when a full refill produces no
        # new tracks (the search/playlist has run dry for this session).
        self._pool_size = SEARCH_POOL_SIZE
        self._candidates = []
        self._exhausted = False
        self._exhausted_warned = False
        self._min_song_s = MIN_CHAPTER_S

        # ffmpeg availability for webm/m4a->FLAC transcoding (detected once).
        self._ffmpeg_path = shutil.which("ffmpeg")
        self._transcode_warned = False

        # Standalone queue simulation (used when no server callbacks are given).
        self._local_entries = []

    # -- injected-callback defaults: standalone CLI keeps a local "queue" ----- #
    def _local_submit(self, path):
        self._local_entries.append(path)
        return len(self._local_entries)

    def _local_queued(self):
        return len(self._local_entries)

    # -- public API (all internally locked) ----------------------------------- #
    def connect_now(self):
        """Block until connected. Returns True on success. YouTube needs no
        login — "connected" just means yt-dlp is available (or mock mode)."""
        with self._lock:
            self._ensure_connected()
        return self._connected

    def sources(self):
        """The source list — search queries and/or playlist URLs. Returns a
        fresh list (safe to mutate); newly created sources may be added by
        ``add_source()`` at any time."""
        return [dict(s) for s in self._sources]

    def add_source(self, name, query):
        """Create a new source (search text or playlist/channel URL) at
        runtime and persist it across restarts.  Returns ``(source_dict,
        created)`` — ``created`` is False when a matching source already
        exists (the existing one is returned and nothing is persisted).
        Duplicates are matched by query text and by source name."""
        with self._lock:
            # Check existing: query match or name match (case-insensitive).
            lq = query.strip().lower()
            for st in self._sources:
                if st.get("query", "").strip().lower() == lq:
                    return st, False
                if st.get("sourceName", "").strip().lower() == name.strip().lower():
                    return st, False
            tok = self._unique_token(name)
            src = {"sourceToken": tok, "sourceName": name.strip(), "query": query.strip()}
            self._sources.append(src)
            if not self.mock:
                self._persist_sources()
            self.log.info("youtube: added source %s -> %r", name, query)
            return src, True

    def _unique_token(self, name):
        """A unique source token: ``src-<slug>`` with a numeric suffix if
        a collision is found in the current source list."""
        base = "src-" + _slug(name)
        if not any(s.get("sourceToken") == base for s in self._sources):
            return base
        n = 2
        while any(s.get("sourceToken") == f"{base}-{n}" for s in self._sources):
            n += 1
        return f"{base}-{n}"

    # -- persistence (real mode only, YAML-less) ------------------------------ #

    def _load_persisted_sources(self):
        """Merge sources from the JSON file in the music dir (if any)."""
        try:
            with open(self._sources_file, "r") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError, ValueError):
            return
        existing_tokens = {s.get("sourceToken") for s in self._sources}
        existing_queries = {s.get("query", "").lower() for s in self._sources}
        added = 0
        for src in data.get("sources", []):
            tok = src.get("sourceToken")
            query = src.get("query", "").lower()
            if not src.get("query"):
                continue
            if tok in existing_tokens or query in existing_queries:
                continue
            self._sources.append(src)
            existing_tokens.add(tok)
            added += 1
        if added:
            self.log.info("youtube: loaded %d persisted source(s)", added)

    def _persist_sources(self):
        """Write non-default sources to the JSON file.  Only called when not
        in mock mode (the caller guarantees this)."""
        try:
            with open(self._sources_file, "w") as f:
                json.dump({"sources": self._sources}, f, indent=2)
        except OSError as e:
            self.log.warning("youtube: could not persist sources: %r", e)

    def play_source(self, spec, want=0):
        """Start (or switch) the source. ``spec`` is a source name or token.

        ``want=0``  -> continuous radio: keep the queue topped up until
                       stop_source()/stop().
        ``want>0``  -> finite harvest: download exactly ``want`` tracks, then
                       pause (for the CLI / tests).
        """
        with self._lock:
            self._ensure_connected()
            st = self._find_source(spec)
            if not st:
                return False
            self._station = st
            self._station_dir = os.path.join(
                self.music_dir, SOURCE_FOLDER_PREFIX + _sanitize(st["sourceName"]))
            self._wanted = max(0, int(want))
            self._done = False
            self._seen.clear()
            self._candidates = []
            self._exhausted = False
            self._exhausted_warned = False
            self._downloaded = 0
            self._last_error = None
            mode = "continuous" if want == 0 else f"harvest {int(want)}"
            self.log.info("youtube: source -> %s (%s)", st["sourceName"], mode)
            return True

    def fetch_more(self, count=None):
        """Queue another batch for the active source (web tab's "Queue more").

        Non-blocking: raises the finite-harvest target by ``count`` (default
        ``self._batch``) and lets the thread loop keep downloading/queuing
        until that many tracks are queued. Returns False when no source is
        active (nothing to fetch more of)."""
        with self._lock:
            if self._station is None:
                return False
            n = max(1, int(count or self._batch))
            # If we were in continuous mode (want=0), switch to finite batches.
            self._wanted = (self._wanted or self._downloaded) + n
            self._done = False
            # The source may have run dry, but a user explicitly asking for
            # more deserves a fresh search attempt (results change over time).
            self._exhausted = False
            self.log.info("youtube: queueing %d more (%s)",
                          n, self._station["sourceName"])
            return True

    def stop_source(self):
        """Stop fetching (already-queued tracks keep playing)."""
        with self._lock:
            name = self._station["sourceName"] if self._station else None
            self._station = None
            self._done = True
            if name:
                self.log.info("youtube: stopped source %s", name)

    def status(self):
        """Snapshot for the server / HTTP API / tests."""
        with self._lock:
            warns = []
            if not self.mock and not self._ytdlp_ok:
                warns.append("yt-dlp not installed — add it with "
                             "`pip install yt-dlp` to make the radio work.")
            if not self.mock and not self._ffmpeg_path:
                warns.append("ffmpeg not found: YouTube downloads in webm/m4a "
                             "can't be decoded. Install it (e.g. sudo apt "
                             "install ffmpeg) to make the radio playable.")
            warn = " ".join(warns) if warns else None
            return {
                "connected": self._connected,
                "mock": self.mock,
                "connect_error": self._connect_error,
                "station": self._station.get("sourceName") if self._station else None,
                "station_token": self._station.get("sourceToken") if self._station else None,
                "station_dir": self._station_dir,
                "downloaded": self._downloaded,
                "pending": self._queued() if self._station else 0,
                "done": self._done,
                "exhausted": self._exhausted,
                "sources": len(self._sources),
                "batch": self._batch,
                "error": str(self._last_error) if self._last_error else None,
                "ffmpeg": self._ffmpeg_path is not None,
                "ytdlp": self._ytdlp_ok,
                "warn": warn,
            }

    @property
    def station_dir(self):
        """Absolute folder the active source's downloads land in (None idle)."""
        with self._lock:
            return self._station_dir

    def stop(self, timeout=None):
        """Stop the daemon thread after the current download finishes."""
        self._stop_event.set()
        if timeout is not None and self.is_alive():
            self.join(timeout)

    # -- the thread loop ------------------------------------------------------ #
    def run(self):
        while not self._stop_event.is_set():
            try:
                self._ensure_connected()
                if self._station is not None and not self._done:
                    self._top_up()
            except Exception as e:
                self._last_error = e
                self.log.warning("youtube: loop error (continuing): %r", e)
            self._stop_event.wait(1.0)

    def run_until_done(self, timeout=120, poll=0.25):
        """Drive the same top-up logic synchronously (standalone CLI/tests).
        Returns True when a finite harvest finished, False on timeout."""
        deadline = time.time() + timeout
        while not self._stop_event.is_set() and time.time() < deadline:
            with self._lock:
                try:
                    self._ensure_connected()
                    if self._station is not None and not self._done:
                        self._top_up()
                except Exception as e:
                    self._last_error = e
            if self._station is not None and self._done:
                return True
            time.sleep(poll)
        return False

    # -- internals ------------------------------------------------------------ #
    def _ensure_connected(self):
        if self._connected:
            return True
        if self.mock:
            self._connected = True
            return True
        if not self._ytdlp_ok:
            self._connect_error = ("yt-dlp not installed (pip install yt-dlp); "
                                   "run with --mock for fabricated sources")
            self.log.warning("youtube: %s", self._connect_error)
            return False
        self._connected = True
        self._connect_error = None
        self.log.info("youtube: connected (yt-dlp, %d source(s))",
                      len(self._sources))
        return True

    def _find_source(self, spec):
        for st in self._sources:
            if st.get("sourceToken") == spec or st.get("sourceName") == spec:
                return st
        return None

    def _top_up(self):
        """Decide whether to fetch more tracks, then harvest a batch."""
        with self._lock:
            if self._station is None or self._done or self._exhausted:
                return
            if self._wanted > 0:
                # finite harvest: just fetch until we have `wanted` tracks
                cap = self._wanted - self._downloaded
                if cap <= 0:
                    self._done = True
                    self.log.info("youtube: harvest complete (%d tracks)",
                                  self._downloaded)
                    return
                need = min(self._lookahead, cap)
            else:
                # continuous radio: fetch when the queue falls below the line.
                # queued() is the injected, read-only server callback.
                pending = self._queued()
                if pending >= self._topup_when:
                    return
                need = self._lookahead
            self._harvest(need)
            if self._wanted > 0 and self._downloaded >= self._wanted:
                self._done = True
                self.log.info("youtube: harvest complete (%d tracks)",
                              self._downloaded)
            elif self._exhausted and not self._done:
                # The pool has run dry (search/playlist exhausted). A finite
                # harvest finishes here; a continuous radio winds down instead
                # of begging an empty well forever.
                self._done = True
                self.log.info("youtube: harvest complete (%d tracks, source "
                              "ran dry)", self._downloaded)

    def _harvest(self, n):
        """Fill the play queue with up to ``n`` new tracks.

        Consumes from the cached deep candidate pool (``self._candidates``),
        refilling it with one ``_list_tracks(need=self._pool_size)`` call
        whenever it's empty — so a single fresh search/playlist listing keeps
        serving many batches. ``n`` is only a hint for a single top-up visit;
        the pool refill size is the module-level ``SEARCH_POOL_SIZE``.

        The source is marked ``_exhausted`` when a *full* pool refill produces
        zero new tracks, so repeated "queue more" clicks on a dead search stop
        pulling instead of cycling the same results forever.
        """
        token = self._station["sourceToken"]
        produced = 0
        cycle_new = 0      # tracks newly written since the last pool refill
        got_pool = False   # a full refill cycle has run within this call
        while not self._stop_event.is_set():
            if not self._candidates:
                if got_pool and cycle_new == 0:
                    self._set_exhausted()
                    break
                try:
                    items = self._list_tracks(token, need=self._pool_size)
                except Exception as e:
                    self._last_error = e
                    self.log.warning("youtube: source fetch failed: %r", e)
                    break
                if not items:
                    self._set_exhausted()
                    break
                self._candidates = list(items)
                got_pool = True
                cycle_new = 0
            item = self._candidates.pop(0)
            vid = item.get("id")
            if vid and vid in self._seen:
                continue
            try:
                paths, new_paths = self._fetch_track(item)
            except Exception as e:
                self._last_error = e
                self.log.warning("youtube: track failed: %r", e)
                continue
            if vid:
                self._seen.add(vid)
            # Finite harvests only count freshly-written tracks toward the
            # target. Continuous radio re-serves everything (repeats are
            # normal; the server's queue dedupes against already-queued paths).
            to_queue = paths if self._wanted == 0 else new_paths
            if not to_queue:
                if self._wanted > 0 and paths:
                    self.log.info("youtube: already have %s (skipped)",
                                  os.path.basename(paths[0]))
                continue
            for p in to_queue:
                self._downloaded += 1
                produced += 1
                self.log.info("youtube: queued %s",
                              os.path.relpath(p, self.music_dir))
                self._submit(p)
            cycle_new += len(new_paths)
            if self._wanted > 0 and self._downloaded >= self._wanted:
                break
            if produced >= n:
                break

    def _set_exhausted(self):
        """Mark the active source dry (a full pool refill produced nothing
        new) and log it once so the user knows why the radio went quiet."""
        self._exhausted = True
        if not self._exhausted_warned:
            self._exhausted_warned = True
            self.log.info("youtube: %r has no more new tracks to offer (%d "
                          "queued) — try searching something else, or hit "
                          "'Queue more' later",
                          self._station["sourceName"], self._downloaded)

    @staticmethod
    def _iter_entries(info):
        """Yield flat video-info dicts from nested yt-dlp structures
        (search results, playlists, or a bare single-video dict)."""
        if not isinstance(info, dict):
            return
        entries = info.get("entries")
        if entries is None:
            yield info
            return
        for e in entries:
            if not e:
                continue
            if e.get("entries"):
                yield from YouTubeService._iter_entries(e)
            else:
                yield e

    def _list_tracks(self, token, need):
        """Return ``need`` candidate tracks for the active source.

        Mock mode serves a stateful cycling pool (like Pandora's radio). Real
        mode does a metadata-only (flat) yt-dlp extract of the source — a
        ``ytsearchN:`` query for plain search terms, or the playlist/channel
        itself for URLs — then filters out live streams, Shorts and videos
        longer than ``max_duration``."""
        src_query = self._station.get("query") or ""
        if self.mock:
            cursor = self._mock_cursor.get(token, 0)
            songs = MOCK_TRACKS.get(token, MOCK_TRACKS["src-lofi"])
            out = []
            for i in range(need):
                idx = cursor + i
                title, artist = songs[idx % len(songs)]
                vid = f"{token}-{idx:04d}"
                out.append({"id": vid, "title": title, "artist": artist,
                            "url": f"mock://{token}/{vid}", "duration": 180})
            self._mock_cursor[token] = cursor + need
            return out

        try:
            from yt_dlp import YoutubeDL
            url = src_query
            if "://" not in url:
                # A deep pool: request `need` results (the caller refills with
                # SEARCH_POOL_SIZE) so batches keep finding fresh tracks
                # instead of re-listing the same top few every time.
                url = f"ytsearch{max(need, 1)}:{url}"
            opts = {
                "quiet": True,
                "no_warnings": True,
                "ignoreerrors": True,
                "socket_timeout": self._download_timeout,
                "extract_flat": "in_playlist",
            }
            info = YoutubeDL(opts).extract_info(url, download=False)
        except Exception as e:
            self._last_error = e
            self.log.warning("youtube: source fetch failed: %r", e)
            return []

        candidates = []
        for e in self._iter_entries(info):
            if not e:
                continue
            vid = e.get("id")
            if not vid:
                continue
            live = e.get("is_live") or str(e.get("live_status") or "") in (
                "is_live", "is_upcoming")
            if live:
                continue
            dur = e.get("duration") or 0
            if dur and self._max_duration and dur > self._max_duration:
                continue
            if dur and 0 < dur < 30:          # skip Shorts
                continue
            candidates.append({
                "id": vid,
                "title": (e.get("title") or "").strip() or f"Video {vid}",
                "artist": (e.get("uploader") or e.get("channel")
                           or e.get("creator") or "Unknown Artist").strip(),
                "url": e.get("url")
                       or f"https://www.youtube.com/watch?v={vid}",
                "duration": dur,
            })
        return candidates

    def _fetch_track(self, item):
        """Download one candidate video into the source folder.

        Returns ``(paths, new_paths)`` where ``paths`` is every playable file
        this video produced on disk and ``new_paths`` is the subset that was
        freshly written during this call. ``paths`` may hold several files
        when a long mix/compilation is split into per-chapter songs, or be
        empty when the download was discarded (a long video with no chapters
        to split). ``new_paths`` stays empty when everything already existed
        on disk (a re-served song already grabbed)."""
        title = (item.get("title") or "").strip() or \
            f"Track {self._downloaded + 1}"
        artist = (item.get("artist") or "").strip() or "Unknown Artist"
        folder = self._station_dir
        os.makedirs(folder, exist_ok=True)
        if self.mock:
            dest = os.path.join(folder, _sanitize(f"{artist} - {title}") + ".wav")
            if os.path.exists(dest):
                return [dest], []
            freq = 200 + (zlib.crc32(dest.encode("utf-8")) % 600)
            _write_mock_wav(dest, freq)
            return [dest], [dest]

        vid = item.get("id") or ""
        url = item.get("url") or f"https://www.youtube.com/watch?v={vid}"
        try:
            from yt_dlp import YoutubeDL
            opts = {
                "format": "bestaudio/best",
                "outtmpl": os.path.join(folder, "%(id)s.%(ext)s"),
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "socket_timeout": self._download_timeout,
            }
            info = YoutubeDL(opts).extract_info(url, download=True)
        except Exception as e:
            raise RuntimeError(f"yt-dlp download failed: {e}") from e

        produced = self._dl_filepath(info, folder, vid)
        if not produced or not os.path.isfile(produced):
            raise RuntimeError("yt-dlp finished without producing a file")
        return self._post_download(produced, folder, artist, title, info)

    def _post_download(self, src, folder, artist, title, info):
        """Decide what a freshly downloaded file becomes on disk.

        * Videos with >=2 chapters and ffmpeg available are split into one
          FLAC per chapter — a 27-minute "mix" turns into its individual
          songs instead of one giant track.
        * Everything else is a single track under its friendly
          ``Artist - Title`` name (so it shows up properly in the library).
        * Long videos WITHOUT chapters are discarded: with no track
          boundaries there's nothing to split, so they'd only ever be a
          chapterless blob in the queue.

        Returns ``(paths, new_paths)`` — the subset of ``paths`` newly
        written this call. ``paths`` is empty when the download was dropped.
        """
        folder = os.path.abspath(folder)
        os.makedirs(folder, exist_ok=True)
        dur = info.get("duration") or 0
        chapters = [c for c in (info.get("chapters") or [])
                    if c.get("start_time") is not None]
        if len(chapters) >= 2 and self._ffmpeg_path:
            paths, news = self._split_by_chapters(src, chapters, dur,
                                                  folder, artist)
            if paths:
                return paths, news
            # nothing usable came out of the chapters — fall through rather
            # than queue a raw blob
        if dur and self._single_limit and dur > self._single_limit:
            self.log.info("youtube: skipping %s — %.0f min and nothing to "
                          "split into songs", os.path.basename(src), dur / 60)
            self._cleanup_orphan(src)
            return [], []
        # Single track: give it the friendly Artist - Title name first.
        ext = os.path.splitext(src)[1].lower()
        dest = os.path.join(folder, _sanitize(f"{artist} - {title}") + ext)
        if dest != src:
            if os.path.exists(dest):
                try:
                    os.unlink(src)
                except OSError:
                    pass
                playable, _ = self._transcode_for_playback(dest)
                return [playable], []
            try:
                os.replace(src, dest)
            except OSError:
                dest = src
        playable, _ = self._transcode_for_playback(dest)
        return [playable], [playable]

    def _split_by_chapters(self, src, chapters, dur, folder, artist):
        """Split a mix/compilation audio file into one FLAC per chapter.

        Chapters shorter than ``_min_song_s`` are dropped (intro/outro noise,
        "thanks for watching" stubs). Files that already exist from an earlier
        split are returned in ``paths`` but not ``new_paths``. The original
        blob is deleted once at least one chapter came out.

        Returns ``(paths, new_paths)``, both empty when nothing usable."""
        outs, news = [], []
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError:
            pass
        for i, ch in enumerate(chapters):
            start = max(0.0, float(ch.get("start_time") or 0))
            if i + 1 < len(chapters):
                end = float(chapters[i + 1].get("start_time") or dur or start)
            else:
                end = float(ch.get("end_time") or dur or start)
            if end - start < self._min_song_s:
                continue
            ch_title = _sanitize(str(ch.get("title") or ""))\
                .strip() or f"Part {i + 1}"
            dest = os.path.join(folder,
                                _sanitize(f"{artist} - {ch_title}") + ".flac")
            if os.path.exists(dest):
                outs.append(dest)
                continue
            if self._run_ffmpeg_segment(src, start, end, dest,
                                        ch_title, artist):
                outs.append(dest)
                news.append(dest)
        if not outs:
            return [], []
        self._cleanup_orphan(src)
        return outs, news

    def _run_ffmpeg_segment(self, src, start, end, dest, title, artist):
        """Cut [start, end) out of src and encode it as a FLAC. Returns True
        on success (like ``_run_ffmpeg`` but for one chapter of a split)."""
        if not self._ffmpeg_path:
            return False
        try:
            subprocess.run(
                [self._ffmpeg_path, "-y", "-v", "error",
                 "-ss", f"{start:.2f}", "-to", f"{end:.2f}", "-i", src,
                 "-c:a", "flac",
                 "-metadata", f"title={title}",
                 "-metadata", f"artist={artist}",
                 dest],
                check=True, timeout=max(120, self._download_timeout * 4))
            self.log.info("youtube: split %s -> %s",
                          os.path.basename(src), os.path.basename(dest))
            return True
        except subprocess.CalledProcessError:
            self.log.warning("youtube: ffmpeg chapter split failed on %s",
                             os.path.basename(src))
            self._cleanup_orphan(dest)
            return False
        except subprocess.TimeoutExpired:
            self.log.warning("youtube: ffmpeg chapter split timed out on %s",
                             os.path.basename(src))
            self._cleanup_orphan(dest)
            return False

    @staticmethod
    def _dl_filepath(info, folder, vid):
        """Best-effort path of the file yt-dlp just wrote, by checking the
        info dict first and falling back to a glob on ``folder/<vid>.*``."""
        if isinstance(info, dict):
            rd = info.get("requested_downloads")
            if rd and isinstance(rd[0], dict) and os.path.isfile(rd[0].get("filepath", "")):
                return rd[0]["filepath"]
            if os.path.isfile(info.get("filepath", "")):
                return info["filepath"]
        if vid:
            import glob
            matches = [m for m in glob.glob(os.path.join(folder, vid + ".*"))
                       if not m.endswith(".part")]
            if matches:
                return matches[0]
        return ""

    # -- Decoding: webm/opus/m4a downloads need transcoding for the server ---- #
    def _transcode_for_playback(self, src):
        """Return ``(path, produced)`` for a downloaded file.

        Files the server can already decode (wav/flac/mp3/ogg) pass through
        untouched. yt-dlp's webm (opus) and m4a (aac) go through ffmpeg to
        FLAC once; ``produced`` is True only when a brand-new FLAC was
        created, so the caller can distinguish "freshly downloaded" from
        "seen before". When ffmpeg is unavailable the original file is kept
        and the caller is told nothing new was produced (status() carries the
        warning)."""
        if os.path.splitext(src)[1].lower() in _PLAYABLE_EXTS:
            return src, False
        dest = os.path.splitext(src)[0] + ".flac"
        if os.path.exists(dest):
            return dest, False
        if self._run_ffmpeg(src, dest):
            return dest, True
        return src, False

    def _run_ffmpeg(self, src, dest):
        """Transcode src -> dest with ffmpeg. Returns True on success."""
        if not self._ffmpeg_path:
            if not self._transcode_warned:
                self._transcode_warned = True
                self.log.warning(
                    "youtube: ffmpeg not found — webm/m4a downloads can't be "
                    "played; install it (e.g. sudo apt install ffmpeg)")
            return False
        try:
            subprocess.run(
                [self._ffmpeg_path, "-y", "-v", "error", "-i", src,
                 "-c:a", "flac", dest],
                check=True, timeout=max(120, self._download_timeout * 4))
            try:
                os.unlink(src)
            except OSError:
                pass
            self.log.info("youtube: transcoded %s", os.path.basename(dest))
            return True
        except subprocess.CalledProcessError:
            self.log.warning("youtube: ffmpeg failed on %s",
                             os.path.basename(src))
            self._cleanup_orphan(dest)
            return False
        except subprocess.TimeoutExpired:
            self.log.warning("youtube: ffmpeg timed out on %s",
                             os.path.basename(src))
            self._cleanup_orphan(dest)
            return False

    @staticmethod
    def _cleanup_orphan(dest):
        """Remove a half-written transcode target, ignoring stragglers."""
        try:
            if os.path.exists(dest):
                os.unlink(dest)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Standalone CLI                                                              #
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--music-dir", default="music",
                    help="where downloads land (default: ./music)")
    ap.add_argument("--mock", action="store_true",
                    help="use mock sources/tracks (no yt-dlp/network needed)")
    ap.add_argument("--list-sources", action="store_true",
                    help="print sources and exit")
    ap.add_argument("--harvest", nargs=2, metavar=("SOURCE", "N"),
                    help="download N tracks from SOURCE (name or token), then exit")
    ap.add_argument("--serve", metavar="SOURCE",
                    help="continuous radio: keep the queue topped up until "
                         "Ctrl-C or --duration expires")
    ap.add_argument("--duration", type=float, default=0,
                    help="for --serve: exit after this many seconds (0 = forever)")
    ap.add_argument("--lookahead", type=int, default=4,
                    help="batch size to keep `N` tracks (default: 4)")
    ap.add_argument("--topup", type=int, default=1,
                    help="refetch when fewer than N queued (default: 1)")
    args = ap.parse_args()

    os.makedirs(args.music_dir, exist_ok=True)
    C.setup_logging(logging.INFO)
    svc = YouTubeService(
        args.music_dir, mock=args.mock, lookahead=args.lookahead,
        topup_when=args.topup)

    if not svc.connect_now():
        print("youtube: cannot connect:")
        print("  " + (svc.status().get("connect_error") or "unknown error"))
        raise SystemExit(1)

    if args.list_sources:
        for st in svc.sources():
            print(f"  {st['sourceToken']:<14} {st['sourceName']:<18} {st['query']}")
        return

    if args.harvest:
        source, n = args.harvest
        if not svc.play_source(source, want=int(n)):
            print(f"youtube: unknown source: {source}")
            raise SystemExit(1)
        if svc.run_until_done(timeout=120):
            print(f"youtube: downloaded {svc.status()['downloaded']} tracks into "
                  f"{svc.status()['station_dir']}")
        else:
            print("youtube: harvest timed out")
            raise SystemExit(1)
        return

    if args.serve:
        if not svc.play_source(args.serve):
            print(f"youtube: unknown source: {args.serve}")
            raise SystemExit(1)
        svc.start()
        print(f"youtube: playing {args.serve} into {args.music_dir} "
              f"(queue target: {args.lookahead}, refill below {args.topup})")
        try:
            if args.duration > 0:
                time.sleep(args.duration)
            else:
                while True:
                    time.sleep(1)
        except KeyboardInterrupt:
            pass
        svc.stop(timeout=10)
        print(f"youtube: stopped after {svc.status()['downloaded']} tracks")
        return

    ap.print_help()


if __name__ == "__main__":
    main()