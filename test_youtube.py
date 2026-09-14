"""
test_youtube.py - tests for the msync YouTube thread (mock mode, no network).

These run the standalone YouTubeService with fabricated sources/tracks and
tiny generated WAV files, so they cover the thread, the lookahead/top-up
loop, dedup, and the submit callback — the parts wired into the server.
"""

import http.client
import json
import os
import threading
import time

import msync_youtube as Y
from conftest import make_tagged_wav, wait_until


def test_parse_sources():
    text = "Lofi Beats|lofi hip hop, Synthwave|synthwave mix, Deep Dub|deep dub, Bare"
    out = Y.parse_sources(text)
    assert [s["sourceName"] for s in out] == ["Lofi Beats", "Synthwave", "Deep Dub", "Bare"]
    assert out[0]["query"] == "lofi hip hop"
    assert out[1]["query"] == "synthwave mix"
    assert out[3]["query"] == "Bare"          # no '|' -> same as the name
    assert len({s["sourceToken"] for s in out}) == 4   # unique tokens
    assert Y.parse_sources("") == []
    assert Y.parse_sources("  , | ,") == []


def _svc(music, **kw):
    return Y.YouTubeService(str(music), mock=True, **kw)


def test_finite_harvest_queues_tracks(tmp_path):
    music = tmp_path / "music"
    music.mkdir()
    submitted = []

    svc = _svc(music, submit=lambda p: submitted.append(p))
    assert svc.connect_now()
    assert svc.play_source("Lofi Beats", want=4)
    assert svc.run_until_done(timeout=30)

    assert svc.status()["downloaded"] == 4
    assert len(submitted) == 4
    folder = os.path.join(str(music), "YouTube - Lofi Beats")
    assert os.path.isdir(folder)
    for path in submitted:
        assert os.path.isfile(path)
        assert path.startswith(folder)
        assert os.path.splitext(path)[1] == ".wav"


def test_finite_harvest_dedupes_tracks(tmp_path):
    """Re-served songs (the mock pool cycles) are never double-downloaded."""
    music = tmp_path / "music"
    music.mkdir()
    submitted = []

    svc = _svc(music, submit=lambda p: submitted.append(p))
    assert svc.play_source("src-deepdub", want=8)   # pool has only 6 unique songs
    assert svc.run_until_done(timeout=30)

    names = {os.path.basename(p) for p in submitted}
    assert len(names) == len(submitted)         # no duplicate filenames
    assert len(submitted) == 6                   # capped at the unique pool
    assert svc.status()["downloaded"] == 6
    assert os.path.isfile(os.path.join(
        str(music), "YouTube - Deep Dub", "Fern Holiday - River Stones.wav"))


def test_continuous_lookahead_tops_up(tmp_path):
    """When the queue drains below topup_when, the thread refetches a batch."""
    music = tmp_path / "music"
    music.mkdir()
    queue = []                                   # simulated server queue
    svc = _svc(music, lookahead=3, topup_when=2,
               queued=lambda: len(queue),
               submit=lambda p: queue.append(p))
    assert svc.play_source("src-synthwave")
    svc.start()

    # wait for the first batch (lookahead=3)
    deadline = time.time() + 10
    while time.time() < deadline and len(queue) < 3:
        time.sleep(0.05)
    first_batch = len(queue)
    assert first_batch == 3

    # drain the queue like playback would
    while queue:
        queue.pop(0)
    deadline = time.time() + 10
    while time.time() < deadline and svc.status()["downloaded"] < 6:
        time.sleep(0.05)
    svc.stop(timeout=10)

    assert svc.status()["downloaded"] >= 6        # kept topping up
    assert not svc.is_alive()


