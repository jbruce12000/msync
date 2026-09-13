#!/usr/bin/env python3
"""
msync_client.py - synced music client.

Listens for the server's UDP sync broadcasts, downloads the current track over
HTTP, and plays it locally aligned to the server's timeline.

Sync model:
  * The server reports a trusted "song_start" wall-clock time for the current
    song. Each client's goal is to have its local playhead equal to
    (now - song_start) at all times.
  * Clients estimate the server's clock via periodic NTP-style exchanges
    (offset + drift in ppm from least-squares on (t2 - t1) vs. round-trips).
  * Playback uses a PLL in the audio callback: target position is derived from
    the estimated server clock; a small pitch correction pulls the local
    playhead toward the target, click-free resampling of drift.

Each client keeps a crossfade/pause behavior mirrored from the server:
  - When server is paused, client pauses too.
  - When the server advances to a new song, client downloads and starts it.

Usage:
    msync_client.py [--server HOST] [--port N] [--player]
"""

import argparse
import http.client
import json
import os
import socket
import statistics
import tempfile
import threading
import time

import sounddevice as sd
import numpy as np
import miniaudio

import config
import msync_common as C
import pid_tune

# Bound the client's download cache: keep at most this many tracks on disk
# so a long-lived client doesn't grow the cache dir without limit.
CACHE_MAX_FILES = 40


class ClockSync:
    """NTP-style client<->server clock offset + drift estimation."""

    # Drift estimator buckets (see _recompute): the wall-clock difference
    # changes only at the *rate difference between two hardware clocks*
    # (tens of ppm, on a minutes timescale), so drift is estimated as the
    # median of the newest MED_BUCKET offset medians minus the median of the
    # earliest MED_BUCKET, divided by the time gap between their centroids.
    MED_BUCKET = 60     # one-second median-offset samples per drift bucket
    DRIFT_VIEW = 300    # max median history kept (seconds at 1 Hz cadence)

    def __init__(self, addr, port, window=20):
        self.addr = addr
        self.port = port
        self.window = window
        self.points = []          # list of (t_rt, offset)
        self.meds = []            # smoothed offset history: (t_rt, median)
        self.offset = 0.0
        self.drift = 0.0          # server clock faster than client (ppm)
        self.rtt = 0.0
        # Round trips slower than this are treated as corrupted (network
        # queueing/jitter) and skipped rather than feeding garbage to the
        # offset estimator. 3 s was far too loose: a one-way stall of even a
        # few hundred ms injects up to |(d_f - d_b)/2| hundreds of ms of
        # offset error while RTT stays under the old gate, and one such
        # sample at a window edge blows the LSQ slope out to tens of
        # thousands of ppm (the +-28k ppm clock spikes). LAN RTT here
        # measures 0.3-12 ms (p99 ~22 ms), so 0.10 s is 4-9x headroom and
        # caps any accepted asymmetry at +-50 ms of offset error.
        self.rtt_limit = 0.10

    def exchange(self, sock):
        """One NTP exchange; returns True if updated."""
        t1 = C.ts()
        try:
            sock.sendto(C.make_packet(C.TYPE_NTP_REQ, t1=t1),
                        (self.addr, self.port))
        except OSError:
            # Transient network drop (interface down mid-run): don't crash,
            # just skip this round trip; the loop keeps retrying.
            return False
        sock.settimeout(1.0)
        try:
            data, _ = sock.recvfrom(2048)
        except socket.timeout:
            return False
        t4 = C.ts()
        p = C.parse_packet(data)
        if not p:
            return False
        ptype, body = p
        if ptype != C.TYPE_NTP_RESP:
            return False
        t1_, t2, t3 = body.get("t1"), body.get("t2"), body.get("t3")
        if None in (t1_, t2, t3):
            return False          # malformed response: ignore, don't crash
        # clock offset (client viewpoint): server_time = client_time + offset
        self.rtt = (t4 - t1_) - (t3 - t2)
        # Reject grossly-corrupted round trips (long queues/jitter, etc.).
        # They would otherwise poison the offset/drift estimate.
        if self.rtt > self.rtt_limit:
            return False
        offset = ((t2 - t1_) + (t3 - t4)) / 2.0
        self.points.append((t4, offset))
        if len(self.points) > self.window:
            self.points.pop(0)
        self._recompute()
        return True

    def _recompute(self):
        """Refit offset (median) and drift (two-bucket median)."""
        if len(self.points) < 2:
            self.offset = self.points[-1][1] if self.points else 0.0
            self.drift = 0.0
            return
        offs = [p[1] for p in self.points]
        # Robust offset: median of recent samples (a slow constant; the
        # residual rate difference is drift, applied separately by the PLL's
        # feedforward). The median keeps one bad round trip from yanking the
        # playhead, unlike a least-squares intercept.
        self.offset = statistics.median(offs)
        # Drift as the rate of the wall-clock difference, estimated robustly.
        # A 1 Hz OLS slope on raw offset samples amplifies one corrupted
        # round trip into a thousands-of-ppm spike (the +-28k ppm clock
        # jitter). Instead, average hard before differentiating: record the
        # per-sample median offset and diff two wide median buckets. Each
        # bucket median is ~0.5 ms stable, so over the ~4 min centroid gap
        # the drift estimate jitters only a couple ppm and is immune to any
        # corruption short of a 50%-of-bucket outage.
        self.meds.append((self.points[-1][0], self.offset))
        if len(self.meds) > self.DRIFT_VIEW:
            self.meds = self.meds[-self.DRIFT_VIEW:]
        if len(self.meds) >= 2 * self.MED_BUCKET:
            new_b = self.meds[-self.MED_BUCKET:]
            old_b = self.meds[:self.MED_BUCKET]
            new = statistics.median(o for _, o in new_b)
            old = statistics.median(o for _, o in old_b)
            t_new = sum(t for t, _ in new_b) / self.MED_BUCKET
            t_old = sum(t for t, _ in old_b) / self.MED_BUCKET
            dt = t_new - t_old
            if dt > 0.0:
                self.drift = (new - old) / dt * 1e6

    def server_now(self):
        """Estimated current server wall-clock time."""
        return C.ts() + self.offset


