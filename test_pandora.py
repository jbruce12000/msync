"""
test_pandora.py - tests for the msync Pandora thread (mock mode, no network).

These run the standalone PandoraService with fabricated stations/tracks and
tiny generated WAV files, so they cover the thread, the lookahead/top-up
loop, dedup, and the submit callback — the parts wired into the server.
"""

import http.client
import json
import os
import threading
import time

import msync_pandora as P
from conftest import make_tagged_wav, wait_until


def test_finite_harvest_queues_tracks(tmp_path):
    music = tmp_path / "music"
    music.mkdir()
    submitted = []

    svc = P.PandoraService(str(music), mock=True,
                          submit=lambda p: submitted.append(p))
    assert svc.connect_now()
    assert svc.play_station("Lite Pop", want=4)
    assert svc.run_until_done(timeout=30)

    assert svc.status()["downloaded"] == 4
    assert len(submitted) == 4
    folder = os.path.join(str(music), "Pandora - Lite Pop")
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

    svc = P.PandoraService(str(music), mock=True,
                          submit=lambda p: submitted.append(p))
    assert svc.play_station("s-rock", want=8)   # pool has only 6 unique songs
    assert svc.run_until_done(timeout=30)

    names = {os.path.basename(p) for p in submitted}
    assert len(names) == len(submitted)         # no duplicate filenames
    assert len(submitted) == 6                   # capped at the unique pool
    assert svc.status()["downloaded"] == 6
    assert os.path.isfile(os.path.join(
        str(music), "Pandora - Deep Cuts Rock",
        "Rita Kane - Highway Static.wav"))


def test_continuous_lookahead_tops_up(tmp_path):
    """When the queue drains below topup_when, the thread refetches a batch."""
    music = tmp_path / "music"
    music.mkdir()
    queue = []                                   # simulated server queue
    svc = P.PandoraService(str(music), mock=True,
                           lookahead=3, topup_when=2,
                           queued=lambda: len(queue),
                           submit=lambda p: queue.append(p))
    assert svc.play_station("s-acoustic")
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
    """A fresh session against files already on disk queues/gathers nothing new."""
    music = tmp_path / "music"
    music.mkdir()
    seen = {"submissions": []}

    svc = P.PandoraService(str(music), mock=True,
                          submit=lambda p: seen["submissions"].append(p))
    assert svc.play_station("Lite Pop", want=4)
    assert svc.run_until_done(timeout=30)
    assert svc.status()["downloaded"] == 4

    # second session, same folder, same station: everything already exists
    svc2 = P.PandoraService(str(music), mock=True,
                            submit=lambda p: seen["submissions"].append(p))
    assert svc2.play_station("Lite Pop", want=4)
    assert svc2.run_until_done(timeout=30)
    assert svc2.status()["downloaded"] == 0       # nothing new to fetch
    assert len(seen["submissions"]) == 4          # no re-submissions either


# --------------------------------------------------------------------------- #
# Integration: PandoraService wired into SyncedServer (mock mode, no network). #
# --------------------------------------------------------------------------- #
def _make_server(tmp_path, name, port):
    """A SyncedServer in a tmp music dir (one seed track so __init__ passes)
    whose PandoraService is wired to the server queue. The service is injected
    explicitly (PandoraService(mock=True) wired to the server's queue
    callbacks) so the tests don't depend on config.PANDORA_ENABLED and can
    never touch a real Pandora account. Caller starts/cleans up."""
    import msync_server as MS
    import msync_pandora as P
    music = tmp_path / "music"
    music.mkdir()
    make_tagged_wav(str(music / "seed.wav"), title="Seed", artist="Seed")
    db = str(tmp_path / f"{name}.db")
    srv = MS.SyncedServer(str(music), port, db)      # PANDORA_ENABLED off by default
    svc = P.PandoraService(str(music), mock=True,
                           queued=srv._pandora_queued,
                           submit=srv._pandora_submit)
    srv.pandora = svc
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


_PANDORA_PORT = 9805


def test_server_wiring_queues_downloads(tmp_path):
    """Downloads from a running Pandora thread land in the server's queue
    (and automatically start playback when nothing is loaded)."""
    srv, MS = _make_server(tmp_path, "wire", _PANDORA_PORT)
    try:
        assert srv.pandora is not None
        srv.pandora.start()
        # service connects + loads the (mock) stations on its own
        assert wait_until(
            lambda: (srv.pandora_status() or {}).get("stations", 0) >= 3,
            timeout=10, label="pandora stations loaded")
        assert srv.pandora.connect_now()

        assert srv.pandora.play_station("Lite Pop")      # continuous radio
        assert wait_until(lambda: len(srv.queue_list()) >= 2, timeout=30,
                          label="pandora downloads submitted to queue")

        folder = os.path.join(str(srv.music_dir), "Pandora - Lite Pop")
        assert os.path.isdir(folder)
        for rel in srv.queue_list():
            assert rel.startswith("Pandora - Lite Pop/")

        # The server's status reports the active station + remaining queued.
        st = srv.pandora_status()
        assert st["station"] == "Lite Pop"
        assert st["pending"] == len(srv.queue_list())

        # _state_payload (and thus /api/state) must not deadlock against the
        # Pandora thread even while it is actively topping up.
        srv._state_payload()
    finally:
        srv.pandora.stop(timeout=10)
        srv.close_audio()
        srv.catalog.close()


