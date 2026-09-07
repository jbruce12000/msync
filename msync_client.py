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


class ClockSync:
    """NTP-style client<->server clock offset + drift estimation."""

    def __init__(self, addr, port, window=20):
        self.addr = addr
        self.port = port
        self.window = window
        self.points = []          # list of (t_rt, offset)
        self.offset = 0.0
        self.drift = 0.0          # server clock faster than client (ppm)
        self.rtt = 0.0
        # Round trips slower than this are treated as corrupted (network
        # queueing/jitter) and skipped rather than feeding garbage to the
        # offset estimator.
        self.rtt_limit = 3.0

    def exchange(self, sock):
        """One NTP exchange; returns True if updated."""
        t1 = C.ts()
        sock.sendto(C.make_packet(C.TYPE_NTP_REQ, t1=t1),
                    (self.addr, self.port))
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
        t1_, t2, t3 = body["t1"], body["t2"], body["t3"]
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
        if len(self.points) < 2:
            self.offset = self.points[-1][1] if self.points else 0.0
            self.drift = 0.0
            return
        ts_ = [p[0] for p in self.points]
        offs = [p[1] for p in self.points]
        n = len(ts_)
        mx = sum(ts_) / n
        my = sum(offs) / n
        # Fit offset = drift*t + offset using timestamps centered on the
        # mean. Centering keeps the numbers small; regressing against the raw
        # epoch (t ~ 1.8e9 s) multiplies any slope by ~1.8e9, blowing the
        # interpolated offset up to hours while the drift stays sane — exactly
        # the "sync is fine, then err explodes to +1e5s" failure mode.
        num = sum((ts_[i] - mx) * (offs[i] - my) for i in range(n))
        den = sum((ts_[i] - mx) ** 2 for i in range(n))
        slope = num / den if den else 0.0
        self.drift = slope * 1e6  # ppm
        # Robust, outlier-resistant offset: the median of recent samples.
        # (Offset is a slow constant; any residual drift is applied separately
        # by the PLL's feedforward.) This keeps one bad round trip from
        # yanking the playhead, unlike a least-squares intercept.
        self.offset = statistics.median(offs)
        # safety net: scrub samples more than 3*stdev from the median
        if n >= 4:
            sd_ = statistics.pstdev(offs)
            if sd_ > 0:
                med = statistics.median(offs)
                keep = [(t, o) for t, o in self.points
                        if abs(o - med) < 3 * sd_]
                if 2 <= len(keep) < n:
                    self.points = keep
                    self._recompute()

    def server_now(self):
        """Estimated current server wall-clock time."""
        return C.ts() + self.offset


