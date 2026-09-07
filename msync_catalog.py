"""
msync_catalog.py - SQLite-backed catalog of the server's music library.

The server walks its music directory and records every audio file in a
SQLite database (location from config.DB_PATH). Albums are folders: any
sub-folder of the music directory is an album whose tracks are the audio
files inside it. Files directly in the music directory are "singles".

Queries power the library UI (albums + tracks), the queue (queue whole
albums or individual tracks), and client track-selection.
"""

import json
import os
import re
import sqlite3
import threading
import time

import miniaudio
from tinytag import TinyTag

import msync_common as C

HIDDEN_DIRS = (".queue",)      # drop-to-queue mailbox is not a music album

AUDIO_EXTS = (".mp3", ".ogg", ".wav", ".flac", ".m4a", ".aac", ".opus", ".wma")


def is_audio_name(name):
    return os.path.splitext(name)[1].lower() in AUDIO_EXTS


def natural_key(s):
    """Case-insensitive natural sort key: 'Song 2' < 'Song 10'."""
    parts = re.split(r"(\d+)", s)
    return [int(p) if p.isdigit() else p.lower() for p in parts]


def _clean_tag(value):
    """Stripped tag string, or None when missing or blank."""
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _clean_track(value):
    """Positive integer track number, or None when missing/illegible."""
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def track_sort_key(relpath, track):
    """Sort key for (relpath, track): tagged track numbers sort first and in
    numeric order; untagged files fall back to natural filename order."""
    return (track if track is not None else 1 << 30,
            natural_key(os.path.basename(relpath)))


def relpath_norm(music_dir, path):
    """Relative path with '/' separators (safe for URLs)."""
    return os.path.relpath(path, music_dir).replace(os.sep, "/")


def abs_path(music_dir, relpath):
    """Turn a catalog relpath back into a filesystem path."""
    return os.path.join(music_dir, *relpath.split("/"))


