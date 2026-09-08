#!/usr/bin/env python3
"""
msync_server.py - synced music server with a play queue.

Responsibilities:
  * Serve music files over HTTP so clients can download the current track.
  * Broadcast UDP SYNC packets describing what's playing and when the current
    song started (in server wall-clock time).
  * Reply to client NTP requests so clients can estimate the server clock.
  * Play the current track locally via sounddevice (with drift correction so
    the server itself stays on its own reported timeline).
  * Maintain a FIFO queue. Songs are added interactively / via HTTP / by
    dropping files in the <music_dir>/.queue folder; the queue is played on
    the server and therefore on every connected client.

Usage:
    msync_server.py [music_dir] [--port N]

Controls (stdin/HTTP):
    space      : play/pause          n : next (queue first, then playlist)
    p          : prev                q : quit
    +/-        : volume
    add <name|glob...> : queue songs (e.g. add "Foo.mp3" or "add *.wav")
    queue      : show the queue      clear : empty the queue

Drop-to-queue:
    Copy any audio file into <music_dir>/.queue while the server runs.
    It is moved into the music dir and appended to the queue.
"""

import argparse
import glob
import json
import os
import socket
import threading
import time
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import miniaudio
import sounddevice as sd
import numpy as np

import config
import msync_common as C
from msync_catalog import (Catalog, abs_path, is_audio_name,
                           relpath_norm)
from msync_inotify import MusicWatcher


# --------------------------------------------------------------------------- #
# Audio cache: decoded current track as float32 numpy array, mono-mixed       #
# --------------------------------------------------------------------------- #
class Song:
    __slots__ = ("name", "path", "data", "sr", "nchannels", "duration")

    def __init__(self, path, relpath=None):
        # name is the catalog relpath ('Album/01 Song.mp3'); for top-level
        # tracks that's just the file name. It's what sync packets, the HTTP
        # API, and client downloads all use to identify the track.
        self.name = relpath or os.path.basename(path)
        self.path = path
        # Decode to float32 numpy stereo (N, 2). Monaural sources are
        # broadcast to both channels; >2ch sources keep the first two.
        C.logger().info("decoding %s", path)
        decoded = miniaudio.decode_file(path, output_format=miniaudio.SampleFormat.FLOAT32)
        self.sr = decoded.sample_rate
        frames = np.frombuffer(decoded.samples, dtype=np.float32)
        if decoded.nchannels == 2:
            self.data = frames.reshape(-1, 2)
        elif decoded.nchannels == 1:
            self.data = np.repeat(frames[:, None], 2, axis=1)
        else:
            f = frames.reshape(-1, decoded.nchannels)
            self.data = f[:, :2].copy() if decoded.nchannels > 2 else f
        self.nchannels = 2
        self.duration = len(self.data) / self.sr


