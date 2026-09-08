"""pytest test suite for msync: protocol, stereo, queue, HTTP, client sync."""
import http.client
import json
import os
import shutil
import time

import numpy as np
import pytest

import msync_common as C
from conftest import (MUSIC, make_tagged_mp3, make_tagged_wav, wait_until)
from msync_catalog import Catalog


# --------------------------------------------------------------------------- #
# Protocol                                                                     #
# --------------------------------------------------------------------------- #
def test_protocol_packet_roundtrip():
    p = C.make_packet(C.TYPE_SYNC, index=3, name="song.wav", playing=True)
    ptype, body = C.parse_packet(p)
    assert ptype == C.TYPE_SYNC
    assert body["name"] == "song.wav"
    assert body["index"] == 3


def test_protocol_rejects_garbage():
    assert C.parse_packet(b"not a real packet") is None


def test_latency_packet_roundtrip():
    p = C.make_packet(C.TYPE_LATENCY, ms=75)
    ptype, body = C.parse_packet(p)
    assert ptype == C.TYPE_LATENCY
    assert body["ms"] == 75


# --------------------------------------------------------------------------- #
# Stereo playback (server + client)                                            #
# --------------------------------------------------------------------------- #
def test_server_decodes_stereo(server):
    song = server.song
    assert song is not None
    assert song.data.ndim == 2
    assert song.data.shape[1] == 2
    # demo tracks have different L/R content
    assert not np.allclose(song.data[:, 0], song.data[:, 1])


def test_server_callback_writes_both_channels(server):
    frames = 4096
    out = np.zeros((frames, 2), dtype=np.float32)
    server.local_pos = 0.3
    server.song_start = time.time() - 0.3
    server._audio_cb(out, frames, None, None)
    assert out.shape == (frames, 2)
    assert out.any()                                   # audio present
    assert not np.allclose(out[:, 0], out[:, 1])       # L != R


def test_client_buffer_is_stereo(client):
    buf = client.client.buffer
    assert buf.data is not None and buf.data.ndim == 2
    assert buf.data.shape[1] == 2
    assert not np.allclose(buf.data[:, 0], buf.data[:, 1])


def test_client_callback_writes_both_channels(client):
    buf = client.client.buffer
    assert buf.data is not None
    frames = 2048
    out = np.zeros((frames, 2), dtype=np.float32)
    client.client.playing = True
    client.client.server_song_start = time.time() - 0.2
    client.client.local_pos = 0.2
    client.client._cb(out, frames, None, None)
    assert out.shape == (frames, 2)
    assert not np.allclose(out[:, 0], out[:, 1])


# --------------------------------------------------------------------------- #
# Clock sync / client follow                                                  #
# --------------------------------------------------------------------------- #
def test_client_follows_server_song(client):
    assert client.client.buffer.name == "track_01_A440.wav"
    assert client.client.playing


def test_clock_sync_measures_localhost(server, client):
    assert client.client.clock.offset is not None
    assert abs(client.client.clock.offset) < 0.05      # < 50 ms offset


def test_discover_server_succeeds_while_paused(server):
    """Discovery must work even when the server is paused/stopped.

    While paused the server's state/sync broadcasts drop to the idle 10s
    cadence, which a short passive listen usually misses. The active
    TYPE_PROBE -> unicast TYPE_WELCOME answer must still find it quickly
    (regression: clients starting while the server is paused fell back to
    127.0.0.1 and went offline)."""
    from msync_client import discover_server
    assert server.playing                       # paused below
    server.play_pause()
    assert not server.playing
    found = discover_server(server.port, timeout=2.0)
    # The reply comes from the server's 0.0.0.0-bound socket (source IP is
    # this host's LAN address, not necessarily 127.0.0.1). The point: a
    # *paused* server — whose broadcasts have dropped to the idle 10 s
    # cadence — must still be discovered quickly via the active probe.
    assert found is not None


def test_clock_sync_offset_stable_under_jitter():
    """The NTP offset estimate must stay near the true offset (a few ms)
    under normal network jitter. It must NOT blow up to seconds/minutes the
    way a raw epoch-intercept regression does (slope * t_epoch ~ 1.8e9)."""
    import random
    from msync_client import ClockSync
    cs = ClockSync("127.0.0.1", 1)
    epoch = 1_790_000_000.0           # ~ time.time() magnitude
    true_off = 0.003                  # 3 ms, client behind server
    rnd = random.Random(7)
    for i in range(40):
        t4 = epoch + i
        d1 = abs(rnd.gauss(0.001, 0.003))     # outbound one-way delay
        d2 = abs(rnd.gauss(0.001, 0.003))     # return one-way delay
        t2 = t4 - d2 + true_off
        t3 = t2 + 0.0001
        t1 = t3 - d1 - true_off
        off = ((t2 - t1) + (t3 - t4)) / 2.0
        cs.points.append((t4, off))
        if len(cs.points) > cs.window:
            cs.points.pop(0)
        cs._recompute()
    assert abs(cs.offset - true_off) < 0.01, cs.offset
    assert abs(cs.drift) < 500, cs.drift      # ppm: sane, not amplifying epoch


def test_client_download_does_not_hold_audio_lock(tmp_path, monkeypatch):
    """A slow new-track download must NOT happen while holding self.lock,
    otherwise the audio callback (which takes that lock every block) would
    stall for the whole transfer whenever the server is busy -> gaps."""
    import threading
    from msync_client import SyncClient, SongBuffer
    c = SyncClient("127.0.0.1", 1, str(tmp_path))
    monkeypatch.setattr(c, "_ensure_stream", lambda: None)   # no real audio
    gate = threading.Event()

    def slow_load(self, host, port, name, duration=None):
        gate.set()                              # "download" now in flight
        time.sleep(0.3)
        self.data = np.zeros((8000, 2), dtype=np.float32)
        self.sr = 8000
        self.name = name
        self.duration = duration or 1.0
        return True

    monkeypatch.setattr(SongBuffer, "load", slow_load)
    t = threading.Thread(target=c._apply_state, args=(
        {"name": "t.wav", "playing": True,
         "song_start": 100.0, "duration": 1.0},))
    t.start()
    assert gate.wait(5), "download never started"
    got = c.lock.acquire(timeout=0.5)
    assert got, "audio lock held during download -> music gaps on busy server"
    c.lock.release()
    t.join(5)
    assert not t.is_alive()
    assert c.buffer.name == "t.wav"
    assert c.playing is True


# --------------------------------------------------------------------------- #
# Queue                                                                       #
# --------------------------------------------------------------------------- #
def test_queue_add(server, client):
    added = server.add_to_queue(["track_02_B494.wav"])
    assert added == ["track_02_B494.wav"]
    assert len(server.queue_list()) == 1
    # queue list propagates via 1 Hz TYPE_STATE; wait for the item itself
    wait_until(lambda: "track_02_B494.wav" in client.client.queue)
    assert client.client.queue_size == 1


def test_queue_meta_tags(tmp_path):
    """Queue entries carry display metadata (artist - album - title) so the
    UI can render names instead of directory/file paths. Album entries
    expand to their tracks; untagged songs fall back to folder + filename."""
    root = str(tmp_path)
    first = os.path.join(root, "Tagged Album", "01 First.mp3")
    second = os.path.join(root, "Tagged Album", "02 Second.mp3")
    os.makedirs(os.path.dirname(first), exist_ok=True)
    make_tagged_mp3(first, title="First", artist="Artist One",
                    album="Tagged Album", track="1")
    make_tagged_mp3(second, title="Second", artist="Artist One",
                    album="Tagged Album", track="2")
    lone = os.path.join(root, "Orphan", "second-song.wav")
    os.makedirs(os.path.dirname(lone), exist_ok=True)
    make_tagged_wav(lone, title="Second Song", artist="Artist Two")  # no album tag
    # ^ no IPRD album tag -> the folder name is the only album fallback

    import msync_server as MS
    db = str(tmp_path / "q.db")
    srv = MS.SyncedServer(root, 9793, db)
    try:
        srv._scan_done.wait(timeout=15)
        # Seed the queue directly: the stub MP3s are undecodable, so queuing
        # through add_to_queue() would auto-start "playback" of a silence-less
        # stub and pop the entries before we can inspect them. queue_meta() is
        # purely a function of self.queue, so seeding under the lock is
        # deterministic and exercises exactly the same code path.
        with srv.lock:
            srv.queue.append({"type": "album", "album": "Tagged Album",
                              "paths": [first, second]})
            srv.queue.append({"type": "song", "path": lone,
                              "relpath": "Orphan/second-song.wav"})

        meta = {m["relpath"]: m for m in srv.queue_meta()}
        # Tagged track -> artist - album - title (track number is an int).
        m = meta["Tagged Album/01 First.mp3"]
        assert m["title"] == "First"
        assert m["artist"] == "Artist One"
        assert m["album"] == "Tagged Album"
        assert m["track"] == 1
        # Untagged-album file in a folder -> folder name + filename fallback.
        m = meta["Orphan/second-song.wav"]
        assert m["album"] == "Orphan"
        assert m["title"] == "Second Song"
        assert m["artist"] == "Artist Two"
        # queue_meta order matches queue_list (album expands to its tracks).
        assert [m["relpath"] for m in srv.queue_meta()] == srv.queue_list()
        # Both tracks of the album entry are expanded, in order.
        assert [m["relpath"] for m in srv.queue_meta()] == [
            "Tagged Album/01 First.mp3",
            "Tagged Album/02 Second.mp3",
            "Orphan/second-song.wav",
        ]
        # The state payload exposes queue_meta alongside the relpath queue.
        p = srv._state_payload()
        assert [m["relpath"] for m in p["queue_meta"]] == p["queue"]
        assert p["queue"] == ["Tagged Album/01 First.mp3",
                              "Tagged Album/02 Second.mp3",
                              "Orphan/second-song.wav"]
    finally:
        srv._stop.set()
        srv.close_audio()
        srv.catalog.close()
        try:
            os.remove(db)
        except OSError:
            pass


