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

  button.theme-toggle {
    background: var(--surface); border: 1px solid var(--border);
    color: var(--text); border-radius: 999px;
    padding: 7px 14px; font-size: 0.85rem; cursor: pointer;
    font-family: inherit;
    display: flex; align-items: center; gap: 6px;
  }
  button.theme-toggle:hover { border-color: var(--accent); }
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
    width: 100%; padding: 8px 10px; border-radius: 6px;
    border: 1px solid var(--border); background: var(--bg); color: var(--text);
    font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: 0.85rem;
  }
  .field { flex: 1; min-width: 160px; }
  .filters-list { display: flex; flex-direction: column; gap: 6px; margin-bottom: 8px; }
  .filter-row { display: flex; gap: 6px; }
  .filter-row input { flex: 1; }
  .filter-row button.remove {
    background: none; border: 1px solid var(--border); color: var(--muted);
    border-radius: 6px; padding: 0 10px; cursor: pointer;
  }
  .filter-row button.remove:hover { color: var(--badge-enc); border-color: var(--badge-enc); }

  .actions { display: flex; gap: 8px; margin-top: 12px; }
  button.primary, button.ghost {
    font-family: inherit; font-size: 0.85rem; font-weight: 500;
    padding: 9px 16px; border-radius: 7px; cursor: pointer; border: 1px solid transparent;
  }
  button.primary { background: var(--accent); color: #fff; }
  button.primary:hover { background: var(--accent-2); }
  button.ghost { background: transparent; border-color: var(--border); color: var(--text); }
  button.ghost:hover { border-color: var(--accent); color: var(--accent); }

  .status { font-size: 0.8rem; color: var(--muted); margin: 4px 2px 16px; font-family: "IBM Plex Mono", monospace; }

  .archive {
    background: var(--surface); border: 1px solid var(--border);
    border-left: 4px solid var(--accent);
    border-radius: 8px; padding: 14px 16px; margin-bottom: 10px;
    box-shadow: var(--shadow); cursor: pointer;
  }
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

  .peel-hint { font-size: 0.7rem; color: var(--muted); margin-top: 8px; }
  .empty {
    text-align: center; padding: 40px 20px; color: var(--muted);
    border: 1px dashed var(--border); border-radius: 10px;
  }
  .empty strong { color: var(--text); display: block; margin-bottom: 4px; }

  @media (prefers-reduced-motion: reduce) {
    * { transition: none !important; }
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
    <button class="theme-toggle" id="themeToggle" aria-label="Toggle light or dark theme">
      <span id="themeIcon">&#9788;</span> <span id="themeLabel">Light</span>
    </button>
  </header>

  <div class="panel">
    <div class="row">
      <div class="field">
        <label for="pathInput">Search path</label>
        <input type="text" id="pathInput" placeholder="e.g. /home/alan/archives">
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
      var encBadge = r.encrypted ? '<span class="badge enc">encrypted</span>' : '';

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

      card.innerHTML =
        '<div class="head"><span class="path">' + escapeHtml(r.path) + '</span><span class="badges">' + encBadge + tagsHtml + '</span></div>' +
        '<div class="meta-line">' + r.original_size.toLocaleString() + ' bytes original &middot; ' + r.layer_count + ' layer(s)' +
        (r.contents ? ' &middot; ' + r.contents.length + ' file(s)' : '') + '</div>' +
        (meta.description ? '<div class="desc">' + escapeHtml(meta.description) + '</div>' : '') +
        contentsHtml +
        '<div class="peel-hint">' + (r.contents && r.contents.length ? 'click to peel open &#8595;' : '') + '</div>';

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

  // Initial listing on load (no filters -- shows everything under the
  // server's default paths, per --web's launch arguments).
  doSearch();
})();
</script>
</body>
</html>
"""