def test_no_double_download_for_existing_file(tmp_path):
    """A fresh session against a fully-grabbed source queues/gathers nothing
    new (and never re-submits files that already exist on disk)."""
    music = tmp_path / "music"
    music.mkdir()
    seen = {"submissions": []}

    svc = _svc(music, submit=lambda p: seen["submissions"].append(p))
    # want=8 > the 6-song mock pool: the whole pool gets grabbed (harvest ends
    # exhausted), so there is genuinely nothing new left for a second session.
    assert svc.play_source("Lofi Beats", want=8)
    assert svc.run_until_done(timeout=30)
    assert svc.status()["downloaded"] == 6
    count_before = len(seen["submissions"])

    # second session, same folder, same source: everything already exists
    svc2 = _svc(music, submit=lambda p: seen["submissions"].append(p))
    assert svc2.play_source("Lofi Beats", want=4)
    assert svc2.run_until_done(timeout=30)
    assert svc2.status()["downloaded"] == 0       # nothing new to fetch
    assert len(seen["submissions"]) == count_before  # no re-submissions either


# --------------------------------------------------------------------------- #
# Integration: YouTubeService wired into SyncedServer (mock mode, no network). #
# --------------------------------------------------------------------------- #
def _make_server(tmp_path, name, port):
    """A SyncedServer in a tmp music dir (one seed track so __init__ passes)
    whose YouTubeService is wired to the server queue. The service is injected
    explicitly (YouTubeService(mock=True) wired to the server's queue
    callbacks) so the tests don't depend on config.YOUTUBE_ENABLED and can
    never touch the network."""
    import msync_server as MS
    music = tmp_path / "music"
    music.mkdir()
    make_tagged_wav(str(music / "seed.wav"), title="Seed", artist="Seed")
    db = str(tmp_path / f"{name}.db")
    srv = MS.SyncedServer(str(music), port, db)
    svc = Y.YouTubeService(str(music), mock=True,
                           batch=MS.config.YOUTUBE_QUEUE_SIZE,
                           queued=srv._youtube_queued,
                           submit=srv._youtube_submit)
    srv.youtube = svc
    return srv, MS


