"""
msync_pandora.py - Pandora radio for msync, as a totally separate daemon thread.

PandoraService owns every Pandora-facing concern:

  * the PandoraAPI session (auth + reconnect)
  * the station list
  * fetching station playlists
  * downloading each track's (short-lived) stream URL into the music folder
    and transcoding Pandora's AAC streams to FLAC with ffmpeg (if installed)
    so the server's miniaudio decoder can play them
  * a lookahead loop that keeps the server's play queue topped up

It shares NOTHING with the server's audio/sync/catalog threads. The only
coupling is two callbacks the server injects:

  * ``queued()``     -> int, how many of THIS station's tracks are still
                        sitting in the server's play queue (read-only)
  * ``submit(path)`` -> called from the Pandora thread whenever a download
                        has finished and is ready to be queued

``queued()`` must be cheap and must not block on Pandora; ``submit()`` is
expected to hand the path to the server's own queue under the server's lock.
Neither callback may call back into PandoraService (no re-entrancy).

with no credentials (or ``mock=True``) the API serves fabricated stations and
the downloader writes tiny generated WAV files, so the whole thread, lookahead
and queue pipeline runs end-to-end without a Pandora account.

The server drives it in BATCH mode: starting a station (or the Pandora tab's
"Queue more" button) queues ``batch`` tracks and then stops — a finite
harvest, never an auto-refilling radio. The CLI's ``--serve`` still runs the
old continuous top-up mode for standalone listening.

CLI (standalone, no server needed):

    python3 msync_pandora.py --mock --list-stations
    python3 msync_pandora.py --mock --harvest "Lite Pop" 6
    python3 msync_pandora.py --mock --serve "Deep Cuts Rock" --duration 30

    # or against a real account:
    MSYNC_PANDORA_USERNAME=you@x MSYNC_PANDORA_PASSWORD=... python3 msync_pandora.py --list-stations
"""

import argparse
import array
import logging
import math
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import wave
import zlib

from pandora_api import PandoraAPI
import msync_common as C

# Downloaded Pandora tracks land in a per-station "album" folder so they show
# up in the library/album UI:  <music_dir>/Pandora - <Station>/Artist - Title.ext
STATION_FOLDER_PREFIX = "Pandora - "

# Formats the server's decoder (miniaudio) can play natively. Pandora only
# serves AAC (m4a), which miniaudio cannot decode, so those get transcoded to
# FLAC with ffmpeg (if present) the moment they're downloaded.
_PLAYABLE_EXTS = frozenset({".wav", ".flac", ".mp3", ".ogg"})


def _sanitize(name):
    """Filename-safe version of a song/station name."""
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name or "untitled"


def _sniff_ext(content_type, head, url):
    """Best-effort audio extension from the response headers + magic bytes."""
    ctype = (content_type or "").split(";")[0].strip().lower()
    if "mpeg" in ctype or ctype == "audio/mp3":
        return ".mp3"
    if "mp4" in ctype or "aac" in ctype or "m4a" in ctype:
        return ".m4a"
    if ctype.startswith("audio/"):
        sub = ctype.split("/", 1)[1].strip()
        if sub in ("ogg", "opus", "wav", "flac"):
            return "." + sub
    if head.startswith(b"ID3") or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return ".mp3"
    if head[4:8] == b"ftyp":
        return ".m4a"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return ".wav"
    if head[:4] == b"OggS":
        return ".ogg"
    base, ext = os.path.splitext(urllib.parse.urlparse(url).path)
    if ext.lower() in (".mp3", ".m4a", ".aac", ".ogg", ".wav", ".flac", ".opus"):
        return ext.lower()
    return ".mp3"


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


