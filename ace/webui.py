"""
webui.py — Local web frontend for archive search
────────────────────────────────────────────────
A small, dependency-free local server (stdlib http.server only — no Flask/
FastAPI needed) that serves a single-page search UI on top of ace.search.
Run via: onion --web PATH [PATH...] [--port 8000]

Two routes:
  GET /            → the page itself (embedded HTML/CSS/JS, no build step)
  GET /api/search  → JSON results, query params:
                       path=<dir>       (repeatable; defaults to the paths
                                         --web was launched with)
                       meta=key:value   (repeatable, AND semantics)
                       any=<text>       (freetext substring match)
                       recursive=0|1    (default 1)

Everything the API returns is already computed without decompression (see
ace/search.py) — this module only adds an HTTP face on top, it doesn't add
any new archive-reading logic of its own.
"""

import json
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .search import search as run_search


def _make_handler(default_paths):
    class Handler(BaseHTTPRequestHandler):
        server_version = "OnionWebUI/0.1"

        def log_message(self, fmt, *args):
            sys.stderr.write("  [WebUI] " + (fmt % args) + "\n")

        def _send(self, status, body_bytes, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)

            if parsed.path == "/":
                self._send(200, PAGE_HTML.encode("utf-8"), "text/html; charset=utf-8")
                return

            if parsed.path == "/api/browse":
                qs = urllib.parse.parse_qs(parsed.query)
                requested = (qs.get("path") or [os.path.expanduser("~")])[0]
                body = json.dumps(_browse(requested)).encode("utf-8")
                self._send(200, body, "application/json")
                return

            if parsed.path == "/api/search":
                qs = urllib.parse.parse_qs(parsed.query)
                paths = qs.get("path") or default_paths
                meta_filters = {}
                for pair in qs.get("meta", []):
                    if ":" in pair:
                        k, _, v = pair.partition(":")
                        meta_filters[k] = v
                any_text = (qs.get("any") or [None])[0]
                recursive = (qs.get("recursive") or ["1"])[0] != "0"

                try:
                    results = list(run_search(
                        paths, meta_filters=meta_filters,
                        any_text=any_text, recursive=recursive,
                    ))
                    body = json.dumps({"ok": True, "results": results}).encode("utf-8")
                    self._send(200, body, "application/json")
                except Exception as e:
                    body = json.dumps({"ok": False, "error": str(e)}).encode("utf-8")
                    self._send(500, body, "application/json")
                return

            self._send(404, b"Not found", "text/plain")

        def do_POST(self):
            parsed = urllib.parse.urlparse(self.path)

            if parsed.path == "/api/set-meta":
                from .transformer import set_meta
                from .search import read_summary

                length = int(self.headers.get("Content-Length", 0))
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    path = payload.get("path", "")
                    new_pairs = payload.get("meta", {})

                    if not os.path.isfile(path) or not path.lower().endswith(".onion"):
                        raise ValueError(f"Not a valid .onion archive path: {path!r}")
                    if not isinstance(new_pairs, dict):
                        raise ValueError("'meta' must be an object of key/value pairs")

                    # Build the final metadata ourselves. AUTO_FIELDS
                    # (provenance bookkeeping) are preserved from the
                    # existing archive unless the client explicitly
                    # overrides them; every OTHER existing field is fully
                    # replaced by whatever the client sends -- this is
                    # what makes field DELETION actually work: if the
                    # user removed a row in the editor, that key is
                    # simply absent from new_pairs, and (being a non-auto
                    # field) it is correctly dropped rather than silently
                    # surviving because it wasn't explicitly overridden.
                    #
                    # hmac_sha256 is deliberately excluded from both sides
                    # entirely and never written back here: with merge=True
                    # set_meta() would otherwise silently carry forward a
                    # now-stale signature from before this edit, since this
                    # endpoint never takes a signing key to recompute one.
                    AUTO_FIELDS = {"created", "source_host"}
                    existing = read_summary(path)
                    existing_meta = dict(existing.get("meta", {})) if existing else {}
                    existing_meta.pop("hmac_sha256", None)
                    new_pairs.pop("hmac_sha256", None)

                    final_meta = {k: v for k, v in existing_meta.items() if k in AUTO_FIELDS}
                    final_meta.update(new_pairs)

                    set_meta(path, final_meta, sign_key=None, merge=False)
                    body = json.dumps({"ok": True}).encode("utf-8")
                    self._send(200, body, "application/json")
                except Exception as e:
                    body = json.dumps({"ok": False, "error": str(e)}).encode("utf-8")
                    self._send(400, body, "application/json")
                return

            if parsed.path == "/api/compress":
                from .analyser    import analyse
                from .transformer import compress_files
                from .manifest    import collect
                from .ignore      import build_matcher

                length = int(self.headers.get("Content-Length", 0))
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    sources     = payload.get("sources", [])
                    dest        = payload.get("dest", "")
                    password    = payload.get("password") or ""
                    meta_pairs  = payload.get("meta") or None
                    no_compress = bool(payload.get("no_compress"))
                    split_huffman = bool(payload.get("split_huffman"))

                    if not sources:
                        raise ValueError("No files or folders selected.")
                    for s in sources:
                        if not os.path.exists(s):
                            raise ValueError(f"Path not found: {s}")
                    if not dest:
                        raise ValueError("No destination filename given.")
                    if not dest.lower().endswith(".onion"):
                        dest += ".onion"
                    if os.path.exists(dest):
                        raise ValueError(f"Destination already exists: {dest}")

                    base_dir = sources[0] if (len(sources) == 1 and os.path.isdir(sources[0])) else ""
                    matcher = build_matcher(extra_patterns=[], base_dir=base_dir, use_default_ignores=True)
                    files, _label = collect(sources, matcher=matcher)

                    if not files:
                        raise ValueError("No files to compress (everything matched an ignore pattern).")

                    total_data = b"".join(d for _, d in files)
                    iset = analyse(total_data, encrypt=bool(password), no_compress=no_compress, split_huffman=split_huffman)
                    compress_files(files, iset, dest, password=password,
                                    audit=True, meta_pairs=meta_pairs, sign_key=None)

                    body = json.dumps({"ok": True, "dest": os.path.abspath(dest),
                                        "file_count": len(files),
                                        "total_bytes": len(total_data)}).encode("utf-8")
                    self._send(200, body, "application/json")
                except Exception as e:
                    body = json.dumps({"ok": False, "error": str(e)}).encode("utf-8")
                    self._send(400, body, "application/json")
                return

            if parsed.path == "/api/verify":
                from .transformer import verify as verify_archive

                length = int(self.headers.get("Content-Length", 0))
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    path     = payload.get("path", "")
                    sign_key = payload.get("sign_key", "")

                    if not os.path.isfile(path) or not path.lower().endswith(".onion"):
                        raise ValueError(f"Not a valid .onion archive path: {path!r}")
                    if not sign_key:
                        raise ValueError("A signing key is required to verify.")

                    valid = verify_archive(path, sign_key)
                    body = json.dumps({"ok": True, "valid": bool(valid)}).encode("utf-8")
                    self._send(200, body, "application/json")
                except ValueError as e:
                    # verify() itself raises ValueError for "no META block"
                    # etc -- distinct from a genuinely invalid signature,
                    # which returns valid:false rather than raising.
                    body = json.dumps({"ok": False, "error": str(e)}).encode("utf-8")
                    self._send(400, body, "application/json")
                except Exception as e:
                    body = json.dumps({"ok": False, "error": str(e)}).encode("utf-8")
                    self._send(500, body, "application/json")
                return

            if parsed.path == "/api/unwrap":
                from .transformer import unwrap
                from .header      import unpack_header

                length = int(self.headers.get("Content-Length", 0))
                try:
                    payload  = json.loads(self.rfile.read(length).decode("utf-8"))
                    path     = payload.get("path", "")
                    password = payload.get("password") or ""

                    if not os.path.isfile(path) or not path.lower().endswith(".onion"):
                        raise ValueError(f"Not a valid .onion archive path: {path!r}")

                    with open(path, "rb") as f:
                        raw = f.read()
                    iset, _, _ = unpack_header(raw)
                    if iset.encrypt and not password:
                        raise ValueError("This archive is encrypted -- a password is required to unwrap it.")

                    written = unwrap(path, password=password)
                    body = json.dumps({"ok": True, "written": written}).encode("utf-8")
                    self._send(200, body, "application/json")
                except Exception as e:
                    body = json.dumps({"ok": False, "error": str(e)}).encode("utf-8")
                    self._send(400, body, "application/json")
                return

            if parsed.path == "/api/delete":
                length = int(self.headers.get("Content-Length", 0))
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    path    = payload.get("path", "")
                    confirm = payload.get("confirm") is True

                    # Defense in depth: the frontend has its own two-step
                    # confirmation UI, but this endpoint also requires an
                    # explicit confirm:true in the payload -- a stray or
                    # scripted POST without it is refused rather than
                    # silently deleting something irreversible.
                    if not confirm:
                        raise ValueError("Deletion requires explicit confirmation (confirm: true).")
                    if not os.path.isfile(path) or not path.lower().endswith(".onion"):
                        raise ValueError(f"Not a valid .onion archive path: {path!r}")

                    os.remove(path)
                    body = json.dumps({"ok": True}).encode("utf-8")
                    self._send(200, body, "application/json")
                except Exception as e:
                    body = json.dumps({"ok": False, "error": str(e)}).encode("utf-8")
                    self._send(400, body, "application/json")
                return

            self._send(404, b"Not found", "text/plain")

    return Handler


