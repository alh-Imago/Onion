"""
daemon.py — Onion background daemon (oniond).

A persistent local process the shell connects to, instead of every
command paying the cost of a fresh Python process + cold directory scan.
Designed as the same seam the sidecar/semantic-index watcher (see
docs/sidecar_semantic_index_design_note.md in the main Imago-Unicell
repo) grows into -- this is now a real, working daemon with a real IPC
protocol AND a live filesystem watcher (ace/watcher.py, backed by the
`watchdog` library) keeping the watched-directories base table current
as files are created/modified/deleted/renamed, not just refreshed on a
manual 'daemon rescan'. What's still NOT built, left as a clean
follow-up: the master/local sidecar index (this daemon's base table
covers .onion archives specifically, not the general-file sidecar
concept the design note describes) and its reconciliation-on-reconnect
logic for removable/offline media.

Process lifecycle (start, discover, connect), a minimal command
dispatch (search, ping, shutdown, watch/unwatch/watched/rescan,
search_all), and a warm per-path result cache are all real and tested.
If `watchdog` isn't installed, watching degrades gracefully to
manual-rescan-only rather than crashing the daemon -- same pattern as
PyQt6/prompt_toolkit elsewhere in this project.

Protocol: newline-delimited JSON over a plain TCP socket bound to
127.0.0.1 (port 0 -- OS picks a free port, avoiding collision with
--web's fixed default). Chosen over a Unix domain socket for simple,
uniform cross-platform behaviour (Windows/Linux/Mac alike) without
platform-specific socket API branches, matching the same "keep it
simple, stdlib only" approach already used for the web UI's server.

Discovery: the daemon writes {pid, port} to a small JSON state file in
the user's home directory. A client doesn't need to trust the pid check
at all -- it just tries to connect to the recorded port; if that
succeeds, the daemon is alive and reachable, which is a stronger
guarantee than a pid-liveness check would be anyway (a stale pid could
have been reused by an unrelated process).
"""

import json
import os
import socket
import socketserver
import sys
import threading
import time

STATE_DIR = os.path.join(os.path.expanduser("~"), ".onion")
STATE_FILE = os.path.join(STATE_DIR, "daemon.json")
WATCHED_FILE = os.path.join(STATE_DIR, "watched_dirs.txt")


def _write_state(port: int):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump({"pid": os.getpid(), "port": port}, f)


def _read_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def read_watched_dirs():
    """Plain text, one path per line -- blank lines and '#' comments
    ignored. Deliberately simple and hand-editable, not JSON: the point
    is a user can open this in any text editor and understand it at a
    glance, same reasoning as choosing plain TCP+JSON-lines over a
    binary protocol for the daemon's own IPC."""
    try:
        with open(WATCHED_FILE) as f:
            return [line.strip() for line in f
                    if line.strip() and not line.strip().startswith("#")]
    except OSError:
        return []


def write_watched_dirs(paths):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(WATCHED_FILE, "w") as f:
        f.write("# Onion watched directories -- one path per line.\n")
        f.write("# Hand-editable; the daemon re-reads this on startup, or\n")
        f.write("# use 'daemon watch <path>' / 'daemon unwatch <path>' from the shell.\n")
        for p in paths:
            f.write(p + "\n")


def _find_in_watched(candidate, watched):
    """Case-appropriate match against the watched list: exact on POSIX,
    case-insensitive on Windows -- os.path.normcase() is a no-op on
    POSIX and lowercases on Windows, matching each platform's own
    filesystem convention (Windows paths are case-insensitive by
    default; without this, 'daemon unwatch C:\\Users\\Alan\\Archives'
    would silently fail to match an entry stored as
    'c:\\users\\alan\\archives' even though it's the same directory).
    Returns the ORIGINAL stored entry (not the normcased form) so
    whatever's displayed/removed keeps its natural casing, or None if
    nothing matches."""
    target_norm = os.path.normcase(candidate)
    for w in watched:
        if os.path.normcase(w) == target_norm:
            return w
    return None