class PandoraService(threading.Thread):
    """Fully independent Pandora radio thread.

    Start it with ``svc.start()`` (like the server's other background
    threads), or drive it synchronously with ``run_until_done()`` for
    finite harvests in the CLI. Pick one mode; don't call both.

    Callbacks (all called from the Pandora thread):
      queued()     -> int   remaining tracks of this station in the server queue
      submit(path) -> None  a finished download to append to the server queue
    """

    def __init__(self, music_dir, username=None, password=None, *,
                 mock=False, lookahead=4, topup_when=1, batch=10,
                 download_timeout=30, queued=None, submit=None, log=None):
        super().__init__(name="pandora", daemon=True)
        self.music_dir = os.path.abspath(music_dir)
        self.mock = bool(mock)
        self.api = PandoraAPI(mock=self.mock)
        self._username = username or ""
        self._password = password or ""
        self._lookahead = max(1, int(lookahead))
        self._topup_when = max(1, int(topup_when))
        self._batch = max(1, int(batch))        # songs per "queue more" batch
        self._download_timeout = download_timeout
        self._queued = queued if callable(queued) else self._local_queued
        self._submit = submit if callable(submit) else self._local_submit
        self.log = log or C.logger()

        self._lock = threading.RLock()
        self._stop_event = threading.Event()

        self._connected = False
        self._connect_error = None
        self._stations = []
        self._stations_loaded = False
        self._station = None            # {"stationName", "stationToken"}
        self._station_dir = None        # absolute album folder for downloads
        self._seen = set()              # trackTokens downloaded this session
        self._wanted = 0                # 0 = continuous radio, >0 = finite harvest
        self._downloaded = 0
        self._done = False
        self._last_error = None

        # ffmpeg availability for AAC->FLAC transcoding (detected once).
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
        """Block until connected. Returns True on success."""
        with self._lock:
            self._ensure_connected()
        return self._connected

    def stations(self):
        """Return the (cached) station list, connecting + refreshing if needed."""
        with self._lock:
            self._ensure_connected()
            self._load_stations()
            return list(self._stations)

    def play_station(self, spec, want=0):
        """Start (or switch) the station. ``spec`` is a station name or token.

        ``want=0``  -> continuous radio: keep the queue topped up until
                       stop_station()/stop().
        ``want>0``  -> finite harvest: download exactly ``want`` tracks, then
                       pause (for the CLI / tests).
        """
        with self._lock:
            self._ensure_connected()
            st = self._find_station(spec)
            if not st:
                return False
            self._station = st
            self._station_dir = os.path.join(
                self.music_dir, STATION_FOLDER_PREFIX + _sanitize(st["stationName"]))
            self._wanted = max(0, int(want))
            self._done = False
            self._seen.clear()
            self._downloaded = 0
            self._last_error = None
            mode = "continuous" if want == 0 else f"harvest {int(want)}"
            self.log.info("pandora: station -> %s (%s)",
                          st["stationName"], mode)
            return True

    def fetch_more(self, count=None):
        """Queue another batch for the active station (web tab's "Queue more").

        Non-blocking: raises the finite-harvest target by ``count`` (default
        ``self._batch``) and lets the thread loop keep downloading/queuing
        until that many tracks are queued. Returns False when no station is
        active (nothing to fetch more of)."""
        with self._lock:
            if self._station is None:
                return False
            n = max(1, int(count or self._batch))
            # If we were in continuous mode (want=0), switch to finite batches.
            self._wanted = (self._wanted or self._downloaded) + n
            self._done = False
            self.log.info("pandora: queueing %d more (%s)",
                          n, self._station["stationName"])
            return True

    def stop_station(self):
        """Stop fetching (already-queued tracks keep playing)."""
        with self._lock:
            name = self._station["stationName"] if self._station else None
            self._station = None
            self._done = True
            if name:
                self.log.info("pandora: stopped station %s", name)

    def status(self):
        """Snapshot for the server / HTTP API / tests."""
        with self._lock:
            warn = None
            if not self.mock and not self._ffmpeg_path:
                warn = ("ffmpeg not found: Pandora's AAC downloads can't be "
                        "decoded. Install it (e.g. sudo apt install ffmpeg) "
                        "to make the radio playable.")
            return {
                "connected": self._connected,
                "mock": self.mock,
                "connect_error": self._connect_error,
                "station": self._station.get("stationName") if self._station else None,
                "station_token": self._station.get("stationToken") if self._station else None,
                "station_dir": self._station_dir,
                "downloaded": self._downloaded,
                "pending": self._queued() if self._station else 0,
                "done": self._done,
                "stations": len(self._stations),
                "batch": self._batch,
                "error": str(self._last_error) if self._last_error else None,
                "ffmpeg": self._ffmpeg_path is not None,
                "warn": warn,
            }

    @property
    def station_dir(self):
        """Absolute folder the active station's downloads land in (None idle)."""
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
                self._load_stations()
            except Exception as e:
                self._last_error = e
                self.log.warning("pandora: loop error (continuing): %r", e)
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
            ok = self.api.connect("mock", "mock")
        else:
            if not (self._username and self._password):
                self._connect_error = ("no credentials (set MSYNC_PANDORA_* or "
                                       "pass username/password; use --mock for "
                                       "a fake account)")
                self.log.warning("pandora: %s", self._connect_error)
                return False
            ok = self.api.connect(self._username, self._password)
        self._connected = ok
        self._connect_error = None if ok else "login failed (tuner.pandora.com)"
        if ok:
            self.log.info("pandora: connected (%s)",
                          "mock" if self.mock else "account")
        else:
            self.log.warning("pandora: %s", self._connect_error)
        return ok

    def _load_stations(self, force=False):
        if not self._connected:
            return []
        if self._stations_loaded and not force:
            return self._stations
        try:
            st = self.api.get_stations()
            if st:
                self._stations = st
                self._stations_loaded = True
        except Exception as e:
            self._last_error = e
        return self._stations

    def _find_station(self, spec):
        for st in self._load_stations():
            if st.get("stationToken") == spec or st.get("stationName") == spec:
                return st
        return None

    def _top_up(self):
        """Decide whether to fetch more tracks, then harvest a batch."""
        with self._lock:
            if self._station is None or self._done:
                return
            if self._wanted > 0:
                # finite harvest: just fetch until we have `wanted` tracks
                cap = self._wanted - self._downloaded
                if cap <= 0:
                    self._done = True
                    self.log.info("pandora: harvest complete (%d tracks)",
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

    def _harvest(self, n):
        token = self._station["stationToken"]
        try:
            items = self.api.get_playlist(token, count=n)
        except Exception as e:
            self._last_error = e
            self.log.warning("pandora: playlist fetch failed: %r", e)
            return
        new_in_batch = 0
        for item in items:
            if self._stop_event.is_set():
                return
            tok = item.get("trackToken")
            if tok and tok in self._seen:
                continue
            try:
                path, wrote_new = self._fetch_track(item)
            except Exception as e:
                self._last_error = e
                self.log.warning("pandora: track failed: %r", e)
                continue
            if not path:
                continue
            if tok:
                self._seen.add(tok)
            if wrote_new:
                self._downloaded += 1
                new_in_batch += 1
            else:
                # Duplicate of a file already on disk. In continuous mode we
                # still re-submit it (radio repeats are normal; the server's
                # queue dedupes against paths already pending). In a finite
                # harvest, duplicates don't count toward the target.
                if self._wanted > 0:
                    self.log.info("pandora: already have %s (skipped)",
                                  os.path.basename(path))
                    continue
            self.log.info("pandora: queued %s",
                          os.path.relpath(path, self.music_dir))
            self._submit(path)
            if self._wanted > 0 and self._downloaded >= self._wanted:
                break
        # Finite harvest that runs out of NEW songs to fetch is complete.
        if self._wanted > 0 and new_in_batch == 0:
            self._done = True
            self.log.info("pandora: harvest complete (%d tracks, nothing new "
                          "left to fetch)", self._downloaded)

    @staticmethod
    def _audio_url(item):
        amap = item.get("audioUrlMap") or {}
        hq = amap.get("highQuality") or {}
        if hq.get("audioUrl"):
            return hq["audioUrl"]
        if amap.get("mediumQuality", {}).get("audioUrl"):
            return amap["mediumQuality"]["audioUrl"]
        return item.get("audioUrl") or ""

    def _fetch_track(self, item):
        """Download one track into the station folder.

        Returns ``(path, wrote_new)`` — ``wrote_new`` is False when the file
        already existed on disk (a re-served song we've already grabbed)."""
        title = (item.get("songName") or "").strip() or f"Track {self._downloaded + 1}"
        artist = (item.get("artistName") or "").strip() or "Unknown Artist"
        folder = self._station_dir
        os.makedirs(folder, exist_ok=True)
        if self.mock:
            dest = os.path.join(folder, _sanitize(f"{artist} - {title}") + ".wav")
            if os.path.exists(dest):
                return dest, False
            freq = 200 + (zlib.crc32(dest.encode("utf-8")) % 600)
            _write_mock_wav(dest, freq)
            return dest, True

        url = self._audio_url(item)
        if not url:
            raise RuntimeError("track has no audioUrl")
        base = os.path.splitext(_sanitize(f"{artist} - {title}"))[0]
        tmp = os.path.join(folder, base + ".part")
        req = urllib.request.Request(
            url, headers={"User-Agent": self.api.user_agent})
        try:
            with urllib.request.urlopen(req, timeout=self._download_timeout) as r:
                ctype = r.headers.get("Content-Type") or ""
                head = r.read(4096)
                ext = _sniff_ext(ctype, head, url)
                dest = os.path.join(folder, base + ext)
                if os.path.exists(dest):
                    # Previously downloaded. AAC files grabbed before the
                    # transcode step (or by an older build) get converted
                    # now; `produced` only turns true when we make a new
                    # playable file, so dedupe/queue semantics stay intact.
                    playable, produced = self._transcode_for_playback(dest)
                    return playable, produced
                with open(tmp, "wb") as f:
                    f.write(head)
                    shutil.copyfileobj(r, f, length=65536)
            os.replace(tmp, dest)
            playable, _ = self._transcode_for_playback(dest)
            return playable, True
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    # -- AAC decoding: Pandora streams need transcoding for the server ---------- #
    def _transcode_for_playback(self, src):
        """Return ``(path, produced)`` for a downloaded file.

        Files the server can already decode (wav/flac/mp3/ogg) pass through
        untouched. Pandora's AAC (m4a) goes through ffmpeg to FLAC once;
        ``produced`` is True only when a brand-new FLAC was created, so the
        caller can distinguish "freshly downloaded" from "seen before".
        When ffmpeg is unavailable the original file is kept and the caller
        is told nothing new was produced (status() carries the warning).

        The FLAC is written next to the source file (same directory), never
        to the process working directory."""
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
                    "pandora: ffmpeg not found — AAC downloads can't be "
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
            self.log.info("pandora: transcoded %s", os.path.basename(dest))
            return True
        except subprocess.CalledProcessError:
            self.log.warning("pandora: ffmpeg failed on %s",
                             os.path.basename(src))
            self._cleanup_orphan(dest)
            return False
        except subprocess.TimeoutExpired:
            self.log.warning("pandora: ffmpeg timed out on %s",
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
                    help="use mock stations/tracks (no account needed)")
    ap.add_argument("--username", default=os.environ.get("MSYNC_PANDORA_USERNAME", ""),
                    help="Pandora email (or MSYNC_PANDORA_USERNAME)")
    ap.add_argument("--password", default=os.environ.get("MSYNC_PANDORA_PASSWORD", ""),
                    help="Pandora password (or MSYNC_PANDORA_PASSWORD)")
    ap.add_argument("--list-stations", action="store_true",
                    help="print stations and exit")
    ap.add_argument("--harvest", nargs=2, metavar=("STATION", "N"),
                    help="download N tracks from STATION (name or token), then exit")
    ap.add_argument("--serve", metavar="STATION",
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
    svc = PandoraService(
        args.music_dir, username=args.username, password=args.password,
        mock=args.mock, lookahead=args.lookahead, topup_when=args.topup)

    if not svc.connect_now():
        print("pandora: cannot connect")
        raise SystemExit(1)

    if args.list_stations:
        for st in svc.stations():
            print(f"  {st['stationToken']:<16} {st['stationName']}")
        return

    if args.harvest:
        station, n = args.harvest
        if not svc.play_station(station, want=int(n)):
            print(f"pandora: unknown station: {station}")
            raise SystemExit(1)
        if svc.run_until_done(timeout=120):
            print(f"pandora: downloaded {svc.status()['downloaded']} tracks into "
                  f"{svc.status()['station_dir']}")
        else:
            print("pandora: harvest timed out")
            raise SystemExit(1)
        return

    if args.serve:
        if not svc.play_station(args.serve):
            print(f"pandora: unknown station: {args.serve}")
            raise SystemExit(1)
        svc.start()
        print(f"pandora: playing {args.serve} into {args.music_dir} "
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
        print(f"pandora: stopped after {svc.status()['downloaded']} tracks")
        return

    ap.print_help()


if __name__ == "__main__":
    main()