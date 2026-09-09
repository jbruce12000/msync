"""Shared pytest fixtures: a live server (+ HTTP) and a live sync client."""
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer

import numpy as np
import pytest
import wave

ROOT = os.path.dirname(os.path.abspath(__file__))
MUSIC = os.path.join(ROOT, "music")
CACHE = os.path.join(os.path.expanduser("~"), ".cache", "msync-pytest")
DEMO_TRACKS = ["track_01_A440.wav", "track_02_B494.wav", "track_03_C554.wav"]
TEST_PORT = 9790

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import msync_common as C  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def demo_tracks():
    """Generate the demo tracks once if they're not already present."""
    missing = [f for f in DEMO_TRACKS
               if not os.path.isfile(os.path.join(MUSIC, f))]
    if missing:
        subprocess.run([sys.executable, os.path.join(ROOT, "make_test_music.py")],
                       check=True, cwd=ROOT)
    yield


def _make_wav(path, freq_l=440.0, freq_r=554.0, seconds=2.0, sr=44100):
    """Synthesize a short stereo WAV file with distinct L/R pitches."""
    t = np.arange(int(sr * seconds)) / sr
    left = np.sin(2 * np.pi * freq_l * t)
    right = np.sin(2 * np.pi * freq_r * t)
    frames = np.stack([left, right], axis=1)
    pcm = (frames * 32767).clip(-32768, 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def make_tagged_wav(path, title=None, artist=None, album=None):
    """A WAV with a RIFF LIST/INFO chunk (INAM/IART/IPRD) so tinytag can
    read title/artist/album. (WAV INFO has no track-number tag.)"""
    import struct
    sr, ch, bits = 44100, 1, 16
    data = b"\x00" * 200
    fmt = struct.pack("<HHIIHH", 1, ch, sr, sr * ch * bits // 8,
                      ch * bits // 8, bits)

    def chunk(tag, payload):
        return tag + struct.pack("<I", len(payload)) + payload + (
            b"\x00" if len(payload) % 2 else b"")

    info_payload = b"INFO"
    for cid, txt in (("INAM", title), ("IART", artist), ("IPRD", album)):
        if txt is None:
            continue
        t = txt.encode("utf-8") + b"\x00"
        info_payload += cid.encode() + struct.pack("<I", len(t)) + t + (
            b"\x00" if len(t) % 2 else b"")
    body = chunk(b"fmt ", fmt) + chunk(b"LIST", info_payload) + chunk(b"data", data)
    with open(str(path), "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WAVE" + body)


def make_tagged_mp3(path, title=None, artist=None, album=None, track=None):
    """A minimal MP3 whose ID3v2.3 tag carries title/artist/album/track.

    The audio payload is a stub — tinytag reads the tag fine, and msync's
    catalog tolerates miniaudio failing to decode it (duration becomes 0)."""
    import struct

    def synchsafe(n):
        return bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F,
                      (n >> 7) & 0x7F, n & 0x7F])

    def frame(fid, text):
        payload = b"\x00" + text.encode("latin-1")
        return fid + struct.pack(">I", len(payload)) + b"\x00\x00" + payload

    frames = b""
    if title is not None:
        frames += frame(b"TIT2", title)
    if artist is not None:
        frames += frame(b"TPE1", artist)
    if album is not None:
        frames += frame(b"TALB", album)
    if track is not None:
        frames += frame(b"TRCK", str(track))
    tag = b"ID3\x03\x00\x00" + synchsafe(len(frames)) + frames
    with open(str(path), "wb") as f:
        f.write(tag + b"\xff\xfb\x90\x00" + b"\x00" * 200)


def wait_until(predicate, timeout=6.0, interval=0.03, label=""):
    """Poll *predicate* every *interval* s until it returns truthy or *timeout*
    elapses. Returns True if satisfied, False on timeout. Replaces brittle
    fixed sleeps so tests run as fast as the system actually converges."""
    import http.client  # noqa: F401  (imported lazily for fixture availability)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def _http_ok(port):
    """True when an HTTP GET /api/state on *port* succeeds."""
    import http.client
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=0.5)
        conn.request("GET", "/api/state")
        r = conn.getresponse()
        r.read()
        conn.close()
        return r.status == 200
    except Exception:
        return False