class SongBuffer:
    """Decoded current song data, downloaded from server via HTTP."""

    def __init__(self, cache_dir):
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        self.name = None
        self.data = None
        self.sr = 44100
        self.nchannels = 2
        self.duration = 0.0

    def load(self, host, port, name, duration=None):
        if name == self.name and self.data is not None:
            return True
        # Try cache first
        path = os.path.join(self.cache_dir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.isfile(path):
            try:
                self._decode(path)
                self.name = name
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
        return True

    def _decode(self, path):
        decoded = miniaudio.decode_file(path, output_format=miniaudio.SampleFormat.FLOAT32)
        self.sr = decoded.sample_rate
        # Normalize to true stereo (N, 2) float32. Monaural sources are
        # broadcast to both channels; >2ch sources keep the first two.
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
        # self.name is set by the caller (SongBuffer.load) to the relpath
        # so don't overwrite it here; just print the basename.
        print(f"[client] loaded {os.path.basename(path)} ({self.duration:.1f}s, stereo)")
        return True


def urllib_quote(name):
    import urllib.parse
    return urllib.parse.quote(name)


class SyncClient:
    def __init__(self, host, port, cache_dir):
        self.host = host
        self.port = port
        self.clock = ClockSync(host, port)
        self.buffer = SongBuffer(cache_dir)

        # current playback state
        self.lock = threading.RLock()
        self.playing = False
        self.server_song_start = 0.0   # server wall time song started
        self.local_pos = 0.0           # local playhead (seconds)
        self.drift_pitch = 0.0         # current applied pitch from PLL
        self._pitch_int = 0.0          # PI integrator state (tight clip + fast unwind)
        self.err_f = 0.0               # low-passed PLL error (EMA of callback err)
        self.err_smooth = 0.0          # rolling avg of callback-boundary error
        self.queue = []                # server queue (names), from TYPE_STATE
        self.queue_size = 0            # from TYPE_SYNC (cheap field)

        self.stream = None
        self.chunk_size = 2048
        self._stop = threading.Event()   # set to leave the run() loop
        self._loading_name = None        # track currently being downloaded
        self._fade_out = False           # set by close_audio -> callback fades
        # If this room's audio path (HDMI -> TV/AVR, ...) delays the output
        # by OUTPUT_LATENCY_MS, play that far ahead of the synced playhead
        # so the sound reaching the listeners lines up with the server room.
        # Tune live from the console with: latency <ms>
        self._out_latency = config.OUTPUT_LATENCY_MS / 1000.0
        self._out_latency_ms = config.OUTPUT_LATENCY_MS  # for display

    # ------------------------------------------------------------------ #
    # HTTP API to the server (state, library, queue, playback control)    #
    # ------------------------------------------------------------------ #
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
    def _cb(self, outdata, frames, time_info, status):
        C.boost_audio_thread()
        with self.lock:
            buf = self.buffer
            if buf.data is None or not self.playing:
                outdata.fill(0)
                return
            # target position per estimated server clock
            target = max(0.0, self.clock.server_now() - self.server_song_start)
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
            ff = self.clock.drift * 1e-6                      # s/s from NTP
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
            idx = max(0.0, (self.local_pos + self._out_latency) * buf.sr)
            n = len(buf.data)
            if idx >= n:
                outdata.fill(0)
                return
            i0 = int(idx)
            i1 = min(int(idx + frames), n)
            out = np.zeros((frames, 2), dtype=np.float32)
            out[: i1 - i0] = buf.data[i0:i1]
            outdata[:] = out
            if self._fade_out:
                nf = min(frames, int(0.15 * buf.sr))
                if nf > 0:
                    outdata[-nf:] *= np.linspace(1.0, 0.0, nf,
                                                 dtype=np.float32)[:, None]
            # advance local playhead by physical samples * pitch correction
            self.local_pos += frames / buf.sr * rate

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

            same_song = (not name or name == self.buffer.name)
            stream_cmd = None
            if same_song:
                # Track the authoritative start time (handles seeks) and
                # mirror play/pause. On resume, rebase local_pos to the
                # live song_start the server reports.
                self.server_song_start = song_start
                if playing != self.playing:
                    self.playing = playing
                    if playing:
                        self.local_pos = max(0.0,
                                             self.clock.server_now() - song_start)
                    # A fresh rebase (or a long pause) invalidates the old
                    # integral: stale windup keeps drift_pitch pinned at
                    # ±MAX_PITCH for the wrong direction. Same for the
                    # smoothed PLL error — it must restart from the new
                    # reference, not decay toward it.
                    self._pitch_int = 0.0
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
            elif self._loading_name == name:
                return                # already fetching this track now
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

        # New track: download + decode into a SCRATCH buffer WITHOUT holding
        # self.lock, so the current track keeps playing while the next one
        # downloads (previously this froze the music for the whole transfer).
        scratch = SongBuffer(self.buffer.cache_dir)
        self._loading_name = name
        try:
            if not scratch.load(self.host, self.port, name, duration):
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
                return
            self.server_song_start = song_start
            self.local_pos = max(0.0, self.clock.server_now() - song_start)
            self._pitch_int = 0.0          # fresh rebase: drop stale windup
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

        # register with server (send our hostname so the web UI can name us)
        sock.sendto(C.make_packet(C.TYPE_REGISTER, host=socket.gethostname()),
                    (self.host, self.port))

        self.clock.exchange(sock)
        last_ntp = time.time()
        last_state = time.time()
        last_status = time.time()

        try:
            while not self._stop.is_set():
                sock.settimeout(0.1)
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
                    elif ptype == C.TYPE_LATENCY:
                        # Server-set output-latency offset (ms) for this room.
                        try:
                            ms = float(body.get("ms", 0.0))
                        except (TypeError, ValueError):
                            ms = 0.0
                        with self.lock:
                            self._out_latency = ms / 1000.0
                            self._out_latency_ms = ms
                except socket.timeout:
                    pass

                now = time.time()
                if now - last_ntp >= C.NTP_INTERVAL:
                    if self.clock.exchange(sock):
                        if abs(self.clock.drift) > 1.0:
                            pass  # debug optionally
                    last_ntp = now
                if now - last_state > 4.0:
                    print(f"[client] no sync from server; drift={self.clock.drift:+.0f}ppm "
                          f"offset={self.clock.offset*1000:+.0f}ms")
                if now - last_status > 2.0:
                    last_status = now
                    with self.lock:
                        buf = self.buffer
                        target = (self.clock.server_now() - self.server_song_start
                                  if buf.data is not None else 0.0)
                        tracker = "playing" if self.playing else "paused"
                        name = buf.name
                    print(f"[client] {tracker}  song={name}  "
                          f"pos={self.local_pos:6.2f}s  target={target:6.2f}s  "
                          f"err={(self.err_smooth)*1000:+7.1f}ms  "
                          f"pitch={self.drift_pitch*1e6:+.0f}ppm  "
                          f"clock={self.clock.drift:+.0f}ppm")
        except KeyboardInterrupt:
            pass
        finally:
            self.close_audio()
            try:
                sock.close()
            except Exception:
                pass


def resolve_server(cli_server, config_server=None):
    """Pick the sync server host/IP. config.py's SERVER takes priority over
    a --server given on the command line (which is only a testing override);
    when neither is set, fall back to the loopback address."""
    if config_server is None:
        config_server = config.SERVER
    if config_server:
        return config_server
    return cli_server or "127.0.0.1"


def main():
    C.setup_logging()
    C.tune_process()          # GIL handoff + process priority (best-effort)
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default=None,
                    help="server host/IP for testing; config.py's SERVER "
                         "takes priority over this (default: 127.0.0.1)")
    ap.add_argument("--port", type=int, default=config.DEFAULT_PORT,
                    help=f"UDP port (default: {config.DEFAULT_PORT})")
    ap.add_argument("--cache", default=config.CACHE_DIR,
                    help="local cache dir for downloaded tracks")
    ap.add_argument("--list", action="store_true",
                    help="print the tracks stored on the server and exit")
    args = ap.parse_args()

    server = resolve_server(args.server, config.SERVER)

    if args.list:
        c = SyncClient(server, args.port, args.cache)
        tracks = c.fetch_tracks()
        if not tracks:
            print("(no tracks found on server)")
        else:
            for t in tracks:
                print(t)
        return

    c = SyncClient(server, args.port, args.cache)
    threading.Thread(target=c._console, daemon=True).start()
    c.run()
    print("bye")


if __name__ == "__main__":
    main()