class SyncedServer:
    def __init__(self, music_dir, port, db_path=None):
        lg = C.logger()
        self.port = port
        self.lock = threading.RLock()
        self.music_dir = music_dir
        self.db_path = db_path or config.DB_PATH
        self.queue_dir = os.path.join(music_dir, config.QUEUE_SUBDIR)
        try:
            os.makedirs(self.queue_dir, exist_ok=True)
        except OSError:
            self.queue_dir = None

        # SQLite catalog of every track (albums = sub-folders). The schema is
        # created now, but the metadata scan + DB population is done in a
        # background thread so a big library doesn't block startup.
        self.catalog = Catalog(music_dir, self.db_path)
        # Fast filesystem walk (no metadata reads) so we can start playing
        # immediately; the full catalog arrives in the background.
        self.playlist = self.catalog.quick_paths()
        self._last_scan = time.time()
        self._scan_done = threading.Event()   # set once the background scan finishes
        if not self.playlist:
            raise SystemExit(f"No audio files found in '{music_dir}'")
        lg.info("server: %d track(s) found (scanning metadata in background)",
                len(self.playlist))

        self.song = None            # currently playing Song object
        self.index = 0
        # Logical play queue: a list of entries. Each entry is either
        #   {"type": "song",  "path": <abs>, "relpath": <rel>}       or
        #   {"type": "album", "album": <name>, "paths": [<abs>, ...] (remaining)}
        # Entries are persisted to the catalog DB so the queue survives
        # restarts. queue_list() expands albums back to a flat list.
        self.queue = self.catalog.load_queue()
        self.playing = True
        self.volume = 1.0
        self.seek = 0.0             # seconds into song at song_start
        self.song_start = 0.0       # server wall-clock time (time.time) at song start
        self._stop = threading.Event()   # set to stop background loops (tests)

        self.stream = None          # PortAudio stream (opened in _play)
        self.local_pos = 0.0        # local playhead (seconds) in current song
        self._pitch_int = 0.0       # drift PI integrator state
        self.err_f = 0.0            # low-passed PLL error (EMA of callback err)
        self._fade_out = False      # set by close_audio -> callback fades to silence

        # Startup playback: resume exactly where the previous run left off —
        # the last-played track starts again (same song, roughly the same
        # position, preserving its album context). If nothing was playing,
        # fall back to the restored queue, and only auto-play if there was
        # neither a current track nor queued items.
        resumed = False
        resume = self.catalog.load_playback()
        if resume is not None:
            try:
                self._play(self._abs(resume["relpath"]),
                           seek=resume["elapsed"], announce="resume")
                if not resume["playing"]:
                    self.playing = False
                # Re-persist so the stored pause state matches reality
                # (_play() recorded playing=True just above).
                self._persist_playback()
                resumed = True
            except Exception as exc:
                C.logger().warning("could not resume %r: %s",
                                   resume["relpath"], exc)
        if not resumed:
            if self.queue:
                self._start_next()
            else:
                self.song = None
                self.playing = False

        # Populate the catalog (metadata reads + DB updates) off the main
        # thread so it never blocks the server on a large library.
        threading.Thread(target=self._background_scan, daemon=True).start()

    def _background_scan(self):
        """Populate the catalog database in the background, then refresh the
        playlist so the full library (albums, track counts, sorting) becomes
        available as soon as the scan completes."""
        lg = C.logger()
        t0 = time.time()
        try:
            n = self.catalog.scan()
            self._refresh_playlist()
            self._last_scan = time.time()
            lg.info("server: library scan complete: %d change(s) in %.1fs "
                    "(%d track(s) in catalog)",
                    n, time.time() - t0, len(self.playlist))
        except Exception as exc:
            lg.error("server: library scan failed: %s", exc)
        finally:
            self._scan_done.set()

    def _refresh_playlist(self, new_playlist=None):
        """Re-derive the playlist from the catalog (new albums/tracks added
        while running should become skippable). If ``new_playlist`` is given,
        it is used instead. The playback cursor is kept on the currently
        playing song, so a reorder (tag-based track numbers vs filename
        order, a new file appearing mid-album, etc.) never moves 'next'."""
        with self.lock:
            new = (new_playlist if new_playlist is not None
                   else self.catalog.abs_paths())
            if self.song is not None:
                try:
                    self.index = max(0, new.index(self._abs(self.song.name)))
                except ValueError:
                    pass          # current song vanished; keep old cursor
            self.playlist = new

    # ------------------------------------------------------------------ #
    # Path helpers                                                        #
    # ------------------------------------------------------------------ #
    def _relpath(self, path):
        return relpath_norm(self.music_dir, path)

    def _abs(self, relpath):
        return abs_path(self.music_dir, relpath)

    # ------------------------------------------------------------------ #
    # Playlist / queue primitives                                        #
    # ------------------------------------------------------------------ #
    def _index_of(self, path):
        try:
            return self.playlist.index(path)
        except ValueError:
            return -1

    def _play(self, path, seek=0.0, announce="queued"):
        """Load + start playing `path`. Caller must hold self.lock."""
        song = Song(path, relpath=self._relpath(path))
        self.index = max(0, self._index_of(path))
        self.seek = seek
        self.song_start = C.ts() - self.seek
        self.local_pos = self.seek
        self.playing = True
        self._pitch_int = 0.0      # fresh track: fresh alignment state
        self.err_f = 0.0
        # Publish the song ref LAST: the audio callback reads it without the
        # lock, so it either sees the fully-updated old state or the fully-
        # updated new state (at most one torn block at a track swap).
        self.song = song
        C.logger().info("playing: %s (%.1fs) [%s]", self.song.name,
                        self.song.duration, announce)
        if self.stream is None or self.stream.samplerate != self.song.sr:
            C.logger().info("opening audio stream (%d Hz, stereo)",
                            self.song.sr)
            self._open_stream()
        self._persist_playback()

    def _start_next(self):
        """Pop the next thing to play from the queue, else advance the
        alphabetical playlist. Albums in the queue play their tracks one at
        a time; the album entry shrinks until it's exhausted, then pops."""
        with self.lock:
            self.seek = 0.0
            while self.queue:
                entry = self.queue[0]
                if entry.get("type") == "album":
                    paths = entry.get("paths") or []
                    if not paths:
                        self.queue.pop(0)
                        continue
                    self._play(paths[0], announce="album")
                    display = self._relpath(paths[0])
                    entry["paths"] = paths[1:]
                    if not entry["paths"]:
                        self.queue.pop(0)
                    self._persist_queue()
                    return {"source": "album", "file": display}
                # song entry
                self.queue.pop(0)
                self._play(entry["path"], announce="queue")
                self._persist_queue()
                return {"source": "queue", "file": entry["relpath"]}
            if not self.playing:
                # Nothing is actively playing and the queue is empty: an
                # advance (the web UI's ✕ on the now-playing slot, or 'next'
                # while stopped) must leave the server idle rather than start
                # an arbitrary playlist track the user didn't ask for.
                if self.song is not None:
                    self._clear_playback()
                self.song = None
                self.local_pos = 0.0
                return {"source": "stopped", "file": None}
            self.index = (self.index + 1) % len(self.playlist)
            self._play(self.playlist[self.index], announce="playlist")
            return {"source": "playlist", "file": self.song.name}

    def _song_entries(self, files):
        """Build song entries from a list of absolute file paths, skipping
        any that are already queued. Returns (entries, added_relpaths)."""
        seen = {e.get("path") for e in self.queue
                if e.get("type") == "song" and e.get("path")}
        added = []
        for f in files:
            if f not in seen:
                seen.add(f)
                added.append(f)
        entries = [{"type": "song", "path": f, "relpath": self._relpath(f)}
                   for f in added]
        return entries, [self._relpath(f) for f in added]

    def add_to_queue(self, specs):
        """Queue one or more songs (names, album names, paths, or globs).
        Returns added relative paths. If nothing has been loaded yet, the
        first queued song starts playing. Album names are stored as album
        entries; everything else as individual song entries."""
        with self.lock:
            added_display = []
            for spec in specs:
                spec = (spec or "").strip().strip("\"'")
                if not spec:
                    continue
                album = self.catalog.album_name(spec)
                if album:
                    rels = self.catalog.album_tracks(album)
                    if not rels:
                        continue
                    new_paths = [self._abs(r) for r in rels]
                    new_paths = [p for p in new_paths
                                 if p not in self._queued_paths()]
                    if new_paths:
                        self.queue.append(
                            {"type": "album", "album": album, "paths": new_paths})
                        added_display.extend(
                            self._relpath(p) for p in new_paths)
                        for p in new_paths:
                            C.logger().info("queued: %s [%s]",
                                            self._relpath(p), album)
                else:
                    files = self.find_files([spec])
                    entries, rels = self._song_entries(files)
                    if entries:
                        self.queue.extend(entries)
                        added_display.extend(rels)
                        for f in entries:
                            C.logger().info("queued: %s", f["relpath"])
            if not added_display:
                return []
            # If there's no current song (never started), kick off now.
            if self.song is None and self.queue:
                self._start_next()
            C.logger().info("queue: %d pending", len(self.queue_list()))
            self._persist_queue()
            return added_display

    def _queued_paths(self):
        """Every absolute path currently represented in the queue
        (albums expanded) — used for dedup."""
        paths = set()
        for e in self.queue:
            if e.get("type") == "song":
                if e.get("path"):
                    paths.add(e["path"])
            elif e.get("type") == "album":
                paths.update(e.get("paths", []))
        return paths

    def add_album(self, album):
        """Queue every track in ``album`` (as one album entry, appended).
        Returns the added relative paths (or [] if unknown)."""
        with self.lock:
            rels = self.catalog.album_tracks(album)
            if not rels:
                return []
            new_paths = [self._abs(r) for r in rels]
            new_paths = [p for p in new_paths if p not in self._queued_paths()]
            if not new_paths:
                self._persist_queue()
                return []
            self.queue.append(
                {"type": "album", "album": album, "paths": new_paths})
            for p in new_paths:
                C.logger().info("queued: %s [%s]", self._relpath(p), album)
            C.logger().info("queue: %d pending", len(self.queue_list()))
            self._persist_queue()
            return [self._relpath(p) for p in new_paths]

    def find_files(self, specs):
        """Resolve user specs (names, album names, paths, globs) to existing
        audio files. Returns absolute paths, ordered by the catalog."""
        found, seen = [], set()
        for spec in specs:
            spec = (spec or "").strip().strip("\"'")
            if not spec:
                continue
            if os.path.isabs(spec):
                if os.path.isfile(spec) and is_audio_name(spec):
                    found.append(spec)
                    seen.add(spec)
                else:
                    print(f"[server] not found: {spec}")
                continue
            if any(ch in spec for ch in "*?["):
                matches = sorted(
                    glob.glob(os.path.join(self.music_dir, spec)))
                if not matches and "**" not in spec:
                    matches = sorted(glob.glob(
                        os.path.join(self.music_dir, "**", spec),
                        recursive=True))
                for p in matches:
                    if os.path.isfile(p) and is_audio_name(p) \
                            and p not in seen:
                        found.append(p)
                        seen.add(p)
                continue
            # relative path (may include the album folder)
            p = os.path.join(self.music_dir, spec)
            if os.path.isfile(p) and is_audio_name(p):
                if p not in seen:
                    found.append(p)
                    seen.add(p)
                continue
            # album name -> every track in that album
            rels = self.catalog.album_match(spec)
            if not rels:
                rels = self.catalog.track_match(spec)   # track by name/path
            for r in rels:
                ap = self._abs(r)
                if ap not in seen:
                    found.append(ap)
                    seen.add(ap)
            if not rels:
                print(f"[server] not found: {spec}")
        return found

    def clear_queue(self):
        with self.lock:
            n = len(self.queue_list())
            self.queue.clear()
            if not self.playing:
                # Nothing is actively playing: a clear empties the whole
                # queue — including the stale now-playing slot — instead of
                # leaving a paused/stopped song behind.
                self.song = None
                self.local_pos = 0.0
                self._clear_playback()
            self._persist_queue()
            C.logger().info("queue cleared (%d removed)", n)
            return n

    def _find_expanded(self, index):
        """Map a flat (expanded) queue index to (entry_index, entry, offset)
        without building the full expanded list. Returns None when out of
        range. Albums occupy len(paths) consecutive slots."""
        acc = 0
        for k, e in enumerate(self.queue):
            n = len(e.get("paths") or []) if e.get("type") == "album" else 1
            if acc <= index < acc + n:
                return k, e, index - acc
            acc += n
        return None

    def remove_from_queue(self, index=None, name=None):
        """Remove one queued item by index or name; returns the removed
        display name (relative path). ``index``/``name`` refer to the
        *expanded* queue (queue_list()), where an album occupies one or more
        slots; removing a slot inside an album keeps the rest."""
        with self.lock:
            if index is not None:
                hit = self._find_expanded(index)
                if hit is None:
                    return None
                k, entry, offset = hit
                if entry.get("type") == "song":
                    removed = entry["relpath"]
                    self.queue.pop(k)
                else:
                    removed = self._relpath(entry["paths"][offset])
                    del entry["paths"][offset]
                    if not entry["paths"]:
                        self.queue.pop(k)
                self._persist_queue()
                return removed
            if name is not None:
                flat = self.queue_list()
                # match against expanded display names
                for i, d in enumerate(flat):
                    if d.lower() == str(name).lower() \
                            or os.path.basename(d).lower() == str(name).lower():
                        removed = self._remove_expanded(d)
                        self._persist_queue()
                        return removed
            return None

    def _remove_expanded(self, relpath):
        """Remove the first occurrence of ``relpath`` from the expanded
        queue. If it's an individual song entry, drop that entry; if it's
        inside an album entry, remove just that track (dropping the album
        entry when it becomes empty). Returns the display name removed."""
        abs_ = self._abs(relpath)
        for i, e in enumerate(self.queue):
            if e.get("type") == "song" and e.get("path") == abs_:
                self.queue.pop(i)
                return e["relpath"]
            if e.get("type") == "album" and abs_ in e.get("paths", []):
                e["paths"].remove(abs_)
                if not e["paths"]:
                    self.queue.pop(i)
                return relpath
        return None

    def _flat_len(self):
        """Number of slots in the expanded queue (albums expand to tracks)."""
        return sum(len(e.get("paths")) if e.get("type") == "album" else 1
                   for e in self.queue)

    def move_queue_item(self, from_index, to_index):
        """Move the item at flat (expanded) queue index ``from_index`` so it
        lands at final flat index ``to_index`` — drag & drop reordering.
        Individual songs move as whole entries; dragging a track out of (or
        within) an album entry splits that album around the track. Returns
        True when the move was applied (False on out-of-range indices)."""
        with self.lock:
            total = self._flat_len()
            if from_index < 0 or from_index >= total \
                    or to_index < 0 or to_index > total:
                return False
            if from_index == to_index:
                return True          # already where it should be
            hit = self._find_expanded(from_index)
            if hit is None:
                return False
            k, entry, offset = hit
            if entry.get("type") == "song":
                dragged = self.queue.pop(k)
            else:
                # Dragged a single track out of an album entry: drop it and
                # keep the remaining tracks as one album entry.
                album = entry.get("album") or ""
                paths = entry["paths"]
                dragged = {"type": "song", "path": paths[offset],
                           "relpath": self._relpath(paths[offset])}
                remaining = paths[:offset] + paths[offset + 1:]
                if remaining:
                    self.queue[k] = {"type": "album", "album": album,
                                     "paths": remaining}
                else:
                    self.queue.pop(k)
            self._insert_song(dragged, min(to_index, self._flat_len()))
            self._persist_queue()
            return True

    def _insert_song(self, dragged, to_index):
        """Insert the 1-slot song entry ``dragged`` so it lands at flat
        index ``to_index`` (0 = front, current length = append). When the
        landing spot falls inside an album entry, that entry is split around
        the new song. Caller must hold the lock."""
        acc = 0
        for k, e in enumerate(self.queue):
            L = len(e.get("paths")) if e.get("type") == "album" else 1
            if to_index == acc:
                self.queue.insert(k, dragged)
                return
            if to_index < acc + L:
                album = e.get("album") or ""
                off = to_index - acc
                pieces = []
                if off:
                    pieces.append({"type": "album", "album": album,
                                   "paths": e["paths"][:off]})
                pieces.append(dragged)
                pieces.append({"type": "album", "album": album,
                               "paths": e["paths"][off:]})
                self.queue[k:k + 1] = pieces
                return
            acc += L
        self.queue.append(dragged)

    def library_list(self):
        """Every track (relative path), in playback order. Freshly dropped
        files appear once the catalog is refreshed."""
        return self.catalog.relpaths()

    def albums(self):
        """[{album, artist, track_count}] for the web UI."""
        return self.catalog.albums()

    def album_tracks(self, album):
        """Relative paths of a single album's tracks, in track order."""
        return self.catalog.album_tracks(album)

    def album_list_full(self):
        """Albums including their track lists, for the web UI library."""
        return self.catalog.albums_with_tracks()

    def queue_list(self):
        """The full expanded queue as a flat list of relative paths
        (albums expanded into their tracks, in order)."""
        with self.lock:
            out = []
            for e in self.queue:
                if e.get("type") == "album":
                    out.extend(self._relpath(p) for p in e.get("paths", []))
                elif e.get("type") == "song" and e.get("path"):
                    out.append(e["relpath"])
            return out

    def queue_meta(self):
        """The queue expanded to flat entries carrying tag metadata for
        display (artist - album - title): [{relpath, title, artist, album,
        track}]. Albums expand to their tracks; untagged songs fall back to
        folder/filename. Index order matches queue_list()."""
        with self.lock:
            out = []
            for e in self.queue:
                if e.get("type") == "album":
                    album = e.get("album") or ""
                    for p in e.get("paths", []):
                        out.append(self._track_meta(self._relpath(p), album))
                elif e.get("type") == "song" and e.get("path"):
                    out.append(self._track_meta(e["relpath"]))
            return out

    def _track_meta(self, relpath, album_hint=""):
        """{relpath, title, artist, album, track} for one track, using the
        catalog's tags when available and folder/filename otherwise."""
        row = self.catalog.track(relpath)
        parts = relpath.split("/", 1)
        folder = parts[0] if len(parts) > 1 else ""
        base = os.path.basename(relpath)
        if row is not None:
            title = row.get("title") or row.get("name") or base
            artist = row.get("artist") or ""
            album = row.get("album") or album_hint or folder
            track = row.get("track") or ""
        else:
            title, artist, album, track = base, "", album_hint or folder, ""
        return {"relpath": relpath, "title": title, "artist": artist,
                "album": album, "track": track}

    def _persist_queue(self):
        """Write the current logical queue to the catalog DB (best-effort)."""
        try:
            self.catalog.save_queue(self.queue)
        except Exception as exc:
            C.logger().warning("failed to persist queue: %s", exc)

    def _persist_playback(self):
        """Write the current track/position so a restart resumes the same
        song (best-effort, never raises). Like everywhere else, the current
        track's identity is its relpath (Song.name)."""
        try:
            if self.song is not None:
                self.catalog.save_playback(self.song.name,
                                           self.local_pos or 0.0,
                                           self.playing)
        except Exception as exc:
            C.logger().warning("failed to persist playback: %s", exc)

    def _clear_playback(self):
        """Forget the persisted current track so a restart doesn't resurrect
        a song the user explicitly removed (best-effort, never raises)."""
        try:
            self.catalog.clear_playback()
        except Exception as exc:
            C.logger().warning("failed to clear playback: %s", exc)

    def _prune_clients(self):
        """Drop rooms whose last heartbeat is older than
        config.CLIENT_STALE_AFTER (best-effort, never raises)."""
        try:
            self.catalog.prune_clients(config.CLIENT_STALE_AFTER)
        except Exception as exc:
            C.logger().warning("failed to prune stale clients: %s", exc)

    # ------------------------------------------------------------------ #
    # Audio callback: server plays so it also matches its own timeline.   #
    # ------------------------------------------------------------------ #
    def _audio_cb(self, outdata, frames, time_info, status):
        C.boost_audio_thread()
        # Deliberately no self.lock here: if another thread is busy decoding
        # the next track, persisting state, or serving downloads under the
        # lock, taking it in the output callback would stall audio (xruns /
        # gaps). Writers publish song/local_pos/timestamps in a safe order
        # (see _play); a torn read is limited to at most one block right at a
        # track swap, which beats a multi-block stall by a wide margin.
        song = self.song
        if song is None or not self.playing:
            outdata.fill(0)
            return
        target = C.ts() - self.song_start
        err = target - self.local_pos
        # Low-pass the error (same as the client): the raw err carries the
        # half-block sawtooth from local_pos stepping once per block and, on
        # a client, reference jitter — enough to slam the ±0.2% pitch clamp
        # every block and start a limit cycle. Drive the controller from the
        # smoothed error.
        self.err_f += C.PLL_ALPHA * (err - self.err_f)
        ef = self.err_f
        # Bounded fast catch-up (same as the client): recovers from a server
        # stall or a fresh resume in a few blocks instead of a slow pitch
        # slew that would take tens of seconds. Gated on the smoothed error.
        if abs(ef) > C.CATCHUP_THRESHOLD:
            self.local_pos += C.CATCHUP_STEP if ef > 0 else -C.CATCHUP_STEP
            err = target - self.local_pos
        # gentle PI drift controller on the smoothed error (server is its own
        # clock, so no FF) with an audible deadband like the client; tight
        # integrator clip + fast unwind so a stale windup can't keep the
        # pitch pinned at ±MAX_PITCH
        ed = float(np.copysign(max(abs(ef) - C.DRIFT_HYSTERESIS, 0.0), ef))
        if ed:
            self._pitch_int += max(-C.INT_LIMIT, min(C.INT_LIMIT, ed))
            # integral pulling against the error: unwind it now
            if self._pitch_int * ef < 0.0:
                self._pitch_int *= 0.5
        else:
            self._pitch_int *= C.INT_UNWIND
        self._pitch_int = max(-C.INT_LIMIT, min(C.INT_LIMIT, self._pitch_int))
        pitch = max(-C.MAX_PITCH, min(C.MAX_PITCH,
                    ed * C.PITCH_GAIN + self._pitch_int * C.PITCH_INT))
        rate = 1.0 + pitch
        idx = max(0.0, self.local_pos * song.sr)
        n = len(song.data)
        i0 = int(min(idx, n))
        i1 = min(i0 + frames, n)
        out = np.zeros((frames, 2), dtype=np.float32)
        if i0 < n:
            try:
                out[: i1 - i0] = song.data[i0:i1]
            except ValueError:
                pass
        outdata[:] = out
        if self._fade_out:
            nf = min(frames, int(0.15 * song.sr))
            if nf > 0:
                outdata[-nf:] *= np.linspace(1.0, 0.0, nf,
                                             dtype=np.float32)[:, None]
        self.local_pos += frames / song.sr * rate

    def _open_stream(self):
        with C.stream_ops_lock:
            try:
                if self.stream is not None:
                    try:
                        self.stream.stop()
                    except Exception:
                        pass
                    self.stream.close()
                    self.stream = None
                self.stream = sd.OutputStream(
                    samplerate=self.song.sr,
                    channels=2,
                    callback=self._audio_cb,
                    blocksize=8192,
                    latency="low",
                )
                self.stream.start()
            except Exception as e:
                print(f"[server] audio stream error: {e}; continuing headless")

    def close_audio(self):
        """Fade out and close the audio stream cleanly (no click on stop)."""
        with C.stream_ops_lock:
            self._fade_out = True
            s = self.stream
            if s is None:
                self._fade_out = False
                return
            time.sleep(0.1)           # let the fade ramp play out
            try:
                s.stop()
            except Exception:
                pass
            try:
                s.close()
            except Exception:
                pass
            self.stream = None
            self._fade_out = False

    # ------------------------------------------------------------------ #
    # Drop-to-queue folder + auto-advance                                 #
    # ------------------------------------------------------------------ #
    def _dedupe_dest(self, fname):
        dest = os.path.join(self.music_dir, fname)
        if not os.path.exists(dest):
            return dest
        base, ext = os.path.splitext(fname)
        i = 1
        while True:
            cand = os.path.join(self.music_dir, f"{base} ({i}){ext}")
            if not os.path.exists(cand):
                return cand
            i += 1

    def _absorb_drops(self):
        if not self.queue_dir:
            return
        try:
            entries = sorted(os.listdir(self.queue_dir))
        except OSError:
            return
        for fname in entries:
            fpath = os.path.join(self.queue_dir, fname)
            if not os.path.isfile(fpath) or not is_audio_name(fname):
                continue
            dest = self._dedupe_dest(fname)
            try:
                os.replace(fpath, dest)
            except OSError as e:
                print(f"[server] drop absorb failed: {e}")
                continue
            self.add_to_queue([os.path.basename(dest)])

    def monitor_loop(self):
        last_catalog = time.time()
        last_playback = 0.0
        last_client_prune = 0.0
        while not self._stop.is_set():
            with self.lock:
                if (self.playing and self.song is not None
                        and self.local_pos >= self.song.duration - 0.1):
                    print(f"[server] ended: {self.song.name}")
                    if self.queue:
                        self._start_next()
                    else:
                        # Queue empty — stop instead of auto-advancing the
                        # playlist to the next album track.
                        self.playing = False
                        self.song = None
                        self._persist_playback()
                # Track where we are so a restart resumes the same song at
                # roughly the same position (also covers clean shutdowns).
                if self.song is not None and time.time() - last_playback >= 5.0:
                    self._persist_playback()
                    last_playback = time.time()
            self._absorb_drops()
            # refresh the catalog periodically so freshly added albums and
            # tracks (e.g. via the drop folder) become browsable/skippable.
            # Skip while the initial background scan is still populating the
            # DB so we don't pile redundant full scans on top of it.
            if (time.time() - last_catalog >= 5.0
                    and self._scan_done.is_set()):
                self.catalog.scan()
                self._refresh_playlist()
                last_catalog = time.time()
            # Forget rooms that haven't been heard from in a long time (see
            # config.CLIENT_STALE_AFTER) so dead rooms don't pile up.
            if time.time() - last_client_prune >= 60.0:
                self._prune_clients()
                last_client_prune = time.time()
            time.sleep(0.25)

    # ------------------------------------------------------------------ #
    # Control commands (also exposed over HTTP / stdin)                   #
    # ------------------------------------------------------------------ #
    def play_pause(self):
        with self.lock:
            self.playing = not self.playing
            if self.playing:
                self.song_start = C.ts() - self.local_pos
            self._pitch_int = 0.0      # pause/resume invalidates old windup
            self.err_f = 0.0           # (and the smoothed PLL error)
            self._persist_playback()
        return self.playing

    def stop_playback(self):
        """Stop (halt) playback and rewind the playhead to the start of the
        current track. A later play/pause resumes the same track from 0."""
        with self.lock:
            self.local_pos = 0.0
            self.seek = 0.0
            self.song_start = C.ts()
            self.playing = False
            self._pitch_int = 0.0      # fresh alignment on the next play
            self.err_f = 0.0
            self._persist_playback()
        return self.playing

    def next(self):
        return self._start_next()

    def prev(self):
        with self.lock:
            self.seek = 0.0
            self.index = (self.index - 1) % len(self.playlist)
            self._play(self.playlist[self.index], announce="playlist")
            return {"source": "playlist", "file": self.song.name}

    def play_song(self, name):
        """Play immediately by name or album name (case-insensitive). A plain
        track name plays just that track; an album name starts the album
        (first track now, the rest queued at the front)."""
        with self.lock:
            album = self.catalog.album_name(name)
            if album:
                return self.play_album(album)
            files = self.find_files([name])
            if not files:
                print(f"[server] not found: {name}")
                return None
            self._play(files[0], announce="manual")
            return self.song.name

    def play_album(self, album):
        """Play an album now: first track plays immediately, the rest are
        queued at the front (as one album entry, in track order). Caller may
        hold the lock."""
        with self.lock:
            rels = self.catalog.album_tracks(album)
            if not rels:
                print(f"[server] album not found: {album}")
                return None
            paths = [self._abs(r) for r in rels]
            rest = paths[1:]
            if rest:
                # prepend the remaining tracks as a fresh album entry
                self.queue.insert(
                    0, {"type": "album", "album": album, "paths": rest})
            self._play(paths[0], announce="album")
            self._persist_queue()
            return {"album": album, "file": self.song.name,
                    "track_count": len(paths)}

    def set_vol(self, v):
        self.volume = max(0.0, min(1.5, v))
        if self.stream is not None:
            self.stream.volume = self.volume
        return self.volume

    # ------------------------------------------------------------------ #
    # UDP sync + NTP + state broadcast                                   #
    # ------------------------------------------------------------------ #
    def _sync_payload(self):
        with self.lock:
            return {
                "index": max(0, self.index),
                "name": self.song.name if self.song else "",
                "playing": self.playing,
                "song_start": self.song_start,
                "duration": self.song.duration if self.song else 0.0,
                "volume": self.volume,
                "queue_size": len(self.queue_list()),
            }

    def _state_payload(self):
        with self.lock:
            payload = self._sync_payload()
            payload.update({
                "queue": self.queue_list(),
                "queue_meta": self.queue_meta(),
                "position": int(self.local_pos),
                "elapsed": round(self.local_pos, 1),
                "playlist_size": len(self.playlist),
                "paused": not self.playing,
                "now_playing": self._now_playing(),
            })
            return payload

    def _now_playing(self):
        """{name, relpath, album, title, artist} for the current track
        (None when idle). name/album fall back to filename/folder; title/
        artist come from the catalog's tags when available."""
        if self.song is None:
            return None
        name = self.song.name          # catalog relpath
        parts = name.split("/")
        row = self.catalog.track(name)
        title = os.path.basename(name)
        artist = ""
        if row is not None:
            title = row.get("title") or title
            artist = row.get("artist") or ""
        return {
            "name": os.path.basename(name),
            "relpath": name,
            "album": parts[0] if len(parts) > 1 else "",
            "title": title,
            "artist": artist,
        }

    def send_latency(self, ip, ms):
        """Tell a client to set its output-latency offset (ms) right now."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(2)
            sock.sendto(C.make_packet(C.TYPE_LATENCY, ms=ms),
                        (ip, self.port))
            sock.close()
        except OSError as e:
            print(f"[server] send_latency {ip}: {e}")

    def udp_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("", self.port))
        except OSError as e:
            print(f"[server] cannot bind UDP {self.port}: {e}")
            return
        bcast = ("255.255.255.255", self.port)
        print(f"[server] UDP sync on port {self.port}")
        last_bcast, last_state = 0.0, 0.0

        while not self._stop.is_set():
            t = C.ts()
            if t - last_bcast >= C.SYNC_INTERVAL:
                sock.sendto(C.make_packet(C.TYPE_SYNC, **self._sync_payload()), bcast)
                last_bcast = t
            if t - last_state >= C.STATE_INTERVAL:
                sock.sendto(C.make_packet(C.TYPE_STATE, **self._state_payload()), bcast)
                last_state = t
            # handle incoming (NTP requests, registers)
            sock.settimeout(C.SYNC_INTERVAL / 2)
            try:
                data, addr = sock.recvfrom(2048)
                p = C.parse_packet(data)
                if not p:
                    continue
                ptype, body = p
                if ptype == C.TYPE_NTP_REQ:
                    # Any valid client packet is a heartbeat: refresh the
                    # room's last-seen (and (re)create the row for a room still
                    # running from before the server restarted) so the web UI's
                    # online status stays accurate between registers.
                    self.catalog.upsert_client(addr[0], "")
                    t2 = C.ts()
                    resp = C.make_packet(C.TYPE_NTP_RESP,
                                         t1=body["t1"], t2=t2, t3=C.ts())
                    sock.sendto(resp, addr)
                elif ptype == C.TYPE_REGISTER:
                    # Record the room (IP + best-effort hostname) so the web
                    # UI can list it and store per-room latency settings. The
                    # packet also carries the room's current sync error (the
                    # same `err` the client prints) for the Configure tab.
                    ip = addr[0]
                    hostname = str(body.get("host") or "").strip()[:64]
                    err = None
                    raw_err = body.get("err_ms")
                    if raw_err is not None:
                        try:
                            err = float(raw_err)
                        except (TypeError, ValueError):
                            err = None
                    self.catalog.upsert_client(ip, hostname, err_ms=err)
                    sock.sendto(C.make_packet(C.TYPE_WELCOME,
                                              **self._sync_payload()), addr)
                    # Push this room's stored output-latency offset (0 if
                    # never set) so a freshly-connected client starts tuned.
                    lat = self.catalog.client_latency(ip)
                    sock.sendto(C.make_packet(C.TYPE_LATENCY, ms=lat), addr)
            except socket.timeout:
                continue
        sock.close()


WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


def build_handler(srv, music_dir, web_dir=WEB_DIR):
    """Build the HTTP request handler class for the msync server.

    Routes:
      GET  /                  -> web UI (index.html)
      GET  /static/<file>     -> web UI assets (css/js)
      GET  /api/state         -> full server state + queue
      GET  /api/library       -> list of available songs
      GET  /api/queue         -> queue + now playing
      POST /api/queue/add     -> add song(s) to queue  (?name= or JSON)
      POST /api/queue/move    -> reorder (?from=N&to=M or JSON)
      POST /api/queue/remove  -> remove by index (?index=N) or name
      POST /api/queue/clear   -> empty the queue
      POST /api/control/toggle|stop|next|prev|volume
      everything else         -> music file download (client fetches songs)
    """

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=music_dir, **kw)

        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode())
            except Exception:
                return {}

        def _send_static(self, relpath):
            root = os.path.realpath(web_dir)
            target = os.path.realpath(os.path.join(root, relpath))
            if not target.startswith(root + os.sep) or not os.path.isfile(target):
                self.send_error(404, "not found")
                return
            ctype = "text/html" if target.endswith(".html") else \
                    "text/css" if target.endswith(".css") else \
                    "application/javascript" if target.endswith(".js") else \
                    "application/octet-stream"
            body = open(target, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path in ("/", "/index.html"):
                self._send_static("index.html")
            elif path.startswith("/static/"):
                self._send_static(path[len("/static/"):])
            elif path == "/api/state":
                # catalog_scan is HTTP-only: the UDP broadcast path shares
                # _state_payload() and shouldn't carry UI-progress chatter.
                payload = srv._state_payload()
                payload["catalog_scan"] = srv.catalog.scan_progress()
                self._json(payload)
            elif path == "/api/library":
                self._json({"songs": srv.library_list(),
                            "albums": srv.album_list_full(),
                            "singles": srv.catalog.singles()})
            elif path == "/api/albums":
                self._json({"albums": srv.album_list_full(),
                            "singles": srv.catalog.singles()})
            elif path.startswith("/api/albums/"):
                album = path[len("/api/albums/"):].removesuffix("/tracks")
                album = urllib.parse.unquote(album)
                rels = srv.album_tracks(album)
                if not rels:
                    self._json({"error": "album not found"}, 404)
                else:
                    self._json({"album": album, "tracks": rels})
            elif path == "/api/queue":
                self._json({"queue": srv.queue_list(),
                            "now_playing": srv.song.name if srv.song else ""})
            elif path == "/api/clients":
                # Forget rooms unseen for config.CLIENT_STALE_AFTER so the
                # Configure tab only ever shows rooms that are still around
                # (the periodic monitor-loop sweep does the same cleanup).
                srv._prune_clients()
                self._json({"clients": srv.catalog.list_clients()})
            else:
                super().do_GET()

        def do_DELETE(self):
            path = urllib.parse.urlparse(self.path).path
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            body = self._read_body() or {}
            if path == "/api/queue/remove":
                index = qs.get("index")
                name = (body.get("name") if "name" in body
                        else (qs.get("name") or [None])[0])
                removed = srv.remove_from_queue(
                    index=int(index[0]) if index else None,
                    name=name)
                self._json({"removed": removed},
                           200 if removed is not None else 404)
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            body = self._read_body() or {}

            if path == "/api/queue/add":
                names = (body.get("names") or
                         ([body["name"]] if body.get("name") else []) or
                         qs.get("names") or qs.get("name") or [])
                if isinstance(names, str):
                    names = [names]
                added = srv.add_to_queue(names)
                self._json({"added": added, "queue_size": len(srv.queue_list())})
            elif path == "/api/queue/add-album":
                name = body.get("album") if "album" in body \
                    else (qs.get("album") or qs.get("name") or [None])[0]
                if not name:
                    self._json({"error": "album required"}, 400)
                else:
                    added = srv.add_album(name)
                    if not added:
                        self._json({"error": f"album not found: {name}"}, 404)
                    else:
                        self._json({"added": added,
                                    "queue_size": len(srv.queue_list())})
            elif path == "/api/queue/remove":
                index = qs.get("index")
                name = (body.get("name") if "name" in body
                        else (qs.get("name") or [None])[0])
                removed = srv.remove_from_queue(
                    index=int(index[0]) if index else None,
                    name=name)
                self._json({"removed": removed},
                           200 if removed is not None else 404)
            elif path == "/api/queue/clear":
                n = srv.clear_queue()
                self._json({"removed": n, "queue_size": 0})
            elif path == "/api/queue/move":
                from_i = (body.get("from") if body.get("from") is not None
                          else (qs.get("from") or [None])[0])
                to_i = (body.get("to") if body.get("to") is not None
                        else (qs.get("to") or [None])[0])
                try:
                    from_i, to_i = int(from_i), int(to_i)
                except (TypeError, ValueError):
                    self._json({"error": "from and to (integer) required"}, 400)
                else:
                    moved = srv.move_queue_item(from_i, to_i)
                    self._json({"moved": moved},
                               200 if moved else 400)
            elif path == "/api/control/toggle":
                if "state" in qs:
                    want = qs["state"][0] in ("1", "true", "yes")
                    result = srv.playing if want == srv.playing \
                        else srv.play_pause()
                else:
                    result = srv.play_pause()
                self._json({"playing": result})
            elif path == "/api/control/stop":
                self._json({"playing": srv.stop_playback()})
            elif path == "/api/control/play":
                name = body.get("name") if "name" in body \
                    else (qs.get("name") or [None])[0]
                album = (qs.get("album") or [None])[0]
                target = name or album
                if not target:
                    self._json({"error": "name required"}, 400)
                else:
                    played = srv.play_song(target)
                    if played is None:
                        self._json({"error": f"not found: {target}"}, 404)
                    else:
                        # play_song returns a dict for albums, string for singles
                        if isinstance(played, dict):
                            self._json({"now_playing": played["file"],
                                        "album": played["album"],
                                        "track_count": played["track_count"]})
                        else:
                            self._json({"now_playing": played})
            elif path == "/api/control/next":
                self._json(srv.next())
            elif path == "/api/control/prev":
                self._json(srv.prev())
            elif path == "/api/control/volume":
                level = None
                if "level" in qs:
                    try:
                        level = float(qs["level"][0])
                    except ValueError:
                        level = None
                elif "level" in body:
                    level = float(body["level"])
                if level is None:
                    self._json({"error": "level required"}, 400)
                else:
                    self._json({"volume": srv.set_vol(level)})
            elif path == "/api/clients/latency":
                ip = (qs.get("ip") or [None])[0] or body.get("ip")
                raw = qs.get("ms") or (["%s" % body["ms"]] if "ms" in body else [])
                if not ip or not raw:
                    self._json({"error": "ip and ms required"}, 400)
                    return
                try:
                    ms = float(raw[0])
                except (ValueError, TypeError):
                    self._json({"error": "ms must be a number"}, 400)
                    return
                if not -1000.0 <= ms <= 1000.0:
                    self._json({"error": "ms out of range (-1000..1000)"}, 400)
                    return
                srv.catalog.set_client_latency(ip, ms)
                srv.send_latency(ip, ms)   # apply live to the client
                self._json({"ip": ip, "latency_ms": ms})
            else:
                self._json({"error": "not found"}, 404)

    return Handler


def main():
    C.setup_logging()
    C.tune_process()          # GIL handoff + process priority (best-effort)
    lg = C.logger()

    ap = argparse.ArgumentParser()
    ap.add_argument("music_dir", nargs="?",
                    help=f"music directory (default: {config.MUSIC_DIR})")
    ap.add_argument("--port", type=int, default=None,
                    help=f"UDP sync port (default: {config.DEFAULT_PORT})")
    ap.add_argument("--db", default=config.DB_PATH,
                    help=f"catalog SQLite database (default: {config.DB_PATH})")
    ap.add_argument("--headless", action="store_true",
                    help="run without a control console (no stdin loop)")
    args = ap.parse_args()

    music_dir = args.music_dir or config.MUSIC_DIR
    port = args.port if args.port is not None else config.DEFAULT_PORT

    lg.info("starting server (music dir: %s, sync port %d)", music_dir, port)
    srv = SyncedServer(music_dir, port, args.db)

    http_port = port + config.HTTP_PORT_OFFSET
    Handler = build_handler(srv, music_dir)

    httpd = ThreadingHTTPServer(("", http_port), Handler)
    lg.info("HTTP on port %d", http_port)
    lg.info("web UI: http://<server-ip>:%d/", http_port)
    if srv.queue_dir:
        lg.info("drop-to-queue folder: %s", srv.queue_dir)

    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    threading.Thread(target=srv.udp_loop, daemon=True).start()
    threading.Thread(target=srv.monitor_loop, daemon=True).start()
    # Start inotify watcher for the music directory
    watcher = MusicWatcher(music_dir, srv.catalog, srv._stop)
    watcher.start()

    print("\nServer controls: space=play/pause  n=next  p=prev  +/-=vol  q=quit")
    print("                 add <song...> | queue | clear | drop files in .queue/\n")
    headless = args.headless
    if headless:
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        srv.close_audio()
        print("bye")
        return
    try:
        while True:
            try:
                line = input("> ").strip()
            except EOFError:
                time.sleep(0.5)
                continue
            if not line:
                continue
            cmd, _, rest = line.partition(" ")
            cmd = cmd.lower()
            if cmd == "q":
                break
            elif cmd == " " or cmd == "space":
                print("  playing =", srv.play_pause())
            elif cmd == "n":
                print("  next:", srv.next())
            elif cmd == "p":
                print("  prev:", srv.prev())
            elif cmd == "+":
                print("  volume =", srv.set_vol(srv.volume + 0.1))
            elif cmd == "-":
                print("  volume =", srv.set_vol(srv.volume - 0.1))
            elif cmd == "queue":
                q = srv.queue_list()
                if not q:
                    print("  queue is empty")
                else:
                    for i, name in enumerate(q, 1):
                        print(f"  {i:2d}. {name}")
            elif cmd == "clear":
                print(f"  cleared {srv.clear_queue()}")
            elif cmd == "add" and rest:
                added = srv.add_to_queue([s.strip() for s in rest.split(",")])
                print("  added:", ", ".join(added))
            else:
                print("  ?:", line)
    except (EOFError, KeyboardInterrupt):
        pass
    srv.close_audio()
    print("bye")


if __name__ == "__main__":
    main()