class Catalog:
    def __init__(self, music_dir, db_path):
        self.music_dir = music_dir
        self.db_path = db_path
        self.lock = threading.RLock()
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # check_same_thread=False allows the catalog to be used from
        # multiple threads (server, inotify watcher, HTTP handler, etc.)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._ensure_schema()

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    def _ensure_schema(self):
        with self._conn:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS tracks (
                    relpath     TEXT PRIMARY KEY,   -- 'Album/01 Song.mp3'
                    name        TEXT NOT NULL,      -- '01 Song.mp3'
                    album       TEXT,               -- tagged album, or folder
                    artist      TEXT DEFAULT '',    -- tagged artist
                    title       TEXT,               -- tagged title, or '01 Song.mp3'
                    track       INTEGER,            -- tagged track number
                    duration    REAL DEFAULT 0,
                    sample_rate INTEGER DEFAULT 0,
                    channels    INTEGER DEFAULT 0,
                    size        INTEGER DEFAULT 0,
                    mtime       REAL DEFAULT 0,
                    added_at    REAL DEFAULT 0
                )""")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tracks_album ON tracks(album)")
            # Migrate databases created before tag support: older rows have an
            # empty title, which scan() treats as needing a metadata re-read.
            cols = {r[1] for r in
                    self._conn.execute("PRAGMA table_info(tracks)")}
            if "title" not in cols:
                self._conn.execute(
                    "ALTER TABLE tracks ADD COLUMN title TEXT")
            if "track" not in cols:
                self._conn.execute(
                    "ALTER TABLE tracks ADD COLUMN track INTEGER")
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS queue (
                    position INTEGER PRIMARY KEY,
                    entry    TEXT NOT NULL   -- JSON: {"type":"song"|"album",...}
                )""")
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS playback (
                    id      INTEGER PRIMARY KEY CHECK (id = 0),
                    relpath TEXT NOT NULL,
                    elapsed REAL NOT NULL DEFAULT 0,
                    playing INTEGER NOT NULL DEFAULT 1
                )""")
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS clients (
                    ip          TEXT PRIMARY KEY,   -- 10.0.0.4
                    hostname    TEXT NOT NULL DEFAULT '',
                    latency_ms  REAL NOT NULL DEFAULT 0,  -- output offset
                    err_ms      REAL NOT NULL DEFAULT 0,  -- sync error reported
                    last_seen   REAL NOT NULL DEFAULT 0
                )""")
            # Migrate databases created before per-client sync-error reporting.
            cols = {r[1] for r in
                    self._conn.execute("PRAGMA table_info(clients)")}
            if "err_ms" not in cols:
                self._conn.execute(
                    "ALTER TABLE clients ADD COLUMN err_ms REAL NOT NULL "
                    "DEFAULT 0")

    # ------------------------------------------------------------------ #
    # Clients (rooms)                                                     #
    # ------------------------------------------------------------------ #
    def upsert_client(self, ip, hostname, last_seen=None, err_ms=None):
        """Record a client heartbeat (hostname is best-effort - a client may
        not send one), keeping any latency setting already stored. err_ms is
        the room's just-reported playback sync error; None (e.g. plain NTP
        heartbeats) keeps the last reported value."""
        at = last_seen if last_seen is not None else time.time()
        with self._conn:
            if err_ms is None:
                self._conn.execute("""
                    INSERT INTO clients (ip, hostname, latency_ms, last_seen)
                    VALUES (?, ?, 0, ?)
                    ON CONFLICT(ip) DO UPDATE SET
                        last_seen = excluded.last_seen
                    """, (ip, hostname, at))
            else:
                self._conn.execute("""
                    INSERT INTO clients (ip, hostname, latency_ms, err_ms,
                                         last_seen)
                    VALUES (?, ?, 0, ?, ?)
                    ON CONFLICT(ip) DO UPDATE SET
                        last_seen = excluded.last_seen,
                        err_ms    = excluded.err_ms
                    """, (ip, hostname, err_ms, at))
            if hostname:
                self._conn.execute("""
                    UPDATE clients SET hostname = ? WHERE ip = ?
                    """, (hostname, ip))

    def client_latency(self, ip):
        """Stored latency offset (ms) for a client, 0 if unknown."""
        row = self._conn.execute(
            "SELECT latency_ms FROM clients WHERE ip = ?", (ip,)).fetchone()
        return float(row[0]) if row else 0.0

    def set_client_latency(self, ip, ms):
        with self._conn:
            self._conn.execute("""
                INSERT INTO clients (ip, hostname, latency_ms, last_seen)
                VALUES (?, '', ?, ?)
                ON CONFLICT(ip) DO UPDATE SET latency_ms = excluded.latency_ms
                """, (ip, ms, time.time()))

    def list_clients(self):
        """Every known client: ip, hostname, latency_ms, err_ms, last_seen."""
        rows = self._conn.execute("""
            SELECT ip, hostname, latency_ms, err_ms, last_seen FROM clients
            ORDER BY hostname, ip""").fetchall()
        return [{"ip": r[0], "hostname": r[1], "latency_ms": r[2],
                 "err_ms": r[3], "last_seen": r[4]} for r in rows]

    def prune_clients(self, stale_after):
        """Delete rooms that haven't been seen for ``stale_after`` seconds
        (their heartbeat/register timestamps in ``last_seen`` are older than
        that) so dead rooms leave the catalog DB — and the Configure tab."""
        cutoff = time.time() - stale_after
        with self._conn:
            self._conn.execute(
                "DELETE FROM clients WHERE last_seen < ?", (cutoff,))

    def _album_of(self, relpath):
        parts = relpath.split("/")
        return parts[0] if len(parts) > 1 else None

    # ------------------------------------------------------------------ #
    # Scanning                                                            #
    # ------------------------------------------------------------------ #
    def _walk_files(self):
        rows = []
        for dirpath, dirnames, filenames in os.walk(self.music_dir):
            dirnames[:] = [d for d in dirnames
                           if d not in HIDDEN_DIRS and not d.startswith(".")]
            for fn in filenames:
                if not is_audio_name(fn):
                    continue
                path = os.path.join(dirpath, fn)
                rp = relpath_norm(self.music_dir, path)
                st = os.stat(path)
                rows.append((rp, fn, st.st_mtime, st.st_size, path))
        return rows

    def quick_paths(self):
        """Fast filesystem walk returning every audio file's absolute path in
        playback order, WITHOUT reading audio metadata or touching the DB.

        This lets the server start playing immediately; the catalog DB is
        populated in the background by scan(). Order is close to abs_paths():
        singles first (natural-sorted), then albums by folder name/track
        filename. Tag-based album names and track-number ordering arrive when
        the background scan finishes and the playlist is refreshed."""
        singles, albums = [], {}
        for rp, fn, mtime, size, path in self._walk_files():
            album = self._album_of(rp)
            if album is None:
                singles.append((rp, path))
            else:
                albums.setdefault(album, []).append((rp, path))
        singles.sort(key=lambda t: natural_key(os.path.basename(t[0])))
        ordered = [p for _, p in singles]
        for album in sorted(albums, key=natural_key):
            tracks = albums[album]
            tracks.sort(key=lambda t: natural_key(os.path.basename(t[0])))
            ordered.extend(p for _, p in tracks)
        return ordered

    def _read_meta(self, path):
        """Audio info + tags for one file. Audio metrics (duration, sample
        rate, channels) come from miniaudio; tag fields (album/artist/title/
        track) come from tinytag and are None when absent. tinytag doesn't
        cover WMA (and most WAVs carry no tags), so those files fall back to
        their folder/filename names."""
        meta = {"duration": 0.0, "sample_rate": 0, "channels": 0,
                "album": None, "artist": None, "title": None, "track": None}
        try:
            info = miniaudio.get_file_info(path)
            sr = info.sample_rate
            meta["duration"] = (info.num_frames / sr) if sr else 0.0
            meta["sample_rate"] = sr
            meta["channels"] = info.nchannels
        except Exception:
            pass
        try:
            tag = TinyTag.get(path)
        except Exception:
            tag = None
        if tag is not None:
            meta["album"] = _clean_tag(getattr(tag, "album", None))
            meta["artist"] = _clean_tag(getattr(tag, "artist", None))
            meta["title"] = _clean_tag(getattr(tag, "title", None))
            meta["track"] = _clean_track(getattr(tag, "track", None))
        return meta

    def scan(self):
        """Refresh the catalog from disk; only changed files are re-read.
        Returns the number of rows added/updated/removed."""
        now = time.time()
        lg = C.logger()
        seen = {}
        for rp, fn, mtime, size, path in self._walk_files():
            seen[rp] = (fn, mtime, size, path)
        total = len(seen)

        with self.lock:
            stale = {}
            for rp, fn, mtime, size, album, added_at, title in self._conn.execute(
                    "SELECT relpath, name, mtime, size, album, added_at, title "
                    "FROM tracks"):
                # relpath is the dict key; the stored tuple is (name, mtime,
                # size, album, added_at, title).
                stale[rp] = (fn, mtime, size, album, added_at, title)

            upserts, missing = [], []
            to_read = []
            for rp, (fn, mtime, size, path) in seen.items():
                old = stale.get(rp)
                # Unchanged files are skipped — unless they came from a
                # pre-tag database (NULL title), which forces one metadata
                # re-read so tag names populate the whole library.
                if (old and old[0] == fn and old[1] == mtime
                        and old[2] == size and old[5] is not None):
                    continue                     # unchanged (already tagged)
                to_read.append((rp, fn, mtime, size, path))
            missing = [rp for rp in stale if rp not in seen]

            # The first scan (or a new/changed file) needs a metadata read per
            # file, which is the slow part on a big library — show progress.
            nread = len(to_read)
            if nread:
                lg.info("catalog: reading metadata for %d new/changed "
                        "track(s) (of %d total)", nread, total)
            for i, (rp, fn, mtime, size, path) in enumerate(to_read, 1):
                meta = self._read_meta(path)
                upserts.append((
                    rp, fn,
                    meta["album"] or self._album_of(rp),  # tag, else folder
                    meta["artist"] or "",
                    meta["title"] or fn,                  # tag, else filename
                    meta["track"],
                    meta["duration"], meta["sample_rate"], meta["channels"],
                    size, mtime, now))
                if nread > 100 and (i % 100 == 0 or i == nread):
                    lg.info("catalog: metadata %d/%d", i, nread)

            with self._conn:
                if upserts:
                    self._conn.executemany("""
                        INSERT OR REPLACE INTO tracks
                        (relpath, name, album, artist, title, track, duration,
                         sample_rate, channels, size, mtime, added_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", upserts)
                if missing:
                    self._conn.executemany(
                        "DELETE FROM tracks WHERE relpath=?",
                        [(m,) for m in missing])
        return len(upserts) + len(missing)

    # ------------------------------------------------------------------ #
    # Queries                                                             #
    # ------------------------------------------------------------------ #
    def track(self, relpath):
        """Row for a single track (dict) or None."""
        with self.lock:
            row = self._conn.execute(
                "SELECT relpath, name, album, artist, title, track, duration,"
                " sample_rate, channels, size, mtime, added_at"
                " FROM tracks WHERE relpath=?", (relpath,)).fetchone()
            if row is None:
                return None
        keys = ("relpath", "name", "album", "artist", "title", "track",
                "duration", "sample_rate", "channels", "size", "mtime",
                "added_at")
        return dict(zip(keys, row))

    def albums(self):
        """[{album, artist, track_count}] sorted by name."""
        with self.lock:
            rows = self._conn.execute("""
                SELECT album, artist, COUNT(*)
                FROM tracks
                WHERE album IS NOT NULL AND album != ''
                GROUP BY album""").fetchall()
        return sorted(
            ({"album": r[0], "artist": r[1] or "", "track_count": r[2]}
             for r in rows),
            key=lambda a: natural_key(a["album"]))

    def albums_with_tracks(self):
        """[{album, artist, track_count, tracks:[{relpath, title}, ...]}]
        sorted by name. Each track carries its display title (tagged title,
        falling back to the filename) so the web UI can show song names."""
        albums = {a["album"]: a for a in self.albums()}
        with self.lock:
            rows = self._conn.execute(
                "SELECT relpath, album, title, track FROM tracks "
                "WHERE album IS NOT NULL AND album != ''").fetchall()
        by_album = {}
        for rel, album, title, track in rows:
            by_album.setdefault(album, []).append((rel, title, track))
        for album, info in albums.items():
            entries = by_album.get(album, [])
            entries.sort(key=lambda e: track_sort_key(e[0], e[2]))
            info["tracks"] = [
                {"relpath": rel, "title": title or os.path.basename(rel)}
                for rel, title, _ in entries]
            info["track_count"] = len(entries)
        return sorted(albums.values(),
                      key=lambda a: natural_key(a["album"]))

    def album_tracks(self, album):
        """relpaths of every track in ``album``, in album order: tagged track
        numbers first, untagged files falling back to natural filename order."""
        with self.lock:
            rows = self._conn.execute(
                "SELECT relpath, track FROM tracks WHERE album=?",
                (album,)).fetchall()
        rows.sort(key=lambda r: track_sort_key(r[0], r[1]))
        return [r[0] for r in rows]

    def singles(self):
        """[{relpath, title, artist}] for tracks outside any album,
        filename-sorted. artist is '' when untagged, so the UI can search
        by author too."""
        with self.lock:
            rows = self._conn.execute(
                "SELECT relpath, title, artist FROM tracks "
                "WHERE album IS NULL OR album=''").fetchall()
        rows.sort(key=lambda r: natural_key(r[0]))
        return [{"relpath": rel, "title": title or os.path.basename(rel),
                 "artist": artist or ""}
                for rel, title, artist in rows]

    def relpaths(self):
        """Every track, ordered for playback: singles first (filename order),
        then albums (by album name, then tagged track number / filename)."""
        with self.lock:
            rows = self._conn.execute(
                "SELECT relpath, album, track FROM tracks").fetchall()
        singles = sorted((r[0] for r in rows if not r[1]),
                         key=lambda r: natural_key(os.path.basename(r)))
        albums = sorted((r for r in rows if r[1]),
                        key=lambda r: (natural_key(r[1]),
                                       track_sort_key(r[0], r[2])))
        return singles + [r[0] for r in albums]

    def abs_paths(self):
        """Filesystem paths matching relpaths() order (the playlist)."""
        return [abs_path(self.music_dir, r) for r in self.relpaths()]

    # ------------------------------------------------------------------ #
    # Matching (user input -> relpaths)                                   #
    # ------------------------------------------------------------------ #
    def album_match(self, spec):
        """relpaths of the album whose name (case-insensitive) is ``spec``."""
        spec = (spec or "").strip()
        with self.lock:
            row = self._conn.execute(
                "SELECT album FROM tracks WHERE album IS NOT NULL "
                "AND album != '' AND album = ?",
                (spec,)).fetchone()
            if row:
                return self.album_tracks(row[0])
            row = self._conn.execute(
                "SELECT DISTINCT album FROM tracks WHERE album IS NOT NULL "
                "AND album != '' AND LOWER(album) = LOWER(?)",
                (spec,)).fetchone()
            return self.album_tracks(row[0]) if row else []

    def album_name(self, spec):
        """Canonical album name matching ``spec`` (case-insensitive), or None."""
        spec = (spec or "").strip()
        with self.lock:
            row = self._conn.execute(
                "SELECT DISTINCT album FROM tracks WHERE album IS NOT NULL "
                "AND album != '' AND LOWER(album) = LOWER(?)",
                (spec,)).fetchone()
            return row[0] if row else None

    def track_match(self, spec):
        """relpaths matching a track: exact relpath first, else basename,
        else the tagged song title."""
        spec = (spec or "").strip()
        rels = []
        with self.lock:
            for cond in ("relpath = ?", "LOWER(relpath) = LOWER(?)",
                         "LOWER(name) = LOWER(?)", "LOWER(title) = LOWER(?)"):
                rels = [r[0] for r in self._conn.execute(
                    f"SELECT relpath FROM tracks WHERE {cond}", (spec,))]
                if rels:
                    break
        return sorted(rels)

    # ------------------------------------------------------------------ #
    # Queue persistence                                                   #
    # ------------------------------------------------------------------ #
    def save_queue(self, entries):
        """Persist the logical queue (list of entry dicts) to the DB."""
        with self.lock:
            with self._conn:
                self._conn.execute("DELETE FROM queue")
                self._conn.executemany(
                    "INSERT INTO queue (position, entry) VALUES (?, ?)",
                    [(i, json.dumps(entry)) for i, entry in enumerate(entries)])

    def load_queue(self):
        """Load the persisted queue from the DB. Returns a list of entry
        dicts in position order. Paths that no longer exist on disk are
        filtered out (songs dropped, album tracks removed)."""
        with self.lock:
            rows = self._conn.execute(
                "SELECT entry FROM queue ORDER BY position").fetchall()
        restored = []
        for r in rows:
            try:
                e = json.loads(r[0])
            except (json.JSONDecodeError, KeyError):
                continue
            if e.get("type") == "song":
                if e.get("path") and os.path.isfile(e["path"]):
                    restored.append(e)
            elif e.get("type") == "album":
                paths = [p for p in e.get("paths", [])
                         if p and os.path.isfile(p)]
                if paths:
                    e["paths"] = paths
                    restored.append(e)
        return restored

    # ------------------------------------------------------------------ #
    # Playback position (resume across restarts)                          #
    # ------------------------------------------------------------------ #
    def save_playback(self, relpath, elapsed=0.0, playing=True):
        """Persist the current track so a restart can resume playback from
        roughly the same position (a single-row table, id 0)."""
        with self.lock:
            with self._conn:
                self._conn.execute(
                    "INSERT OR REPLACE INTO playback (id, relpath, elapsed,"
                    " playing) VALUES (0, ?, ?, ?)",
                    (relpath, float(elapsed), 1 if playing else 0))

    def clear_playback(self):
        """Forget the persisted current track/position (used when the user
        clears or removes everything while nothing is playing)."""
        with self.lock:
            with self._conn:
                self._conn.execute("DELETE FROM playback WHERE id = 0")

    def load_playback(self):
        """The previously-playing track as {relpath, elapsed, playing}, or
        None when nothing was ever played (or the file no longer exists)."""
        with self.lock:
            row = self._conn.execute(
                "SELECT relpath, elapsed, playing FROM playback WHERE id = 0"
            ).fetchone()
        if not row:
            return None
        relpath, elapsed, playing = row
        if not relpath or not os.path.isfile(abs_path(self.music_dir, relpath)):
            return None
        return {"relpath": relpath, "elapsed": float(elapsed or 0.0),
                "playing": bool(playing)}