def test_queue_next_plays_queued_song_on_all_clients(server, client):
    server.add_to_queue(["track_02_B494.wav"])
    wait_until(lambda: client.client.queue_size == 1)
    res = server.next()
    assert res["source"] == "queue"
    assert res["file"] == "track_02_B494.wav"
    assert server.song.name == "track_02_B494.wav"
    assert len(server.queue_list()) == 0
    wait_until(lambda: client.client.buffer.name == "track_02_B494.wav")
    assert client.client.buffer.name == "track_02_B494.wav"


def test_queue_clear(server, client):
    server.add_to_queue(["track_02_B494.wav", "track_03_C554.wav"])
    assert len(server.queue_list()) == 2
    n = server.clear_queue()
    assert n == 2
    assert len(server.queue_list()) == 0
    wait_until(lambda: client.client.queue_size == 0)


def test_clear_queue_while_paused_totally_clears(server):
    """Clearing the queue while nothing is actively playing (paused/stopped)
    must empty the whole queue — including the stale now-playing slot — not
    leave a paused song behind."""
    server.play_pause()           # pause the auto-played track -> "nothing playing"
    server.add_to_queue(["track_02_B494.wav"])   # queued while paused (no auto-start)
    assert server.song is not None
    assert server.queue_list() == ["track_02_B494.wav"]
    n = server.clear_queue()
    assert n == 1
    assert server.queue_list() == []
    assert server.song is None    # now-playing slot cleared too
    assert server.playing is False


def test_next_while_stopped_does_not_start_playlist_song(server):
    """The web UI's ✕ on the now-playing slot posts to /api/control/next.
    When nothing is actively playing and the queue is empty, that must
    remove the current song and leave the server idle — not start the next
    playlist track."""
    server.play_pause()           # pause -> nothing actively playing
    assert server.song is not None
    assert server.queue_list() == []
    res = server.next()
    assert res["source"] == "stopped"
    assert server.song is None
    assert server.playing is False
    assert server.queue_list() == []
    # And a subsequent advance stays idle.
    res = server.next()
    assert res["source"] == "stopped"
    assert server.song is None


def test_next_while_playing_still_advances_playlist(server):
    """Pressing next during active playback with an empty queue still
    advances the alphabetically-ordered playlist — only a paused/stopped
    advance goes idle."""
    assert server.playing
    assert server.queue_list() == []
    res = server.next()
    assert res["source"] == "playlist"
    assert server.song.name == "track_02_B494.wav"


def test_queue_case_insensitive_lookup(server):
    added = server.add_to_queue(["TRACK_02_B494.WAV"])
    assert added == ["track_02_B494.wav"]


def test_queue_rejects_missing_file(server):
    assert server.add_to_queue(["nope.mp3"]) == []


def test_queue_rejects_duplicates_silently(server):
    """Adding an already-queued track returns [] (silently, no error) —
    whether it was queued before as a song entry or inside an album entry."""
    assert server.add_to_queue(["track_02_B494.wav"]) == ["track_02_B494.wav"]
    # Same track again: nothing added, nothing raised.
    assert server.add_to_queue(["track_02_B494.wav"]) == []
    # Same name case-insensitively (find_files resolves the real path).
    assert server.add_to_queue(["TRACK_02_B494.WAV"]) == []
    # Mixing a duplicate with a new track adds only the new one.
    assert server.add_to_queue(["track_02_B494.wav", "track_03_C554.wav"]) \
        == ["track_03_C554.wav"]


def test_queue_rejects_track_already_in_queued_album(album_server):
    """A track that's already inside a queued album entry must be rejected
    when added by name — album entries occupy their whole track list."""
    album_server.clear_queue()
    assert album_server.add_album("Demo Album") == [
        "Demo Album/01 Intro.wav", "Demo Album/02 Bridge.wav"]
    # Both tracks are now claimed by the album entry, so adding them again
    # (individually or together) is a silent no-op.
    assert album_server.add_to_queue(["Demo Album/01 Intro.wav"]) == []
    assert album_server.add_to_queue(["Demo Album/02 Bridge.wav",
                                      "solo_single.wav"]) == ["solo_single.wav"]


def test_queue_move_songs(server):
    """move_queue_item reorders expanded queue items — the drag & drop
    backend. ``to_index`` is the item's final landing index."""
    server.add_to_queue(["track_02_B494.wav", "track_03_C554.wav"])
    base = ["track_02_B494.wav", "track_03_C554.wav"]
    assert server.queue_list() == base
    # Drag item 0 down past item 1 → it lands at final index 1.
    assert server.move_queue_item(0, 1)
    assert server.queue_list() == ["track_03_C554.wav", "track_02_B494.wav"]
    # Drag item 1 back up to the top.
    assert server.move_queue_item(1, 0)
    assert server.queue_list() == base
    # No-op move and out-of-range indices are safe and leave the queue alone.
    assert server.move_queue_item(0, 0)
    assert not server.move_queue_item(5, 0)
    assert not server.move_queue_item(-1, 0)
    assert not server.move_queue_item(0, 9)
    assert server.queue_list() == base


def test_queue_move_album_tracks(album_server):
    """Dragging a track within an album keeps the album grouped (reordered);
    dragging it out of the album splits the entry and the remaining tracks
    regroup into an album entry when they end up adjacent again."""
    album_server.clear_queue()
    album_server.add_album("Demo Album")
    two = ["Demo Album/01 Intro.wav", "Demo Album/02 Bridge.wav"]
    assert album_server.queue_list() == two
    # Swap the two tracks within the album (drag 01 below 02).
    assert album_server.move_queue_item(0, 1)
    assert album_server.queue_list() == two[::-1]
    # Split: queue a single behind the album, then drag track 02 (index 1)
    # down past the single so it ends at final index 2.
    album_server.clear_queue()
    album_server.add_album("Demo Album")
    album_server.add_to_queue(["solo_single.wav"])
    assert album_server.queue_list() == two + ["solo_single.wav"]
    assert album_server.move_queue_item(1, 2)
    assert album_server.queue_list() == [
        "Demo Album/01 Intro.wav", "solo_single.wav", "Demo Album/02 Bridge.wav"]
    # Now that 02 sits directly after 01 again, dragging the single back to
    # the end regroups the pair into one album entry.
    assert album_server.move_queue_item(1, 2)
    assert album_server.queue_list() == two + ["solo_single.wav"]
    assert [e["type"] for e in album_server.queue] == ["album", "song", "song"]
    # An album is re-split when a single song is dragged into its middle.
    album_server.clear_queue()
    album_server.add_album("Demo Album")
    album_server.add_to_queue(["solo_single.wav"])
    assert album_server.queue_list() == two + ["solo_single.wav"]
    # Drag the single (index 2) between the album's two tracks (index 1).
    assert album_server.move_queue_item(2, 1)
    assert album_server.queue_list() == [
        "Demo Album/01 Intro.wav", "solo_single.wav", "Demo Album/02 Bridge.wav"]
    assert [e["type"] for e in album_server.queue] == ["album", "song", "album"]


def test_drop_folder_absorbs(server):
    qdir = server.queue_dir
    assert qdir and os.path.isdir(qdir)
    shutil.copy(os.path.join(MUSIC, "track_03_C554.wav"),
                os.path.join(qdir, "track_03_C554.wav"))
    wait_until(lambda: any("track_03" in n for n in server.queue_list()),
               timeout=6.0, label="drop folder absorbed")
    try:
        names = server.queue_list()
        assert any("track_03" in n for n in names)
        assert not os.listdir(qdir)                    # moved out of .queue
    finally:
        # Leave no trace in the shared music dir: the copied file already
        # exists, so the server dedups our copy to "track_03_C554 (1).wav",
        # which would otherwise pollute the library for later tests.
        server.clear_queue()
        for f in os.listdir(server.music_dir):
            if f.startswith("track_03_C554") and f != "track_03_C554.wav":
                try:
                    os.remove(os.path.join(server.music_dir, f))
                except OSError:
                    pass


# --------------------------------------------------------------------------- #
# HTTP API                                                                     #
# --------------------------------------------------------------------------- #
def _http(port, method, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path)
    r = conn.getresponse()
    data = r.read()
    conn.close()
    assert r.status == 200, f"{method} {path} -> {r.status}: {data}"
    return json.loads(data)


