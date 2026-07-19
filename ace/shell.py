"""
shell.py — Interactive Onion shell (onion --shell, or bare `onion`).

A domain-specific REPL, same category as sqlite3's or redis-cli's
interactive prompt -- not a general-purpose shell replacing bash/
PowerShell. Commands drop the `onion`/`--` prefix since you're already
inside the Onion domain; the prompt makes that plain so it's never
confused with the surrounding OS shell.

Anything NOT recognised as an Onion command (mv, cp, ls, dir, move,
copy, and so on) passes straight through to the real OS shell, run with
this shell's own tracked cwd. Deliberate, not a gap to fill in later --
this stays scoped to Onion's own operations; ordinary file/OS commands
are the OS's job to handle, not something to reimplement here.

Ties into the daemon (ace/daemon.py) for search, so repeated searches in
one session are warm rather than cold-scanning the filesystem every
time. Any frontend spawned from within the shell (web/qt) discovers the
SAME daemon automatically via the shared state file
(~/.onion/daemon.json) -- no explicit wiring needed for that part, it
falls out of the daemon's discovery mechanism for free.

Bare `search` (no arguments) launches a guided live search
(shell_livesearch.py) instead of listing everything -- type a term, see
live green/yellow/red feedback against known metadata with an automatic
deep-search fallback on a miss, Tab to add another term, Enter to run
the accumulated search. Requires prompt_toolkit (optional extra);
falls back to the old list-everything behaviour with a note if it isn't
installed, rather than crashing. `search key=value ...` (with arguments
on the same line) is unaffected -- the original one-shot behaviour.

Honest scope note: web/qt, when spawned from here, still do their own
in-process ace.search calls today, same as when launched directly from
the CLI -- they don't yet ROUTE their search calls through this daemon.
That's a real, separate follow-up (touching already-working, tested
code in webui.py/qtui deserves its own careful pass), not bundled in
here silently.
"""

import os
import shlex
import subprocess
import sys

from . import daemon


PROMPT_TEMPLATE = "onion:{cwd}> "


