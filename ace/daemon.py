"""
daemon.py — Onion background daemon (oniond).

A persistent local process the shell connects to, instead of every
command paying the cost of a fresh Python process + cold directory scan.
Designed as the same seam the sidecar/semantic-index watcher (see
docs/sidecar_semantic_index_design_note.md in the main Imago-Unicell
repo) will eventually grow into -- this first version is a real,
working daemon with a real IPC protocol, but the watcher/reconciliation
side of that design is deliberately NOT implemented here yet. What's
built now: process lifecycle (start, discover, connect), a minimal
command dispatch (search, ping, shutdown), and a warm per-path result
cache. What's NOT built yet, on purpose, left as a clean follow-up: the
filesystem watcher, the master/local sidecar index, reconciliation.

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
    try:
        server.serve_forever()
    finally:
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
        start_new_session=True,
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