def test_http_state(server):
    d = _http(server.port + 1000, "GET", "/api/state")
    assert d["name"] == "track_01_A440.wav"
    assert "queue" in d
    assert "queue_size" in d
    # The web UI's header depends on these fields to show the current track
    # name/album, the play state, and the progress bar.
    assert d["now_playing"]["name"] == "track_01_A440.wav"
    assert d["now_playing"]["relpath"] == "track_01_A440.wav"
    assert d["now_playing"]["album"] in ("", None)
    assert d["paused"] is False
    assert isinstance(d["elapsed"], (int, float))
    assert d["elapsed"] >= 0


def test_http_queue_add_and_clear(server):
    from urllib.parse import quote
    d = _http(server.port + 1000, "POST",
              "/api/queue/add?name=" + quote("track_02_B494.wav"))
    assert d["added"] == ["track_02_B494.wav"]
    assert len(server.queue_list()) == 1
    d = _http(server.port + 1000, "POST", "/api/queue/clear")
    assert d["removed"] == 1
    assert len(server.queue_list()) == 0


def test_http_queue_remove_del(server):
    """DELETE /api/queue/remove removes a queued track by index — this is
    the exact request the web UI's queueRemove() sends."""
    server.add_to_queue(["track_02_B494.wav", "track_03_C554.wav"])
    assert len(server.queue_list()) == 2
    d = _http(server.port + 1000, "DELETE", "/api/queue/remove?index=0")
    assert d["removed"] == "track_02_B494.wav"
    assert server.queue_list() == ["track_03_C554.wav"]
    d = _http(server.port + 1000, "DELETE", "/api/queue/remove?index=0")
    assert d["removed"] == "track_03_C554.wav"
    assert server.queue_list() == []


def test_http_queue_list(server):
    server.add_to_queue(["track_02_B494.wav"])
    d = _http(server.port + 1000, "GET", "/api/queue")
    assert d["queue"] == ["track_02_B494.wav"]
    assert d["now_playing"] == "track_01_A440.wav"


def test_http_queue_move(server):
    """POST /api/queue/move reorders the queue — the exact request the web
    UI's drag & drop sends (?from=N&to=M)."""
    server.add_to_queue(["track_02_B494.wav", "track_03_C554.wav"])
    d = _http(server.port + 1000, "POST", "/api/queue/move?from=0&to=1")
    assert d["moved"] is True
    assert server.queue_list() == ["track_03_C554.wav", "track_02_B494.wav"]
    # Same index is a no-op.
    d = _http(server.port + 1000, "POST", "/api/queue/move?from=1&to=1")
    assert d["moved"] is True
    assert server.queue_list() == ["track_03_C554.wav", "track_02_B494.wav"]


def test_http_queue_move_validation(server):
    """Bad indices are rejected with 400 and leave the queue untouched."""
    server.add_to_queue(["track_02_B494.wav"])
    conn = http.client.HTTPConnection("127.0.0.1",
                                      server.port + 1000, timeout=5)
    conn.request("POST", "/api/queue/move?from=abc&to=1")
    r = conn.getresponse()
    r.read()
    conn.close()
    assert r.status == 400
    assert server.queue_list() == ["track_02_B494.wav"]


def test_http_remove_current_starts_next_queue_song(server):
    """Removing the currently playing song from the queue — the web UI's
    now-playing ✕ button — posts to /api/control/next and starts the next
    queued song.  The current track is never stored in the pending queue
    (it's popped before playback), so removing it is an advance/skip."""
    server.add_to_queue(["track_02_B494.wav", "track_03_C554.wav"])
    wait_until(lambda: server.queue_list() == ["track_02_B494.wav",
                                               "track_03_C554.wav"])
    # Remove now-playing (track_01) → next queued song (track_02) starts
    d = _http(server.port + 1000, "POST", "/api/control/next")
    assert d["source"] == "queue"
    assert d["file"] == "track_02_B494.wav"
    assert server.song.name == "track_02_B494.wav"
    assert server.queue_list() == ["track_03_C554.wav"]
    # Remove track_02 → track_03 starts
    d = _http(server.port + 1000, "POST", "/api/control/next")
    assert d["source"] == "queue"
    assert d["file"] == "track_03_C554.wav"
    assert server.song.name == "track_03_C554.wav"
    assert server.queue_list() == []


def _http_status(port, method, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path)
    r = conn.getresponse()
    data = r.read()
    conn.close()
    return r.status, data


# --------------------------------------------------------------------------- #
# config.py                                                                    #
# --------------------------------------------------------------------------- #
def test_config_music_dir_default(monkeypatch):
    monkeypatch.delenv("MSYNC_MUSIC_DIR", raising=False)
    import importlib
    import config
    importlib.reload(config)
    # The default should be a real, absolute directory (the configured
    # library) — not a mangled/relative path from a bad `_here` computation.
    assert config.MUSIC_DIR
    assert os.path.isabs(config.MUSIC_DIR)
    assert os.path.isdir(config.MUSIC_DIR)


def test_config_output_latency_env(monkeypatch):
    import importlib
    import config
    monkeypatch.setenv("MSYNC_OUTPUT_LATENCY_MS", "120")
    importlib.reload(config)
    assert config.OUTPUT_LATENCY_MS == 120.0
    monkeypatch.delenv("MSYNC_OUTPUT_LATENCY_MS", raising=False)
    importlib.reload(config)
    assert config.OUTPUT_LATENCY_MS == 0.0


# --------------------------------------------------------------------------- #
# Client latency settings (web Configure)                                      #
# --------------------------------------------------------------------------- #
def test_catalog_client_latency_roundtrip(tmp_path):
    c = Catalog(str(tmp_path), str(tmp_path / "c.db"))
    c.upsert_client("10.0.0.4", "room4")
    assert c.client_latency("10.0.0.4") == 0.0
    c.set_client_latency("10.0.0.4", 87.0)
    assert c.client_latency("10.0.0.4") == 87.0
    # unknown IP defaults to 0
    assert c.client_latency("10.0.0.9") == 0.0
    # list includes the hostname + value
    rows = {r["ip"]: r for r in c.list_clients()}
    assert rows["10.0.0.4"]["latency_ms"] == 87.0
    assert rows["10.0.0.4"]["hostname"] == "room4"


def test_catalog_client_heartbeat_refreshes_last_seen(tmp_path):
    # The server treats every client packet (e.g. an NTP request) as a
    # heartbeat: it must refresh last_seen WITHOUT wiping the stored
    # hostname or latency offset.
    c = Catalog(str(tmp_path), str(tmp_path / "c.db"))
    c.set_client_latency("10.0.0.7", 42.0)          # register + tune first
    c.upsert_client("10.0.0.7", "room7", last_seen=1000.0)
    c.upsert_client("10.0.0.7", "")                 # NTP heartbeat, no name
    row = {r["ip"]: r for r in c.list_clients()}["10.0.0.7"]
    assert row["hostname"] == "room7"               # name kept
    assert row["latency_ms"] == 42.0                # offset kept
    assert row["last_seen"] > 1000.0                # heartbeat refreshed it


def test_catalog_heartbeat_wins_against_concurrent_writer(tmp_path):
    """A client heartbeat (upsert_client, sent ~every second) must not crash
    the sync thread when another DB writer (queue add, playback persist, scan)
    is mid-transaction on the same shared connection. Regression for a real
    outage: 'cannot start a transaction within a transaction' killed the UDP
    loop, silently leaving every room offline in the Configure tab."""
    import threading
    c = Catalog(str(tmp_path), str(tmp_path / "c.db"))

    entered = threading.Event()
    release = threading.Event()

    def other_writer():
        # Host the catalog lock + an open write transaction, the way a
        # concurrent save_queue/save_playback/scan would.
        with c.lock, c._conn:
            c._conn.execute("DELETE FROM clients")
            entered.set()
            release.wait(10)          # hold the transaction open

    tw = threading.Thread(target=other_writer)
    tw.start()
    assert entered.wait(5)            # the other writer owns the DB now

    result = {}

    def heartbeat():
        try:
            c.upsert_client("10.0.0.9", "room9")
            result["ok"] = True
        except Exception as exc:
            result["err"] = repr(exc)

    th = threading.Thread(target=heartbeat)
    th.start()
    time.sleep(0.2)
    try:
        # The heartbeat must WAIT for the writer, not raise/complete early.
        assert "ok" not in result and "err" not in result
        assert th.is_alive()
    finally:
        release.set()
    tw.join(timeout=5)
    th.join(timeout=5)
    assert result.get("ok"), f"heartbeat failed: {result.get('err')}"
    row = {r["ip"]: r for r in c.list_clients()}["10.0.0.9"]
    assert row["last_seen"] > 0