class BaseTable:
    """The persistent base table the daemon holds in memory: watched
    directories, each scanned once at watch-time and then kept live via
    a DirectoryWatcher (ace/watcher.py) -- create/modify/delete/move
    events update this table incrementally as they happen, rather than
    only ever refreshing on a manual 'daemon rescan'. Keyed per watched
    directory so a single directory can be rescanned/unwatched/re-
    watched cleanly without touching the others; within a directory,
    keyed by archive path so a single file's create/update/delete is an
    O(1) dict operation, not a full directory re-list."""

    def __init__(self):
        self._lock = threading.Lock()
        self._tables = {}    # watched_dir -> {archive_path: summary}
        self._watchers = {}  # watched_dir -> DirectoryWatcher

    def watched_dirs(self):
        with self._lock:
            return list(self._tables.keys())

    def scan_one(self, path):
        """Full (re)scan of one watched directory -- used both for the
        initial scan when a directory is first watched, and for the
        manual 'daemon rescan' escape hatch (reconciliation sweep, for
        whatever the live watcher might have missed while the daemon
        wasn't running -- see the sidecar design note's reconnect
        discussion; the same principle applies here)."""
        from ace.search import search as run_search
        summaries = list(run_search([path], meta_filters={}, any_text=None, recursive=True))
        by_path = {s["path"]: s for s in summaries}
        with self._lock:
            self._tables[path] = by_path
        return len(by_path)

    def remove(self, path):
        with self._lock:
            self._tables.pop(path, None)
        self._stop_watcher(path)

    def count_for(self, path):
        with self._lock:
            return len(self._tables.get(path, {}))

    def all_summaries(self):
        with self._lock:
            combined = []
            for by_path in self._tables.values():
                combined.extend(by_path.values())
            return combined

    def _start_watcher(self, watched_dir):
        if watched_dir in self._watchers:
            return
        try:
            from .watcher import DirectoryWatcher
        except ImportError:
            print(f"[oniond] Warning: watchdog not installed -- {watched_dir} will only "
                  f"refresh on 'daemon rescan', not live. Install it with: pip install watchdog")
            return
        w = DirectoryWatcher(watched_dir, lambda *a: self._on_event(watched_dir, *a))
        w.start()
        self._watchers[watched_dir] = w

    def _stop_watcher(self, watched_dir):
        w = self._watchers.pop(watched_dir, None)
        if w:
            w.stop()

    def stop_all_watchers(self):
        for watched_dir in list(self._watchers.keys()):
            self._stop_watcher(watched_dir)

    def _on_event(self, watched_dir, event_type, path, dest_path=None):
        """Callback fed to this watched directory's DirectoryWatcher.
        Only .onion files matter to this table -- anything else is
        ignored here (this is the Onion-specific rule the generic
        watcher module deliberately doesn't hardcode itself)."""
        from ace.search import read_summary

        def upsert(p):
            if not p.lower().endswith(".onion"):
                return
            summary = read_summary(p)
            with self._lock:
                table = self._tables.get(watched_dir)
                if table is None:
                    return  # directory was unwatched concurrently with this event
                if summary is not None:
                    table[p] = summary
                else:
                    table.pop(p, None)  # existed as a name but isn't a valid archive (or was deleted mid-read)

        def drop(p):
            with self._lock:
                table = self._tables.get(watched_dir)
                if table is not None:
                    table.pop(p, None)

        if event_type in ("created", "modified"):
            upsert(path)
        elif event_type == "deleted":
            drop(path)
        elif event_type == "moved":
            drop(path)
            if dest_path:
                upsert(dest_path)

    def load_from_disk(self):
        """Called once at daemon startup: read the watched-dirs list and
        scan each. A directory that's been removed/is unreadable since
        the list was last written is skipped with a warning, not a crash.
        This full scan IS this session's reconciliation sweep -- it
        naturally picks up anything that changed while the daemon wasn't
        running, the same way the live watcher will pick up anything
        that changes while it IS running, from this point forward."""
        for path in read_watched_dirs():
            if not os.path.isdir(path):
                print(f"[oniond] Warning: watched directory no longer exists, skipping: {path}")
                continue
            count = self.scan_one(path)
            self._start_watcher(path)
            print(f"[oniond] Watching {path} ({count} archive(s), live)")


