"""
msync_inotify.py - inotify-based watcher for the music directory.

Detects new files added to the music directory (and subdirectories)
and refreshes the catalog immediately, rather than waiting for the
polling interval.
"""

import os
import threading
import time

import msync_common as C

# Audio extensions (must match msync_catalog.py)
AUDIO_EXTS = {".mp3", ".ogg", ".wav", ".flac", ".m4a", ".aac", ".opus", ".wma"}


class MusicWatcher:
    """
    Watch the music directory for new/removed audio files using Linux inotify.
    
    When a change is detected, calls catalog.scan() to refresh the database.
    Debounces rapid changes (e.g., bulk copies) with a configurable interval.
    """

    def __init__(self, music_dir, catalog, stop_event, debounce_sec=2.0):
        """
        Parameters
        ----------
        music_dir : str
            Root music directory to watch.
        catalog : Catalog
            The catalog instance to refresh on changes.
        stop_event : threading.Event
            Set this event to stop the watcher thread.
        debounce_sec : float
            Minimum seconds between catalog refreshes (default: 2.0).
        """
        self.music_dir = music_dir
        self.catalog = catalog
        self.stop_event = stop_event
        self.debounce_sec = debounce_sec
        self._thread = None

    def start(self):
        """Start the watcher in a background daemon thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        C.logger().info("watching %s for music changes", self.music_dir)

    def stop(self):
        """Signal the watcher to stop (also happens via stop_event)."""
        self.stop_event.set()

    # ------------------------------------------------------------------ #
    def _run(self):
        try:
            import inotify_simple
            from inotify_simple import flags
        except ImportError:
            C.logger().warning("inotify_simple not installed – falling back to "
                               "polling")
            self._polling_fallback()
            return

        watch_mask = (flags.CREATE | flags.MOVED_TO | flags.CLOSE_WRITE |
                      flags.DELETE | flags.MOVED_FROM)

        inotify = inotify_simple.INotify()
        watches = {}            # wd -> directory path

        def add_watch(path):
            """Recursively add inotify watches for *path* and its children."""
            try:
                wd = inotify.add_watch(path, watch_mask)
                watches[wd] = path
            except OSError:
                return
            for entry in os.scandir(path):
                if entry.is_dir() and not entry.name.startswith("."):
                    add_watch(entry.path)

        add_watch(self.music_dir)

        last_refresh = time.time()

        try:
            while not self.stop_event.is_set():
                # Block until an event arrives or 1 s timeout
                events = inotify.read(timeout=1000)
                now = time.time()
                if not events:
                    continue

                refresh_needed = False
                new_dirs = []

                for ev in events:
                    name = ev.name
                    if not name or name.startswith("."):
                        continue
                    ext = os.path.splitext(name)[1].lower()
                    if ext in AUDIO_EXTS:
                        refresh_needed = True
                    # Track newly created directories to add watches
                    if ev.mask & flags.CREATE:
                        parent = watches.get(ev.wd, self.music_dir)
                        full = os.path.join(parent, name)
                        if os.path.isdir(full):
                            new_dirs.append(full)

                # Add watches for any new subdirectories (new albums)
                for d in new_dirs:
                    add_watch(d)

                # Debounce: don't scan more often than debounce_sec
                if refresh_needed and (now - last_refresh) >= self.debounce_sec:
                    self.catalog.scan()
                    last_refresh = now
                    C.logger().info("catalog refreshed (file change detected)")

        except Exception as exc:
            C.logger().error("watcher error: %s", exc)
        finally:
            inotify.close()

    # ------------------------------------------------------------------ #
    def _polling_fallback(self):
        """Fallback: poll the directory every hour if inotify is unavailable.
        This is only a safety net — inotify provides immediate notification,
        and the server's monitor_loop already rescans the catalog periodically."""
        while not self.stop_event.is_set():
            self.stop_event.wait(timeout=3600.0)
            if self.stop_event.is_set():
                break
            self.catalog.scan()