class OnionShell:
    def __init__(self):
        self.cwd = os.getcwd()
        self.sock = None

    def connect(self):
        print("Connecting to the Onion daemon...")
        try:
            self.sock = daemon.ensure_running()
            print("Connected.\n")
        except RuntimeError as e:
            print(f"Warning: {e}", file=sys.stderr)
            print("Continuing without the daemon -- searches will be slower (no cache).\n")
            self.sock = None

    def prompt(self):
        return PROMPT_TEMPLATE.format(cwd=self.cwd)

    def run(self):
        self.connect()
        print("Onion interactive shell. Type 'help' for commands, 'exit' to quit.\n")
        while True:
            try:
                line = input(self.prompt())
            except (EOFError, KeyboardInterrupt):
                print()
                break
            line = line.strip()
            if not line:
                continue
            if not self._dispatch(line):
                break
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        print("Goodbye.")

    def _dispatch(self, line: str) -> bool:
        """Returns False to end the shell loop, True to continue."""
        try:
            parts = shlex.split(line)
        except ValueError as e:
            print(f"Parse error: {e}")
            return True
        if not parts:
            return True

        cmd, rest = parts[0].lower(), parts[1:]

        if cmd in ("exit", "quit"):
            return False
        elif cmd == "help":
            self._cmd_help()
        elif cmd == "cd":
            self._cmd_cd(rest)
        elif cmd == "pwd":
            print(self.cwd)
        elif cmd == "search":
            self._cmd_search(rest)
        elif cmd == "compress":
            self._cmd_compress(rest)
        elif cmd == "web":
            self._cmd_spawn_frontend("--web", rest)
        elif cmd == "qt":
            self._cmd_spawn_frontend("--qt", rest)
        elif cmd == "daemon":
            self._cmd_daemon(rest)
        else:
            self._cmd_passthrough(line)
        return True

    def _cmd_passthrough(self, line: str):
        """Anything not recognised as an Onion command falls straight
        through to the real OS shell -- mv/cp/ls/dir/move/copy/etc. all
        just work, using the Onion shell's own tracked cwd, without this
        shell needing to know or reimplement any of them. Deliberate
        design, not a missing feature: this stays a domain-specific
        prompt for Onion's own operations, and normal file/OS commands
        are the OS's job, not something to duplicate here."""
        result = subprocess.run(line, shell=True, cwd=self.cwd)
        if result.returncode != 0:
            print(f"(exit code {result.returncode})")

    def _cmd_help(self):
        print("""
Commands:
  search                                 guided live search (type a term, Tab
                                          for another, Enter to run -- needs
                                          prompt_toolkit, falls back if absent)
  search [key=value ...] [any <text>]   one-shot search, same as before
  cd <path>                              change the shell's working directory
  pwd                                    show the current directory
  compress <path> [-e] [-p password]     compress a file/folder here
             [--meta key=value ...]
  web                                    launch the web UI here (separate process)
  qt                                     launch the desktop UI here (separate process)
  daemon status | stop                   check or stop the background daemon
  help                                   this message
  exit / quit                            leave the shell

  Anything else (mv, cp, ls, dir, move, copy, etc.) passes straight
  through to the real OS shell, using this shell's current directory --
  Onion doesn't reimplement general file operations, only its own.
""")

    def _cmd_cd(self, args):
        if not args:
            print(self.cwd)
            return
        target = os.path.abspath(os.path.join(self.cwd, os.path.expanduser(args[0])))
        if not os.path.isdir(target):
            print(f"Not a directory: {target}")
            return
        self.cwd = target

    def _cmd_search(self, args):
        """Parses: key=value pairs become metadata filters; every other
        bare word is freetext, appended in order. The 'any' keyword is
        accepted but optional -- 'search invoice' and 'search any invoice'
        do the same thing. (Previously a bare word with no 'any' keyword
        was silently dropped instead of being treated as freetext --
        `search invoice` looked like it worked but was actually running
        an unfiltered search; fixed here.)

        Bare `search` (no arguments at all) launches the guided live
        search instead of listing everything -- type a term, Tab to add
        another, Enter to run the accumulated search. Requires
        prompt_toolkit; falls back to listing everything with a note if
        it isn't installed, rather than crashing."""
        if not args:
            terms = self._guided_search_terms()
            if terms is None:
                return  # cancelled, or fell back and already printed a note
            args = terms

        meta_filters = {}
        any_parts = []
        for token in args:
            if token == "any":
                continue  # optional marker, kept for explicitness/readability
            elif "=" in token:
                k, _, v = token.partition("=")
                meta_filters[k] = v
            else:
                any_parts.append(token)
        any_text = " ".join(any_parts) or None

        request_args = {
            "paths": [self.cwd], "meta_filters": meta_filters,
            "any_text": any_text, "recursive": True,
        }

        if self.sock:
            try:
                resp = daemon.send_request(self.sock, "search", request_args)
            except OSError:
                print("Daemon connection lost, reconnecting...")
                self.connect()
                resp = self._search_direct(request_args) if not self.sock else \
                    daemon.send_request(self.sock, "search", request_args)
        else:
            resp = self._search_direct(request_args)

        if not resp.get("ok"):
            print(f"Error: {resp.get('error')}")
            return
        results = resp["results"]
        tag = " (cached)" if resp.get("cached") else ""
        print(f"{len(results)} match(es){tag}.")
        for r in results:
            enc = " [encrypted]" if r.get("encrypted") else ""
            tags = (r.get("meta") or {}).get("tags")
            tag_str = f"  tags: {tags}" if tags else ""
            print(f"  {r['path']}{enc}{tag_str}")

    def _guided_search_terms(self):
        """Runs the live guided search UI, returns a list of term
        strings to be parsed the same way as normal `search` arguments,
        or None if cancelled (caller should just return)."""
        try:
            from .shell_livesearch import gather_known_terms, make_deep_search_fn, run_guided_search
        except ImportError:
            print("(prompt_toolkit not installed -- listing everything instead. "
                  "Install it for guided live search: pip install prompt_toolkit)")
            return []

        print("Guided search: type a term (green=found, yellow=checking, red=not found).")
        print("Tab adds another term, Enter searches, Ctrl-C cancels.\n")
        known = gather_known_terms([self.cwd])
        deep = make_deep_search_fn([self.cwd])
        terms = run_guided_search(known, deep)
        if not terms:
            print("(cancelled, no search run)")
            return None
        return terms

    def _search_direct(self, request_args):
        from ace.search import search as run_search
        results = list(run_search(
            request_args["paths"], meta_filters=request_args["meta_filters"],
            any_text=request_args["any_text"], recursive=request_args["recursive"],
        ))
        return {"ok": True, "results": results, "cached": False}

    def _cmd_compress(self, args):
        if not args:
            print("Usage: compress <path> [-e] [-p password] [--meta key=value ...]")
            return
        src = os.path.abspath(os.path.join(self.cwd, args[0]))
        if not os.path.exists(src):
            print(f"Not found: {src}")
            return

        encrypt = "-e" in args
        password = ""
        meta = {}
        i = 1
        while i < len(args):
            if args[i] == "-p" and i + 1 < len(args):
                password = args[i + 1]; i += 2; continue
            if args[i] == "--meta" and i + 1 < len(args):
                k, _, v = args[i + 1].partition("="); meta[k] = v; i += 2; continue
            i += 1

        dest = src + ".onion"
        if os.path.exists(dest):
            print(f"Destination already exists: {dest}")
            return

        from ace.analyser import analyse
        from ace.transformer import compress_files
        from ace.manifest import collect
        from ace.ignore import build_matcher

        base_dir = src if os.path.isdir(src) else ""
        matcher = build_matcher(extra_patterns=[], base_dir=base_dir, use_default_ignores=True)
        files, _ = collect([src], matcher=matcher)
        if not files:
            print("Nothing to compress (everything matched an ignore pattern).")
            return
        total_data = b"".join(d for _, d in files)
        iset = analyse(total_data, encrypt=(encrypt or bool(password)))
        compress_files(files, iset, dest, password=password, audit=True, meta_pairs=meta or None)
        print(f"Created: {dest} ({len(files)} file(s))")

    def _cmd_spawn_frontend(self, flag, args):
        print(f"Launching {flag} at {self.cwd} (separate process)...")
        subprocess.Popen(
            [sys.executable, "-m", "ace.cli", flag, self.cwd] + args,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    def _cmd_daemon(self, args):
        sub = args[0] if args else "status"
        if sub == "status":
            if self.sock:
                try:
                    resp = daemon.send_request(self.sock, "ping")
                    print("Daemon: running and reachable." if resp.get("pong") else "Daemon: unexpected response.")
                except OSError:
                    print("Daemon: connection lost.")
            else:
                print("Daemon: not connected.")
        elif sub == "stop":
            if self.sock:
                daemon.send_request(self.sock, "shutdown")
                print("Daemon stop requested.")
                self.sock = None
            else:
                print("Not connected to a daemon.")
        else:
            print("Usage: daemon status | stop")


def main():
    OnionShell().run()


if __name__ == "__main__":
    main()