class ServerHarness:
    def __init__(self, port, music_dir=MUSIC, db_path=None, auto_play=None):
        import msync_server as MS
        from msync_inotify import MusicWatcher
        self.music_dir = music_dir
        self.db_path = db_path or os.path.join(music_dir, "test-msync.db")
        self.srv = MS.SyncedServer(music_dir, port, self.db_path)
        self.port = port
        self.httpd = self._start_http(MS, self.srv, port)
        threading.Thread(target=self.srv.udp_loop, daemon=True).start()
        threading.Thread(target=self.srv.monitor_loop, daemon=True).start()
        # Start inotify watcher
        self.watcher = MusicWatcher(music_dir, self.srv.catalog, self.srv._stop)
        self.watcher.start()
        wait_until(lambda: _http_ok(port + MS.config.HTTP_PORT_OFFSET),
                   timeout=8.0, label="server HTTP ready")
        # Wait for the background catalog scan so catalog/album queries and
        # tests see the full library (the scan is fast for the test dirs).
        self.srv._scan_done.wait(timeout=15.0)
        # A fresh server starts idle (it only plays the persisted queue at
        # startup). Most tests assume *something* is playing, so if auto_play
        # is given, queue that track now: add_to_queue auto-starts it when
        # nothing is playing, and the track is popped from the queue — leaving
        # the queue empty, exactly matching the classic behavior.
        if auto_play and self.srv.song is None:
            self.srv.add_to_queue([auto_play])

    def _start_http(self, MS, srv, port):
        Handler = MS.build_handler(srv, self.music_dir)
        httpd = ThreadingHTTPServer(("", port + MS.config.HTTP_PORT_OFFSET),
                                    Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd

    def stop(self):
        self.srv._stop.set()
        try:
            self.httpd.shutdown()
        except Exception:
            pass
        self.srv.close_audio()
        self.srv.catalog.close()
        try:
            os.remove(self.db_path)
        except OSError:
            pass


@pytest.fixture
def server(tmp_path):
    db = str(tmp_path / "msync.db")
    h = ServerHarness(TEST_PORT, MUSIC, db, auto_play=DEMO_TRACKS[0])
    yield h.srv
    h.stop()


ALBUM_PORT = TEST_PORT + 1


@pytest.fixture
def album_server(tmp_path):
    lib = str(tmp_path / "library")
    alb = os.path.join(lib, "Demo Album")
    os.makedirs(alb, exist_ok=True)
    _make_wav(os.path.join(alb, "01 Intro.wav"), 440.0, 554.0, seconds=3.0)
    _make_wav(os.path.join(alb, "02 Bridge.wav"), 494.0, 659.0, seconds=3.0)
    _make_wav(os.path.join(lib, "solo_single.wav"), 330.0, 392.0, seconds=3.0)
    db = str(tmp_path / "album.db")
    h = ServerHarness(ALBUM_PORT, lib, db, auto_play="solo_single.wav")
    yield h.srv
    h.stop()


class ClientHarness:
    def __init__(self, host, port):
        import msync_client as MC
        self.client = MC.SyncClient(host, port, CACHE)
        self.stop_evt = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("", port))
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sock.sendto(C.make_packet(C.TYPE_REGISTER), (host, port))
        self.client.clock.exchange(self.sock)
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while not self.stop_evt.is_set():
            self.sock.settimeout(0.1)
            try:
                data, addr = self.sock.recvfrom(4096)
            except socket.timeout:
                time.sleep(0.02)
                continue
            except OSError:
                return  # socket closed during teardown
            p = C.parse_packet(data)
            if not p:
                continue
            ptype, body = p
            if ptype == C.TYPE_SYNC:
                self.client._apply_state(body)
            elif ptype == C.TYPE_STATE:
                with self.client.lock:
                    self.client.queue = list(body.get("queue", []))
                    self.client.queue_size = len(self.client.queue)
            time.sleep(0.02)

    def stop(self):
        self.stop_evt.set()
        try:
            self.sock.close()
        except Exception:
            pass
        self.client.close_audio()
        if self.client._prefetch is not None:
            self.client._prefetch.close()
        self.client.buffer.close()


@pytest.fixture
def client(server):
    h = ClientHarness("127.0.0.1", TEST_PORT)
    wait_until(lambda: (h.client.buffer is not None
                        and h.client.buffer.data is not None
                        and h.client.playing),
               timeout=10.0, label="client following server")
    yield h
    h.stop()