class SongBuffer:
    """Decoded current song data, downloaded from server via HTTP.

    Decoded audio is stored on disk as a raw float32 binary file and read
    back via numpy.memmap.  On machines with plenty of RAM the OS keeps the
    pages resident (zero extra I/O); on memory-constrained hosts (e.g. a
    Raspberry Pi Zero with 512 MB) the kernel pages unused regions out to
    swap/SD-card, letting the client run without fitting the whole decoded
    song in physical memory.
    """

    def __init__(self, cache_dir):
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        self.name = None
        self.data = None
        self.sr = 44100
        self.nchannels = 2
        self.duration = 0.0
        self._decoded_path = None   # on-disk float32 file behind self.data

    def close(self):
        """Release the memmap and remove the decoded temp file (if any)."""
        self._release_data()
        self.name = None
        self.data = None
        self.duration = 0.0
        self._decoded_path = None

    def _release_data(self):
        """Flush and close the current memmap, then delete its temp file."""
        if self.data is not None and hasattr(self.data, "flush"):
            try:
                self.data.flush()
            except Exception:
                pass
            del self.data
            self.data = None
        if self._decoded_path is not None:
            try:
                os.remove(self._decoded_path)
            except OSError:
                pass
            self._decoded_path = None

    def load(self, host, port, name, duration=None):
        if name == self.name and self.data is not None:
            return True
        # Release any previous decoded data before loading a new song
        self._release_data()
        # Try cache first (skip the HTTP round-trip)
        path = os.path.join(self.cache_dir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.isfile(path):
            try:
                self._decode(path)
                self.name = name
                self._prune_cache(keep=path)
                return True
            except Exception:
                pass
        # Download via HTTP
        url_host = host
        http_port = port + config.HTTP_PORT_OFFSET
        conn = http.client.HTTPConnection(url_host, http_port, timeout=15)
        try:
            conn.request("GET", "/" + urllib_quote(name))
            resp = conn.getresponse()
            if resp.status != 200:
                print(f"[client] HTTP {resp.status} fetching {name}")
                return False
            with open(path, "wb") as f:
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
        except Exception as e:
            print(f"[client] download failed: {e}")
            return False
        finally:
            conn.close()
        if not self._decode(path):
            return False
        self.name = name
        self._prune_cache(keep=path)
        return True

    def _prune_cache(self, keep=None):
        """Bound the download cache to CACHE_MAX_FILES tracks, deleting the
        oldest (by mtime) when over. ``keep`` is the just-downloaded file,
        which is spared even under clock skew.  Files whose name ends with
        ``.decoded`` are memmap temporaries that should have been cleaned up
        by ``_release_data``; any strays are also removed.  Failures are
        ignored: this is a cache, not a requirement."""
        try:
            entries = []
            for root, _dirs, files in os.walk(self.cache_dir):
                for fn in files:
                    p = os.path.join(root, fn)
                    # Always remove orphaned .decoded temp files
                    if fn.endswith(".decoded"):
                        try:
                            os.remove(p)
                        except OSError:
                            pass
                        continue
                    if p == keep:
                        continue
                    try:
                        entries.append((os.path.getmtime(p), os.path.getsize(p), p))
                    except OSError:
                        pass
            if len(entries) > CACHE_MAX_FILES:
                entries.sort(key=lambda e: (e[0], e[1]))
                for _mtime, _size, p in entries[:len(entries) - CACHE_MAX_FILES]:
                    try:
                        os.remove(p)
                        # Also remove its companion .decoded if present
                        try:
                            os.remove(p + ".decoded")
                        except OSError:
                            pass
                    except OSError:
                        pass
        except OSError:
            pass

    def _decode(self, path):
        """Decode *path* to a disk-backed memmap of stereo float32 samples.

        The decoded samples are written to a ``.decoded`` file next to the
        source and memory-mapped read-only.  This keeps the decoded audio
        on disk and lets the OS page it in/out on demand — critical on
        memory-constrained hosts like the Pi Zero — while appearing as a
        normal numpy array to the audio callback.
        """
        decoded = miniaudio.decode_file(path, output_format=miniaudio.SampleFormat.FLOAT32)
        self.sr = decoded.sample_rate
        # Normalize to true stereo (N, 2) float32. Monaural sources are
        # broadcast to both channels; >2ch sources keep the first two.
        frames = np.frombuffer(decoded.samples, dtype=np.float32)
        if decoded.nchannels == 2:
            stereo = frames.reshape(-1, 2)
        elif decoded.nchannels == 1:
            stereo = np.repeat(frames[:, None], 2, axis=1)
        else:
            f = frames.reshape(-1, decoded.nchannels)
            stereo = f[:, :2].copy() if decoded.nchannels > 2 else f

        # Write the decoded stereo float32 to a disk file and memmap it so
        # the OS can page regions out of physical RAM on memory-constrained
        # hosts.  On machines with headroom the pages stay resident (zero
        # extra I/O beyond the initial decode+write).
        decoded_path = path + ".decoded"
        with open(decoded_path, "wb") as f:
            f.write(stereo.tobytes())
        del stereo          # free the in-memory copy immediately

        self.data = np.memmap(decoded_path, dtype=np.float32, mode="r")
        # Reshape back to (N, 2) stereo — memmap supports this without
        # copying; the view is backed by the same disk pages.
        nframes = len(self.data) // 2
        self.data = self.data.reshape(nframes, 2)
        self._decoded_path = decoded_path

        self.nchannels = 2
        self.duration = nframes / self.sr
        # self.name is set by the caller (SongBuffer.load) to the relpath
        # so don't overwrite it here; just print the basename.
        print(f"[client] loaded {os.path.basename(path)} "
              f"({self.duration:.1f}s, stereo, memmap)")
        return True


def urllib_quote(name):
    import urllib.parse
    return urllib.parse.quote(name)


class SyncClient:
    def __init__(self, host, port, cache_dir, provisional=False):
        self.host = host
        self.port = port
        # True when `host` was chosen by broadcast discovery (or its loopback
        # fallback) rather than an explicit --server flag. A provisional host
        # may be stale (e.g. discovery raced a network blip and fell back to
        # 127.0.0.1), so such a client re-runs discovery if it stops hearing
        # the server, and retargets itself without a restart.
        self._provisional = provisional
        self.clock = ClockSync(host, port)
        self.buffer = SongBuffer(cache_dir)

        # current playback state
        self.lock = threading.RLock()
        self.playing = False
        self.server_song_start = 0.0   # server wall time song started
        self._epoch = 0                # server's track-start counter (in SYNC)
        # Fresh-track-start quorum. When a new track (or a restart) starts,
        # every connected room casts an "offset vote" for the server's round;
        # the server closes the round with the consensus offset (median of the
        # voters' NTP estimates) and broadcasts it as TYPE_START_ACK. Each room
        # then lays the playhead onto the server timeline with that SAME
        # consensus offset, so the song opening is ONE shared start instead of
        # each room's own estimate — bang-bang (and the PLL generally) sees a
        # single small bias on the first blocks instead of per-room jitter.
        self._consensus_offset_ms = None   # quorum offset (ms) for the epoch
        self._consensus_for_epoch = None   # epoch that offset belongs to
        self._vote_round_epoch = None      # epoch whose round we are voting in
        self._vote_until = 0.0             # when our vote round closes (local)
        self._last_vote = 0.0              # last time we cast a vote
        self.local_pos = 0.0           # local playhead (seconds)
        # Anchor for the err / PLL reference. `err` must never be computed by
        # comparing `local_pos` against a *fresh* `server_now() - song_start`
        # every block: NTP re-estimation shifts `server_now()` between blocks,
        # so a playhead that is perfectly locked to the server still reads a
        # large spurious error right after a rebase (worst right after a song
        # download thrashes the clock estimate). Instead we pin the reference
        # once at each rebase (adopt/resume/seek) and track it with the local
        # clock, so NTP steps cancel out of the error the PLL chases.
        self._base_clock = C.ts()      # client wall time at last rebase
        self._base_pos = 0.0           # target position at that rebase
        self.drift_pitch = 0.0         # current applied pitch from PLL
        self._pitch_int = 0.0          # PI integrator state (tight clip + fast unwind)
        self._mode = "idle"            # controller mode: BANG / PID / PI / idle
        self._pid = pid_tune.compute_gains()   # critical PID gains, computed
                                               # once at start-up (test mode)
        self._pid_int = 0.0            # PID integral state
        self._pid_prev = 0.0           # previous smoothed error for the D term
        self.err_f = 0.0               # low-passed PLL error (EMA of callback err)
        self.err_smooth = 0.0          # rolling avg of callback-boundary error
        self.queue = []                # server queue (names), from TYPE_STATE
        self.queue_size = 0            # from TYPE_SYNC (cheap field)

        # Next-track prefetch: the following queued song is downloaded +
        # decoded in a background thread right after each adoption, so a
        # normal auto-advance can swap it in with zero HTTP/decode latency.
        # Prediction is best-effort (server TYPE_STATE queue); a miss falls
        # back to the on-demand download path.
        self._prefetch = None          # ready decoded buffer for the next track
        self._prefetching = None       # name currently being prefetched (if any)
        self._prefetch_warmed = False  # front pages of _prefetch warmed for swap

        self.stream = None
        self.chunk_size = 2048
        self._stop = threading.Event()   # set to leave the run() loop
        self._loading_name = None        # track currently being downloaded
        self._fade_out = False           # set by close_audio -> callback fades
        # Click-free state transitions. _prev_playing lets the callback emit
        # one last faded block when the server pauses (instead of snapping
        # from full program level to silence) and fade the first block back in
        # on resume; _last_buffer_name flags the first block of a new track
        # for a fade-in; _gain_env ramps server-pushed volume/mute changes
        # instead of stepping the gain mid-sample.
        self._prev_playing = False
        self._last_buffer_name = None
        # When an anchor (adopt/resume/seek) calls _rebase() from the run-loop
        # thread, the base is pinned at an arbitrary wall instant BETWEEN
        # PortAudio blocks. local_pos only steps once per block, so the true
        # server timeline pulls ahead of the frozen playhead by up to one full
        # block (~186ms at 8192 frames/44.1k) before the next callback. The
        # callback advances the playhead and reference forward by that gap at
        # the next block boundary (see _realign_on_next_cb); this flag says
        # that advance is pending.
        self._realign_on_next_cb = False
        self._gain_env = 1.0
        # If this room's audio path (HDMI -> TV/AVR, ...) delays the output
        # by OUTPUT_LATENCY_MS, play that far ahead of the synced playhead
        # so the sound reaching the listeners lines up with the server room.
        # Tune live from the console with: latency <ms>
        self._out_latency = config.OUTPUT_LATENCY_MS / 1000.0
        self._out_latency_ms = config.OUTPUT_LATENCY_MS  # for display
        # Per-room volume/mute, pushed from the server's Configure tab via
        # TYPE_VOLUME (and persisted server-side per room IP).
        self._volume = 1.0               # gain 0.0..1.5
        self._muted = False

    # ------------------------------------------------------------------ #
    # HTTP API to the server (state, library, queue, playback control)    #
    # ------------------------------------------------------------------ #
    def _sendto(self, sock, payload, addr):
        """UDP send that never crashes the run loop. A transient network
        drop (interface down, ENOBUFS) must not kill the process — the
        client should keep listening and retry, not exit."""
        try:
            sock.sendto(payload, addr)
            return True
        except OSError:
            return False

    def register(self, sock):
        """Send a REGISTER heartbeat (best-effort). Keeps the web UI's room
        list + online status fresh even across a server restart."""
        return self._sendto(
            sock, C.make_packet(C.TYPE_REGISTER,
                                host=socket.gethostname(),
                                err_ms=round(self.err_smooth * 1000, 1)),
            (self.host, self.port))

    def _api(self, method, path, params=None, body=None):
        import urllib.parse as up
        if params:
            path += "?" + up.urlencode(params)
        conn = http.client.HTTPConnection(
            self.host, self.port + config.HTTP_PORT_OFFSET, timeout=5)
        try:
            payload = json.dumps(body).encode() if body is not None else None
            headers = {"Content-Type": "application/json"} if payload else {}
            conn.request(method, path, body=payload, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            return json.loads(raw.decode()) if raw else {}
        except Exception as e:
            print(f"[client] API {method} {path} failed: {e}")
            return None
        finally:
            conn.close()

    def fetch_tracks(self):
        """Names of every track stored on the server."""
        d = self._api("GET", "/api/library")
        return d.get("songs", []) if d else []

    def select_track(self, name):
        """Tell the server to play ``name`` right now (all clients follow)."""
        d = self._api("POST", "/api/control/play", params={"name": name})
        return (d or {}).get("now_playing")

    def queue_add(self, name):
        """Ask the server to add ``name`` to the play queue."""
        d = self._api("POST", "/api/queue/add", params={"name": name})
        return (d or {}).get("added", [])

    def queue_clear(self):
        d = self._api("POST", "/api/queue/clear")
        return (d or {}).get("removed")

    def pause(self):
        return (self._api("POST", "/api/control/toggle",
                          params={"state": 0}) or {}).get("playing")

    def resume(self):
        return (self._api("POST", "/api/control/toggle",
                          params={"state": 1}) or {}).get("playing")

    def next_song(self):
        d = self._api("POST", "/api/control/next")
        return (d or {}).get("file")

    def prev_song(self):
        return self._api("POST", "/api/control/prev")

    def set_volume(self, level):
        d = self._api("POST", "/api/control/volume", params={"level": level})
        return (d or {}).get("volume")

    def fetch_albums(self):
        d = self._api("GET", "/api/albums")
        return (d or {}).get("albums", [])

    def select_album(self, album):
        d = self._api("POST", "/api/control/play", params={"album": album})
        return (d or {}).get("now_playing")

    def queue_album(self, album):
        d = self._api("POST", "/api/queue/add-album", params={"album": album})
        return (d or {}).get("added", [])

    # ------------------------------------------------------------------ #
    # Playhead advance + drift correction in callback                     #
    # ------------------------------------------------------------------ #
    def _apply_gain(self, out):
        """Apply this room's volume/mute (server-pushed) with a short linear
        ramp whenever the gain level changes. A mute toggle or a volume move
        is a hard step in the waveform if applied instantly (a click); ramping
        over a few ms makes it inaudible."""
        tg = 0.0 if self._muted else self._volume
        current = self._gain_env
        if tg == current:
            out *= current
            return
        n = min(out.shape[0], int(C.AUDIO_FADE_SEC * self.buffer.sr) or 1)
        if n < out.shape[0]:
            out[:n] *= np.linspace(current, tg, n, dtype=np.float32)[:, None]
            out[n:] *= tg
        else:
            out *= tg
        self._gain_env = tg

    # ------------------------------------------------------------------ #
    def _cb(self, outdata, frames, time_info, status):
        C.boost_audio_thread()
        with self.lock:
            buf = self.buffer
            if buf.data is None:
                self._prev_playing = False
                self._last_buffer_name = None
                outdata.fill(0)
                return
            sr = buf.sr
            n_fade = min(frames, int(C.AUDIO_FADE_SEC * sr)) if sr else 0
            pl = self.playing
            # Pause: emit one last short block faded to silence, so the
            # speakers don't snap from full program level to zeros between
            # callbacks. The stream stays open; from here on we fill silence.
            if not pl and self._prev_playing:
                idx = max(0.0, (self.local_pos + self._out_latency) * sr)
                n = len(buf.data)
                i0 = int(idx)
                i1 = min(i0 + frames, n)
                last = np.zeros((frames, 2), dtype=np.float32)
                if i0 < n:
                    last[: i1 - i0] = buf.data[i0:i1]
                self._apply_gain(last)
                if n_fade > 0:
                    last[-n_fade:] *= np.linspace(1.0, 0.0, n_fade,
                                                  dtype=np.float32)[:, None]
                outdata[:] = last
                self._prev_playing = False
                return
            if not pl:
                outdata.fill(0)
                return
            # Resume / fresh start: fade the first audible block back in.
            if not self._prev_playing:
                self._prev_playing = True
                resume_fade = True
            else:
                resume_fade = False
            # A rebase from the run-loop thread (track adopt / resume / seek) pinned
            # the reference at an arbitrary instant BETWEEN PortAudio blocks,
            # and local_pos can only advance inside a block — so since that
            # anchor, the true server timeline has pulled ahead of the frozen
            # playhead by up to one full block (~186ms at 8192 frames/44.1k).
            # Snap BOTH the playhead and the reference forward by that gap at
            # this block edge: the block then reads the CURRENT track content
            # and err starts ~0, instead of a phantom lag that bang-bang rails
            # to MAX_PITCH and catchup grinds away over the first seconds of
            # the song (the start-of-song 100ms+ spike / click). This also
            # absorbs a long adopt gap (e.g. an on-demand download+decode):
            # the timeline kept running, so the playhead catches it in one
            # block instead of chasing for a minute.
            if self._realign_on_next_cb:
                self._realign_on_next_cb = False
                dt = ((C.ts() - self._base_clock)
                      * (1.0 + self.clock.drift * 1e-6))
                self.local_pos += dt
                self._base_pos += dt
                self._base_clock = C.ts()
            # Target position: pinned to the last rebase and advanced by the
            # local clock at the (NTP-measured) server clock rate, NOT
            # re-derived from a fresh `server_now() - song_start`. This bakes
            # the current NTP *offset* in once; later offset re-estimation
            # steps then move the reference without creating a fake `err`
            # (the culprit behind the 100ms+ spike seen on 10.0.0.4 right
            # after a song switch). `clock.drift` is a smooth regression
            # slope (not a jumpy median like `offset`), so using it live keeps
            # the target tracking the true server rate and the PLL's own
            # feedforward cancels out of the error dynamics.
            target = max(0.0, self._base_pos
                         + (1.0 + self.clock.drift * 1e-6)
                         * (C.ts() - self._base_clock))
            err = target - self.local_pos
            # Low-pass the error for the rate controller: the raw error
            # carries tens of ms of NTP reference jitter plus a half-block
            # sawtooth from local_pos stepping once per block. Fed straight
            # into the PI, that noise saturates the ±0.2% pitch clamp on
            # nearly every block and the actuator limit-cycles at ±MAX_PITCH
            # (an audible slow speed wobble). Drive the loop from the
            # smoothed error instead.
            self.err_f += C.PLL_ALPHA * (err - self.err_f)
            ef = self.err_f
            # Bounded fast re-alignment: right after a pause/resume, a busy
            # server stall, or a seek, a rate-only PLL capped at MAX_PITCH
            # would take tens of seconds to close a large gap. Nudge the
            # playhead toward the target by a few ms per block instead (a
            # sub-block skip/repeat that is effectively inaudible). Gated on
            # the smoothed error so reference jitter can't trip it.
            if abs(ef) > C.CATCHUP_THRESHOLD:
                self.local_pos += C.CATCHUP_STEP if ef > 0 else -C.CATCHUP_STEP
                err = target - self.local_pos
            if config.BANG_BANG:
                # PID test mode. OUTSIDE the ±window: apply FULL pitch power
                # (±MAX_PITCH = ±2000ppm) in the direction of the error
                # (bang-bang); drop stale windup so it can't bleed into the
                # next in-window phase. INSIDE the window: run the host's critically-
                # damped PID, computed once at start-up (see pid_tune.py).
                if abs(ef) > config.BANG_BANG_WINDOW_MS / 1000.0:
                    self._mode = "BANG"
                    self._pitch_int = 0.0
                    self._pid_int = 0.0
                    self.drift_pitch = float(
                        -C.MAX_PITCH if ef < 0 else C.MAX_PITCH)
                else:
                    self._mode = "PID"
                    g = self._pid
                    dt = frames / buf.sr
                    self._pid_int = float(np.clip(
                        self._pid_int + ef * dt, -C.MAX_PITCH / g["ki"],
                        C.MAX_PITCH / g["ki"]))
                    d = g["kd"] * (ef - self._pid_prev) / dt
                    self._pid_prev = ef
                    self.drift_pitch = float(np.clip(
                        self.clock.drift * 1e-6
                        + g["kp"] * ef
                        + g["ki"] * self._pid_int
                        + d,
                        -C.MAX_PITCH, C.MAX_PITCH))
            else:
                self._mode = "PI"
                # PI drift controller on the smoothed error, with an audible
                # deadband: sub-DRIFT_HYSTERESIS errors are inaudible and (for
                # this box) mostly NTP-reference noise, so don't chase them —
                # chasing produces a ±MAX_PITCH wobble. Errors inside the deadband
                # only let the integrator unwind.
                ed = float(np.copysign(max(abs(ef) - C.DRIFT_HYSTERESIS, 0.0), ef))
                if ed:
                    self._pitch_int += np.clip(ed, -C.INT_LIMIT, C.INT_LIMIT)
                    # integral pulling against the error: unwind it now, not
                    # slowly over the next several seconds
                    if self._pitch_int * ef < 0.0:
                        self._pitch_int *= 0.5
                else:
                    self._pitch_int *= C.INT_UNWIND
                self._pitch_int = np.clip(self._pitch_int, -C.INT_LIMIT, C.INT_LIMIT)
                ff = self.clock.drift * 1e-6                  # s/s from NTP
                self.drift_pitch = float(np.clip(
                    ff + ed * C.PITCH_GAIN + self._pitch_int * C.PITCH_INT,
                    -C.MAX_PITCH, C.MAX_PITCH))
            rate = 1.0 + self.drift_pitch
            # Status metric: smooth the CALLBACK-boundary error (the quantity
            # the PLL chases). An instantaneous sample taken between
            # callbacks is meaningless — local_pos only steps once per block,
            # so it reads up to ±one block of sawtooth even when the music is
            # perfectly aligned.
            self.err_smooth = 0.9 * self.err_smooth + 0.1 * err

            # Output index is the synced playhead plus this room's output
            # latency: sound written now is heard OUTPUT_LATENCY_MS later,
            # so pull the samples from that much further ahead on the timeline.
            # (The PLL still chases the raw playhead; only what we emit is
            # offset, so sync/control logic is unchanged.)
            idx = max(0.0, (self.local_pos + self._out_latency) * sr)
            n = len(buf.data)
            if idx >= n:
                # This track's data is exhausted. Normally the server has
                # already moved on to the next track and we're still
                # downloading/decoding it, so output silence — but keep
                # advancing the playhead. A frozen playhead makes err grow
                # without bound for the entire download gap and hammers the
                # PLL to ±MAX_PITCH. Advancing keeps us locked to the server
                # timeline; the next track's adoption re-bases it exactly.
                outdata.fill(0)
                self.local_pos += frames / sr * rate
                return
            i0 = int(idx)
            i1 = min(int(idx + frames), n)
            out = np.zeros((frames, 2), dtype=np.float32)
            valid = i1 - i0
            out[:valid] = buf.data[i0:i1]
            # Last audible block of this track (the read window runs past the
            # end): fade the tail so the cut to download-gap silence is smooth
            # instead of clipping whatever note was playing.
            if valid and int(idx + frames) > n and n_fade:
                nf = min(valid, n_fade)
                out[valid - nf:valid] *= np.linspace(1.0, 0.0, nf,
                                                     dtype=np.float32)[:, None]
            # Per-room volume/mute (server-pushed), ramped on changes.
            self._apply_gain(out)
            # First block of a new (or restarted, sample-rate change) track:
            # fade its head so a loud song opening can't click straight out of
            # the previous track's tail or the download-gap silence.
            if buf.name != self._last_buffer_name:
                if n_fade:
                    out[:n_fade] *= np.linspace(0.0, 1.0, n_fade,
                                                dtype=np.float32)[:, None]
                self._last_buffer_name = buf.name
            if resume_fade and n_fade:
                out[:n_fade] *= np.linspace(0.0, 1.0, n_fade,
                                            dtype=np.float32)[:, None]
            outdata[:] = out
            if self._fade_out:
                nf = min(frames, int(0.15 * sr))
                if nf > 0:
                    outdata[-nf:] *= np.linspace(1.0, 0.0, nf,
                                                 dtype=np.float32)[:, None]
            # advance local playhead by physical samples * pitch correction
            self.local_pos += frames / sr * rate

    # ------------------------------------------------------------------ #
    def _ensure_stream(self):
        # (Re)create the output stream for the current song's sample rate,
        # always in stereo so clients play true stereo.
        with C.stream_ops_lock:
            if self.stream is not None and self.stream.samplerate == self.buffer.sr:
                return
            if self.stream is not None:
                try:
                    self.stream.stop()
                    self.stream.close()
                except Exception:
                    pass
                self.stream = None
            if not self.buffer.sr:
                return
            try:
                self.stream = sd.OutputStream(
                    samplerate=self.buffer.sr,
                    channels=2,
                    callback=self._cb,
                    blocksize=8192,
                    latency="low",
                )
                self.stream.start()
            except Exception as e:
                print(f"[client] audio stream error: {e}")

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
    def _note_epoch(self, epoch):
        """React to the server's track-start counter (part of every SYNC):
        when a new track (or restart) begins while we are connected, join its
        fresh-start-quorum round so _anchor_offset() can use the consensus.
        A client that first connects mid-song sees the current epoch without
        it being a start: it simply tracks it (its own NTP offset anchors the
        song, as before)."""
        with self.lock:
            if epoch == self._epoch:
                return
            if self._epoch != 0:
                self._vote_round_epoch = epoch
                self._vote_until = time.time() + C.START_ROUND_SEC
                self._last_vote = 0.0
            self._epoch = epoch
            # A consensus only applies to its own start: forget a stale one
            # until the server agrees this round.
            self._consensus_for_epoch = None
            self._consensus_offset_ms = None

    # ------------------------------------------------------------------ #
    def _anchor_offset(self, epoch):
        """The clock offset to use when laying the playhead onto the server
        timeline: the quorum consensus the server agreed at track start where
        one exists, else this room's own NTP estimate. Every room using the
        SAME consensus offset anchors a new song at the SAME content position,
        killing the per-room +-10ms+ start jitter that used to bang the pitch
        rail right on song open."""
        if (self._consensus_for_epoch == epoch
                and self._consensus_offset_ms is not None):
            return self._consensus_offset_ms / 1000.0
        return self.clock.offset

    # ------------------------------------------------------------------ #
    def _apply_start_ack(self, body):
        """Apply the server's quorum consensus for a track start: the agreed
        clock offset for `epoch`, used by _anchor_offset() on song open."""
        ack_epoch = body.get("epoch")
        ack_offset = body.get("offset_ms")
        if ack_epoch is None or ack_offset is None:
            return
        with self.lock:
            if ack_epoch == self._epoch:
                self._consensus_offset_ms = float(ack_offset)
                self._consensus_for_epoch = ack_epoch

    # ------------------------------------------------------------------ #
    def _maybe_vote(self, sock):
        """Cast our offset vote for the open track-start round (called every
        run() loop turn). The server keeps the last vote per room and closes
        the round with the consensus once a quorum has voted or its deadline
        passes. A dropped vote is harmless: the round closes by deadline or
        the room falls back to its own NTP estimate."""
        now = time.time()
        if self._vote_round_epoch is None or now >= self._vote_until:
            self._vote_round_epoch = None
            return
        if now - self._last_vote < C.START_VOTE_INTERVAL:
            return
        self._last_vote = now
        try:
            sock.sendto(C.make_packet(C.TYPE_START_VOTE,
                                      epoch=self._vote_round_epoch,
                                      offset_ms=round(self.clock.offset * 1000.0,
                                                     3)),
                        (self.host, self.port))
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    def _rebase(self):
        """Pin the PLL's target reference to the just-committed playhead.

        Called (under self.lock) whenever `local_pos` is re-anchored to the
        server timeline (new track, resume, seek). Snapshots the local wall
        clock so the callback tracks the target from the single reference
        point instead of re-deriving `server_now() - song_start` fresh every
        block — that re-derivation is what turns a live NTP offset
        re-estimation into a spurious 100ms+ err right after a song switch.
        The NTP feedforward (`clock.drift`) still drives the rate; only the
        error *reference* is frozen here.

        The pin is re-applied at the next audio block boundary (see
        `_realign_on_next_cb` in the callback): this method runs in the
        run-loop thread at an arbitrary wall instant between PortAudio
        blocks, and `local_pos` only steps once per block — so between the
        anchor and the next block the true server timeline has pulled ahead
        of the frozen playhead by up to one full block (~186ms at the
        8192-frame buffer). The callback advances BOTH the playhead and the
        reference by that gap, so the post-anchor song starts at the current
        content with err ~0 instead of a phantom lag that bang-bang rails to
        MAX_PITCH and catchup grinds away over the first seconds (the
        100ms+ start-of-song spike seen after a pause across a track change).
        """
        self._base_clock = C.ts()
        self._base_pos = self.local_pos
        self._realign_on_next_cb = True

    # ------------------------------------------------------------------ #
    def _apply_state(self, st):
        """Adopt the server's broadcast state.

        The audio callback (PortAudio thread) takes ``self.lock`` on every
        block, so this method must never do slow or blocking work while
        holding it, and must never call into PortAudio (stream.stop() waits
        for the in-flight callback, which wants this very lock) while holding
        it either. New-track downloads/decode happen OUTSIDE the lock so a
        busy server can't turn a slow HTTP fetch into a gap; the finished
        scratch buffer is swapped in atomically.
        """
        with self.lock:
            name = st.get("name")
            playing = st.get("playing", False)
            song_start = st.get("song_start", 0.0)
            duration = st.get("duration", 0.0)
            self.queue_size = st.get("queue_size", self.queue_size)
            # Fresh track start? The server bumps `epoch` on every new track
            # (and restart): join its start-quorum round so every room anchors
            # this song at the same consensus start time. The LOCAL `epoch`
            # stays bound to this state all the way down to the adopt site:
            # a newer sync arriving mid-download must not re-anchor this
            # track against the newer track's consensus.
            epoch = st.get("epoch", self._epoch)
            self._note_epoch(epoch)

            same_song = (not name or name == self.buffer.name)
            stream_cmd = None
            if same_song:
                # Track the authoritative start time (handles seeks) and
                # mirror play/pause. On resume, rebase local_pos to the
                # live song_start the server reports.
                seeked = (song_start != self.server_song_start
                          and self.server_song_start != 0.0)
                self.server_song_start = song_start
                if playing != self.playing:
                    self.playing = playing
                    if playing:
                        self.local_pos = max(
                            0.0, C.ts() + self._anchor_offset(epoch) - song_start)
                        self._rebase()
                    # A fresh rebase (or a long pause) invalidates the old
                    # integral: stale windup keeps drift_pitch pinned at
                    # ±MAX_PITCH for the wrong direction. Same for the
                    # smoothed PLL error — it must restart from the new
                    # reference, not decay toward it.
                    self._pitch_int = 0.0
                    self._pid_int = 0.0
                    self._pid_prev = 0.0
                    self.err_f = 0.0
                    # The stream STAYS OPEN through pauses — the callback
                    # just fills silence. Restarting PortAudio re-primes the
                    # device buffer, pushing this client audibly behind the
                    # (continuously-running) server by ~one buffer depth: a
                    # constant offset a rate-only PLL can only remove very
                    # slowly. Restart only if the stream somehow went away.
                    if playing and self.stream is not None \
                            and not self.stream.active:
                        stream_cmd = "start"
                elif seeked and self.playing:
                    # Server seeked within this track: re-anchor the playhead
                    # to the new song_start so this room doesn't keep playing
                    # the pre-seek position (a stale `_base_pos` would hold the
                    # err reference on the old timeline).
                    self.local_pos = max(
                        0.0, C.ts() + self._anchor_offset(epoch) - song_start)
                    self._rebase()
                    self._pitch_int = 0.0
                    self._pid_int = 0.0
                    self._pid_prev = 0.0
                    self.err_f = 0.0
                # Keep the NEXT track on deck even when this song hasn't
                # changed (steady play, a long pause, or just after resume).
                # The adopt path below also prefetches, but a same_song sync
                # is the only sync that runs while a room sits paused on the
                # last track of an old queue: if nothing re-predicts here, the
                # next track's prefetch goes stale and the post-pause switch
                # falls back to an on-demand download+decode on the audio
                # thread — the run-loop decode then starves the callback for
                # the whole transfer (that + the phase sawtooth is the
                # 100ms+ start-of-song error on the 2nd track after a pause).
                self._maybe_prefetch()
            elif self._loading_name == name or self._prefetching == name:
                return                # already fetching this track (download or prefetch)
            prev_name = self.buffer.name    # how we tell a newer state "took over"

        # Play/pause toggles are applied outside the lock: stream.stop()
        # waits for the in-flight callback, and that callback is blocked on
        # self.lock if we hold it here -> deadlock on pause.
        if stream_cmd is not None and self.stream:
            try:
                with C.stream_ops_lock:
                    if not self.stream.active:
                        self.stream.start()
            except Exception:
                pass
            return
        if same_song:
            return

        # New track: if the next track was prefetched, adopt it instantly —
        # no HTTP, no decode, warm pages. Otherwise download + decode into a
        # SCRATCH buffer WITHOUT holding self.lock, so the current track keeps
        # playing while the next one downloads (previously this froze the
        # music for the whole transfer).
        scratch = None
        with self.lock:
            if self._prefetch is not None and self._prefetch.name == name:
                scratch = self._prefetch
                self._prefetch = None
                # Only clear the in-flight marker if it refers to the track
                # we're adopting. A prefetch for a DIFFERENT track may still be
                # running (the ready one was predicted earlier, the newer pick
                # superseded it, then a manual next announced this one): wiping
                # the marker would make that worker discard its perfectly good
                # result when it finishes a moment later.
                if self._prefetching == name:
                    self._prefetching = None
                self._prefetch_warmed = False
        if scratch is None:
            scratch = SongBuffer(self.buffer.cache_dir)
            self._loading_name = name
            try:
                if not scratch.load(self.host, self.port, name, duration):
                    scratch.close()     # clean up any partial decoded file
                    return
            finally:
                # clear the in-flight marker (a false "loading" only ever skips a
                # redundant fetch, it never breaks playback)
                if self._loading_name == name:
                    self._loading_name = None
        with self.lock:
            # only adopt if no newer state swapped in a different track while
            # we were downloading (the buffer still holds `prev_name`, the
            # song we were already on)
            if self.buffer.name != prev_name:
                scratch.close()     # discard: a newer track superseded this one
                return
            self.server_song_start = song_start
            self.local_pos = max(
                0.0, C.ts() + self._anchor_offset(epoch) - song_start)
            self._rebase()
            self._pitch_int = 0.0          # fresh rebase: drop stale windup
            self._pid_int = 0.0            # ... and the PID integral state
            self._pid_prev = 0.0           # ... and the D-term memory
            self.err_f = 0.0               # ... and the smoothed PLL error
            self.playing = playing
            # Publish the new buffer LAST so the callback either sees the old
            # or the new track fully initialised, never a partial one.
            self.buffer = scratch
            toggle = None
            if self.stream and self.playing and not self.stream.active:
                toggle = "start"
        # Re-create the stream if the sample rate changed, and apply any
        # start, all OUTSIDE the lock (see note at top of this method).
        self._ensure_stream()
        if toggle is not None and self.stream:
            try:
                with C.stream_ops_lock:
                    if not self.stream.active:
                        self.stream.start()
            except Exception:
                pass
        # Get a head start on the track after this one so the next
        # auto-advance can be swapped in with zero HTTP/decode latency.
        self._maybe_prefetch()

    # ------------------------------------------------------------------ #
    def _maybe_prefetch(self):
        """Decode the next queued track in a background thread so a normal
        auto-advance can be swapped in with zero HTTP/decode latency on the
        critical path.

        Prediction is best-effort, from the server's TYPE_STATE queue
        (refreshed every STATE_INTERVAL): prepare the first queue entry that
        isn't the track currently playing, which also absorbs the ~1s of
        stale queue around a swap (STATE lags SYNC). Only one prefetch runs
        at a time; a miss (manual prev/next/pick) simply falls back to the
        on-demand download path. Callers never hold self.lock here.
        """
        with self.lock:
            if self._prefetching is not None:
                return                       # one is already in flight
            if not self.queue:
                return
            # Candidate: first queued track that isn't currently playing or
            # already prepared.
            name = next((q for q in self.queue if q != self.buffer.name),
                        None)
            if not name:
                return
            if self._prefetch is not None and self._prefetch.name == name:
                return                       # already prepared
            cache_dir = self.buffer.cache_dir
            host, port = self.host, self.port
            self._prefetching = name
        threading.Thread(target=self._prefetch_worker,
                         args=(host, port, name, cache_dir),
                         daemon=True).start()

    def _prefetch_worker(self, host, port, name, cache_dir):
        """Background fetch + decode of the predicted next track."""
        try:
            scratch = SongBuffer(cache_dir)
            if not scratch.load(host, port, name):
                scratch.close()              # partial / failed fetch
                with self.lock:
                    if self._prefetching == name:
                        self._prefetching = None
                return
            with self.lock:
                if self._prefetching != name:
                    scratch.close()          # superseded by an even newer pick
                    return
                if self.buffer.name == name:
                    # The track became current before the prefetch landed
                    # (the on-demand path already adopted it): drop the copy.
                    self._prefetching = None
                    scratch.close()
                    return
                old = self._prefetch
                self._prefetch = scratch
                self._prefetching = None
                self._prefetch_warmed = False   # a fresh buffer: warm again
                if old is not None:
                    old.close()
        except Exception as exc:
            with self.lock:
                if self._prefetching == name:
                    self._prefetching = None
            print(f"[client] prefetch failed for {name}: {exc!r}")

    def _warm_prefetch(self):
        """Touch the front pages of the prefetched buffer shortly before the
        current track ends, so the swap's first callbacks never stall on cold
        mmap reads. Best-effort and idempotent. The touch happens OUTSIDE the
        lock because a cold read can take tens of ms and must not delay the
        audio callback (which waits on the same lock every block)."""
        with self.lock:
            b = self.buffer
            p = self._prefetch
            if p is None or p.data is None or self._prefetch_warmed:
                return
            if b.duration <= 0:
                return
            # Same pinned reference the callback chases.
            pos = (self._base_pos
                   + (1.0 + self.clock.drift * 1e-6)
                   * (C.ts() - self._base_clock))
            if pos < b.duration - C.PREFETCH_PREWARM_LEAD_S:
                return
            data, sr = p.data, p.sr
            self._prefetch_warmed = True
        try:
            n = int(min(C.PREFETCH_PREWARM_S * sr, len(data)))
            if n > 0:
                _ = data[:n]                 # pull the front pages into cache
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Interactive console (select tracks, control playback via HTTP)      #
    # ------------------------------------------------------------------ #
    def _console(self):
        print("[client] console: tracks | play <name> | add <name> | "
              "pause | resume | next | prev | vol <0..1.5> | latency <ms> | "
              "queue | clear | q")
        while not self._stop.is_set():
            try:
                line = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                return
            if not line:
                continue
            cmd, _, rest = line.partition(" ")
            cmd = cmd.lower()
            try:
                if cmd in ("q", "quit", "exit"):
                    self._stop.set()
                elif cmd in ("tracks", "list"):
                    tracks = self.fetch_tracks()
                    if not tracks:
                        print("  (no tracks found on server)")
                    else:
                        cur = self.buffer.name
                        for i, t in enumerate(tracks, 1):
                            mark = "  <-- playing" if t == cur else ""
                            print(f"  {i:2d}. {t}{mark}")
                elif cmd == "albums":
                    albums = self.fetch_albums()
                    if not albums:
                        print("  (no albums on server)")
                    else:
                        for a in albums:
                            art = f" — {a['artist']}" if a.get("artist") else ""
                            print(f"  {a['album']}{art}"
                                  f" ({a['track_count']} tracks)")
                elif cmd == "play" and rest:
                    n = self.select_track(rest.strip())
                    print(f"  now playing: {n}" if n
                          else f"  not found on server: {rest}")
                elif cmd == "add" and rest:
                    print("  queued:", self.queue_add(rest.strip()))
                elif cmd == "pause":
                    print("  paused =", self.pause())
                elif cmd == "resume":
                    print("  playing =", self.resume())
                elif cmd == "next":
                    print("  next:", self.next_song())
                elif cmd == "prev":
                    self.prev_song()
                    print("  previous song")
                elif cmd == "vol" and rest:
                    try:
                        print("  volume =", self.set_volume(float(rest.split()[0])))
                    except ValueError:
                        print("  usage: vol <0.0..1.5>")
                elif cmd == "latency":
                    # live output-latency compensation for this room, in ms
                    try:
                        ms = float(rest.split()[0])
                    except (ValueError, IndexError):
                        print(f"  usage: latency <ms>  (current: "
                              f"{self._out_latency * 1000:.0f} ms)")
                    else:
                        with self.lock:
                            self._out_latency = ms / 1000.0
                        print(f"  output latency compensation: {ms:.0f} ms")
                        print("  (Use the web UI's Configure tab, or set "
                              "config.OUTPUT_LATENCY_MS, to make it permanent.")
                elif cmd == "queue":
                    print("  queue:", ", ".join(self.queue) if self.queue
                          else "(empty)")
                elif cmd == "clear":
                    print("  cleared", self.queue_clear(), "queued")
                else:
                    print("  ?:", line)
            except Exception as e:
                print(f"  error: {e}")

    # ------------------------------------------------------------------ #
    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("", self.port))
        except OSError as e:
            print(f"[client] cannot bind UDP {self.port}: {e}; retrying listen")
            return
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        print(f"[client] listening on UDP {self.port}")

        # register with server (send our hostname so the web UI can name us,
        # plus our current sync error so the Configure tab can display it)
        self.register(sock)

        self.clock.exchange(sock)
        last_ntp = time.time()
        last_state = time.time()
        last_status = time.time()
        last_reg = time.time()   # re-register periodically (heartbeat)
        last_probe = 0.0         # last re-discovery attempt (provisional hosts)

        last_warn = 0.0

        try:
            while not self._stop.is_set():
                sock.settimeout(1.0 if not self.playing else 0.1)
                try:
                    data, addr = sock.recvfrom(4096)
                    p = C.parse_packet(data)
                    if not p:
                        continue
                    ptype, body = p
                    if ptype == C.TYPE_SYNC:
                        self._apply_state(body)
                        last_state = time.time()
                    elif ptype == C.TYPE_STATE:
                        with self.lock:
                            self.queue = list(body.get("queue", []))
                            self.queue_size = len(self.queue)
                        # A queue change may invalidate the predicted next
                        # track (something was added/removed): re-arm.
                        self._maybe_prefetch()
                    elif ptype == C.TYPE_LATENCY:
                        # Server-set output-latency offset (ms) for this room.
                        try:
                            ms = float(body.get("ms", 0.0))
                        except (TypeError, ValueError):
                            ms = 0.0
                        with self.lock:
                            self._out_latency = ms / 1000.0
                            self._out_latency_ms = ms
                    elif ptype == C.TYPE_VOLUME:
                        # Server-set volume (0..1.5) and mute state for this
                        # room (Configure tab). Mute silences the output
                        # without touching the volume level so unmute restores
                        # it.
                        try:
                            vol = float(body.get("vol", 1.0))
                            muted = bool(body.get("muted", 0))
                        except (TypeError, ValueError):
                            vol, muted = 1.0, False
                        with self.lock:
                            self._volume = max(0.0, min(1.5, vol))
                            self._muted = muted
                    elif ptype == C.TYPE_START_ACK:
                        # Quorum consensus for the current track start: the
                        # agreed clock offset every room will anchor with.
                        self._apply_start_ack(body)
                except socket.timeout:
                    pass
                except Exception as exc:
                    # A malformed packet or a transient state-apply error must
                    # not kill the run loop (the server is equally tolerant):
                    # without this a single bad datagram stops audio + sync.
                    if time.time() - last_warn >= 5.0:
                        print(f"[client] sync loop error (continuing): {exc!r}")
                        last_warn = time.time()

                now = time.time()
                ntp_int = C.NTP_INTERVAL if self.playing else C.IDLE_NTP_INTERVAL
                # Cast our offset vote for the open track-start round (no-op
                # unless a fresh track/restart just began).
                self._maybe_vote(sock)
                # How long a server is allowed to go silent before we call it
                # lost. Sync lands every SYNC_INTERVAL (0.05s) while playing
                # but only every IDLE_SYNC_INTERVAL (10s) when paused, so the
                # threshold must widen in step or a healthy-but-paused server
                # reads as "no sync" every 4s (and provokes constant re-discovery).
                stale_s = (4.0 if self.playing
                           else max(C.IDLE_SYNC_INTERVAL * 2.5, 8.0))
                if now - last_ntp >= ntp_int:
                    if self.clock.exchange(sock):
                        if abs(self.clock.drift) > 1.0:
                            pass  # debug optionally
                    last_ntp = now
                if now - last_reg >= C.REGISTER_INTERVAL:
                    # Re-register so the web UI's room list stays populated
                    # (hostname fresh, last-seen fresh, sync error fresh) even
                    # if the server restarted or a packet was lost.
                    self.register(sock)
                    last_reg = now
                if self._provisional and now - last_state > stale_s \
                        and now - last_probe >= C.REGISTER_INTERVAL:
                    # Provisional host (discovery / loopback fallback) but no
                    # server sync for a while — our resolved address may be
                    # stale (e.g. discovery raced a network blip and we fell
                    # back to 127.0.0.1). Re-run discovery to retarget the
                    # real server without restarting; the server answers the
                    # active probe directly, so hearing it re-anchors us and
                    # the heartbeat/register follows.
                    last_probe = now
                    found = discover_server(self.port, timeout=1.5)
                    if found and found != self.host:
                        print(f"[client] re-discovered server at {found}; "
                              f"retargeting from {self.host}")
                        with self.lock:
                            self.host = found
                            self.clock = ClockSync(found, self.port)
                        # Sync resumes on the next broadcast; refresh state too.
                        last_state = now
                if now - last_state > stale_s:
                    print(f"[client] no sync from server; drift={self.clock.drift:+.0f}ppm "
                          f"offset={self.clock.offset*1000:+.0f}ms")
                if now - last_status > 2.0:
                    last_status = now
                    with self.lock:
                        buf = self.buffer
                        # Same anchored reference the PLL chases (matches the
                        # callback's err), not a fresh server_now() — the two
                        # would disagree during NTP offset re-estimation.
                        target = (self._base_pos
                                  + (1.0 + self.clock.drift * 1e-6)
                                  * (C.ts() - self._base_clock)
                                  if buf.data is not None else 0.0)
                        tracker = "playing" if self.playing else "paused"
                        name = buf.name
                    print(f"[client] {tracker}  song={name}  "
                          f"pos={self.local_pos:6.2f}s  target={target:6.2f}s  "
                          # err is the smoothed callback-boundary error the
                          # controller actually gates on (self.err_f), NOT the
                          # heavier err_smooth heartbeat metric: mode/window only
                          # line up with the value driving the BANG/PID/PI choice.
                          f"err={self.err_f*1000:+7.1f}ms  "
                          f"pitch={self.drift_pitch*1e6:+.0f}ppm  "
                          f"mode={self._mode}  "
                          f"window=±{config.BANG_BANG_WINDOW_MS:.0f}ms  "
                          f"clock={self.clock.drift:+.0f}ppm")
                # Keep the prefetched buffer's front pages resident for the
                # imminent swap (cheap, idempotent, no lock held on touch).
                self._warm_prefetch()
        except KeyboardInterrupt:
            pass
        finally:
            self.close_audio()
            if self._prefetch is not None:
                self._prefetch.close()   # release prefetched memmap + temp
            self.buffer.close()          # release memmap + temp file
            try:
                sock.close()
            except Exception:
                pass


def discover_server(port, timeout=3.0):
    """Find the server's IP on the LAN and return it, else None.

    Two mechanisms, tried together:
      1. Active probe: broadcast a TYPE_PROBE; the server answers directly
         (unicast) with TYPE_WELCOME. This works even when the server is
         paused/stopped, when its state broadcasts slump to the idle (10 s)
         cadence — a passive listen would usually miss them in a short
         window.
      2. Passive listen: the server also broadcasts TYPE_STATE (1 Hz when
         playing). Serves as a fallback / older-server compatibility.

    Returns the source IP of the first valid answer."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        sock.bind(("", port))
        sock.settimeout(timeout)
        sock.sendto(C.make_packet(C.TYPE_PROBE), ("255.255.255.255", port))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(4096)
                p = C.parse_packet(data)
                if p and p[0] in (C.TYPE_WELCOME, C.TYPE_STATE):
                    return addr[0]          # source IP of the broadcast/reply
            except socket.timeout:
                break
    except OSError:
        pass
    finally:
        sock.close()
    return None


def resolve_server(cli_server=None):
    """Pick the sync server host/IP. Returns (host, provisional): an explicit
    --server flag is authoritative (provisional=False); otherwise the client
    auto-discovers (provisional=True) and falls back to 127.0.0.1 (also
    provisional, so it can re-discover later instead of staying wedged).

    Priority: --server flag > UDP broadcast discovery > 127.0.0.1 fallback."""
    if cli_server:
        return cli_server, False
    port = config.DEFAULT_PORT
    found = discover_server(port)
    if found:
        print(f"[client] discovered server at {found}")
        return found, True
    print("[client] no server found on LAN; falling back to 127.0.0.1")
    return "127.0.0.1", True


def main():
    C.setup_logging()
    C.tune_process()          # GIL handoff + process priority (best-effort)
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default=None,
                    help="server host/IP to connect to; when omitted the "
                         "client auto-discovers the server on the LAN "
                         "(falls back to 127.0.0.1)")
    ap.add_argument("--port", type=int, default=config.DEFAULT_PORT,
                    help=f"UDP port (default: {config.DEFAULT_PORT})")
    ap.add_argument("--cache", default=config.CACHE_DIR,
                    help="local cache dir for downloaded tracks")
    ap.add_argument("--list", action="store_true",
                    help="print the tracks stored on the server and exit")
    args = ap.parse_args()

    server, provisional = resolve_server(args.server)

    if args.list:
        c = SyncClient(server, args.port, args.cache, provisional=provisional)
        tracks = c.fetch_tracks()
        if not tracks:
            print("(no tracks found on server)")
        else:
            for t in tracks:
                print(t)
        return

    c = SyncClient(server, args.port, args.cache, provisional=provisional)
    threading.Thread(target=c._console, daemon=True).start()
    c.run()
    print("bye")


if __name__ == "__main__":
    main()
