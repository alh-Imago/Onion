"""
watcher.py — Live filesystem watching for the daemon's base table.

Wraps the `watchdog` library (chosen deliberately over hand-rolling
inotify/ReadDirectoryChangesW/FSEvents bindings directly -- a mature,
battle-tested library already solves the buffer-overflow handling,
event coalescing, and move-pair reconstruction correctly per platform;
reinventing that blind would be a real risk for no benefit). One
`DirectoryWatcher` per watched root, each normalizing whatever the
platform backend reports into one simple, OS-agnostic callback shape:

    callback(event_type, path, dest_path=None)
    event_type in {"created", "modified", "deleted", "moved"}

Filters out directory-level events (watchdog reports a directory as
"modified" whenever its contents change, which is just noise for a
table that only cares about individual files) -- callers only ever see
file-level events. Deciding WHAT to do with a given path (e.g. "only
.onion files matter") is deliberately left to the callback, not this
module -- this stays a generic file-change notifier, not something that
hardcodes Onion-specific rules.
"""

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


class _Handler(FileSystemEventHandler):
    def __init__(self, callback):
        self._callback = callback

    def on_created(self, event):
        if not event.is_directory:
            self._callback("created", event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self._callback("modified", event.src_path)

    def on_deleted(self, event):
        if not event.is_directory:
            self._callback("deleted", event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._callback("moved", event.src_path, event.dest_path)


class DirectoryWatcher:
    """Watches one directory root (recursively) and calls *callback* on
    every file-level create/modify/delete/move. start()/stop() control
    the underlying watchdog Observer thread; safe to stop() a watcher
    that was never started."""

    def __init__(self, root_path, callback):
        self.root_path = root_path
        self._callback = callback
        self._observer = None

    def start(self):
        if self._observer is not None:
            return  # already running
        self._observer = Observer()
        self._observer.schedule(_Handler(self._callback), self.root_path, recursive=True)
        self._observer.start()

    def stop(self):
        if self._observer is None:
            return
        self._observer.stop()
        self._observer.join(timeout=2)
        self._observer = None

    def is_running(self):
        return self._observer is not None and self._observer.is_alive()