def test_server_pandora_http_api(tmp_path):
    """The HTTP API exposes station list, play/stop, and status."""
    srv, MS = _make_server(tmp_path, "api", _PANDORA_PORT + 1)
    httpd = _start_http(srv, str(srv.music_dir), _PANDORA_PORT + 1)
    http_port = _PANDORA_PORT + 1 + MS.config.HTTP_PORT_OFFSET
    try:
        srv.pandora.start()
        assert wait_until(
            lambda: (srv.pandora_status() or {}).get("stations", 0) >= 3,
            timeout=10, label="pandora stations loaded")

        status, data = _http_json(http_port, "GET", "/api/pandora/stations")
        assert status == 200 and len(data["stations"]) == 3

        # play a station over HTTP, then watch the server queue fill up
        status, data = _http_json(
            http_port, "POST", "/api/pandora/play",
            {"station": "Deep Cuts Rock"})
        assert status == 200 and data["ok"] is True
        assert wait_until(lambda: len(srv.queue_list()) >= 2, timeout=30,
                          label="queue filled after HTTP play")
        assert all(r.startswith("Pandora - Deep Cuts Rock/")
                   for r in srv.queue_list())

        # /api/state carries a live pandora status block (no deadlock)
        status, data = _http_json(http_port, "GET", "/api/state")
        assert status == 200
        assert data["pandora"]["station"] == "Deep Cuts Rock"
        assert data["pandora"]["pending"] == len(srv.queue_list())

        # status endpoint agrees
        status, data = _http_json(http_port, "GET", "/api/pandora")
        assert status == 200 and data["station"] == "Deep Cuts Rock"

        # "queue more" fetches another batch for the active station and the
        # status reflects the configured batch size
        status, data = _http_json(http_port, "POST", "/api/pandora/more")
        assert status == 200 and data["ok"] is True
        assert data["batch"] == MS.config.PANDORA_QUEUE_SIZE
        st = srv.pandora_status()
        assert st["station"] == "Deep Cuts Rock"
        assert st["batch"] == MS.config.PANDORA_QUEUE_SIZE

        # stop over HTTP: no further downloads for a finite account... here we
        # just verify the station clears and the status endpoint reflects it.
        status, data = _http_json(http_port, "POST", "/api/pandora/stop")
        assert status == 200 and data["ok"] is True
        st = srv.pandora_status()
        assert st["station"] is None and st["done"] is True
    finally:
        srv.pandora.stop(timeout=10)
        try:
            httpd.shutdown()
        except Exception:
            pass
        srv.close_audio()
        srv.catalog.close()


# -- AAC transcoding (Pandora's streams need ffmpeg for miniaudio) ---------- #

def _fake_ffmpeg(tmp_path, fail=False):
    """A fake `ffmpeg` that copies $5 into $8 (or exits 1 when failing)."""
    script = tmp_path / "fake-ffmpeg"
    script.write_text("#!/usr/bin/env bash\n"
                      + ("exit 1\n" if fail else 'cp "$5" "$8"\n'))
    script.chmod(0o755)
    return str(script)


def _mk(src_path, content=b"not-really-audio"):
    src_path.write_bytes(content)
    assert src_path.is_file()
    return str(src_path)


def test_transcode_playable_files_pass_through(tmp_path):
    svc = P.PandoraService(str(tmp_path), mock=True)
    svc._ffmpeg_path = None                        # force: no ffmpeg available
    src = _mk(tmp_path / "already.wav")
    path, produced = svc._transcode_for_playback(src)
    assert path == src and produced is False
    assert os.path.isfile(src)                       # untouched
    st = svc.status()
    assert st["ffmpeg"] is False                     # no ffmpeg in this run
    assert st["warn"] is None                        # playable => no warning


def test_transcode_m4a_with_ffmpeg(tmp_path):
    svc = P.PandoraService(str(tmp_path), mock=True)
    svc._ffmpeg_path = _fake_ffmpeg(tmp_path)
    src = _mk(tmp_path / "Artist - Track.m4a", content=b"m4a-bytes")
    path, produced = svc._transcode_for_playback(src)
    assert produced is True
    assert path.endswith(".flac") and os.path.isfile(path)
    with open(path, "rb") as f:
        assert f.read() == b"m4a-bytes"
    assert not os.path.exists(src)                   # AAC source removed
    assert os.path.dirname(path) == str(tmp_path)    # next to the source, same dir
    # an existing FLAC is never re-created / re-produced
    path2, produced2 = svc._transcode_for_playback(src)
    assert produced2 is False and path2 == path


def test_transcode_missing_ffmpeg_keeps_source_and_warns(tmp_path):
    svc = P.PandoraService(str(tmp_path), mock=False, username="u", password="p")
    svc._ffmpeg_path = None                          # force: no ffmpeg
    src = _mk(tmp_path / "Artist - Track.m4a")
    path, produced = svc._transcode_for_playback(src)
    assert path == src and produced is False        # kept, no new file
    st = svc.status()
    assert st["warn"]                                # surfaced in status
    assert "ffmpeg" in st["warn"].lower()


def test_transcode_ffmpeg_failure_keeps_source(tmp_path):
    svc = P.PandoraService(str(tmp_path), mock=True)
    svc._ffmpeg_path = _fake_ffmpeg(tmp_path, fail=True)
    src = _mk(tmp_path / "Artist - Track.m4a")
    path, produced = svc._transcode_for_playback(src)
    assert path == src and produced is False
    assert not os.path.exists(str(tmp_path / "Artist - Track.flac"))  # orphan cleaned


def test_fetch_more_queues_another_batch(tmp_path):
    """One batch queues exactly the batch size and stops; fetch_more() raises
    the target so the thread queues a second batch on demand."""
    music = tmp_path / "music"
    music.mkdir()
    submitted = []
    svc = P.PandoraService(str(music), mock=True, batch=3,
                           submit=lambda p: submitted.append(p))
    assert svc.play_station("Lite Pop", want=3)
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

    # no station active -> fetch_more reports False
    svc.stop_station()
    assert svc.fetch_more() is False