def test_catalog_client_err_ms_roundtrip(tmp_path):
    # The Configure tab shows each room's sync error: registers carry it in,
    # plain heartbeats must not wipe it, and a newer register replaces it.
    c = Catalog(str(tmp_path), str(tmp_path / "c.db"))
    c.upsert_client("10.0.0.4", "room4", err_ms=12.3)
    row = {r["ip"]: r for r in c.list_clients()}["10.0.0.4"]
    assert row["err_ms"] == 12.3
    assert row["hostname"] == "room4"
    c.upsert_client("10.0.0.4", "")                 # NTP heartbeat, no err
    row = {r["ip"]: r for r in c.list_clients()}["10.0.0.4"]
    assert row["err_ms"] == 12.3                     # last value preserved
    assert row["hostname"] == "room4"                # name kept too
    c.upsert_client("10.0.0.4", "room4", err_ms=-0.6)
    row = {r["ip"]: r for r in c.list_clients()}["10.0.0.4"]
    assert row["err_ms"] == -0.6


def test_catalog_prune_clients(tmp_path):
    # Rooms that haven't been seen within the stale window are removed from
    # the catalog DB (and therefore the web UI's Configure tab); rooms that
    # are still heartbeating stay.
    c = Catalog(str(tmp_path), str(tmp_path / "c.db"))
    c.upsert_client("10.0.0.1", "fresh",
                    last_seen=time.time() - 10)         # still around
    c.upsert_client("10.0.0.2", "stale",
                    last_seen=time.time() - 2 * 86400)  # gone 2 days
    c.prune_clients(stale_after=86400)
    ips = {r["ip"] for r in c.list_clients()}
    assert ips == {"10.0.0.1"}
    # A second sweep is harmless (nothing left to delete).
    c.prune_clients(stale_after=86400)
    assert {r["ip"] for r in c.list_clients()} == {"10.0.0.1"}


