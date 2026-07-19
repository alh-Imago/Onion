"""
shell_livesearch.py — Guided, live-feedback search for the interactive shell.

Typing bare `search` (no arguments) launches this instead of the old
"list everything" behaviour. As each term is typed:
  - GREEN as soon as it matches something already known from a fast,
    already-scanned index (tag names/values, other metadata values seen
    across the current directory) -- this is the "fast index" from the
    sidecar/semantic-index design (docs/sidecar_semantic_index_design_note.md,
    Imago-Unicell repo), realised here in its simplest possible form: a
    flat set of strings gathered from ace.search.search(), not yet the
    real persistent master index that design describes.
  - YELLOW while a term isn't in the fast index yet and a background
    deep search (a real ace.search.search(any_text=...) call) is
    checking whether a full scan finds it anyway (e.g. a filename inside
    a TOC that never made it into the flat fast-index set).
  - RED if the deep search comes back empty too -- genuinely not found,
    not just "not indexed yet."

Tab commits the current term (as a metadata filter if it contains '=',
otherwise as freetext) and starts entry on the next one. Enter commits
whatever's currently typed (if any) and finishes, running the full
accumulated search. Ctrl-C cancels the whole thing with no search run.

Rapid typing correctly supersedes a stale in-flight deep search for an
abandoned term -- verified directly (see the module's own dev-test
history), not just assumed safe from the async structure.

Requires prompt_toolkit (optional extra, `pip install prompt_toolkit`
or `onion-compress[shell]`) for real live terminal input -- this is a
genuinely different thing from the line-buffered `input()` the rest of
the shell uses, needing raw terminal handling that's properly
platform-specific to get right, hence reaching for an established
library rather than hand-rolling it.
"""

import threading


def parse_search_terms(args):
    """key=value pairs become metadata filters; every other bare word is
    freetext, appended in order. The literal word 'any' is accepted but
    optional/ignored. Lives here (not in shell.py) specifically so this
    module's own accumulated-stats function and shell.py's one-shot
    search both call the exact same parser -- two separate copies of the
    same syntax would risk drifting out of sync with each other, which
    would show a live count that disagrees with what Enter actually
    returns."""
    meta_filters = {}
    any_parts = []
    for token in args:
        if token == "any":
            continue
        elif "=" in token:
            k, _, v = token.partition("=")
            meta_filters[k] = v
        else:
            any_parts.append(token)
    any_text = " ".join(any_parts) or None
    return meta_filters, any_text


def make_accumulated_stats_fn(paths):
    """Returns a callable(term_list) -> (dir_count, file_count) counting
    how many directories/archives match the given committed terms so
    far, parsed with parse_search_terms(). Used to show the running
    "N directories, M files" narrowing indicator during guided search."""
    from ace.search import search as run_search
    import os

    def stats_fn(term_list):
        meta_filters, any_text = parse_search_terms(term_list)
        results = list(run_search(paths, meta_filters=meta_filters, any_text=any_text, recursive=True))
        dirs = {os.path.dirname(r["path"]) for r in results}
        return len(dirs), len(results)

    return stats_fn


class LiveMatchState:
    """Tracks fast-index / deep-search status for the currently-typed text.
    Kept separate from any terminal-rendering code so the state machine
    itself is testable without a real (or simulated) terminal at all."""

    def __init__(self, known_terms, deep_search_fn):
        self.known_terms = known_terms
        self.deep_search_fn = deep_search_fn
        self._lock = threading.Lock()
        self._current_text = ""
        self._deep_running = False
        self._deep_result_for = None
        self._deep_found = None
        self._on_change_callback = None

    def set_on_change(self, cb):
        self._on_change_callback = cb

    def update_text(self, text):
        with self._lock:
            self._current_text = text
            self._deep_result_for = None
            self._deep_found = None
        if not text or self._found_fast(text):
            return
        with self._lock:
            self._deep_running = True

        def worker(target_text):
            found = self.deep_search_fn(target_text)
            with self._lock:
                # Only apply this result if the user hasn't since moved on
                # to typing something else -- an in-flight search for an
                # abandoned term must never colour the CURRENT term.
                if self._current_text == target_text:
                    self._deep_running = False
                    self._deep_result_for = target_text
                    self._deep_found = found
            if self._on_change_callback:
                self._on_change_callback()

        threading.Thread(target=worker, args=(text,), daemon=True).start()

    def _found_fast(self, text):
        t = text.lower()
        return any(t in known.lower() for known in self.known_terms)

    def status(self):
        """Returns 'empty' | 'green' | 'yellow' | 'red'."""
        with self._lock:
            text = self._current_text
            deep_running = self._deep_running
            deep_result_for = self._deep_result_for
            deep_found = self._deep_found
        if not text:
            return "empty"
        if self._found_fast(text):
            return "green"
        if deep_result_for == text:
            return "green" if deep_found else "red"
        return "yellow"