_base_table = BaseTable()


class _SearchCache:
    """Warm per-(paths, filters) result cache -- the actual speed-up this
    daemon exists to provide over a cold CLI invocation. Deliberately
    simple (no invalidation beyond a short TTL) for this first version;
    real invalidation is the watcher's job once that's built."""

    def __init__(self, ttl_seconds=10):
        self.ttl = ttl_seconds
        self._store = {}
        self._lock = threading.Lock()

    def _key(self, paths, meta_filters, any_text, recursive):
        return json.dumps([sorted(paths), sorted(meta_filters.items()), any_text, recursive], sort_keys=True)

    def get(self, paths, meta_filters, any_text, recursive):
        key = self._key(paths, meta_filters, any_text, recursive)
        with self._lock:
            entry = self._store.get(key)
        if entry and (time.time() - entry[0]) < self.ttl:
            return entry[1]
        return None

    def put(self, paths, meta_filters, any_text, recursive, results):
        key = self._key(paths, meta_filters, any_text, recursive)
        with self._lock:
            self._store[key] = (time.time(), results)


_cache = _SearchCache()


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        for line in self.rfile:
            try:
                request = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                self._respond({"ok": False, "error": "malformed request"})
                continue

            cmd = request.get("cmd")
            try:
                if cmd == "ping":
                    self._respond({"ok": True, "pong": True})
                elif cmd == "search":
                    self._handle_search(request)
                elif cmd == "search_all":
                    self._handle_search_all(request)
                elif cmd == "watch":
                    self._handle_watch(request)
                elif cmd == "unwatch":
                    self._handle_unwatch(request)
                elif cmd == "watched":
                    self._handle_watched(request)
                elif cmd == "rescan":
                    self._handle_rescan(request)
                elif cmd == "shutdown":
                    self._respond({"ok": True})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                else:
                    self._respond({"ok": False, "error": f"unknown command: {cmd!r}"})
            except Exception as e:
                self._respond({"ok": False, "error": str(e)})

    def _handle_search(self, request):
        from ace.search import search as run_search

        args = request.get("args", {})
        paths = args.get("paths", [])
        meta_filters = args.get("meta_filters", {})
        any_text = args.get("any_text")
        recursive = args.get("recursive", True)

        cached = _cache.get(paths, meta_filters, any_text, recursive)
        if cached is not None:
            self._respond({"ok": True, "results": cached, "cached": True})
            return

        results = list(run_search(paths, meta_filters=meta_filters, any_text=any_text, recursive=recursive))
        _cache.put(paths, meta_filters, any_text, recursive, results)
        self._respond({"ok": True, "results": results, "cached": False})

    def _handle_search_all(self, request):
        """Queries the in-memory base table across ALL watched
        directories -- no disk I/O, just filtering already-scanned
        summaries. This is what the shell's `/a` scope calls."""
        from ace.search import filter_summaries

        args = request.get("args", {})
        meta_filters = args.get("meta_filters", {})
        any_text = args.get("any_text")

        all_summaries = _base_table.all_summaries()
        results = list(filter_summaries(all_summaries, meta_filters=meta_filters, any_text=any_text))
        self._respond({"ok": True, "results": results, "watched_count": len(_base_table.watched_dirs())})

    def _handle_watch(self, request):
        path = os.path.abspath(request.get("args", {}).get("path", ""))
        if not os.path.isdir(path):
            self._respond({"ok": False, "error": f"Not a directory: {path}"})
            return
        watched = read_watched_dirs()
        if _find_in_watched(path, watched) is None:
            watched.append(path)
            write_watched_dirs(watched)
        count = _base_table.scan_one(path)
        _base_table._start_watcher(path)
        self._respond({"ok": True, "path": path, "count": count})

    def _handle_unwatch(self, request):
        args = request.get("args", {})
        watched = read_watched_dirs()
        target = None
        if "index" in args:
            idx = args["index"]
            if 0 <= idx < len(watched):
                target = watched[idx]
        elif "path" in args:
            candidate = os.path.abspath(args["path"])
            target = _find_in_watched(candidate, watched)
        if target is None:
            self._respond({"ok": False, "error": "Not currently watched."})
            return
        watched.remove(target)
        write_watched_dirs(watched)
        _base_table.remove(target)
        self._respond({"ok": True, "path": target})

    def _handle_watched(self, request):
        watched = read_watched_dirs()
        result = [{"path": p, "count": _base_table.count_for(p)} for p in watched]
        self._respond({"ok": True, "watched": result})

    def _handle_rescan(self, request):
        path = request.get("args", {}).get("path")
        watched = read_watched_dirs()
        if path:
            path = os.path.abspath(path)
            matched = _find_in_watched(path, watched)
            if matched is None:
                self._respond({"ok": False, "error": f"Not currently watched: {path}"})
                return
            count = _base_table.scan_one(matched)
            self._respond({"ok": True, "rescanned": [{"path": matched, "count": count}]})
        else:
            rescanned = []
            for p in watched:
                if os.path.isdir(p):
                    rescanned.append({"path": p, "count": _base_table.scan_one(p)})
                else:
                    rescanned.append({"path": p, "count": None, "error": "no longer exists"})
            self._respond({"ok": True, "rescanned": rescanned})

    def _respond(self, obj):
        self.wfile.write((json.dumps(obj) + "\n").encode("utf-8"))


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def run():
    server = _Server(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    _write_state(port)
    print(f"[oniond] Listening on 127.0.0.1:{port} (pid {os.getpid()})")
    _base_table.load_from_disk()
    try:
        server.serve_forever()
    finally:
        _base_table.stop_all_watchers()
        try:
            os.remove(STATE_FILE)
        except OSError:
            pass


def try_connect(timeout=0.3):
    """Return a connected socket to an already-running daemon, or None."""
    state = _read_state()
    if not state:
        return None
    try:
        sock = socket.create_connection(("127.0.0.1", state["port"]), timeout=timeout)
        return sock
    except OSError:
        return None


def spawn_detached_kwargs():
    """Extra subprocess.Popen() kwargs to fully detach a spawned child
    (own process group/session, survives the parent exiting) --
    platform-specific, and genuinely different, not just named
    differently: start_new_session is POSIX-only (Python's own docs say
    so explicitly; the Windows subprocess module doesn't have this
    concept at all under that name). The Windows equivalent is
    CREATE_NEW_PROCESS_GROUP combined with DETACHED_PROCESS -- both of
    which only EXIST as subprocess module attributes on Windows, so
    referencing them unconditionally would crash with AttributeError on
    Linux/Mac. Used by both this module's own ensure_running() and
    shell.py's frontend-spawning, so there's one place this platform
    check lives rather than two copies that could drift apart."""
    import subprocess
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS}
    return {"start_new_session": True}


def ensure_running(startup_wait=3.0):
    """Connect to an existing daemon, or spawn a fresh one and wait for it
    to become reachable. Returns a connected socket, or raises RuntimeError
    if the daemon never came up within startup_wait seconds."""
    sock = try_connect()
    if sock:
        return sock

    import subprocess
    subprocess.Popen(
        [sys.executable, "-m", "ace.daemon"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        **spawn_detached_kwargs(),
    )

    deadline = time.time() + startup_wait
    while time.time() < deadline:
        sock = try_connect()
        if sock:
            return sock
        time.sleep(0.1)
    raise RuntimeError("Daemon did not become reachable within the startup window.")


def send_request(sock, cmd, args=None, timeout=10.0):
    """Send one newline-delimited JSON request and read one response."""
    sock.settimeout(timeout)
    payload = json.dumps({"cmd": cmd, "args": args or {}}) + "\n"
    sock.sendall(payload.encode("utf-8"))
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf += chunk
    return json.loads(buf.decode("utf-8"))


if __name__ == "__main__":
    run()
