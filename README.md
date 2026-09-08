# msync — play the same music, in sync, on every machine

msync lets you start a song on one machine and have it play **in sync on
every other machine in the house** — your phone, a laptop, the living-room
PC — all at the same instant, staying together for the whole song.

It's two small Python programs:

- **The server** — one machine you pick to be the DJ. It has the music
  files, plays them, and tells everyone else what's playing.
- **The client** — runs on every other machine. It listens, downloads the
  current song automatically, and plays it perfectly in time with everyone
  else. You never copy music to clients; the server sends it to them.

Audio is **stereo** on every machine, and timing stays aligned to within a
few milliseconds even across Wi-Fi.

### Features

- **Synced playback** — start a song on one machine and every other machine
  plays it in perfect lock-step.
- **Multi-threaded & jitter-free** — audio, sync, catalog scanning, and the
  web/HTTP server each run on their own thread, so playback never stutters
  or clicks — even while the server reacts to new music, serves downloads,
  or queues in the middle of a track.
- **Click-free drift correction** — each client keeps itself locked to the
  sync server by nudging its sample rate by an inaudible fraction of a
  percent, glitch-free — no pops, no cracks, no audio artifacts.
- **Room-latency compensation** — rooms whose sound reaches the speakers
  through a slow path (HDMI → TV/AVR, Bluetooth, …) arrive late no matter
  how tight the sync loop is. The web UI's **Configure** tab sets each
  room's output-latency offset live (−1000 to +1000 ms, 1 ms at a time);
  the value is stored in the server's database and pushed to that room's
  client instantly — and again the next time it starts — so the tuning is
  permanent.
- **Web UI** — browse your local music collection from any browser, search
  by song, artist, or album, and queue up selections with a click. A
  **Configure** tab lists every connected room and tunes each one's output
  latency on the fly. Works on phones, tablets, laptops — any device on the
  same network.
- **Play queue** — build a custom set on the fly; queue individual tracks or
  whole albums from the web UI, any client console, or the HTTP API.
- **Party mode** — one click in the web UI shuffles your whole collection and
  drops 50 random tracks into the queue — hours of music, zero decisions.
- **Album & tag support** — reads audio tags (artist, album, track number,
  title) automatically; groups tracks into albums for easy browsing.
- **Auto-discovery** — new or removed files are picked up while the server
  runs, no restart needed.
- **No music copying** — clients download the current song from the server
  automatically; your library lives in one place.
- **Multi-platform** — runs anywhere Python 3.9+ runs (Linux, macOS,
  Windows).

---

## What you need

- Python 3.9+ on every machine.
- The machines on the same network (Wi-Fi is fine).
- Some music (mp3, ogg, wav, flac, m4a, aac, opus, wma).

## One-time install (once, on each machine)

msync runs as auto-starting **systemd services**: `msync-server` on the DJ
machine, `msync-client` on every other machine. Two installer scripts do the
whole job — they create `./venv`, install the dependencies
(`pip install -r requirements.txt`), register the service, and start it:

- **On the server (the DJ machine, where the music lives):**
  ```bash
  cd msync
  sudo ./install-msync-server-service.sh
  ```
- **On each client machine:**
  ```bash
  cd msync
  sudo ./install-msync-client-service.sh
  ```

The scripts are safe to re-run — they update the service in place and
restart it, which is also how you apply later changes to `config.py`.

### config.py — set your values first

Open `config.py` (next to the scripts) and make sure the values match your
setup **before** installing. The installer reads it; every machine must
agree on the sync port, and each client must know the server's IP:

- **`MUSIC_DIR`** — where the music lives (default `./music`); set on the
  **server**.
- **`SERVER`** — the **server's** IP address, e.g. `10.0.0.2`; set on
  **every client**.
- **`DEFAULT_PORT`** — the UDP sync port (default **9770**); must be the
  same on every machine. The web UI port is `DEFAULT_PORT + HTTP_PORT_OFFSET`
  (default **10770**).