def gather_known_terms(paths):
    """Flat set of searchable strings from everything ace.search already
    knows about archives under *paths* -- tag values, description text,
    other metadata values, and (via the TOC) filenames inside directory
    archives. This is the simplest possible fast index: a plain scan
    result, not a persistent one -- the real sidecar master index this
    is standing in for would make this instant even on a huge collection;
    this version still walks the directory once, same cost as a normal
    `search` with no filters."""
    from ace.search import search as run_search

    terms = set()
    for summary in run_search(paths, meta_filters={}, any_text=None, recursive=True):
        for value in (summary.get("meta") or {}).values():
            if isinstance(value, list):
                terms.update(str(v) for v in value)
            else:
                terms.add(str(value))
        for entry in (summary.get("contents") or []):
            if entry.get("path"):
                terms.add(entry["path"])
    return terms


def make_deep_search_fn(paths):
    """Returns a callable(text) -> bool checking whether a real
    any_text search under *paths* finds anything at all."""
    from ace.search import search as run_search

    def deep_search(text):
        results = list(run_search(paths, meta_filters={}, any_text=text, recursive=True))
        return len(results) > 0

    return deep_search


def run_guided_search(known_terms, deep_search_fn, accumulated_stats_fn=None, _input=None, _output=None):
    """Runs the live terminal UI; returns the list of committed term
    strings (empty list if cancelled with Ctrl-C).

    accumulated_stats_fn(term_list) -> (dir_count, file_count), if given,
    is called in the background after every Tab commit (and once at
    startup, with an empty list) to show a running "N directories,
    M files" indicator -- how far the committed terms so far are
    narrowing the search, updated each time another term is added.
    Computed off the main thread (same pattern as the deep-search
    fallback) so a slower directory doesn't freeze typing while it runs.

    _input/_output are for automated testing only (inject a simulated
    terminal via prompt_toolkit's create_pipe_input()/DummyOutput()) --
    real usage never sets these, so the real terminal is used."""
    from prompt_toolkit import Application
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.layout import Layout, Window, HSplit
    from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.styles import Style
    from prompt_toolkit.lexers import Lexer
    import threading

    committed = []
    state = LiveMatchState(known_terms, deep_search_fn)
    buf = Buffer()
    app_ref = {}
    stats = {"dirs": None, "files": None, "computing": False, "generation": 0}

    def on_state_change():
        if app_ref.get("app"):
            app_ref["app"].invalidate()

    state.set_on_change(on_state_change)

    def recompute_stats():
        if not accumulated_stats_fn:
            return
        stats["computing"] = True
        my_generation = stats["generation"]
        on_state_change()

        def worker():
            dirs, files = accumulated_stats_fn(list(committed))
            # Only apply if nothing's been committed/changed since this
            # calculation started -- same stale-result guard as the
            # per-term deep search, for the same reason.
            if stats["generation"] == my_generation:
                stats["dirs"], stats["files"] = dirs, files
                stats["computing"] = False
                on_state_change()

        threading.Thread(target=worker, daemon=True).start()

    def on_text_changed(_):
        state.update_text(buf.text)

    buf.on_text_changed += on_text_changed

    STYLE_MAP = {"green": "class:found", "red": "class:notfound", "yellow": "class:searching", "empty": ""}

    class StatusLexer(Lexer):
        def lex_document(self, document):
            def get_line(lineno):
                text = document.lines[lineno] if lineno < len(document.lines) else ""
                return [(STYLE_MAP.get(state.status(), ""), text)]
            return get_line

    kb = KeyBindings()

    @kb.add("tab")
    def _commit(event):
        text = buf.text.strip()
        if text:
            committed.append(text)
            buf.text = ""
            stats["generation"] += 1
            recompute_stats()

    @kb.add("enter")
    def _finish(event):
        text = buf.text.strip()
        if text:
            committed.append(text)
        event.app.exit(result=committed)

    @kb.add("c-c")
    def _cancel(event):
        event.app.exit(result=[])

    def committed_text():
        return [("class:committed", "  ".join(f"[{t}]" for t in committed) or "(type a term, Tab to add another, Enter to search)")]

    def stats_text():
        if not accumulated_stats_fn:
            return [("", "")]
        if stats["computing"] and stats["dirs"] is None:
            return [("class:stats", "counting...")]
        if stats["dirs"] is None:
            return [("", "")]
        note = " (updating...)" if stats["computing"] else ""
        return [("class:stats", f"{stats['dirs']} director{'y' if stats['dirs']==1 else 'ies'}, "
                                  f"{stats['files']} file{'' if stats['files']==1 else 's'}{note}")]

    stats_window = Window(FormattedTextControl(stats_text), height=1)
    input_window = Window(BufferControl(buffer=buf, lexer=StatusLexer()), height=1)
    committed_window = Window(FormattedTextControl(committed_text), height=1)
    layout = Layout(HSplit([stats_window, committed_window, input_window]))

    style = Style.from_dict({
        "found": "#00ff00 bold",
        "notfound": "#ff0000 bold",
        "searching": "#ffff00",
        "committed": "#888888",
        "stats": "#00afff bold",
    })

    if accumulated_stats_fn:
        recompute_stats()  # baseline count (0 terms = everything) before the first keystroke

    app = Application(layout=layout, key_bindings=kb, style=style, full_screen=False,
                      input=_input, output=_output)
    app_ref["app"] = app
    result = app.run()
    return result if result is not None else committed