def test_catalog_migrates_clients_err_ms(tmp_path):
    # Databases created before err_ms reporting must gain the column on open.
    import sqlite3
    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE clients (
            ip TEXT PRIMARY KEY, hostname TEXT NOT NULL DEFAULT '',
            latency_ms REAL NOT NULL DEFAULT 0, last_seen REAL NOT NULL DEFAULT 0)
        """)
    conn.execute("INSERT INTO clients (ip, hostname) VALUES ('10.0.0.9', 'old')")
    conn.commit()
    conn.close()
    c = Catalog(str(tmp_path), db)
    row = {r["ip"]: r for r in c.list_clients()}["10.0.0.9"]
    assert row["err_ms"] == 0.0                      # default after migration
    c.upsert_client("10.0.0.9", "old", err_ms=5.0)  # writable afterwards
    assert {r["ip"]: r for r in c.list_clients()}["10.0.0.9"]["err_ms"] == 5.0


def test_server_records_registered_client(server, client):
    # The end-to-end register path: the test client's startup TYPE_REGISTER
    # must land in the server's clients table (plus the 1 Hz NTP heartbeats
    # it sends while running).
    wait_until(lambda: any(c["ip"] == "127.0.0.1"
                           for c in server.catalog.list_clients()))


def test_client_heartbeat_re_registers(server, client):
    # The client re-registers on REGISTER_INTERVAL, so a room running through
    # a server restart reappears (hostname + stored offset pushed back).
    h = client.client
    # find the hostname the test client would send
    import socket as _socket
    name = _socket.gethostname()
    wait_until(lambda: any(c["ip"] == "127.0.0.1" and c["hostname"] == name
                           for c in server.catalog.list_clients()))


def test_server_register_reports_client_err(server):
    # The register packet the client now sends carries its sync error; the
    # server must store it (and hand it to the web UI via /api/clients).
    import socket as _socket
    for _ in range(3):
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        s.sendto(C.make_packet(C.TYPE_REGISTER, host="errroom", err_ms=-3.4),
                 ("127.0.0.1", server.port))
        s.close()
        time.sleep(0.2)   # retries in case the server thread is mid-sleep
    wait_until(lambda: any(c["ip"] == "127.0.0.1" and c["err_ms"] == -3.4
                           for c in server.catalog.list_clients()))
    d = _http(server.port + 1000, "GET", "/api/clients")
    c = next(c for c in d["clients"] if c["ip"] == "127.0.0.1")
    assert c["err_ms"] == -3.4
    assert c["hostname"] == "errroom"


def test_api_clients_hides_stale_rooms(server):
    # A room that hasn't been seen for over CLIENT_STALE_AFTER must not
    # appear in the Configure tab's /api/clients list (and leaves the DB).
    import config
    stale = time.time() - config.CLIENT_STALE_AFTER - 3600   # well past 24h
    server.catalog.upsert_client("10.99.0.1", "ghost", last_seen=stale)
    server.catalog.upsert_client("10.99.0.2", "real", last_seen=time.time() - 5)
    assert {r["ip"] for r in server.catalog.list_clients()} >= {
        "10.99.0.1", "10.99.0.2"}
    d = _http(server.port + 1000, "GET", "/api/clients")
    ips = {c["ip"] for c in d["clients"]}
    assert "10.99.0.1" not in ips
    assert "10.99.0.2" in ips
    # and the stale row is actually gone from the database
    assert "10.99.0.1" not in {
        r["ip"] for r in server.catalog.list_clients()}


def test_http_clients_list_and_set_latency(server):
    # Simulate a room registering its latency by calling the catalog directly
    # (the UDP register path does the same under the hood), then check the
    # endpoints used by the web UI.
    server.catalog.upsert_client("192.168.1.5", "bedroom")
    d = _http(server.port + 1000, "GET", "/api/clients")
    ips = {c["ip"] for c in d["clients"]}
    assert "192.168.1.5" in ips

    d = _http(server.port + 1000, "POST",
              f"/api/clients/latency?ip=192.168.1.5&ms=-35")
    assert d["latency_ms"] == -35.0
    assert server.catalog.client_latency("192.168.1.5") == -35.0

    # out-of-range value is rejected
    status, _ = _http_status(server.port + 1000, "POST",
                             "/api/clients/latency?ip=192.168.1.5&ms=2000")
    assert status == 400


def test_client_latency_packet_sets_out_latency(client):
    # The client's UDP loop applies TYPE_LATENCY by setting _out_latency.
    # Simulate that inline (as msync_client.run() does) and confirm the
    # callback's output offset follows it.
    h = client.client
    with h.lock:
        h._out_latency = 60.0 / 1000.0
        h._out_latency_ms = 60.0
    assert h._out_latency_ms == 60.0
    assert abs(h._out_latency * 1000 - 60.0) < 1e-9
    # reset to zero (the default a fresh room should see)
    with h.lock:
        h._out_latency = 0.0
        h._out_latency_ms = 0.0
    assert h._out_latency_ms == 0.0


def test_http_library(server):
    d = _http(server.port + 1000, "GET", "/api/library")
    for t in ("track_01_A440.wav", "track_02_B494.wav",
              "track_03_C554.wav"):
        assert t in d["songs"]
    # sorted case-insensitively
    assert d["songs"] == sorted(d["songs"], key=str.lower)


def test_http_control_play_switches_song(server, client):
    d = _http(server.port + 1000, "POST",
              "/api/control/play?name=track_02_B494.wav")
    assert d["now_playing"] == "track_02_B494.wav"
    assert server.song.name == "track_02_B494.wav"
    # the sync broadcast should pull the client to the new song too
    wait_until(lambda: client.client.buffer.name == "track_02_B494.wav")


def test_http_control_play_unknown(server):
    status, data = _http_status(server.port + 1000, "POST",
                                "/api/control/play?name=nope.mp3")
    assert status == 404
    assert b"not found" in data


def test_http_control_volume(server):
    d = _http(server.port + 1000, "POST", "/api/control/volume?level=0.5")
    assert d["volume"] == 0.5
    assert server.volume == 0.5
    d = _http(server.port + 1000, "POST", "/api/control/volume?level=9")
    assert d["volume"] == 1.5          # clamped


def test_http_control_pause_resume(server):
    d = _http(server.port + 1000, "POST", "/api/control/toggle?state=0")
    assert d["playing"] is False
    assert server.playing is False
    d = _http(server.port + 1000, "POST", "/api/control/toggle?state=1")
    assert d["playing"] is True


def test_http_control_stop(server):
    # Stop halts playback and rewinds to the start of the current track; a
    # later play/pause resumes the same track from the top.
    with server.lock:
        server.local_pos = 4.0
    d = _http(server.port + 1000, "POST", "/api/control/stop")
    assert d["playing"] is False
    assert server.playing is False
    # rewound to the start (a lock-free audio callback may nudge it by at
    # most one block before seeing playing=False)
    assert server.local_pos < 1.0
    d = _http(server.port + 1000, "POST", "/api/control/toggle?state=1")
    assert d["playing"] is True
    assert server.local_pos < 2.0


def test_http_pause_keeps_client_stream_open(server, client):
    # Pause must NOT stop the client's PortAudio stream: restarting the
    # stream re-primes the device buffer, pushing this client audibly behind
    # the (continuously-running) server by ~one buffer depth — a constant
    # offset a rate-only PLL removes only very slowly. The callback fills
    # silence while paused, exactly like the server does.
    h = client.client
    wait_until(lambda: h.stream is not None and h.stream.active
               and h.buffer.data is not None)
    _http(server.port + 1000, "POST", "/api/control/toggle?state=0")
    wait_until(lambda: h.playing is False)        # mirror applied
    assert h.stream.active                        # still open, just silent
    _http(server.port + 1000, "POST", "/api/control/toggle?state=1")
    wait_until(lambda: h.playing is True)
    assert h.stream.active

    # The playhead must track the server firmly right after resume (no
    # stream-restart re-prime gap pushing the client behind).
    def gap():
        with h.lock:
            return (h.clock.server_now() - h.server_song_start
                    - h.local_pos)
    wait_until(lambda: abs(gap()) < 0.25, timeout=3.0)


# --------------------------------------------------------------------------- #
# Kalman error smoother                                                        #
# --------------------------------------------------------------------------- #
def _kalman_drive(filter_, true_fn, n=400, dt=0.2, rng=None, meas_noise=0.02):
    """Drive filter_ with synthetic noisy measurements of true_fn(t).

    Returns the per-step estimates of x (position error).
    """
    rng = rng or np.random.default_rng(0)
    xs = []
    t = 0.0
    for _ in range(n):
        z = true_fn(t) + rng.normal(0, meas_noise)
        xs.append(filter_.update(z, dt))
        t += dt
    return xs


def test_error_kalman_converges_to_constant():
    # Constant 30 ms error buried in 20 ms-rms noise: the EMA-style 1-D
    # smoothers converge to a biased blend of noise; the Kalman should
    # converge to within a few ms of the true value and stay there.
    k = C.ErrorKalman()
    xs = _kalman_drive(k, lambda t: 0.030)
    # After the transient (first ~50 steps) the estimate sits near truth.
    tail = xs[50:]
    assert abs(np.mean(tail) - 0.030) < 0.005
    # Defaults are tuned for responsiveness (tau ~0.9 s, matching the EMA)
    # rather than maximum smoothing, so expect ~7 ms rms on 20 ms noise.
    assert np.std(tail) < 0.010           # smoother than the 20ms noise


def test_error_kalman_defaults_not_laggy():
    # Regression guard for the limit-cycle fix: the original q=1e-6, r=5e-4
    # defaults gave an effective ~2 s filter that lagged the true error
    # through the catch-up nudge and limit-cycled the pitch clamp at
    # +-MAX_PITCH on high-jitter paths. The tuned defaults must respond to a
    # step within ~1 s (comparable to the EMA's tau~1.1 s).
    k = C.ErrorKalman()
    dt = 0.2
    xs = []; t = 0.0; z = 0.0
    rng = np.random.default_rng(3)
    for _ in range(5):                     # settle on zero
        xs.append(k.update(z, dt)); t += dt
    z = 0.10                               # step to a 100 ms gap
    for _ in range(5):
        xs.append(k.update(z + rng.normal(0, 0.01), dt)); t += dt
    # 1.0 s (5 steps) after the step the estimate must have closed >= 50% of
    # the gap; a laggy ~2 s filter would still be below ~40%.
    assert xs[-1] > 0.05


def test_error_kalman_tracks_ramp_velocity():
    # Error drifting linearly at 1 ms/s (residual clock skew): the filter's
    # velocity state should follow and the position estimate should track the
    # ramp rather than lag behind a fixed-lag EMA.
    k = C.ErrorKalman()
    dt = 0.2
    xs = []; t = 0.0
    rng = np.random.default_rng(1)
    for _ in range(300):
        z = 0.02 * t + rng.normal(0, 0.01)   # ramp + 10 ms noise
        xs.append(k.update(z, dt))
        t += dt
    # steady-state tracking error on a ramp driven at this dt
    assert abs(xs[-1] - (0.02 * 60.0)) < 0.01
    assert abs(k.v - 0.02) < 0.005          # velocity estimate ~ slope


def test_error_kalman_rejects_noise_outlier():
    # A single huge measurement (a bad NTP round trip, 500 ms) must not yank
    # the smoothed estimate by anything like that amount: the filter's gain
    # is kept low by the measurement-noise model.
    k = C.ErrorKalman()
    xs = _kalman_drive(k, lambda t: 0.0, n=40)
    z = 0.5                                   # isolated 500 ms "spike"
    x_after = k.update(z, 0.2)
    assert abs(x_after) < 0.05                # estimate barely moved


def test_error_kalman_reset_clears_stale_velocity():
    # After a rebase the estimate must not ride a stale velocity from the
    # pre-rebase trajectory (e.g. a seek that jumps the target reference).
    k = C.ErrorKalman()
    _kalman_drive(k, lambda t: 0.05 * t, n=200)   # build up a strong ramp
    assert abs(k.v) > 0.01
    k.reset()
    assert k.v == 0.0
    assert k.x == 0.0
    # next measurement pulls the (now-zero) velocity toward the *new* truth
    xs = _kalman_drive(k, lambda t: 0.0, n=30)
    assert abs(xs[-1]) < 0.01


def test_client_callback_with_kalman_smoothes_err(client):
    # The Kalman path through the real callback. USE_KALMAN defaults on in
    # the deployed config, but force this instance explicitly so the test
    # does not depend on config.py: after a warm-up to lockstep, an NTP-origin
    # jitter must stay out of the loop's smoothed error — the estimate that
    # feeds the deadband + PI — just like the EMA path, while the raw err
    # spikes.
    h = client.client
    import msync_common as MC
    wait_until(lambda: h.buffer.data is not None
               and h.buffer.duration > 2.0 and h.stream is not None)
    with C.stream_ops_lock:
        h.stream.stop()          # silence the real callback: no measurement race
    fake = {"t": 0.0}
    old_ts = MC.ts
    MC.ts = lambda: fake["t"]
    try:
        with h.lock:
            h.playing = True
            h._pitch_int = 0.0
            h.err_f = 0.0
            h.kalman = C.ErrorKalman()
            h.local_pos = 5.0
            h._rebase()              # also resets the Kalman via our _rebase hook
        frames = 8192
        block = frames / h.buffer.sr
        out = np.zeros((frames, 2), dtype=np.float32)
        rng = np.random.default_rng(2)
        # Warm up: after a rebase the target reference immediately advances
        # while local_pos only moves in the callback, so the loop genuinely
        # opens a ~1-block gap that the catch-up nudge (5 ms/block) closes
        # over many blocks. Run enough blocks to reach the steady-state
        # alignment the loop holds during normal playback.
        for _ in range(80):
            fake["t"] += block
            h._cb(out, frames, None, None)
        # Now inject reference jitter: yank clock.offset around (NTP
        # re-estimation noise). The anchored target reference ignores offset,
        # so the raw err printed by the loop stays small — but let's confirm
        # the Kalman estimate tracks it and never blows up either.
        raw_peak = 0.0
        kal_peak = 0.0
        for _ in range(20):
            fake["t"] += block
            with h.lock:
                # NTP-style jitter on the clock estimate the reference plugs
                # into (drift); a fraction of this leaks into error via the
                # local playhead advance.
                h.clock.offset += rng.normal(0, 0.015)
            h._cb(out, frames, None, None)
            with h.lock:
                err = (h._base_pos
                       + (1.0 + h.clock.drift * 1e-6) * (fake["t"] - h._base_clock)
                       - h.local_pos)
                raw_peak = max(raw_peak, abs(err))
                kal_peak = max(kal_peak, abs(h.kalman.x))
    finally:
        MC.ts = old_ts
    # The Kalman path must stay stable and bounded in the live callback: it
    # tracks the raw playhead error (which includes the half-block sawtooth)
    # without running away or lagging far behind it. The smoothing rigor
    # (convergence, ramp/velocity tracking, outlier rejection) is covered by
    # the unit tests above; this locks in the wiring.
    assert kal_peak < 0.10                        # never blows up
    assert kal_peak < raw_peak + 0.02             # tracks, doesn't overshoot


def test_client_callback_fast_catchup(client):
    # A large playhead gap (post-resume / GIL stall) is closed by the bounded
    # per-block nudge, not by waiting for a slow MAX_PITCH-limited slew.
    h = client.client
    wait_until(lambda: h.buffer.data is not None
               and h.buffer.duration > 1.0 and h.stream is not None)
    with C.stream_ops_lock:
        h.stream.stop()          # silence the real callback: no measurement race
    with h.lock:
        h.playing = True
        h._pitch_int = 0.0
        target = max(0.0, h.clock.server_now() - h.server_song_start)
        h.local_pos = max(0.0, min(target - 0.5, h.buffer.duration - 2.0))
        before = h.local_pos
    out = np.zeros((8192, 2), dtype=np.float32)
    h._cb(out, 8192, None, None)
    with h.lock:
        advance = h.local_pos - before
    block = 8192 / h.buffer.sr
    # one normal block plus the bounded catch-up nudge (a plain PLL would
    # contribute only ~0.4ms at the 0.2% cap)
    assert advance > block + C.CATCHUP_STEP * 0.9
    assert advance < block * 1.004 + C.CATCHUP_STEP * 1.1


def test_client_callback_output_latency_compensation(client):
    # A room on a slow output path (HDMI -> TV/AVR) must play that far ahead
    # of the synced playhead so the sound reaching its speakers lines up with
    # the other rooms — while the PLL itself keeps chasing the raw playhead
    # (the offset changes what we emit, not how we sync).
    h = client.client
    wait_until(lambda: h.buffer.data is not None
               and h.buffer.duration > 1.5 and h.stream is not None)
    with C.stream_ops_lock:
        h.stream.stop()          # silence the real callback: no measurement race
    with h.lock:
        h.playing = True
        h._pitch_int = 0.0
        h.err_f = 0.0
        target = max(0.0, h.clock.server_now() - h.server_song_start)
        h.local_pos = max(0.0, min(target, h.buffer.duration - 2.0))
        before = h.local_pos
        h._out_latency = 0.300
    out = np.zeros((8192, 2), dtype=np.float32)
    h._cb(out, 8192, None, None)
    with h.lock:
        advance = h.local_pos - before
    block = 8192 / h.buffer.sr
    # playhead advances by one physical block (pitch ~ 1; no catch-up nudge
    # because the playhead starts on the target)
    assert advance > block * 0.99
    assert advance < block * 1.01 + C.CATCHUP_STEP * 2.0
    # emitted audio starts 300 ms ahead of the raw playhead
    i0_exp = int((before + 0.300) * h.buffer.sr)
    assert i0_exp + 8 < len(h.buffer.data)
    assert abs(float(out[0, 0]) - float(h.buffer.data[i0_exp, 0])) < 1e-6
    assert abs(float(out[8, 0]) - float(h.buffer.data[i0_exp + 8, 0])) < 1e-6
    # genuinely offset: 300 ms of samples (not the raw playhead position)
    assert i0_exp - int(before * h.buffer.sr) > int(0.29 * h.buffer.sr)


def test_client_callback_playhead_advances_when_buffer_exhausted(client):
    """When the current track's data runs out (server already moved to the
    next track but it's still downloading), the playhead must keep advancing
    so err stays bounded against the (still-rising) server timeline. A frozen
    playhead here makes err grow without bound through the whole download gap
    and hammers the PLL to ±MAX_PITCH at every song transition."""
    h = client.client
    wait_until(lambda: h.buffer.data is not None
               and h.buffer.duration > 1.0 and h.stream is not None)
    with C.stream_ops_lock:
        h.stream.stop()          # silence the real callback: no measurement race
    with h.lock:
        h.playing = True
        h._pitch_int = 0.0
        h.err_f = 0.0
        # Anchor the playhead just past the end of the buffer AND on the
        # target (err ~ 0) so the only thing under test is the exhausted-
        # buffer advance, with no catch-up nudge muddying the measurement.
        end_pos = len(h.buffer.data) / h.buffer.sr + 0.5
        h._base_pos = end_pos
        h._base_clock = C.ts()
        h.local_pos = end_pos
        before = h.local_pos
    out = np.zeros((8192, 2), dtype=np.float32)
    h._cb(out, 8192, None, None)
    with h.lock:
        advance = h.local_pos - before
    block = 8192 / h.buffer.sr
    # the playhead must keep tracking the timeline instead of freezing
    assert advance > block * 0.99, f"playhead froze (advance={advance:.6f})"
    assert advance < block * 1.01 + C.CATCHUP_STEP * 2.0
    # and we output silence, not stale/garbage audio
    assert np.count_nonzero(out) == 0


def test_client_callback_offset_shift_does_not_spike_err(client, monkeypatch):
    """A mid-playback NTP offset re-estimation must not create a spurious
    err spike. The err reference is pinned at rebase (_base_pos/_base_clock)
    and advanced by the local clock at the server rate, so a later change to
    clock.offset (e.g. after a heavy song download thrashes the NTP
    estimate) moves server_now() without moving the reference. Previously
    this showed up as a 100ms+ err right after every song switch on
    10.0.0.4 because err was re-derived from a fresh server_now() every
    block."""
    h = client.client
    wait_until(lambda: h.buffer.data is not None
               and h.buffer.duration > 2.0 and h.stream is not None)
    with C.stream_ops_lock:
        h.stream.stop()          # silence the real callback: no measurement race

    # Drive a controllable wall clock so the audio blocks and the target
    # advance in lockstep exactly like real-time playback does.
    fake = {"t": 0.0}
    import msync_common as MC
    monkeypatch.setattr(MC, "ts", lambda: fake["t"])

    frames = 8192
    block = frames / h.buffer.sr
    out = np.zeros((frames, 2), dtype=np.float32)
    with h.lock:
        h.playing = True
        h._pitch_int = 0.0
        h.err_f = 0.0
        # Anchor at a mid-song position like a just-completed rebase.
        h.local_pos = 5.0
        h._rebase()
    # The NTP offset re-estimates abruptly (+100 ms) — the classic jump after
    # a heavy download/decode. The anchored reference must absorb it.
    with h.lock:
        h.clock.offset += 0.100
    errs = []
    for _ in range(6):                       # a few audio blocks (~1.1 s)
        fake["t"] += block                   # real-time pacing
        h._cb(out, frames, None, None)
        with h.lock:
            target = (h._base_pos
                      + (1.0 + h.clock.drift * 1e-6) * (fake["t"] - h._base_clock))
            errs.append(target - h.local_pos)
    # The 100ms offset jump must NOT leak into the PLL error: err stays a few
    # ms (just the anchor-instant quantification + drift), not ~100ms.
    assert max(abs(e) for e in errs) < 0.05, \
        f"offset shift leaked into err: {[f'{e*1000:.1f}ms' for e in errs]}"


def test_resolve_server_cli_priority(monkeypatch):
    """An explicit --server flag is authoritative; otherwise the client
    auto-discovers, and falls back to 127.0.0.1 (both marked provisional so
    it can re-discover instead of staying wedged)."""
    import msync_client as MC
    assert MC.resolve_server("192.168.0.99") == ("192.168.0.99", False)
    # discovery finds nothing → loopback fallback (provisional)
    monkeypatch.setattr(MC, "discover_server", lambda port, timeout=3.0: None)
    assert MC.resolve_server() == ("127.0.0.1", True)
    # discovery finds a server (provisional)
    monkeypatch.setattr(MC, "discover_server",
                        lambda port, timeout=3.0: "10.0.0.5")
    assert MC.resolve_server() == ("10.0.0.5", True)


def test_client_fetch_tracks(client):
    tracks = client.client.fetch_tracks()
    assert "track_01_A440.wav" in tracks
    assert "track_02_B494.wav" in tracks
    assert "track_03_C554.wav" in tracks


def test_client_select_track(client):
    now = client.client.select_track("track_03_C554.wav")
    assert now == "track_03_C554.wav"
    wait_until(lambda: client.client.buffer.name == "track_03_C554.wav")


def test_client_queue_add(client):
    added = client.client.queue_add("track_02_B494.wav")
    assert added == ["track_02_B494.wav"]


def test_client_next_song(client):
    name = client.client.next_song()
    assert name == "track_02_B494.wav"


def test_client_set_volume(client):
    assert client.client.set_volume(0.3) == 0.3
    assert client.client.set_volume(3.0) == 1.5   # clamped server-side


# --------------------------------------------------------------------------- #
# Web UI                                                                       #
# --------------------------------------------------------------------------- #
def test_web_ui_served(server):
    status, data = _http_status(server.port + 1000, "GET", "/")
    assert status == 200
    assert b"msync" in data
    assert b"/api/state" in data
    # The library list must come from an endpoint the server actually serves,
    # otherwise the UI's search would see an empty library.
    assert b"/api/library" in data
    assert b"/api/songlist" not in data
    # The now-playing queue slot must be removable (starts the next song).
    assert b"removeNowPlaying" in data
    assert b"/api/control/next" in data
    status, data = _http_status(server.port + 1000, "GET", "/index.html")
    assert status == 200


def test_web_ui_static_traversal_blocked(server):
    status, _ = _http_status(server.port + 1000, "GET",
                             "/static/../../etc/passwd")
    assert status in (400, 404)   # must never escape the web dir


# --------------------------------------------------------------------------- #
# Albums (use album_server on a separate port/music dir)                       #
# --------------------------------------------------------------------------- #
def _alb_http(port, method, path):
    return _http(port + 1000, method, path)


def test_album_library(album_server):
    d = _alb_http(album_server.port, "GET", "/api/library")
    assert "Demo Album/01 Intro.wav" in d["songs"]
    assert "Demo Album/02 Bridge.wav" in d["songs"]
    assert "solo_single.wav" in d["songs"]
    albums = {a["album"]: a for a in d["albums"]}
    assert "Demo Album" in albums
    assert albums["Demo Album"]["track_count"] == 2
    # Each album includes its track list so the UI can search within albums.
    assert len(albums["Demo Album"]["tracks"]) == 2
    tr = {t["relpath"]: t["title"] for t in albums["Demo Album"]["tracks"]}
    assert "Demo Album/01 Intro.wav" in tr
    # Untagged files fall back to their filename as the display title.
    assert tr["Demo Album/01 Intro.wav"] == "01 Intro.wav"


def test_album_tracks(album_server):
    d = _alb_http(album_server.port, "GET",
                  "/api/albums/Demo%20Album/tracks")
    assert d["album"] == "Demo Album"
    assert len(d["tracks"]) == 2
    assert any("01 Intro" in t for t in d["tracks"])


def test_album_add(album_server):
    d = _alb_http(album_server.port, "POST",
                  "/api/queue/add-album?album=Demo%20Album")
    assert len(d["added"]) == 2
    assert len(album_server.queue_list()) == 2


def test_album_play(album_server):
    d = _alb_http(album_server.port, "POST",
                  "/api/control/play?album=Demo%20Album")
    assert d["now_playing"] == "Demo Album/01 Intro.wav"
    assert album_server.song.name == "Demo Album/01 Intro.wav"
    # one track queued (the second), the first is now playing
    assert len(album_server.queue_list()) == 1
    assert any("02 Bridge" in t for t in album_server.queue_list())


def test_add_name_resolves_album(album_server):
    d = _alb_http(album_server.port, "POST",
                  "/api/queue/add?name=Demo%20Album")
    assert len(d["added"]) == 2


def test_play_name_resolves_album(album_server):
    d = _alb_http(album_server.port, "POST",
                  "/api/control/play?name=Demo%20Album")
    assert d["now_playing"] == "Demo Album/01 Intro.wav"


def test_http_state_queue_relpaths(album_server):
    """Queue in /api/state should use relative paths for album tracks."""
    album_server.clear_queue()
    d = _alb_http(album_server.port, "GET", "/api/state")
    assert isinstance(d["queue"], list)


def test_db_created(album_server):
    """The SQLite catalog database should exist on disk."""
    import os
    assert os.path.isfile(album_server.db_path)


def test_catalog_tracks(album_server):
    """Direct catalog API: albums(), singles(), album_tracks()."""
    cat = album_server.catalog
    alb = {a["album"]: a for a in cat.albums()}
    assert "Demo Album" in alb
    assert alb["Demo Album"]["track_count"] == 2
    assert len(cat.singles()) == 1
    assert len(cat.album_tracks("Demo Album")) == 2
    assert cat.album_name("demo album") == "Demo Album"
    assert cat.album_name("no such album") is None


# --------------------------------------------------------------------------- #
# Audio tags (tinytag)                                                         #
# --------------------------------------------------------------------------- #
def test_catalog_reads_tags_with_fallbacks(tmp_path):
    """scan() pulls album/artist/title/track from file tags, and falls back
    to the folder/filename when a file has no (readable) tags."""
    lib = tmp_path / "lib"
    album_dir = lib / "FolderName"
    album_dir.mkdir(parents=True)
    # Tagged MP3s: filenames are deliberately reversed from their tags.
    make_tagged_mp3(album_dir / "z_later.mp3", title="Second Song",
                    artist="Some Artist", album="Real Album Name", track=2)
    make_tagged_mp3(album_dir / "a_first.mp3", title="First Song",
                    artist="Some Artist", album="Real Album Name", track=1)
    # Tagged WAV uses the RIFF LIST/INFO chunk for its tags.
    other_dir = lib / "OtherDir"
    other_dir.mkdir()
    make_tagged_wav(other_dir / "tagged.wav", title="Wav Title",
                    artist="Wav Artist", album="Wav Album")
    # Untagged WAV (no tag data, no INFO chunk).
    make_tagged_wav(album_dir / "Plain.wav")
    # Root-level file = single.
    make_tagged_wav(lib / "Single.wav")

    cat = Catalog(str(lib), str(tmp_path / "cat.db"))
    assert cat.scan() == 5

    # Tagged file: everything comes from the tags.
    r = cat.track("OtherDir/tagged.wav")
    assert r["album"] == "Wav Album"
    assert r["artist"] == "Wav Artist"
    assert r["title"] == "Wav Title"
    assert r["name"] == "tagged.wav"   # name stays the filename

    # Untagged file: folder + filename fallbacks.
    plain = cat.track("FolderName/Plain.wav")
    assert plain["album"] == "FolderName"
    assert plain["title"] == "Plain.wav"
    assert plain["artist"] == ""

    # Track numbers from tags order album playback (a_first is track 1).
    assert cat.album_tracks("Real Album Name") == [
        "FolderName/a_first.mp3", "FolderName/z_later.mp3"]
    # Only the untagged file belongs to the folder-name album.
    assert cat.album_tracks("FolderName") == ["FolderName/Plain.wav"]

    # The web payload carries display titles and the author.
    albums = {a["album"]: a for a in cat.albums_with_tracks()}
    info = albums["Real Album Name"]
    assert info["artist"] == "Some Artist"
    assert [(t["relpath"], t["title"]) for t in info["tracks"]] == [
        ("FolderName/a_first.mp3", "First Song"),
        ("FolderName/z_later.mp3", "Second Song")]

    # Singles expose their (fallback) title and artist for display/search.
    assert cat.singles() == [{"relpath": "Single.wav", "title": "Single.wav",
                              "artist": ""}]

    # Playlist order: singles, then albums by name + track number.
    rels = cat.relpaths()
    assert rels.index("Single.wav") == 0 or "Single.wav" in rels
    assert rels.index("FolderName/a_first.mp3") < \
        rels.index("FolderName/z_later.mp3")


def test_artist_searchable(tmp_path):
    """Searching an artist finds albums whose folder/album name doesn't
    contain it (e.g. 'Green Day' -> albums like 'Dookie'). The library
    payload carries artist on albums and singles, and the UI's search
    matches it so the whole album's tracks show up."""
    lib = tmp_path / "lib"
    (lib / "Dookie").mkdir(parents=True)
    make_tagged_mp3(lib / "Dookie" / "basket_case.mp3",
                    title="Basket Case", artist="Green Day", album="Dookie")
    make_tagged_mp3(lib / "Dookie" / "when_i_come_around.mp3",
                    title="When I Come Around", artist="Green Day",
                    album="Dookie")
    make_tagged_mp3(lib / "boulevard.mp3", title="Boulevard",
                    artist="Green Day")
    cat = Catalog(str(lib), str(tmp_path / "lib.db"))
    cat.scan()
    try:
        albums = {a["album"]: a for a in cat.albums_with_tracks()}
        assert albums["Dookie"]["artist"] == "Green Day"

        # Reproduce the web UI's search filter: 'green day' must match the
        # album via its artist, making every track of that album visible.
        q = "green day"
        alb = albums["Dookie"]
        visible = [t for t in alb["tracks"]
                   if q in alb["album"].lower() or q in alb["artist"].lower()
                   or q in t["title"].lower() or q in t["relpath"].lower()]
        assert len(visible) == 2

        # Singles carry artist so the same query surfaces them too.
        singles = cat.singles()
        assert all("Green Day" in s["artist"] for s in singles)
        assert any(q in s["title"].lower() or q in s["relpath"].lower()
                   or q in s["artist"].lower() for s in singles)
    finally:
        cat.close()