def _start_http(srv, music_dir, port):
    """Boot the HTTP API (same as the server's main()); returns the httpd."""
    import msync_server as MS
    from http.server import ThreadingHTTPServer
    Handler = MS.build_handler(srv, music_dir)
    httpd = ThreadingHTTPServer(("", port + MS.config.HTTP_PORT_OFFSET), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _http_json(port, method, path, body=None, timeout=5.0):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    payload = json.dumps(body).encode() if body is not None else None
    conn.request(method, path, body=payload,
                 headers={"Content-Type": "application/json"} if payload else {})
    r = conn.getresponse()
    data = json.loads(r.read().decode())
    conn.close()
    return r.status, data


_YOUTUBE_PORT = 9810


def test_server_wiring_queues_downloads(tmp_path):
    """Downloads from a running YouTube thread land in the server's queue
    (and automatically start playback when nothing is loaded)."""
    srv, MS = _make_server(tmp_path, "wire", _YOUTUBE_PORT)
    try:
        assert srv.youtube is not None
        srv.youtube.start()
        # the service connects (mock) and is immediately ready
        assert wait_until(
            lambda: (srv.youtube_status() or {}).get("connected", False),
            timeout=10, label="youtube connected")
        assert srv.youtube.play_source("Lofi Beats")      # continuous radio
        assert wait_until(lambda: len(srv.queue_list()) >= 2, timeout=30,
                          label="youtube downloads submitted to queue")

        folder = os.path.join(str(srv.music_dir), "YouTube - Lofi Beats")
        assert os.path.isdir(folder)
        for rel in srv.queue_list():
            assert rel.startswith("YouTube - Lofi Beats/")

        # The server's status reports the active source + remaining queued.
        st = srv.youtube_status()
        assert st["station"] == "Lofi Beats"
        assert st["pending"] == len(srv.queue_list())

        # _state_payload (and thus /api/state) must not deadlock against the
        # YouTube thread even while it is actively topping up.
        srv._state_payload()
    finally:
        srv.youtube.stop(timeout=10)
        srv.close_audio()
        srv.catalog.close()


def test_server_youtube_http_api(tmp_path):
    """The HTTP API exposes source list, play/stop, and status."""
    srv, MS = _make_server(tmp_path, "api", _YOUTUBE_PORT + 1)
    httpd = _start_http(srv, str(srv.music_dir), _YOUTUBE_PORT + 1)
    http_port = _YOUTUBE_PORT + 1 + MS.config.HTTP_PORT_OFFSET
    try:
        srv.youtube.start()
        assert wait_until(
            lambda: (srv.youtube_status() or {}).get("connected", False),
            timeout=10, label="youtube connected")

        status, data = _http_json(http_port, "GET", "/api/youtube/sources")
        assert status == 200 and len(data["sources"]) == 3

        # play a source over HTTP, then watch the server queue fill up
        status, data = _http_json(
            http_port, "POST", "/api/youtube/play",
            {"source": "Synthwave"})
        assert status == 200 and data["ok"] is True
        assert wait_until(lambda: len(srv.queue_list()) >= 2, timeout=30,
                          label="queue filled after HTTP play")
        assert all(r.startswith("YouTube - Synthwave/")
                   for r in srv.queue_list())

        # /api/state carries a live youtube status block (no deadlock)
        status, data = _http_json(http_port, "GET", "/api/state")
        assert status == 200
        assert data["youtube"]["station"] == "Synthwave"
        assert data["youtube"]["pending"] == len(srv.queue_list())

        # status endpoint agrees
        status, data = _http_json(http_port, "GET", "/api/youtube")
        assert status == 200 and data["station"] == "Synthwave"

        # "queue more" fetches another batch and reflects the configured size
        status, data = _http_json(http_port, "POST", "/api/youtube/more")
        assert status == 200 and data["ok"] is True
        assert data["batch"] == MS.config.YOUTUBE_QUEUE_SIZE
        st = srv.youtube_status()
        assert st["station"] == "Synthwave"
        assert st["batch"] == MS.config.YOUTUBE_QUEUE_SIZE

        # stop over HTTP clears the source
        status, data = _http_json(http_port, "POST", "/api/youtube/stop")
        assert status == 200 and data["ok"] is True
        st = srv.youtube_status()
        assert st["station"] is None and st["done"] is True
    finally:
        srv.youtube.stop(timeout=10)
        try:
            httpd.shutdown()
        except Exception:
            pass
        srv.close_audio()
        srv.catalog.close()


# -- Decoding: webm/opus/m4a downloads need ffmpeg for miniaudio ---------- #

def _fake_ffmpeg(tmp_path, fail=False):
    """A fake `ffmpeg` that copies its input (the file after ``-i``) to its
    output (the last argument each invocation), like a real transcode/split."""
    script = tmp_path / "fake-ffmpeg"
    body = (
        "exit 1\n" if fail else
        """args=("$@")
n=${#args[@]}
src=""
for ((i=0;i<n;i++)); do
  if [ "${args[$i]}" = "-i" ]; then src="${args[$((i+1))]}"; fi
done
cp "$src" "${args[$((n-1))]}"
""")
    script.write_text("#!/usr/bin/env bash\nset -e\n" + body)
    script.chmod(0o755)
    return str(script)


def test_transcode_webm_with_ffmpeg(tmp_path):
    """yt-dlp's webm (opus) downloads get transcoded to FLAC for the server."""
    svc = _svc(tmp_path)
    svc._ffmpeg_path = _fake_ffmpeg(tmp_path)
    src = tmp_path / "Artist - Track.webm"
    src.write_bytes(b"webm-bytes")
    path, produced = svc._transcode_for_playback(str(src))
    assert produced is True
    assert path.endswith(".flac") and os.path.isfile(path)
    with open(path, "rb") as f:
        assert f.read() == b"webm-bytes"
    assert not os.path.exists(str(src))              # source removed
    assert os.path.dirname(path) == str(tmp_path)    # next to the source
    # an existing FLAC is never re-created / re-produced
    path2, produced2 = svc._transcode_for_playback(str(src))
    assert produced2 is False and path2 == path


def test_transcode_missing_ffmpeg_keeps_source_and_warns(tmp_path):
    svc = Y.YouTubeService(str(tmp_path), mock=False)
    svc._ffmpeg_path = None                          # force: no ffmpeg
    src = tmp_path / "Artist - Track.webm"
    src.write_bytes(b"webm-bytes")
    path, produced = svc._transcode_for_playback(str(src))
    assert path == str(src) and produced is False    # kept, no new file
    st = svc.status()
    assert st["warn"]                                # surfaced in status
    assert "ffmpeg" in st["warn"].lower()


# -- candidate pool depth + exhaustion: real 5-batches actually fill ---------- #

def test_queue_more_fills_full_batch_from_deep_pool(tmp_path):
    """'queue 5 more' actually adds 5: candidates come from a deep cached
    pool (SEARCH_POOL_SIZE = 20), not a shallow per-batch listing, so a second
    batch finds fresh tracks instead of recycling the first few results. When
    the pool finally runs dry the service winds down instead of spinning."""
    music = tmp_path / "music"
    music.mkdir()
    submitted = []
    svc = Y.YouTubeService(str(music), mock=False, batch=5,
                           submit=lambda p: submitted.append(p))
    svc._ytdlp_ok = True
    # a deep candidate list, served the way a search would list it
    pool = [{"id": f"vid{i:03d}", "title": f"Song {i:02d}",
             "artist": "Deep Pool", "url": f"https://example.invalid/v{i}",
             "duration": 180} for i in range(25)]
    svc._list_tracks = lambda token, need: [dict(x) for x in pool[:need]]

    folder = os.path.join(str(music), "YouTube - Lofi Beats")

    def fake_fetch(item):
        dest = os.path.join(folder, f"{item['artist']} - {item['title']}.wav")
        if os.path.exists(dest):
            return [dest], []
        os.makedirs(folder, exist_ok=True)          # like the real _fetch_track
        Y._write_mock_wav(dest, 200 + (int(item["id"][3:]) % 800))
        return [dest], [dest]

    svc._fetch_track = fake_fetch

    assert svc.play_source("Lofi Beats", want=5)
    assert svc.run_until_done(timeout=30)
    assert len(submitted) == 5

    # a second batch is served from the same deep pool — all 5 arrive
    assert svc.fetch_more(5) is True
    assert svc.run_until_done(timeout=30)
    assert len(submitted) == 10
    assert len({os.path.basename(p) for p in submitted}) == 10

    # keep asking; the pool (20 from one refill) eventually runs dry at 20
    for _ in range(3):
        assert svc.fetch_more(5) is True
        assert svc.run_until_done(timeout=30)
    assert svc.status()["downloaded"] == 20
    assert len({os.path.basename(p) for p in submitted}) == 20

    # demanding more after the source is dry winds down cleanly
    assert svc.fetch_more(5) is True
    assert svc.run_until_done(timeout=30)
    assert svc.status()["exhausted"] is True
    assert svc.status()["downloaded"] == 20


# -- splitting long 'mix' videos into per-chapter songs ---------------------- #

def test_split_mix_into_chapters(tmp_path):
    """A long mix/compilation splits into one FLAC per song chapter; tiny
    intro/outro chapters are dropped, and re-splitting a finished mix writes
    nothing new."""
    svc = Y.YouTubeService(str(tmp_path), mock=False)
    svc._ffmpeg_path = _fake_ffmpeg(tmp_path)
    src = tmp_path / "vid900.webm"
    src.write_bytes(b"audio-bytes")
    chapters = [
        {"title": "Neon Drive", "start_time": 0.0},
        {"title": "First Light", "start_time": 254.0},
        {"title": "Night City", "start_time": 508.0},
    ]
    paths, news = svc._split_by_chapters(str(src), chapters, 762.0,
                                         str(tmp_path), "Mix Artist")
    assert [os.path.basename(p) for p in paths] == [
        "Mix Artist - Neon Drive.flac",
        "Mix Artist - First Light.flac",
        "Mix Artist - Night City.flac",
    ]
    assert news == paths and all(os.path.isfile(p) for p in paths)
    assert not os.path.exists(str(src))               # blob deleted
    for p in paths:
        with open(p, "rb") as f:
            assert f.read() == b"audio-bytes"         # fake ffmpeg copies

    # re-running finds the already-split files without writing anything new
    paths2, news2 = svc._split_by_chapters(str(src), chapters, 762.0,
                                           str(tmp_path), "Mix Artist")
    assert paths2 == paths and news2 == []

    # chapters shorter than MIN_CHAPTER_S are dropped entirely
    tiny = [{"title": "Intro", "start_time": 0.0},
            {"title": "Clicks", "start_time": 5.0}]
    paths3, news3 = svc._split_by_chapters(str(src), tiny, 30.0,
                                           str(tmp_path / "other"), "A")
    assert paths3 == [] and news3 == []


def test_post_download_splits_chaptered_mix_into_songs(tmp_path):
    """A 27-minute mix with per-song chapters becomes two real songs."""
    svc = Y.YouTubeService(str(tmp_path), mock=False)
    svc._ffmpeg_path = _fake_ffmpeg(tmp_path)
    src = tmp_path / "vid901.webm"
    src.write_bytes(b"mix-bytes")
    info = {"title": "Synthwave Mix", "uploader": "Some Channel",
            "duration": 27 * 60,
            "chapters": [
                {"title": "Neon Drive", "start_time": 0.0},
                {"title": "First Light", "start_time": 254.0},
            ]}
    folder = tmp_path / "out"
    paths, news = svc._post_download(str(src), str(folder), "Some Channel",
                                     "Synthwave Mix", info)
    assert len(paths) == 2 and news == paths
    assert os.path.dirname(paths[0]) == str(folder)
    assert sorted(os.path.basename(p) for p in paths) == [
        "Some Channel - First Light.flac",
        "Some Channel - Neon Drive.flac",
    ]
    assert not os.path.exists(str(src))               # original blob removed


def test_post_download_skips_long_video_without_chapters(tmp_path):
    """A long video with NO chapters can't be split — the blob is skipped
    instead of queueing one giant 27-minute 'track'."""
    svc = Y.YouTubeService(str(tmp_path), mock=False)
    svc._single_limit = 10 * 60
    src = tmp_path / "vid902.webm"
    src.write_bytes(b"blob-bytes")
    info = {"title": "27 Minute Super Mix", "uploader": "Some Channel",
            "duration": 27 * 60}                    # no "chapters" key
    paths, news = svc._post_download(str(src), str(tmp_path), "Some Channel",
                                     "27 Minute Super Mix", info)
    assert paths == [] and news == []
    assert not os.path.exists(str(src))             # discarded


def test_post_download_keeps_a_single_song(tmp_path):
    """Reasonable-length single tracks keep the friendly Artist - Title name
    and pass through untouched (no chapter splitting, no drop)."""
    svc = Y.YouTubeService(str(tmp_path), mock=False)
    svc._ffmpeg_path = None                         # no transcode: stays webm
    svc._single_limit = 10 * 60
    src = tmp_path / "vid903.webm"
    src.write_bytes(b"song-bytes")
    info = {"title": "All Night", "uploader": "An Artist", "duration": 240}
    folder = tmp_path / "out"
    paths, news = svc._post_download(str(src), str(folder), "An Artist",
                                     "All Night", info)
    assert paths == [os.path.join(str(folder), "An Artist - All Night.webm")]
    assert news == paths and os.path.isfile(paths[0])
    assert not os.path.exists(str(src))             # moved under friendly name


def test_fetch_more_queues_another_batch(tmp_path):
    """One batch queues exactly the batch size and stops; fetch_more() raises
    the target so the thread queues a second batch on demand."""
    music = tmp_path / "music"
    music.mkdir()
    submitted = []
    svc = _svc(music, batch=3, submit=lambda p: submitted.append(p))
    assert svc.play_source("Lofi Beats", want=3)
    assert svc.run_until_done(timeout=30)
    first = svc.status()
    assert first["done"] is True
    assert first["downloaded"] >= 3 and len(submitted) >= 3
    assert first["batch"] == 3
    first_count = len(submitted)

    # a fresh batch is requested explicitly, never automatically
    assert svc.fetch_more() is True
    assert svc.status()["done"] is False
    assert svc.run_until_done(timeout=30)
    second = svc.status()
    assert second["done"] is True
    assert second["downloaded"] >= first["downloaded"]
    assert len(submitted) > first_count              # second batch actually queued

    # no source active -> fetch_more reports False
    svc.stop_source()
    assert svc.fetch_more() is False


# -- search box: create a source at runtime ---------------------------------- #

def test_add_source_persists_across_instances(tmp_path):
    """The web tab / API creates a source with add_source(); real (non-mock)
    services persist it to <music_dir>/youtube_sources.json so it survives
    restarts, and a duplicate search finds the existing source instead."""
    music = tmp_path / "music"
    music.mkdir()

    svc = Y.YouTubeService(str(music), mock=False)   # real mode, no network
    src, created = svc.add_source("Deep Jazz", "smooth deep jazz radio")
    assert created is True and src["sourceName"] == "Deep Jazz"
    assert src["sourceToken"] == "src-deep-jazz"
    assert svc.sources()[-1] == src

    # persisted on disk; sources = 4 defaults + 1 new
    assert os.path.isfile(os.path.join(str(music), "youtube_sources.json"))
    assert len(svc.sources()) == len(Y.DEFAULT_SOURCES) + 1

    # same query again -> existing source, nothing new written
    src2, created2 = svc.add_source("different name", "smooth deep jazz radio")
    assert created2 is False and src2 is src
    assert len(svc.sources()) == len(Y.DEFAULT_SOURCES) + 1

    # a brand-new service on the same dir re-loads the persisted source
    svc2 = Y.YouTubeService(str(music), mock=False)
    names = [s["sourceName"] for s in svc2.sources()]
    assert "Deep Jazz" in names
    assert len(svc2.sources()) == len(svc.sources())


def test_add_source_mock_mode_is_ephemeral(tmp_path):
    """Mock mode creates sources, but never writes them to disk."""
    music = tmp_path / "music"
    music.mkdir()
    svc = _svc(music)
    src, created = svc.add_source("My Bandcamp", "https://x.bandcamp.com")
    assert created is True and src["query"] == "https://x.bandcamp.com"
    assert not os.path.exists(os.path.join(str(music), "youtube_sources.json"))
    # unique token when the slug collides
    src2, created2 = svc.add_source("My Bandcamp!", "other query")
    assert created2 is True and src2["sourceToken"] == "src-my-bandcamp-2"


def test_server_youtube_search_http(tmp_path):
    """POST /api/youtube/search creates (or reuses) a source and starts it."""
    srv, MS = _make_server(tmp_path, "search", _YOUTUBE_PORT + 2)
    httpd = _start_http(srv, str(srv.music_dir), _YOUTUBE_PORT + 2)
    http_port = _YOUTUBE_PORT + 2 + MS.config.HTTP_PORT_OFFSET
    try:
        srv.youtube.start()
        assert wait_until(
            lambda: (srv.youtube_status() or {}).get("connected", False),
            timeout=10, label="youtube connected")

        status, data = _http_json(http_port, "POST", "/api/youtube/search",
                                  {"query": "lofi jazz hip hop beats"})
        assert status == 200 and data["ok"] is True
        assert data["created"] is True
        assert data["source"]["sourceName"] == "lofi jazz hip hop beats"
        assert data["batch"] == MS.config.YOUTUBE_QUEUE_SIZE

        # the new source appears in the source list and started harvesting
        status, data = _http_json(http_port, "GET", "/api/youtube/sources")
        assert status == 200
        assert any(s["sourceName"] == "lofi jazz hip hop beats"
                   for s in data["sources"])
        assert wait_until(lambda: len(srv.queue_list()) >= 2, timeout=30,
                          label="search batch queued")
        assert all(r.startswith("YouTube - lofi jazz hip hop beats/")
                   for r in srv.queue_list())

        # searching the same text reuses the source (no duplicate, still plays)
        status, data = _http_json(http_port, "POST", "/api/youtube/search",
                                  {"query": "lofi jazz hip hop beats"})
        assert status == 200 and data["ok"] is True
        assert data["created"] is False
        assert data["source"]["sourceName"] == "lofi jazz hip hop beats"
        st = srv.youtube_status()
        assert st["station"] == "lofi jazz hip hop beats"

        # blank query is a 400
        status, _ = _http_json(http_port, "POST", "/api/youtube/search",
                               {"query": "   "})
        assert status == 400
    finally:
        srv.youtube.stop(timeout=10)
        try:
            httpd.shutdown()
        except Exception:
            pass
        srv.close_audio()
        srv.catalog.close()