- **`DB_PATH`** — where the server stores its catalog and each room's
  latency settings (default `./msync.db`).
- **`OUTPUT_LATENCY_MS`** — optional fallback for a client whose audio path
  is slow (default `0`); you normally set each room's offset from the web
  UI's Configure tab instead.

(Alternatively set the matching `MSYNC_*` environment variables —
`MSYNC_SERVER`, `MSYNC_MUSIC_DIR`, `MSYNC_DEFAULT_PORT`, `MSYNC_DB_PATH` —
which override config.py. After changing anything, re-run the installer to
apply.)

> Ubuntu/Debian may also need `sudo apt install libportaudio2` (the sound
> library the client/server use).

### Starting / stopping / restarting

- **Restart the server** (e.g. after pulling updated code):
  ```bash
  sudo systemctl restart msync-server
  ```
- **Restart a client**:
  ```bash
  sudo systemctl restart msync-client
  ```
- Check status, or start/stop manually:
  ```bash
  systemctl status msync-client        # or msync-server
  sudo systemctl stop msync-client     # / start / enable / disable
  ```

### Logs

Both services write their output (what's playing, sync quality, errors) to
the system journal. The quickest way to see what a service is doing:

```bash
# follow live (like `tail -f`)
journalctl -u msync-client -f        # or msync-server

# last 50 lines, once
journalctl -u msync-client -n 50

# only recent activity
journalctl -u msync-server --since "10 minutes ago"

# everything since the service was last started
journalctl -u msync-server -b

# warnings and errors only
journalctl -u msync-client -p warning
```

You can also read both services at once:
`journalctl -u msync-server -u msync-client -f`. If you need to
troubleshoot, `sudo journalctl -u msync-client --since today` covers a whole
day in one go.

## Playing music — 3 steps

### 1. Put music on the server machine

Create a `music` folder next to the scripts and drop your songs in it:

```bash
mkdir -p music
cp /path/to/songs/* music/
```

msync reads each file's **audio tags** (via `tinytag`) to show its real
album / artist / track number / song title. Folders and filenames are the
fallback: if a file has no (or unreadable) tags, its folder becomes the album
and its filename the title. So put each album in its own folder and you can
browse, play, or queue whole albums from the web UI, any client, or the HTTP
API — or rely on the tags alone. Files at the top level of `music/` are
individual songs ("singles"). Everything plays in alphabetical order (albums
by tagged track number when available).

New or removed files are detected automatically while the server runs — it
watches the music folder (Linux `inotify`) and refreshes its catalog the
moment they appear, no restart needed. (You can also add files straight to
the play queue, see below.)

Don't have any music handy? Generate 3 demo singles, or demo singles *plus*
a couple of sample albums:

```bash
python3 make_test_music.py          # 3 demo singles
python3 make_test_music.py --with-albums   # add 2 sample albums too
```

### 2. Install the server service (the DJ machine)

With your music in place and `config.py` set (`MUSIC_DIR`, `DEFAULT_PORT`),
run:

```bash
cd msync
sudo ./install-msync-server-service.sh
```

The installer creates `./venv`, installs the dependencies, and registers and
starts the `msync-server` service. That machine is now the DJ — it plays out
loud itself, coordinates everyone else, and serves the web UI. Its startup
looks like this (view it with `journalctl -u msync-server -f`):

```
[server] UDP sync on port 9770
[server] HTTP on port 10770 (music dir: /home/you/msync/music)
[server] web UI: http://<server-ip>:10770/
```

### 3. Install the client service on every other machine

On each other machine, make sure `config.py`'s `SERVER` is set to the DJ
machine's IP (e.g. `10.0.0.2`), then run:

```bash
cd msync
sudo ./install-msync-client-service.sh
```

The installer creates `./venv`, installs the dependencies, and registers and
starts the `msync-client` service. Each client downloads the current song
and joins the party — there's no limit on how many. Its logs
(`journalctl -u msync-client -f`) show a status line every couple of seconds
with the sync quality:

```
[client] playing  song=track_01_A440.wav  pos=  7.71s  target=  7.77s  err=    +0ms ...
```

`err` is how far off the machine is from the DJ — a handful of milliseconds
or less is excellent.

> Prefer a quick manual run for development/testing (no systemd, output on
> your terminal)? Install the deps once with `python3 -m venv venv &&
> ./venv/bin/pip install -r requirements.txt`, then run
> `./venv/bin/python msync_server.py` on the DJ machine and
> `./venv/bin/python msync_client.py --server 192.168.1.50` on a client.

---

## Controlling playback

Everything is controlled from the **server** and everyone follows — from the
browser, from the server/client consoles when run in a terminal, or over
HTTP.

| Key / command | What it does |
|---|---|
| `space` | play / pause |
| `n` | next song (plays anything in the queue first) |
| `p` | previous song |
| `+` / `-` | louder / quieter |
| `add <name>` | add a song to the play queue |
| `queue` | show what's queued |
| `clear` | empty the play queue |
| `q` | quit the server |

## Picking tracks — the web UI

The server includes a **built-in web interface** that lets you browse your
local music collection and queue up selections from any browser — on a phone,
tablet, laptop, or another PC on the same network.

Open a browser and go to:

```
http://<server-ip>:10770/
```

(From the server machine itself that's just `http://localhost:10770/`.)

The page shows:

- **Now Playing** — current track, progress bar, and playback controls.
- **Library** — your entire local music collection, grouped by album, with
  a search box to filter by song title, artist, or album name.
- **Queue** — the upcoming play queue with reorder, remove, clear, and
  **Party** controls. Party mode shuffles your entire collection and queues 50
  random tracks with one click.
- **Configure** — every room running a client, with a per-room slider (and a
  typed value box) for how far that room plays ahead of or behind the sync
  timeline — applied instantly and saved in the server's database. Most
  useful for rooms whose audio path adds latency, like **HDMI into a TV/AVR**
  or Bluetooth speakers. See "The Configure tab" below.

Click **▶ play** next to any track (or album header) to start it instantly
on every machine, or click **+ queue** to line it up next. You can queue
individual tracks, whole albums, or build a custom set on the fly —
everything plays in sync on every connected client. Pause, skip, and volume
controls are right on the page, and it updates itself every second.

## Choosing tracks from a client

Clients also have a small text console — use it when you launch a client
manually in a terminal (as a systemd service it has no keyboard, so drive it
from the web UI instead). While a client is running in a terminal, type:

| command | what it does |
|---|---|
| `tracks` | list everything stored on the server (current song is marked) |
| `albums` | list the albums stored on the server |
| `play "song.mp3"` | start that song on every machine right now |
| `play "Album Name"` | start a whole album (first track now, rest queued) |
| `add "song.mp3"` | add a song to the queue |
| `add "Album Name"` | queue every track in the album |
| `pause` / `resume` | pause or resume everyone |
| `next` / `prev` | skip forward / back |
| `vol 0.7` | set volume (0.0 – 1.5) |
| `latency 120` | tune this room's output-latency offset live (ms); the web UI's Configure tab saves it permanently |
| `queue` | show what's queued |
| `clear` | empty the queue |
| `q` | stop this client |

Or, to just see the library and exit:

```bash
python3 msync_client.py --server 192.168.1.50 --list
```

## The play queue

Queue up tracks and they play, in order, on every machine — like calling a
song at a DJ.

There are several easy ways to add to the queue:

- **On the server console**: `add "my song.mp3"` (globs work too:
  `add *.wav`) or `add "Album Name"` to queue a whole album.
- **In the web UI**: click **+ queue** on any track or album, or remove
  items with the ✕ button.
- **Party mode**: click the **🎉 Party** button in the web UI's Queue panel
  to shuffle your entire collection and queue 50 random tracks — perfect when
  you just want non-stop music. If your library has fewer than 50 tracks,
  everything gets queued; tracks already in the queue are skipped.
- **From a client console**: `add "my song.mp3"` or `add "Album Name"`.
- **Over the network from any machine** (great for guests!):
  ```
  curl -X POST "http://192.168.1.50:10770/api/queue/add?name=my%20song.mp3"
  curl -X POST "http://192.168.1.50:10770/api/queue/add-album?album=My%20Album"
  curl -X POST "http://192.168.1.50:10770/api/control/play?name=my%20song.mp3"
  ```
  (`/api/queue/remove?name=…`, `/api/queue/clear`, and
  `/api/control/play?name=…` work the same way.)
- **The magic drop folder**: while the server runs, copy any audio file into
  `music/.queue/`. The server grabs it, files it into the music folder, and
  adds it to the queue.

**Clearing**: `clear` (or the Queue panel's **clear** button) empties the
whole queue. When something is playing, the current song keeps playing and
the server simply has nothing queued afterward; when nothing is playing (a
paused or stopped server), the clear also drops the stale now-playing slot so
the queue panel truly empties. The ✕ on the now-playing row is the same: it
skips to the next queued song while playing, but when the server isn't
playing and the queue is empty it just removes the current song instead of
starting an arbitrary one.

## Configuration

All the "where things live" settings sit in one file, `config.py`, next to
the scripts — the server and every client read it:

- **`MUSIC_DIR`** — where the server keeps your tracks (default `./music`).
  Either edit the file, or set the `MSYNC_MUSIC_DIR` environment variable to
  point the server at a folder without touching code:
  ```
  MSYNC_MUSIC_DIR=/mnt/nas/songs python3 msync_server.py
  ```
- **`DB_PATH`** — where the server stores its music catalog (an SQLite
  database, default `./msync.db`). Override with `MSYNC_DB_PATH`.
- **`CACHE_DIR`** — where clients store downloaded copies of the current
  track.
- **`DEFAULT_PORT`** — the UDP sync port (default **9770**). Change it in
  config.py or via `MSYNC_DEFAULT_PORT`; every client and server must agree.
- **`HTTP_PORT_OFFSET`** — the HTTP port is the UDP port plus this offset
  (default **+1000** → HTTP **10770**), used for the web UI and downloads.
- **`CLIENT_STALE_AFTER`** — how long (seconds) a room can go without a
  heartbeat before the server deletes it from the catalog DB and the
  Configure tab (default **86400** = 24 hours). Env override:
  `MSYNC_CLIENT_STALE_AFTER`.
- **`OUTPUT_LATENCY_MS`** — **local fallback** for this machine's
  audio-output latency, in milliseconds (default `0`). The normal way to set
  a room's offset is the web UI's Configure tab: the server stores each
  room's value in its database and pushes it to the client whenever it
  connects or the value changes. This config value only applies before a
  freshly-installed client has heard from its server (the server's stored
  value wins as soon as the client registers). Env override:
  `MSYNC_OUTPUT_LATENCY_MS`.

## The Configure tab — rooms with a slow audio path (HDMI, Bluetooth, …)

If one room sounds **late** even though every client reports `err` ≈ 0ms, its
speakers are probably behind a high-latency output chain. The **Configure**
tab in the web UI exists for exactly this: rooms whose audio output adds
latency. That's most common with a client connected by **HDMI into a TV or
AVR** (which buffers 100–300ms of audio for video sync), but also describes
Bluetooth/USB speakers, soundbars, or external DACs with their own
buffering. The sync loop can't see any of that (ALSA/PipeWire only know
about the digital side of the port); only an output-latency offset fixes it.

1. Open `http://<server-ip>:10770/` and switch to the **Configure** tab.
   Every connected room appears there automatically — hostname, IP, and a
   live online/offline status. Clients re-register and heartbeat every few
   seconds, so the list stays current even if you restart a room or the
   server. Rooms that haven't been heard from for over **24 hours** are
   removed from the list (and the database), so dead rooms don't pile up.
2. Each room has a slider spanning **−1000 ms to +1000 ms** in 1 ms steps,
   plus a **value box on the right** that you can drag along with the slider
   *or type into* for an exact offset (Enter or click away to apply). Rooms
   default to **0 ms**.
3. Move the room's slider while music plays and listen: a positive offset
   makes that room play that far *ahead* of the sync timeline, so slow
   speakers arrive in time; a negative offset plays it late relative to the
   others. An HDMI room usually needs a positive offset in the tens to low
   hundreds of ms — start at 0 and slide up until it stops sounding late.
4. The value is stored in the server's database and **pushed to that room's
   client immediately** — and reapplied from the database the next time the
   client connects, so the tuning is permanent until you change it.

Sign is intuitive: if a room is *late*, slide up.

If you'd rather have a measured starting point than tune by ear,
`tools/measure_latency.py` plays a chirp through the target output and a
reference output, records both with a USB microphone, cross-correlates, and
prints a suggested value you can type into the Configure value box:

```bash
./venv/bin/python tools/measure_latency.py --list     # check device names
./venv/bin/python tools/measure_latency.py            # measure
```

(Overrides if auto-detection picks wrong devices: `--sink`, `--ref`, `--mic`
each match a substring of the device name.) The printed value is how much
*slower* the target path is than the reference path — exactly the offset to
enter.

## How the sync stays perfect

The DJ stamps each song with its start time and broadcasts it. Every client
constantly measures how fast its own clock runs compared to the DJ's
(simple NTP), then nudges its playback speed by a tiny, inaudible amount —
at most 0.2% — to stay locked. That's how a whole house keeps in time
without any clicks or skips.

## Firewall / networking

If clients can't see the server, open these ports for the server's IP:

| Port | Use |
|---|---|
| **9770 / UDP** | sync & timing broadcasts |
| **10770 / TCP** | web UI, HTTP API, and music download (`port + 1000` — see config.py) |

## Troubleshooting

- **"No audio device" / sound errors on the server** — it still runs and
  coordinates clients; only its own speakers are silent.
- **Client says "no sync from server"** — check the IP, the firewall, and
  that the server is actually running.
- **A room shows *offline* in the Configure tab** — it hasn't been heard
  from in ~15 seconds. The server marks a room online whenever its client
  sends a sync/timing packet, so make sure that room's client service is
  running and can reach the server (see the bullet above), then it should
  turn green within a few seconds. A room that stops reporting for over
  **24 hours** is removed from the list entirely (configurable via
  `MSYNC_CLIENT_STALE_AFTER`, in seconds).
- **Clients sound out of sync** — wait ~10 seconds for the clock estimator
  to settle; then `err` should drop to a few ms.
- **Music is only on one speaker or sounds thin** — that's expected for the
  demo tracks; real stereo songs play in full stereo on every machine.

## Running the tests (optional)

```bash
source venv/bin/activate
./venv/bin/pip install -r requirements-dev.txt   # adds pytest
pytest -q
```

## Project layout

```
config.py          where everything lives (music dir, catalog DB, ports)
msync_server.py    the DJ: music, albums, queue, timing, HTTP API + web UI
msync_client.py    the listener: downloads + syncs + plays (+ picks tracks)
msync_catalog.py   the SQLite album/track catalog
msync_inotify.py   watches the music folder and refreshes the catalog
msync_common.py    shared sync protocol (you can ignore this)
make_test_music.py generates demo songs (--with-albums for sample albums)
web/               the web UI (index.html + vendored sortable.min.js drag lib)
music/             your music (config.MUSIC_DIR); sub-folders are albums
music/.queue/      drop-to-queue folder (created automatically)
msync.db           the catalog database (config.DB_PATH)
```