def run(paths, port=8000):
    handler = _make_handler(paths)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"\n  [WebUI] Serving Onion search at {url}")
    print(f"  [WebUI] Scanning: {', '.join(os.path.abspath(p) for p in paths)}")
    print(f"  [WebUI] Press Ctrl+C to stop.\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  [WebUI] Stopped.")
        httpd.shutdown()


def _browse(requested_path):
    """
    List subdirectories AND files of *requested_path*, for both the
    search-path folder picker and the archive-creation file/folder
    picker. Local-only concern: this server binds to 127.0.0.1, so it
    exposes no more than what's already reachable via --search on any
    path the server process can read -- listing directory/file NAMES
    (not file contents) is no more permissive than that.
    """
    path = os.path.abspath(os.path.expanduser(requested_path or "~"))
    if not os.path.isdir(path):
        path = os.path.expanduser("~")

    dirs, files = [], []
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if entry.name.startswith("."):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        dirs.append(entry.name)
                    elif entry.is_file(follow_symlinks=False):
                        files.append({"name": entry.name, "size": entry.stat().st_size})
                except OSError:
                    continue  # unreadable entry (permissions, broken symlink, etc.) -- skip
    except PermissionError:
        pass  # can't list this directory at all -- return it with empty lists

    dirs.sort(key=str.lower)
    files.sort(key=lambda f: f["name"].lower())
    parent = os.path.dirname(path) if path != os.path.dirname(path) else None

    return {"path": path, "parent": parent, "dirs": dirs, "files": files}


PAGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Onion — Archive Search</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:        #EEF1F0;
    --surface:   #FFFFFF;
    --text:      #1B2422;
    --muted:     #5B6B67;
    --accent:    #2B6E63;
    --accent-2:  #1F5147;
    --border:    #D3DAD8;
    --badge-enc: #8A4B9E;
    --badge-bg:  #E9F1EF;
    --shadow:    0 1px 2px rgba(20,30,28,0.06), 0 4px 14px rgba(20,30,28,0.05);
  }
  [data-theme="dark"] {
    --bg:        #12181A;
    --surface:   #1B2325;
    --text:      #E4EAE8;
    --muted:     #8FA19C;
    --accent:    #4FBFAD;
    --accent-2:  #6FD6C4;
    --border:    #2B3638;
    --badge-enc: #B98BCB;
    --badge-bg:  #223330;
    --shadow:    0 1px 2px rgba(0,0,0,0.3), 0 4px 18px rgba(0,0,0,0.35);
  }
  * { box-sizing: border-box; }
  html, body {
    margin: 0; padding: 0;
    background: var(--bg); color: var(--text);
    font-family: "IBM Plex Sans", ui-sans-serif, system-ui, sans-serif;
    transition: background 0.15s ease, color 0.15s ease;
  }
  body { min-height: 100vh; }
  .wrap { max-width: 880px; margin: 0 auto; padding: 28px 20px 80px; }

  header.top {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 22px;
  }
  header.top h1 {
    font-size: 1.35rem; font-weight: 600; margin: 0;
    letter-spacing: -0.01em;
  }
  header.top h1 .layers {
    display: inline-block; color: var(--accent); margin-right: 2px;
  }
  header.top .sub {
    font-family: "IBM Plex Mono", ui-monospace, monospace;
    font-size: 0.72rem; color: var(--muted); margin-top: 2px;
  }
  .header-actions { display: flex; align-items: center; gap: 8px; }

  button.theme-toggle {
    background: var(--surface); border: 1px solid var(--border);
    color: var(--text); border-radius: 999px;
    padding: 7px 14px; font-size: 0.85rem; cursor: pointer;
    font-family: inherit;
    display: flex; align-items: center; gap: 6px;
    touch-action: manipulation; -webkit-tap-highlight-color: transparent;
  }
  button.theme-toggle:hover { border-color: var(--accent); }
  button.theme-toggle:active { background: var(--bg); }
  button.theme-toggle:focus-visible, a:focus-visible, input:focus-visible,
  .archive:focus-visible, button:focus-visible {
    outline: 2px solid var(--accent); outline-offset: 2px;
  }

  .panel {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 10px; padding: 18px; box-shadow: var(--shadow);
    margin-bottom: 22px;
  }
  .row { display: flex; gap: 10px; margin-bottom: 10px; flex-wrap: wrap; }
  .row:last-child { margin-bottom: 0; }
  label {
    font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.06em;
    color: var(--muted); display: block; margin-bottom: 4px;
  }
  input[type=text] {
    width: 100%; padding: 10px 10px; border-radius: 6px;
    border: 1px solid var(--border); background: var(--bg); color: var(--text);
    font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: 16px;
    touch-action: manipulation;
  }
  .field { flex: 1; min-width: 160px; }
  .path-row { display: flex; gap: 8px; }
  .path-row input { flex: 1; }
  .path-row button { white-space: nowrap; }

  .modal-overlay {
    display: none; position: fixed; inset: 0; z-index: 50;
    background: rgba(10,16,15,0.45);
    align-items: center; justify-content: center;
  }
  .modal-overlay.open { display: flex; }
  .modal {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 12px; box-shadow: var(--shadow);
    width: min(520px, 92vw); max-height: 78vh;
    display: flex; flex-direction: column; overflow: hidden;
  }
  .modal.modal-wide { width: min(620px, 94vw); }
  .modal-body-scroll { overflow-y: auto; padding: 14px 16px; flex: 1; -webkit-overflow-scrolling: touch; }
  .field-block { margin-bottom: 14px; }
  .field-block label {
    font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.06em;
    color: var(--muted); display: block; margin-bottom: 4px;
  }
  .field-block input[type=text], .field-block input[type=password] {
    width: 100%; padding: 10px; border-radius: 6px; border: 1px solid var(--border);
    background: var(--bg); color: var(--text);
    font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: 16px;
  }
  .checkbox-field { display: flex; align-items: flex-start; }
  .checkbox-label {
    display: flex; align-items: flex-start; gap: 8px; cursor: pointer;
    font-size: 0.8rem; color: var(--muted); text-transform: none; letter-spacing: normal;
  }
  .checkbox-label input[type=checkbox] {
    width: 20px; height: 20px; flex: 0 0 auto; margin-top: 1px;
    accent-color: var(--accent); cursor: pointer; touch-action: manipulation;
  }
  .time-warning {
    display: none; margin-top: 8px; padding: 8px 10px; border-radius: 6px;
    background: var(--badge-bg); color: var(--badge-enc);
    font-size: 0.76rem; line-height: 1.4;
  }
  .time-warning.show { display: block; }
  .modal-head {
    display: flex; justify-content: space-between; align-items: center;
    padding: 14px 16px; border-bottom: 1px solid var(--border);
    font-weight: 600; font-size: 0.95rem;
  }
  .modal-head button {
    border: none; background: none; font-size: 1.3rem; color: var(--muted); cursor: pointer;
    min-width: 40px; min-height: 40px; touch-action: manipulation; -webkit-tap-highlight-color: transparent;
  }
  .modal-head button:hover { color: var(--badge-enc); }
  .modal-head button:active { color: var(--badge-enc); }
  .breadcrumb {
    padding: 10px 16px; font-family: "IBM Plex Mono", monospace; font-size: 0.76rem;
    color: var(--muted); border-bottom: 1px solid var(--border);
    white-space: nowrap; overflow-x: auto;
  }
  .breadcrumb .seg {
    cursor: pointer; padding: 6px 3px; display: inline-block;
    touch-action: manipulation; -webkit-tap-highlight-color: transparent;
  }
  .breadcrumb .seg:hover { color: var(--accent); text-decoration: underline; }
  .breadcrumb .seg:active { color: var(--accent); }
  .breadcrumb .sep { margin: 0 2px; color: var(--border); }
  .folder-list { overflow-y: auto; padding: 6px 0; flex: 1; -webkit-overflow-scrolling: touch; }
  .folder-row {
    display: flex; align-items: center; gap: 10px;
    padding: 12px 16px; font-size: 0.9rem; cursor: pointer;
    min-height: 44px;
    touch-action: manipulation; -webkit-tap-highlight-color: transparent;
  }
  .folder-row:hover { background: var(--bg); }
  .folder-row:active { background: var(--bg); }
  .folder-row .icon { color: var(--accent); }
  .folder-row.up { color: var(--muted); font-style: italic; }
  .folder-empty { padding: 20px 16px; color: var(--muted); font-size: 0.82rem; text-align: center; }

  .checklist .folder-row { justify-content: space-between; }
  .checklist .folder-row .left { display: flex; align-items: center; gap: 10px; }
  .checklist .folder-row input[type=checkbox] {
    width: 20px; height: 20px; accent-color: var(--accent); cursor: pointer;
    touch-action: manipulation;
  }
  .checklist .folder-row .file-size { color: var(--muted); font-size: 0.72rem; font-family: "IBM Plex Mono", monospace; }
  .checklist .folder-row .nav-hint { color: var(--muted); font-size: 0.7rem; }

  .selected-summary {
    margin-top: 10px; padding: 10px 12px; background: var(--bg);
    border: 1px dashed var(--border); border-radius: 8px;
  }
  .selected-summary .toc-tag { font-size: 0.68rem; color: var(--muted); display: block; margin-bottom: 6px; }
  .selected-list { display: flex; flex-direction: column; gap: 4px; max-height: 140px; overflow-y: auto; }
  .selected-item {
    display: flex; justify-content: space-between; align-items: center;
    font-family: "IBM Plex Mono", monospace; font-size: 0.76rem;
    padding: 4px 6px; background: var(--surface); border-radius: 5px;
  }
  .selected-item button {
    background: none; border: none; color: var(--muted); cursor: pointer;
    font-size: 0.9rem; min-width: 28px; min-height: 28px;
    touch-action: manipulation; -webkit-tap-highlight-color: transparent;
  }
  .selected-item button:hover, .selected-item button:active { color: var(--badge-enc); }
  .selected-empty { font-size: 0.76rem; color: var(--muted); font-style: italic; }
  .modal-actions {
    display: flex; justify-content: flex-end; gap: 8px;
    padding: 12px 16px; border-top: 1px solid var(--border);
  }
  .filters-list { display: flex; flex-direction: column; gap: 8px; margin-bottom: 8px; }
  .filter-row { display: flex; gap: 8px; }
  .filter-row input { flex: 1; }
  .filter-row button.remove {
    background: none; border: 1px solid var(--border); color: var(--muted);
    border-radius: 6px; padding: 0 10px; cursor: pointer;
    min-width: 40px; min-height: 40px; font-size: 1rem;
    touch-action: manipulation; -webkit-tap-highlight-color: transparent;
  }
  .filter-row button.remove:hover { color: var(--badge-enc); border-color: var(--badge-enc); }
  .filter-row button.remove:active { background: var(--bg); }

  .actions { display: flex; gap: 8px; margin-top: 12px; }
  button.primary, button.ghost {
    font-family: inherit; font-size: 0.85rem; font-weight: 500;
    padding: 9px 16px; border-radius: 7px; cursor: pointer; border: 1px solid transparent;
    min-height: 40px; touch-action: manipulation; -webkit-tap-highlight-color: transparent;
  }
  button.primary { background: var(--accent); color: #fff; }
  button.primary:hover { background: var(--accent-2); }
  button.primary:active { background: var(--accent-2); transform: scale(0.98); }
  button.ghost { background: transparent; border-color: var(--border); color: var(--text); }
  button.ghost:hover { border-color: var(--accent); color: var(--accent); }
  button.ghost:active { background: var(--bg); }
  button.danger {
    font-family: inherit; font-size: 0.85rem; font-weight: 500;
    padding: 9px 16px; border-radius: 7px; cursor: pointer;
    background: transparent; border: 1px solid var(--badge-enc); color: var(--badge-enc);
    min-height: 40px; touch-action: manipulation; -webkit-tap-highlight-color: transparent;
  }
  button.danger:hover, button.danger:active { background: var(--badge-bg); }
  button.danger.danger-armed { background: var(--badge-enc); color: #fff; }

  .archive-actions {
    margin-top: 10px; padding-top: 10px; border-top: 1px dashed var(--border);
    display: none; flex-wrap: wrap; gap: 8px; align-items: center;
  }
  .archive.open .archive-actions { display: flex; }
  .unwrap-password {
    flex: 1; min-width: 140px; padding: 8px; border-radius: 6px; border: 1px solid var(--border);
    background: var(--bg); color: var(--text); font-family: "IBM Plex Mono", monospace; font-size: 16px;
  }
  .action-status { font-size: 0.76rem; color: var(--muted); font-family: "IBM Plex Mono", monospace; }

  .status { font-size: 0.8rem; color: var(--muted); margin: 4px 2px 16px; font-family: "IBM Plex Mono", monospace; }

  .archive {
    background: var(--surface); border: 1px solid var(--border);
    border-left: 4px solid var(--accent);
    border-radius: 8px; padding: 14px 16px; margin-bottom: 10px;
    box-shadow: var(--shadow); cursor: pointer;
    touch-action: manipulation; -webkit-tap-highlight-color: transparent;
  }
  .archive:active { background: var(--bg); }
  .archive .head { display: flex; justify-content: space-between; align-items: baseline; gap: 10px; }
  .archive .path {
    font-family: "IBM Plex Mono", monospace; font-size: 0.92rem; font-weight: 500;
    word-break: break-all;
  }
  .archive .meta-line {
    font-family: "IBM Plex Mono", monospace; font-size: 0.76rem; color: var(--muted);
    margin-top: 4px;
  }
  .archive .desc { font-size: 0.85rem; margin-top: 6px; color: var(--text); }
  .badges { display: flex; gap: 6px; flex-wrap: wrap; }
  .badge {
    font-size: 0.68rem; padding: 2px 8px; border-radius: 999px;
    background: var(--badge-bg); color: var(--accent-2); font-weight: 500;
    font-family: "IBM Plex Mono", monospace;
  }
  .badge.enc { background: var(--badge-bg); color: var(--badge-enc); }

  .contents {
    margin-top: 10px; padding-top: 10px; border-top: 1px dashed var(--border);
    display: none;
  }
  .archive.open .contents { display: block; }
  .contents table { width: 100%; border-collapse: collapse; font-family: "IBM Plex Mono", monospace; font-size: 0.78rem; }
  .contents td { padding: 3px 6px; }
  .contents td.size { color: var(--muted); text-align: right; white-space: nowrap; }
  .contents .toc-tag {
    font-size: 0.65rem; color: var(--muted); margin-bottom: 4px; display: block;
  }

  .meta-editor {
    margin-top: 10px; padding-top: 10px; border-top: 1px dashed var(--border);
    display: none;
  }
  .archive.open .meta-editor { display: block; }
  .meta-editor .toc-tag {
    font-size: 0.65rem; color: var(--muted); margin-bottom: 6px; display: block;
  }
  .meta-fields { display: flex; flex-direction: column; gap: 6px; margin-bottom: 8px; }
  .meta-field-row { display: flex; gap: 6px; }
  .meta-field-row .meta-key {
    flex: 0 0 30%; font-size: 0.8rem;
  }
  .meta-field-row .meta-val { flex: 1; font-size: 0.8rem; }
  .meta-field-row input {
    padding: 7px 8px; border-radius: 6px; border: 1px solid var(--border);
    background: var(--bg); color: var(--text);
    font-family: "IBM Plex Mono", ui-monospace, monospace;
  }
  .meta-field-row button.remove-field {
    background: none; border: 1px solid var(--border); color: var(--muted);
    border-radius: 6px; padding: 0 10px; cursor: pointer;
    min-width: 40px; min-height: 40px; font-size: 1rem;
    touch-action: manipulation; -webkit-tap-highlight-color: transparent;
  }
  .meta-field-row button.remove-field:hover,
  .meta-field-row button.remove-field:active { color: var(--badge-enc); border-color: var(--badge-enc); }
  .meta-editor-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .meta-save-status { font-size: 0.76rem; color: var(--muted); font-family: "IBM Plex Mono", monospace; }
  .meta-note { font-size: 0.68rem; color: var(--muted); margin-top: 8px; }

  .signature-block {
    margin-top: 10px; padding-top: 10px; border-top: 1px dashed var(--border);
    display: none;
  }
  .archive.open .signature-block { display: block; }
  .signature-block .toc-tag { font-size: 0.65rem; color: var(--muted); margin-bottom: 6px; display: block; }
  .sig-row { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
  .sig-hash {
    font-family: "IBM Plex Mono", monospace; font-size: 0.78rem;
    background: var(--bg); padding: 5px 8px; border-radius: 5px; border: 1px solid var(--border);
  }
  .sig-key {
    flex: 1; min-width: 140px; padding: 7px 8px; border-radius: 6px; border: 1px solid var(--border);
    background: var(--bg); color: var(--text); font-family: "IBM Plex Mono", monospace; font-size: 16px;
  }
  .sig-status { font-size: 0.76rem; color: var(--muted); font-family: "IBM Plex Mono", monospace; }
  .sig-status.sig-valid { color: var(--accent-2); font-weight: 500; }
  .sig-status.sig-invalid { color: var(--badge-enc); font-weight: 500; }

  .peel-hint { font-size: 0.7rem; color: var(--muted); margin-top: 8px; }
  .empty {
    text-align: center; padding: 40px 20px; color: var(--muted);
    border: 1px dashed var(--border); border-radius: 10px;
  }
  .empty strong { color: var(--text); display: block; margin-bottom: 4px; }

  @media (prefers-reduced-motion: reduce) {
    * { transition: none !important; }
  }

  /* Touch/coarse-pointer devices (finger, not mouse/trackpad) get a bit
     more breathing room around tap targets -- detected via input
     precision, not screen size, since a touch laptop can have a large
     screen and a mouse-driven small window shouldn't get finger-sized
     controls it doesn't need. */
  @media (pointer: coarse) {
    .archive { padding: 16px 18px; }
    .filter-row { gap: 10px; }
    .actions { gap: 10px; }
    button.primary, button.ghost { padding: 11px 18px; }
    .folder-row { padding: 14px 16px; }
  }

  @media (max-width: 480px) {
    .path-row { flex-direction: column; }
    .path-row button { width: 100%; }
  }
</style>
</head>
<body data-theme="light">
<div class="wrap">

  <header class="top">
    <div>
      <h1><span class="layers">&#9678;</span> Onion — Archive Search</h1>
      <div class="sub">metadata &amp; contents, read without decompression</div>
    </div>
    <div class="header-actions">
      <button class="ghost" id="newArchiveBtn" type="button">+ New Archive</button>
      <button class="theme-toggle" id="themeToggle" aria-label="Toggle light or dark theme">
        <span id="themeIcon">&#9788;</span> <span id="themeLabel">Light</span>
      </button>
    </div>
  </header>

  <div class="panel">
    <div class="row">
      <div class="field">
        <label for="pathInput">Search path</label>
        <div class="path-row">
          <input type="text" id="pathInput" placeholder="e.g. /home/alan/archives">
          <button class="ghost" id="browseBtn" type="button">Browse&hellip;</button>
        </div>
      </div>
    </div>

    <label>Metadata filters</label>
    <div class="filters-list" id="filtersList"></div>
    <button class="ghost" id="addFilter" type="button">+ Add filter</button>

    <div class="row" style="margin-top:12px;">
      <div class="field">
        <label for="anyInput">Free text (filename or any metadata value)</label>
        <input type="text" id="anyInput" placeholder="e.g. invoice">
      </div>
    </div>

    <div class="actions">
      <button class="primary" id="searchBtn" type="button">Search</button>
      <button class="ghost" id="clearBtn" type="button">Clear</button>
    </div>
  </div>

  <div class="status" id="status">Enter a path above and search, or search with no filters to list everything.</div>
  <div id="results"></div>

</div>

<div class="modal-overlay" id="browseOverlay">
  <div class="modal" role="dialog" aria-modal="true" aria-label="Choose a folder">
    <div class="modal-head">
      <span>Choose a folder</span>
      <button class="ghost" id="browseClose" type="button" aria-label="Close">&times;</button>
    </div>
    <div class="breadcrumb" id="breadcrumb"></div>
    <div class="folder-list" id="folderList"></div>
    <div class="modal-actions">
      <button class="ghost" id="browseCancel" type="button">Cancel</button>
      <button class="primary" id="browseSelect" type="button">Select this folder</button>
    </div>
  </div>
</div>

<div class="modal-overlay" id="newArchiveOverlay">
  <div class="modal modal-wide" role="dialog" aria-modal="true" aria-label="Create new archive">
    <div class="modal-head">
      <span>Create New Archive</span>
      <button class="ghost" id="newArchiveClose" type="button" aria-label="Close">&times;</button>
    </div>
    <div class="modal-body-scroll">

      <div class="field-block">
        <label for="archivePassword">Password (optional — leave blank for no encryption)</label>
        <input type="password" id="archivePassword" placeholder="Leave blank for no encryption">
      </div>

      <div class="field-block checkbox-field">
        <label class="checkbox-label">
          <input type="checkbox" id="archiveNoCompress">
          No compression (store raw) — keeps the file fully searchable via metadata and the
          TOC block without running any compression algorithm. Useful for files that don't
          compress well, or when the point is search, not size.
        </label>
      </div>

      <div class="field-block checkbox-field">
        <label class="checkbox-label">
          <input type="checkbox" id="archiveSplitHuffman">
          Experimental: split-stream Huffman — separate Huffman trees for literal data vs
          match data. Not a universal win: genuinely smaller on random/incompressible and
          highly-repetitive data, genuinely <em>larger</em> on typical source code, small
          files, and general text. Try it and compare.
        </label>
        <div class="time-warning" id="splitHuffmanWarning">
          &#9888; Pure Python, no hardware acceleration — noticeably slower than the default,
          especially on larger files. May take several seconds to tens of seconds depending on
          size and content.
        </div>
      </div>

      <label>Browse and check files/folders to include</label>
      <div class="breadcrumb" id="archiveBreadcrumb"></div>
      <div class="folder-list checklist" id="archiveFileList"></div>

      <div class="selected-summary" id="selectedSummary">
        <span class="toc-tag">Selected (0)</span>
        <div class="selected-list" id="selectedList"></div>
      </div>

      <div class="field-block" style="margin-top:14px;">
        <label for="archiveDest">Save as</label>
        <input type="text" id="archiveDest" placeholder="e.g. /home/alan/archives/my_archive.onion">
      </div>

      <label>Metadata (optional)</label>
      <div class="meta-fields" id="newArchiveMetaFields"></div>
      <button class="ghost" id="newArchiveAddField" type="button">+ Add field</button>
    </div>
    <div class="modal-actions">
      <span class="meta-save-status" id="archiveStatus"></span>
      <button class="ghost" id="newArchiveCancel" type="button">Cancel</button>
      <button class="primary" id="newArchiveCreate" type="button">Create Archive</button>
    </div>
  </div>
</div>

<script>
(function() {
  var body = document.body;
  var saved = localStorage.getItem('onion-theme') || 'light';
  body.setAttribute('data-theme', saved);
  updateToggleLabel(saved);

  document.getElementById('themeToggle').addEventListener('click', function() {
    var cur = body.getAttribute('data-theme');
    var next = cur === 'light' ? 'dark' : 'light';
    body.setAttribute('data-theme', next);
    localStorage.setItem('onion-theme', next);
    updateToggleLabel(next);
  });

  function updateToggleLabel(theme) {
    document.getElementById('themeIcon').innerHTML = theme === 'light' ? '&#9788;' : '&#9789;';
    document.getElementById('themeLabel').textContent = theme === 'light' ? 'Light' : 'Dark';
  }

  var filtersList = document.getElementById('filtersList');

  function addFilterRow(key, value) {
    var row = document.createElement('div');
    row.className = 'filter-row';
    row.innerHTML =
      '<input type="text" class="filter-key" placeholder="key (e.g. tags)" value="' + (key||'') + '">' +
      '<input type="text" class="filter-val" placeholder="value (e.g. invoice)" value="' + (value||'') + '">' +
      '<button type="button" class="remove" aria-label="Remove filter">&times;</button>';
    row.querySelector('.remove').addEventListener('click', function() { row.remove(); });
    filtersList.appendChild(row);
  }
  document.getElementById('addFilter').addEventListener('click', function() { addFilterRow(); });
  addFilterRow(); // start with one empty row

  document.getElementById('clearBtn').addEventListener('click', function() {
    document.getElementById('pathInput').value = '';
    document.getElementById('anyInput').value = '';
    filtersList.innerHTML = '';
    addFilterRow();
    document.getElementById('results').innerHTML = '';
    document.getElementById('status').textContent = 'Enter a path above and search, or search with no filters to list everything.';
  });

  // ── Folder browser modal ─────────────────────────────────────────────────
  var browseState = { path: null };
  var overlay = document.getElementById('browseOverlay');

  function openBrowser() {
    var start = document.getElementById('pathInput').value.trim() || null;
    overlay.classList.add('open');
    loadFolder(start);
  }
  function closeBrowser() { overlay.classList.remove('open'); }

  function loadFolder(path) {
    var url = '/api/browse' + (path ? ('?path=' + encodeURIComponent(path)) : '');
    fetch(url).then(function(r) { return r.json(); }).then(function(data) {
      browseState.path = data.path;
      renderBreadcrumb(data.path);
      renderFolderList(data);
    }).catch(function(err) {
      document.getElementById('folderList').innerHTML =
        '<div class="folder-empty">Could not read that folder: ' + escapeHtml(String(err)) + '</div>';
    });
  }

  function renderBreadcrumb(path) {
    var el = document.getElementById('breadcrumb');
    var parts = path.split('/').filter(Boolean);
    var acc = '';
    var html = '<span class="seg" data-path="/">/</span>';
    parts.forEach(function(part) {
      acc += '/' + part;
      html += '<span class="sep">/</span><span class="seg" data-path="' + escapeHtml(acc) + '">' + escapeHtml(part) + '</span>';
    });
    el.innerHTML = html;
    el.querySelectorAll('.seg').forEach(function(seg) {
      seg.addEventListener('click', function() { loadFolder(seg.getAttribute('data-path')); });
    });
  }

  function renderFolderList(data) {
    var el = document.getElementById('folderList');
    el.innerHTML = '';
    if (data.parent) {
      var up = document.createElement('div');
      up.className = 'folder-row up';
      up.innerHTML = '<span class="icon">&#8598;</span> .. (up one level)';
      up.addEventListener('click', function() { loadFolder(data.parent); });
      el.appendChild(up);
    }
    if (!data.dirs || data.dirs.length === 0) {
      var empty = document.createElement('div');
      empty.className = 'folder-empty';
      empty.textContent = 'No subfolders here.';
      el.appendChild(empty);
      return;
    }
    data.dirs.forEach(function(name) {
      var row = document.createElement('div');
      row.className = 'folder-row';
      row.innerHTML = '<span class="icon">&#128193;</span> ' + escapeHtml(name);
      row.addEventListener('click', function() {
        loadFolder(data.path.replace(/\/$/, '') + '/' + name);
      });
      el.appendChild(row);
    });
  }

  document.getElementById('browseBtn').addEventListener('click', openBrowser);
  document.getElementById('browseClose').addEventListener('click', closeBrowser);
  document.getElementById('browseCancel').addEventListener('click', closeBrowser);
  document.getElementById('browseSelect').addEventListener('click', function() {
    document.getElementById('pathInput').value = browseState.path;
    closeBrowser();
    doSearch();
  });
  overlay.addEventListener('click', function(e) { if (e.target === overlay) closeBrowser(); });
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape' && overlay.classList.contains('open')) closeBrowser();
  });

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function(c) {
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
    });
  }

  function renderResults(data) {
    var container = document.getElementById('results');
    var status = document.getElementById('status');
    container.innerHTML = '';

    if (!data.ok) {
      status.textContent = 'Error: ' + data.error;
      return;
    }
    if (data.results.length === 0) {
      status.textContent = '0 match(es).';
      container.innerHTML = '<div class="empty"><strong>No matching archives found.</strong>Try a broader path, remove a filter, or check the free-text spelling.</div>';
      return;
    }
    status.textContent = data.results.length + ' match(es). Click an archive to peel it open.';

    data.results.forEach(function(r) {
      var card = document.createElement('div');
      card.className = 'archive';
      card.tabIndex = 0;

      var meta = r.meta || {};
      var tags = meta.tags;
      var tagsHtml = '';
      if (tags) {
        var list = Array.isArray(tags) ? tags : [tags];
        tagsHtml = list.map(function(t) { return '<span class="badge">' + escapeHtml(t) + '</span>'; }).join('');
      }
      var encBadge = r.encrypted ? '<span class="badge enc">&#128274; encrypted</span>' : '';

      var contentsHtml = '';
      if (r.contents && r.contents.length) {
        var rows = r.contents.map(function(e) {
          return '<tr><td>' + escapeHtml(e.path) + '</td><td class="size">' + e.size.toLocaleString() + ' B</td></tr>';
        }).join('');
        contentsHtml =
          '<div class="contents"><span class="toc-tag">contents (from TOC, no decompression)</span>' +
          '<table>' + rows + '</table></div>';
      } else if (r.contents === null) {
        contentsHtml = '<div class="contents"><span class="toc-tag">no TOC block in this archive (single-file or older archive)</span></div>';
      }

      var AUTO_FIELDS = ['created', 'source_host', 'hmac_sha256'];
      var editableEntries = Object.keys(meta)
        .filter(function(k) { return AUTO_FIELDS.indexOf(k) === -1; })
        .map(function(k) { return [k, Array.isArray(meta[k]) ? meta[k].join(', ') : String(meta[k])]; });

      var metaRowsHtml = editableEntries.map(function(pair) {
        return '<div class="meta-field-row">' +
          '<input type="text" class="meta-key" value="' + escapeHtml(pair[0]) + '" placeholder="field name">' +
          '<input type="text" class="meta-val" value="' + escapeHtml(pair[1]) + '" placeholder="value (comma-separate for a list)">' +
          '<button type="button" class="remove-field" aria-label="Remove field">&times;</button>' +
          '</div>';
      }).join('');

      var sigHtml = '';
      if (meta.hmac_sha256) {
        var hash = String(meta.hmac_sha256);
        var shortHash = hash.length > 20 ? (hash.slice(0, 10) + '…' + hash.slice(-8)) : hash;
        sigHtml =
          '<div class="signature-block">' +
            '<span class="toc-tag">signature (read-only — not editable here)</span>' +
            '<div class="sig-row">' +
              '<code class="sig-hash" title="' + escapeHtml(hash) + '">' + escapeHtml(shortHash) + '</code>' +
              '<input type="password" class="sig-key" placeholder="signing key to verify">' +
              '<button type="button" class="ghost verify-sig">Verify</button>' +
              '<span class="sig-status">present, unverified</span>' +
            '</div>' +
          '</div>';
      }

      var metaEditorHtml =
        '<div class="meta-editor">' +
          '<span class="toc-tag">metadata (created/source host preserved automatically, not shown here)</span>' +
          '<div class="meta-fields">' + metaRowsHtml + '</div>' +
          '<div class="meta-editor-row">' +
            '<button type="button" class="ghost add-field">+ Add field</button>' +
            '<button type="button" class="primary save-meta">Save changes</button>' +
            '<span class="meta-save-status"></span>' +
          '</div>' +
          '<div class="meta-note">Removing a row and saving deletes that field. There is no undo -- re-add it manually if needed.</div>' +
        '</div>';

      var actionsHtml =
        '<div class="archive-actions">' +
          (r.encrypted ? '<input type="password" class="unwrap-password" placeholder="password to unwrap">' : '') +
          '<button type="button" class="ghost unwrap-btn">Remove wrapper (restore file)</button>' +
          '<button type="button" class="danger delete-btn">Delete archive</button>' +
          '<span class="action-status"></span>' +
        '</div>';

      card.innerHTML =
        '<div class="head"><span class="path">' + escapeHtml(r.path) + '</span><span class="badges">' + encBadge + tagsHtml + '</span></div>' +
        '<div class="meta-line">' + r.original_size.toLocaleString() + ' bytes original &middot; ' + r.layer_count + ' layer(s)' +
        (r.contents ? ' &middot; ' + r.contents.length + ' file(s)' : '') + '</div>' +
        (meta.description ? '<div class="desc">' + escapeHtml(meta.description) + '</div>' : '') +
        contentsHtml +
        sigHtml +
        metaEditorHtml +
        actionsHtml +
        '<div class="peel-hint">click to peel open &#8595;</div>';

      var actionsEl = card.querySelector('.archive-actions');
      actionsEl.addEventListener('click', function(e) { e.stopPropagation(); });
      actionsEl.addEventListener('keydown', function(e) { e.stopPropagation(); });

      actionsEl.querySelector('.unwrap-btn').addEventListener('click', function() {
        var statusEl = actionsEl.querySelector('.action-status');
        var pwInput = actionsEl.querySelector('.unwrap-password');
        var password = pwInput ? pwInput.value : '';
        statusEl.textContent = 'Removing wrapper...';
        fetch('/api/unwrap', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path: r.path, password: password }),
        }).then(function(res) { return res.json(); }).then(function(result) {
          if (result.ok) {
            statusEl.textContent = 'Restored. Refreshing...';
            setTimeout(doSearch, 700);
          } else {
            statusEl.textContent = 'Error: ' + result.error;
          }
        }).catch(function(err) { statusEl.textContent = 'Request failed: ' + err; });
      });

      var deleteBtn = actionsEl.querySelector('.delete-btn');
      var deleteArmed = false, deleteTimer = null;
      deleteBtn.addEventListener('click', function() {
        var statusEl = actionsEl.querySelector('.action-status');
        if (!deleteArmed) {
          deleteArmed = true;
          deleteBtn.textContent = 'Really delete? Click again (6s)';
          deleteBtn.classList.add('danger-armed');
          deleteTimer = setTimeout(function() {
            deleteArmed = false;
            deleteBtn.textContent = 'Delete archive';
            deleteBtn.classList.remove('danger-armed');
          }, 6000);
          return;
        }
        clearTimeout(deleteTimer);
        deleteArmed = false;
        deleteBtn.textContent = 'Delete archive';
        deleteBtn.classList.remove('danger-armed');
        statusEl.textContent = 'Deleting...';
        fetch('/api/delete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path: r.path, confirm: true }),
        }).then(function(res) { return res.json(); }).then(function(result) {
          if (result.ok) {
            statusEl.textContent = 'Deleted.';
            setTimeout(doSearch, 500);
          } else {
            statusEl.textContent = 'Error: ' + result.error;
          }
        }).catch(function(err) { statusEl.textContent = 'Request failed: ' + err; });
      });

      var metaEditorEl = card.querySelector('.meta-editor');
      metaEditorEl.addEventListener('click', function(e) { e.stopPropagation(); });
      metaEditorEl.addEventListener('keydown', function(e) { e.stopPropagation(); });

      var sigBlockEl = card.querySelector('.signature-block');
      if (sigBlockEl) {
        sigBlockEl.addEventListener('click', function(e) { e.stopPropagation(); });
        sigBlockEl.addEventListener('keydown', function(e) { e.stopPropagation(); });
        sigBlockEl.querySelector('.verify-sig').addEventListener('click', function() {
          var keyInput = sigBlockEl.querySelector('.sig-key');
          var statusEl = sigBlockEl.querySelector('.sig-status');
          var key = keyInput.value;
          if (!key) { statusEl.textContent = 'Enter a key first'; statusEl.className = 'sig-status'; return; }
          statusEl.textContent = 'Verifying...';
          fetch('/api/verify', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: r.path, sign_key: key }),
          }).then(function(res) { return res.json(); }).then(function(result) {
            if (!result.ok) {
              statusEl.textContent = 'Error: ' + result.error;
              statusEl.className = 'sig-status';
            } else if (result.valid) {
              statusEl.textContent = '\u2713 present and confirmed';
              statusEl.className = 'sig-status sig-valid';
            } else {
              statusEl.textContent = '\u2717 invalid signature (wrong key or archive modified)';
              statusEl.className = 'sig-status sig-invalid';
            }
          }).catch(function(err) {
            statusEl.textContent = 'Request failed: ' + err;
            statusEl.className = 'sig-status';
          });
        });
      }

      function addFieldRow(key, val) {
        var row = document.createElement('div');
        row.className = 'meta-field-row';
        row.innerHTML =
          '<input type="text" class="meta-key" value="' + (key||'') + '" placeholder="field name">' +
          '<input type="text" class="meta-val" value="' + (val||'') + '" placeholder="value (comma-separate for a list)">' +
          '<button type="button" class="remove-field" aria-label="Remove field">&times;</button>';
        row.querySelector('.remove-field').addEventListener('click', function() { row.remove(); });
        metaEditorEl.querySelector('.meta-fields').appendChild(row);
      }
      metaEditorEl.querySelectorAll('.remove-field').forEach(function(btn) {
        btn.addEventListener('click', function() { btn.closest('.meta-field-row').remove(); });
      });
      metaEditorEl.querySelector('.add-field').addEventListener('click', function() { addFieldRow(); });

      metaEditorEl.querySelector('.save-meta').addEventListener('click', function() {
        var statusEl = metaEditorEl.querySelector('.meta-save-status');
        var newMeta = {};
        metaEditorEl.querySelectorAll('.meta-field-row').forEach(function(row) {
          var k = row.querySelector('.meta-key').value.trim();
          var v = row.querySelector('.meta-val').value.trim();
          if (!k) return;
          newMeta[k] = v.indexOf(',') !== -1 ? v.split(',').map(function(s) { return s.trim(); }) : v;
        });
        statusEl.textContent = 'Saving...';
        fetch('/api/set-meta', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path: r.path, meta: newMeta }),
        }).then(function(res) { return res.json(); }).then(function(result) {
          if (result.ok) {
            statusEl.textContent = 'Saved.';
            doSearch();
          } else {
            statusEl.textContent = 'Error: ' + result.error;
          }
        }).catch(function(err) {
          statusEl.textContent = 'Request failed: ' + err;
        });
      });

      card.addEventListener('click', function() { card.classList.toggle('open'); });
      card.addEventListener('keydown', function(e) {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); card.classList.toggle('open'); }
      });
      container.appendChild(card);
    });
  }

  function doSearch() {
    var params = new URLSearchParams();
    var path = document.getElementById('pathInput').value.trim();
    if (path) params.append('path', path);

    filtersList.querySelectorAll('.filter-row').forEach(function(row) {
      var k = row.querySelector('.filter-key').value.trim();
      var v = row.querySelector('.filter-val').value.trim();
      if (k && v) params.append('meta', k + ':' + v);
    });

    var any = document.getElementById('anyInput').value.trim();
    if (any) params.append('any', any);

    document.getElementById('status').textContent = 'Searching...';
    fetch('/api/search?' + params.toString())
      .then(function(res) { return res.json(); })
      .then(renderResults)
      .catch(function(err) {
        document.getElementById('status').textContent = 'Request failed: ' + err;
      });
  }

  document.getElementById('searchBtn').addEventListener('click', doSearch);
  document.getElementById('anyInput').addEventListener('keydown', function(e) {
    if (e.key === 'Enter') doSearch();
  });
  document.getElementById('pathInput').addEventListener('keydown', function(e) {
    if (e.key === 'Enter') doSearch();
  });

  // ── New Archive modal ────────────────────────────────────────────────────
  var archiveOverlay = document.getElementById('newArchiveOverlay');
  var archiveSelection = new Map(); // absolute path -> {type, name, size}
  var archiveCurrentPath = null;

  function joinPath(base, name) { return base.replace(/\/$/, '') + '/' + name; }

  function openNewArchiveModal() {
    archiveSelection.clear();
    document.getElementById('archivePassword').value = '';
    document.getElementById('archiveNoCompress').checked = false;
    document.getElementById('archiveSplitHuffman').checked = false;
    document.getElementById('splitHuffmanWarning').classList.remove('show');
    document.getElementById('archiveDest').value = '';
    document.getElementById('archiveStatus').textContent = '';
    var metaFieldsEl = document.getElementById('newArchiveMetaFields');
    metaFieldsEl.innerHTML = '';
    addArchiveMetaRow();
    archiveOverlay.classList.add('open');

    var startPath = document.getElementById('pathInput').value.trim() || null;
    var url = '/api/browse' + (startPath ? ('?path=' + encodeURIComponent(startPath)) : '');
    fetch(url).then(function(r) { return r.json(); }).then(function(data) {
      archiveCurrentPath = data.path;
      renderArchiveBreadcrumb(data.path);
      renderArchiveFileList(data);
      renderSelectedSummary();
      // Prefill with a sensible full path so a bare filename edit still
      // lands somewhere the user can see, rather than relative to
      // wherever the server process happens to be running from.
      document.getElementById('archiveDest').value = joinPath(data.path, 'archive.onion');
    }).catch(function(err) {
      document.getElementById('archiveFileList').innerHTML =
        '<div class="folder-empty">Could not read that folder: ' + escapeHtml(String(err)) + '</div>';
    });
  }
  function closeNewArchiveModal() { archiveOverlay.classList.remove('open'); }

  function loadArchiveFolder(path) {
    var url = '/api/browse' + (path ? ('?path=' + encodeURIComponent(path)) : '');
    fetch(url).then(function(r) { return r.json(); }).then(function(data) {
      archiveCurrentPath = data.path;
      renderArchiveBreadcrumb(data.path);
      renderArchiveFileList(data);
    }).catch(function(err) {
      document.getElementById('archiveFileList').innerHTML =
        '<div class="folder-empty">Could not read that folder: ' + escapeHtml(String(err)) + '</div>';
    });
  }

  function renderArchiveBreadcrumb(path) {
    var el = document.getElementById('archiveBreadcrumb');
    var parts = path.split('/').filter(Boolean);
    var acc = '';
    var html = '<span class="seg" data-path="/">/</span>';
    parts.forEach(function(part) {
      acc += '/' + part;
      html += '<span class="sep">/</span><span class="seg" data-path="' + escapeHtml(acc) + '">' + escapeHtml(part) + '</span>';
    });
    el.innerHTML = html;
    el.querySelectorAll('.seg').forEach(function(seg) {
      seg.addEventListener('click', function() { loadArchiveFolder(seg.getAttribute('data-path')); });
    });
  }

  function renderArchiveFileList(data) {
    var el = document.getElementById('archiveFileList');
    el.innerHTML = '';
    if (data.parent) {
      var up = document.createElement('div');
      up.className = 'folder-row up';
      up.innerHTML = '<span class="icon">&#8598;</span> .. (up one level)';
      up.addEventListener('click', function() { loadArchiveFolder(data.parent); });
      el.appendChild(up);
    }
    (data.dirs || []).forEach(function(name) {
      var full = joinPath(data.path, name);
      var row = document.createElement('div');
      row.className = 'folder-row';
      var checked = archiveSelection.has(full) ? 'checked' : '';
      row.innerHTML =
        '<span class="left"><input type="checkbox" ' + checked + '> <span class="icon">&#128193;</span> ' + escapeHtml(name) + '</span>' +
        '<span class="nav-hint">open &rarr;</span>';
      var cb = row.querySelector('input');
      cb.addEventListener('click', function(e) {
        e.stopPropagation();
        if (cb.checked) archiveSelection.set(full, { type: 'dir', name: name });
        else archiveSelection.delete(full);
        renderSelectedSummary();
      });
      row.addEventListener('click', function(e) {
        if (e.target === cb) return;
        loadArchiveFolder(full);
      });
      el.appendChild(row);
    });
    (data.files || []).forEach(function(f) {
      var full = joinPath(data.path, f.name);
      var row = document.createElement('div');
      row.className = 'folder-row';
      var checked = archiveSelection.has(full) ? 'checked' : '';
      row.innerHTML =
        '<span class="left"><input type="checkbox" ' + checked + '> ' + escapeHtml(f.name) + '</span>' +
        '<span class="file-size">' + f.size.toLocaleString() + ' B</span>';
      var cb = row.querySelector('input');
      cb.addEventListener('change', function() {
        if (cb.checked) archiveSelection.set(full, { type: 'file', name: f.name, size: f.size });
        else archiveSelection.delete(full);
        renderSelectedSummary();
      });
      el.appendChild(row);
    });
    if ((!data.dirs || !data.dirs.length) && (!data.files || !data.files.length) && !data.parent) {
      el.innerHTML += '<div class="folder-empty">Empty folder.</div>';
    }
  }

  function renderSelectedSummary() {
    var listEl = document.getElementById('selectedList');
    var tagEl = document.querySelector('#selectedSummary .toc-tag');
    tagEl.textContent = 'Selected (' + archiveSelection.size + ')';
    listEl.innerHTML = '';
    if (archiveSelection.size === 0) {
      listEl.innerHTML = '<div class="selected-empty">Nothing selected yet -- check files or folders above.</div>';
      return;
    }
    archiveSelection.forEach(function(info, path) {
      var item = document.createElement('div');
      item.className = 'selected-item';
      var icon = info.type === 'dir' ? '&#128193;' : '&#128196;';
      item.innerHTML = '<span>' + icon + ' ' + escapeHtml(path) + '</span><button type="button" aria-label="Remove">&times;</button>';
      item.querySelector('button').addEventListener('click', function() {
        archiveSelection.delete(path);
        renderSelectedSummary();
        // Refresh the current folder view too, in case the removed item
        // is visible there, so its checkbox reflects the new state.
        if (archiveCurrentPath) loadArchiveFolder(archiveCurrentPath);
      });
      listEl.appendChild(item);
    });
  }

  function addArchiveMetaRow(key, val) {
    var row = document.createElement('div');
    row.className = 'meta-field-row';
    row.innerHTML =
      '<input type="text" class="meta-key" value="' + (key||'') + '" placeholder="field name">' +
      '<input type="text" class="meta-val" value="' + (val||'') + '" placeholder="value (comma-separate for a list)">' +
      '<button type="button" class="remove-field" aria-label="Remove field">&times;</button>';
    row.querySelector('.remove-field').addEventListener('click', function() { row.remove(); });
    document.getElementById('newArchiveMetaFields').appendChild(row);
  }

  document.getElementById('newArchiveBtn').addEventListener('click', openNewArchiveModal);
  document.getElementById('newArchiveClose').addEventListener('click', closeNewArchiveModal);
  document.getElementById('newArchiveCancel').addEventListener('click', closeNewArchiveModal);
  document.getElementById('newArchiveAddField').addEventListener('click', function() { addArchiveMetaRow(); });
  document.getElementById('archiveSplitHuffman').addEventListener('change', function() {
    document.getElementById('splitHuffmanWarning').classList.toggle('show', this.checked);
  });
  archiveOverlay.addEventListener('click', function(e) { if (e.target === archiveOverlay) closeNewArchiveModal(); });
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape' && archiveOverlay.classList.contains('open')) closeNewArchiveModal();
  });

  document.getElementById('newArchiveCreate').addEventListener('click', function() {
    var statusEl = document.getElementById('archiveStatus');
    var sources = Array.from(archiveSelection.keys());
    if (sources.length === 0) {
      statusEl.textContent = 'Select at least one file or folder first.';
      return;
    }
    var dest = document.getElementById('archiveDest').value.trim();
    if (!dest) {
      statusEl.textContent = 'Enter a destination filename first.';
      return;
    }
    var password = document.getElementById('archivePassword').value;
    var noCompress = document.getElementById('archiveNoCompress').checked;
    var splitHuffman = document.getElementById('archiveSplitHuffman').checked;
    var meta = {};
    document.querySelectorAll('#newArchiveMetaFields .meta-field-row').forEach(function(row) {
      var k = row.querySelector('.meta-key').value.trim();
      var v = row.querySelector('.meta-val').value.trim();
      if (!k) return;
      meta[k] = v.indexOf(',') !== -1 ? v.split(',').map(function(s) { return s.trim(); }) : v;
    });

    statusEl.textContent = 'Creating archive...';
    fetch('/api/compress', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sources: sources, dest: dest, password: password, meta: Object.keys(meta).length ? meta : null, no_compress: noCompress, split_huffman: splitHuffman }),
    }).then(function(res) { return res.json(); }).then(function(result) {
      if (result.ok) {
        statusEl.textContent = 'Created: ' + result.dest + ' (' + result.file_count + ' file(s))';
        setTimeout(function() {
          closeNewArchiveModal();
          document.getElementById('pathInput').value = archiveCurrentPath || '';
          doSearch();
        }, 900);
      } else {
        statusEl.textContent = 'Error: ' + result.error;
      }
    }).catch(function(err) {
      statusEl.textContent = 'Request failed: ' + err;
    });
  });

  // Initial listing on load (no filters -- shows everything under the
  // server's default paths, per --web's launch arguments).
  doSearch();
})();
</script>
</body>
</html>
"""