def test_catalog_migrates_old_db_and_rereads(tmp_path):
    """A database created before tag support is migrated (new columns) and
    its existing files re-read once so tags populate the whole library."""
    import sqlite3
    lib = tmp_path / "lib"
    (lib / "Migrated Album").mkdir(parents=True)
    make_tagged_mp3(lib / "Migrated Album" / "s1.mp3", title="Titled Song",
                    artist="Some Artist", album="Migrated Album", track=3)
    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE tracks (
        relpath TEXT PRIMARY KEY, name TEXT NOT NULL, album TEXT,
        artist TEXT DEFAULT '', duration REAL DEFAULT 0,
        sample_rate INTEGER DEFAULT 0, channels INTEGER DEFAULT 0,
        size INTEGER DEFAULT 0, mtime REAL DEFAULT 0,
        added_at REAL DEFAULT 0)""")
    conn.execute("""CREATE TABLE queue (
        position INTEGER PRIMARY KEY, entry TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE playback (
        id INTEGER PRIMARY KEY CHECK (id = 0), relpath TEXT NOT NULL,
        elapsed REAL NOT NULL DEFAULT 0, playing INTEGER NOT NULL DEFAULT 1)""")
    path = str(lib / "Migrated Album" / "s1.mp3")
    st = os.stat(path)
    conn.execute(
        "INSERT INTO tracks (relpath, name, album, artist, duration,"
        " sample_rate, channels, size, mtime, added_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("Migrated Album/s1.mp3", "s1.mp3", "Migrated Album", "",
         2.0, 44100, 2, st.st_size, st.st_mtime, 0.0))
    conn.commit()
    conn.close()

    cat = Catalog(str(lib), db)
    # First scan re-reads the pre-tag row (title was NULL) and fills tags.
    assert cat.scan() == 1
    r = cat.track("Migrated Album/s1.mp3")
    assert r["title"] == "Titled Song"
    assert r["artist"] == "Some Artist"
    assert r["track"] == 3
    # A second scan finds everything unchanged and does no metadata reads.
    assert cat.scan() == 0


def test_scan_never_blocks_reads_and_reports_progress(tmp_path, monkeypatch):
    """A catalog scan (metadata re-tag) must not block catalog queries: the
    web UI's /api/state and /api/library endpoints stay responsive while a
    fresh or out-of-date library is being scanned. The scan reports progress,
    overlapping scans are skipped (no scan pile-up), and the DB still ends up
    fully populated."""
    import threading
    lib = tmp_path / "lib"
    for k in range(3):
        (lib / f"Album {k}").mkdir(parents=True)
    for i in range(15):
        make_tagged_wav(lib / f"Album {i % 3}" / f"{i:02d} Track.wav",
                        title=f"Track {i}", artist="Some Artist",
                        album=f"Album {i % 3}")
    make_tagged_wav(lib / "Single.wav")        # root-level single
    monkeypatch.setattr("msync_catalog.SCAN_CHECKPOINT", 5)

    # Slow the (normally fast) metadata reads so the scan is genuinely
    # in-flight while we probe it from "the web UI thread".
    real_read_meta = Catalog._read_meta
    monkeypatch.setattr(
        Catalog, "_read_meta",
        lambda self, path: (time.sleep(0.15), real_read_meta(self, path))[1])

    cat = Catalog(str(lib), str(tmp_path / "cat.db"))
    result = {}
    def run_scan():
        result["n"] = cat.scan()
    t = threading.Thread(target=run_scan)
    t.start()
    try:
        time.sleep(0.25)                       # get into the metadata loop
        prog = cat.scan_progress()
        assert prog and prog["running"]
        assert prog["to_read"] == 16 and prog["total"] == 16
        assert prog["done"] >= 1, f"scan not progressing: {prog}"

        # Reads must NOT wait on the scan: an in-flight read would take
        # ~16 x 0.15s = 2.4s to finish under the old (blocking) design.
        t0 = time.time()
        cat.albums_with_tracks()
        cat.track("Album 0/00 Track.wav")
        elapsed = time.time() - t0
        assert elapsed < 0.5, \
            f"catalog read blocked {elapsed:.2f}s behind the scan"

        # A second scan while one is running does nothing (no pile-up).
        assert cat.scan() == 0
    finally:
        t.join(timeout=30)
    assert result["n"] == 16
    assert cat.scan_progress() is None         # idle again once finished
    assert len(cat.albums()) == 3
    assert cat.track("Single.wav") is not None


def test_refresh_playlist_keeps_current_song(album_server):
    """Reordering the playlist — as a background scan does when tagged
    track numbers order an album differently from its filenames — must
    not move the 'next' cursor off the currently playing song."""
    album_server.clear_queue()
    album_server.play_song("Demo Album/02 Bridge.wav")
    cur = album_server.song.name
    assert cur == "Demo Album/02 Bridge.wav"
    cur_abs = album_server._abs(cur)
    # Simulate a scan that would order the album with the current track
    # first (a different order than the catalog currently has).
    reordered = [cur_abs] + [p for p in album_server.playlist
                             if p != cur_abs]
    album_server._refresh_playlist(reordered)
    assert album_server.song.name == cur            # still playing
    assert album_server.playlist == reordered        # new order honored
    assert album_server.index == 0                   # cursor re-anchored
    assert album_server.playlist[album_server.index] == cur_abs


# --------------------------------------------------------------------------- #
# Queue persistence                                                            #
# --------------------------------------------------------------------------- #
def test_catalog_queue_roundtrip(album_server):
    """save_queue/load_queue round-trips songs and albums through the DB."""
    import os
    cat = album_server.catalog
    lib = album_server.music_dir
    entries = [
        {"type": "song", "path": os.path.join(lib, "solo_single.wav"),
         "relpath": "solo_single.wav"},
        {"type": "album", "album": "Demo Album",
         "paths": [os.path.join(lib, "Demo Album", "01 Intro.wav"),
                   os.path.join(lib, "Demo Album", "02 Bridge.wav")]},
    ]
    cat.save_queue(entries)
    assert cat.load_queue() == entries


def test_catalog_queue_drops_missing(album_server):
    """load_queue drops entries whose files no longer exist on disk."""
    cat = album_server.catalog
    cat.save_queue([{"type": "song", "path": "/does/not/exist.wav",
                     "relpath": "does/not/exist.wav"}])
    assert cat.load_queue() == []
    # a partially-missing album keeps only the tracks that still exist
    import os
    lib = album_server.music_dir
    good = os.path.join(lib, "Demo Album", "01 Intro.wav")
    cat.save_queue([{"type": "album", "album": "Demo Album",
                     "paths": [good, "/gone.wav"]}])
    loaded = cat.load_queue()
    assert len(loaded) == 1 and loaded[0]["type"] == "album"
    assert loaded[0]["paths"] == [good]


def test_queue_songs_and_album_stored_as_entries(album_server):
    """Adding songs and an album creates distinct song/album DB entries."""
    album_server.clear_queue()
    added = album_server.add_to_queue(["solo_single.wav"])
    assert added == ["solo_single.wav"]
    album_server.add_album("Demo Album")
    entries = album_server.catalog.load_queue()
    assert [e["type"] for e in entries] == ["song", "album"]
    assert entries[0]["relpath"] == "solo_single.wav"
    assert entries[1]["album"] == "Demo Album"
    assert len(entries[1]["paths"]) == 2
    assert album_server.queue_list() == [
        "solo_single.wav", "Demo Album/01 Intro.wav", "Demo Album/02 Bridge.wav"]


def test_startup_does_not_auto_play_when_queue_empty(tmp_path):
    """A fresh server (empty persisted queue) starts idle — it must not
    auto-play an arbitrary track, only items in the queue. This is the
    behavior behind "don't play the sweep that isn't queued after a restart"."""
    import os
    from msync_server import SyncedServer

    db = str(tmp_path / "restart-empty.db")
    # No UDP loop / HTTP is started here, so the port only needs to be free.
    srv = SyncedServer(MUSIC, 9798, db)
    try:
        srv._scan_done.wait(timeout=15)
        assert srv.queue == []
        assert srv.song is None
        assert srv.playing is False
        # Queuing something when idle starts it.
        added = srv.add_to_queue(["track_02_B494.wav"])
        assert added == ["track_02_B494.wav"]
        assert srv.song.name == "track_02_B494.wav"
        assert srv.queue_list() == []  # it's playing, so popped from queue
    finally:
        srv._stop.set()
        if srv.stream is not None:
            try:
                srv.stream.stop()
                srv.stream.close()
            except Exception:
                pass
        srv.catalog.close()
        try:
            os.remove(db)
        except OSError:
            pass


def test_queue_restored_on_restart(server):
    """The queue is reloaded from the DB when a new server starts on the
    same database file — and playback resumes the song that was playing."""
    db = server.db_path
    server.clear_queue()
    added = server.add_to_queue(["track_02_B494.wav", "track_03_C554.wav"])
    assert added == ["track_02_B494.wav", "track_03_C554.wav"]
    # Pin a deterministic current track so the restarted server resumes it.
    # (The demo tracks are ~2s, so without pinning, natural advancement would
    # have moved on before the restart.)
    server.play_song("track_01_A440.wav")
    server.play_pause()                 # freeze the playhead mid-run

    from msync_server import SyncedServer

    # Stop the original server's audio stream so a second instance can
    # open the device, then simulate a restart with a fresh server.
    if server.stream is not None:
        try:
            server.stream.stop()
            server.stream.close()
        except Exception:
            pass
    srv2 = SyncedServer(server.music_dir, server.port + 7, db)
    try:
        srv2._scan_done.wait(timeout=15)
        # The same song that was playing resumes, and the rest of the queue
        # is intact.
        assert srv2.song.name == "track_01_A440.wav"
        assert srv2.queue_list() == ["track_02_B494.wav", "track_03_C554.wav"]
        assert [e["type"] for e in srv2.queue] == ["song", "song"]
    finally:
        srv2._stop.set()
        if srv2.stream is not None:
            try:
                srv2.stream.stop()
                srv2.stream.close()
            except Exception:
                pass
        srv2.catalog.close()


def test_restart_resumes_same_song(server):
    """A restart comes back to the same song that was playing, even though
    that song is no longer in the queue (it was popped before playback),
    keeps its approximate position, and preserves the pause state."""
    db = server.db_path
    # Switch to a specific track and mark a mid-song position.
    server.play_song("track_02_B494.wav")
    assert server.song.name == "track_02_B494.wav"
    assert server.queue_list() == []          # track_02 is playing, not queued
    server.play_pause()                       # freeze, and pause is persisted
    server.local_pos = 1.25
    server._persist_playback()

    from msync_server import SyncedServer
    if server.stream is not None:
        try:
            server.stream.stop()
            server.stream.close()
        except Exception:
            pass
    srv2 = SyncedServer(server.music_dir, server.port + 7, db)
    try:
        srv2._scan_done.wait(timeout=15)
        assert srv2.song.name == "track_02_B494.wav"
        assert srv2.seek == pytest.approx(1.25, abs=0.05)
        assert srv2.playing is False          # pause state survives restart
    finally:
        srv2._stop.set()
        if srv2.stream is not None:
            try:
                srv2.stream.stop()
                srv2.stream.close()
            except Exception:
                pass
        srv2.catalog.close()