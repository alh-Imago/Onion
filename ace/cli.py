"""
cli.py — Onion Compression Engine CLI
───────────────────────────────────────
Usage:
  onion -c <path> [options]           compress file(s) or directory
  onion -d <file.onion> [options]     decompress / extract
  onion -i <file.onion>               inspect archive (no decompression needed)
  onion --set-meta <file.onion> ...   update metadata without recompressing
  onion --verify <file.onion>         verify HMAC signature
  onion --unwrap <file.onion>         restore original file(s), delete the archive
  onion --delete <file.onion>         permanently delete the archive, no extraction
  onion --search <path> [...]         search .onion archives by metadata, no decompression
  onion --web <path> [...]            launch local web UI (browser, no install beyond stdlib)
  onion --qt [path...]                launch native desktop UI (requires: pip install PyQt6)

Search options:
  --meta key=value          require this metadata field to match (repeatable, AND)
                            tags=x matches an archive tagged [x, ...]; tags=x,y
                            requires both x and y present
  --any <text>              case-insensitive substring match against filename
                            (including inside directory archives via TOC) and
                            any metadata value
  --no-recursive            only scan the given path(s) themselves, not subdirs

Web/desktop UI options:
  --port <n>                port for --web (default: 8000)
  (--qt takes no extra options beyond the starting path(s))

Compress options:
  -o <path>                 output path (default: <name>.onion)
  -e                        encrypt with AES-256-GCM
  -p <password>             password (else prompts)
  --exclude <pattern>       exclude glob pattern (repeatable)
  --no-default-ignores      disable built-in ignore list (e.g. *.onion, __pycache__/, .git/)
  --no-audit                omit audit block
  --meta key=value          add metadata (repeatable)
                            tags=a,b,c  →  list
                            age=42      →  integer
  --sign-key <key>          HMAC-sign the archive with this key
  --fast                    use LZ4 instead of LZ77/LZMA (requires pip install lz4)
  --encrypt-only            skip compression entirely, AES-256-GCM only (implies -e)
  --no-compress             store payload raw, skip compression -- independent of
                            encryption (unlike --encrypt-only). Header/TOC/META
                            wrapper still applies, so it stays fully searchable.
  --split-huffman           EXPERIMENTAL, opt-in only, never automatic. Separate
                            Huffman trees for literals vs match data. Pure Python
                            (slower). Genuinely mixed results -- smaller on
                            random/repetitive data, larger on typical source code,
                            small files, and general text. Try it and compare.

Decompress options:
  -o <path>                 output path (default: auto, stripping .onion)
  -p <password>             password for encrypted archives

Unwrap options (restores original file(s), then deletes the archive):
  -p <password>             password, if the archive is encrypted
                            Refuses to overwrite an existing destination.

Delete options (permanent, no extraction):
  --yes                     skip the "type yes to confirm" prompt (for scripting)

set-meta options:
  --meta key=value          metadata to set/merge (repeatable)
  --replace                 replace all metadata instead of merging
  --sign-key <key>          re-sign after update
  -p <password>             alias for --sign-key (convenience)

verify options:
  --sign-key <key>          key to verify against
  -p <password>             alias for --sign-key
"""

import argparse
import getpass
import os
import sys


def _compress(args):
    from .analyser    import analyse
    from .transformer import compress_files
    from .manifest    import collect
    from .ignore      import build_matcher
    from .meta        import parse_pairs

    paths = args.compress_files
    for p in paths:
        if not os.path.exists(p):
            print(f"Error: path not found: {p}", file=sys.stderr); sys.exit(1)

    base_dir = paths[0] if (len(paths) == 1 and os.path.isdir(paths[0])) else ""
    matcher  = build_matcher(
        extra_patterns      = args.exclude or [],
        base_dir            = base_dir,
        use_default_ignores = not args.no_default_ignores,
    )

    try:
        files, label = collect(paths, matcher=matcher)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr); sys.exit(1)

    if not files:
        print("Error: no files to compress after applying ignore patterns.", file=sys.stderr)
        sys.exit(1)

    # Password
    password = ""
    if args.encrypt:
        if args.password:
            password = args.password
        else:
            password = getpass.getpass("Enter encryption password: ")
            confirm  = getpass.getpass("Confirm password: ")
            if password != confirm:
                print("Error: passwords do not match.", file=sys.stderr); sys.exit(1)
        if not password:
            print("Error: password cannot be empty.", file=sys.stderr); sys.exit(1)

    # Metadata
    meta_pairs = None
    if args.meta:
        try:
            meta_pairs = parse_pairs(args.meta)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr); sys.exit(1)

    sign_key = args.sign_key or None

    dest      = args.output or (label + ".onion")
    total_raw = sum(len(d) for _, d in files)

    print(f"\nOnion Compression Engine v0.1")
    print(f"─────────────────────────────")
    if len(files) == 1:
        print(f"Source : {files[0][0]}  ({total_raw:,} bytes)")
    else:
        print(f"Source : {len(files)} files  ({total_raw:,} bytes total)")
        for rel, data in files[:8]:
            print(f"         {rel}  ({len(data):,} bytes)")
        if len(files) > 8:
            print(f"         ... and {len(files)-8} more")
    print(f"Dest   : {dest}")
    enc_label = "compress + AES-256-GCM" if args.encrypt else "compress"
    if sign_key: enc_label += " + HMAC-signed"
    print(f"Mode   : {enc_label}")
    if meta_pairs:
        print(f"Meta   : {list(meta_pairs.keys())}")

    total_data = b"".join(d for _, d in files)
    print(f"\n[Phase 1 — Strategist]")
    encrypt_only  = getattr(args, 'encrypt_only', False)
    no_compress   = getattr(args, 'no_compress', False)
    split_huffman = getattr(args, 'split_huffman', False)
    if encrypt_only: args.encrypt = True
    if encrypt_only and not args.encrypt:
        args.encrypt = True
    iset = analyse(total_data, encrypt=args.encrypt,
                   fast=getattr(args,'fast',False),
                   encrypt_only=encrypt_only,
                   no_compress=no_compress,
                   split_huffman=split_huffman)

    print(f"\n[Phase 2 — Transformer]")
    compress_files(files, iset, dest,
                   password=password,
                   audit=not args.no_audit,
                   meta_pairs=meta_pairs,
                   sign_key=sign_key)
    print(f"\nDone.\n")


def _decompress(args):
    from .transformer import decompress
    from .header      import unpack_header

    src = args.decompress_file
    if not os.path.isfile(src):
        print(f"Error: file not found: {src}", file=sys.stderr); sys.exit(1)

    with open(src, "rb") as f: raw = f.read()
    iset, _, _ = unpack_header(raw)

    password = ""
    if iset.encrypt:
        if args.password:
            password = args.password
        else:
            password = getpass.getpass("Enter decryption password: ")
        if not password:
            print("Error: archive is encrypted but no password supplied.",
                  file=sys.stderr); sys.exit(1)

    if args.output:
        dest = args.output
    elif src.endswith(".onion"):
        dest = src[:-6] or "extracted"
    else:
        dest = src + ".extracted"

    print(f"\nOnion Compression Engine v0.1")
    print(f"─────────────────────────────")
    print(f"Source : {src}  ({os.path.getsize(src):,} bytes)")
    print(f"Dest   : {dest}")
    if iset.encrypt: print(f"Mode   : decrypt + decompress")

    written = decompress(src, dest, password=password)
    print(f"\nExtracted {len(written)} file(s).\n")


def _inspect(args):
    from .header      import unpack_header, unpack_audit, AUDIT_MAGIC
    from .instruction import AlgoID
    from .transformer import _DECOMPRESS
    from .manifest    import is_manifest, unpack as manifest_unpack
    from .meta        import unpack as meta_unpack, is_meta, block_size
    from .toc         import unpack as toc_unpack, is_toc, block_size as toc_block_size
    import json, struct

    src = args.inspect_file
    if not os.path.isfile(src):
        print(f"Error: file not found: {src}", file=sys.stderr); sys.exit(1)

    with open(src, "rb") as f:
        data = f.read()

    iset, payload_offset, has_audit = unpack_header(data)
    total_payload = iset.layers[-1].compressed_size if iset.layers else 0

    print(f"\nOnion Archive Inspector")
    print(f"───────────────────────")
    print(f"File            : {src}  ({os.path.getsize(src):,} bytes)")
    print(f"Original size   : {iset.original_size:,} bytes")
    print(f"Original CRC32  : 0x{iset.original_crc:08X}")
    print(f"Encrypted       : {'Yes' if iset.encrypt else 'No'}")
    print(f"Layers          : {len(iset.layers)}")
    print()
    for i, layer in enumerate(iset.layers):
        print(f"  Layer {i+1}: {AlgoID.name(layer.algo_id)}")
        print(f"    Compressed size : {layer.compressed_size:,} bytes")
        print(f"    Checksum CRC32  : 0x{layer.checksum:08X}")

    # Trailing blocks: AUDIT, then TOC, then META (in that order on disk)
    trail = payload_offset + total_payload
    if has_audit:
        audit_recipe = unpack_audit(data, trail)
        if audit_recipe:
            aj_len = struct.unpack_from(">H", data, trail + 4)[0]
            trail += 4 + 2 + aj_len
            print(f"\nAudit Block:")
            print(json.dumps(audit_recipe, indent=2))

    # Contents: fast path via TOC block (no decompression, any archive size).
    # Falls back to full decompression only for older archives written
    # before the TOC block existed.
    toc_entries = toc_unpack(data, trail) if is_toc(data, trail) else None
    if toc_entries is not None:
        trail += toc_block_size(data, trail)
        total_raw = sum(e["size"] for e in toc_entries)
        print(f"\nContents ({len(toc_entries)} file(s), {total_raw:,} bytes uncompressed) [from TOC, no decompression]:")
        for e in toc_entries:
            print(f"  {e['size']:>10,}  {e['path']}")
    elif not iset.encrypt:
        try:
            payload = data[payload_offset: payload_offset + total_payload]
            current = payload
            for layer in reversed(iset.layers):
                current = _DECOMPRESS[layer.algo_id](current, "")
            if is_manifest(current):
                files     = manifest_unpack(current)
                total_raw = sum(len(d) for _, d in files)
                print(f"\nContents ({len(files)} file(s), {total_raw:,} bytes uncompressed) [decompressed, no TOC in this archive]:")
                for rel, fdata in files:
                    print(f"  {len(fdata):>10,}  {rel}")
        except Exception:
            pass
    else:
        print(f"\n  (File listing not available for encrypted archives without a TOC block)")

    if is_meta(data, trail):
        meta = meta_unpack(data, trail)
        if meta:
            print(f"\nMetadata:")
            for k, v in meta.items():
                if k == 'hmac_sha256':
                    print(f"  hmac_sha256    : {v[:24]}...  (use --verify to check)")
                else:
                    print(f"  {k:<16} : {v}")
    print()


def _set_meta(args):
    from .transformer import set_meta
    from .meta        import parse_pairs

    src = args.set_meta_file
    if not os.path.isfile(src):
        print(f"Error: file not found: {src}", file=sys.stderr); sys.exit(1)

    if not args.meta:
        print("Error: --meta key=value required.", file=sys.stderr); sys.exit(1)

    try:
        pairs = parse_pairs(args.meta)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr); sys.exit(1)

    sign_key = args.sign_key or args.password or None
    merge    = not args.replace

    print(f"\nOnion — updating metadata: {src}")
    set_meta(src, pairs, sign_key=sign_key, merge=merge)
    print(f"Done.\n")


def _verify(args):
    from .transformer import verify

    src = args.verify_file
    if not os.path.isfile(src):
        print(f"Error: file not found: {src}", file=sys.stderr); sys.exit(1)

    sign_key = args.sign_key or args.password
    if not sign_key:
        sign_key = getpass.getpass("Enter signing key: ")

    print(f"\nOnion — verifying: {src}")
    try:
        ok = verify(src, sign_key)
        if ok:
            print(f"  ✓ HMAC-SHA256 signature VALID — archive is authentic and unmodified.\n")
        else:
            print(f"  ✗ HMAC-SHA256 signature INVALID — archive may have been tampered with.\n")
            sys.exit(2)
    except ValueError as e:
        print(f"  Error: {e}\n", file=sys.stderr); sys.exit(1)


def _unwrap(args):
    from .transformer import unwrap
    from .header      import unpack_header

    src = args.unwrap_file
    if not os.path.isfile(src):
        print(f"Error: file not found: {src}", file=sys.stderr); sys.exit(1)

    with open(src, "rb") as f: raw = f.read()
    iset, _, _ = unpack_header(raw)

    password = ""
    if iset.encrypt:
        if args.password:
            password = args.password
        else:
            password = getpass.getpass("Enter decryption password: ")
        if not password:
            print("Error: archive is encrypted but no password supplied.",
                  file=sys.stderr); sys.exit(1)

    print(f"\nOnion — removing wrapper: {src}")
    try:
        written = unwrap(src, password=password)
        print(f"\nDone. Original restored, archive wrapper removed. {len(written)} file(s).\n")
    except ValueError as e:
        print(f"  Error: {e}\n", file=sys.stderr); sys.exit(1)


def _delete(args):
    src = args.delete_file
    if not os.path.isfile(src):
        print(f"Error: file not found: {src}", file=sys.stderr); sys.exit(1)
    if not src.lower().endswith(".onion"):
        print(f"Error: not a .onion archive: {src}", file=sys.stderr); sys.exit(1)

    if not args.yes:
        print(f"\nThis will PERMANENTLY delete the archive (no extraction, no undo):")
        print(f"  {src}")
        confirm = input("Type 'yes' to confirm: ").strip().lower()
        if confirm != "yes":
            print("Cancelled -- nothing was deleted.\n")
            return

    os.remove(src)
    print(f"\nDeleted: {src}\n")


def _search(args):
    from .search import search

    meta_filters = {}
    for pair in args.meta:
        if '=' not in pair:
            print(f"Error: --meta value must be key=value, got: {pair!r}", file=sys.stderr)
            sys.exit(1)
        k, _, v = pair.partition('=')
        meta_filters[k.strip()] = v.strip()

    paths = args.search_paths
    for p in paths:
        if not os.path.exists(p):
            print(f"Error: path not found: {p}", file=sys.stderr); sys.exit(1)

    results = list(search(
        paths,
        meta_filters=meta_filters,
        any_text=args.any_text,
        recursive=not args.no_recursive,
    ))

    if not results:
        print("\nNo matching archives found.\n")
        return

    print(f"\nOnion Search — {len(results)} match(es)")
    print("─" * 60)
    for r in results:
        tags = r["meta"].get("tags")
        desc = r["meta"].get("description", "")
        enc  = " [encrypted]" if r["encrypted"] else ""
        print(f"\n  {r['path']}{enc}")
        print(f"    {r['original_size']:,} bytes original, {r['layer_count']} layer(s)")
        if tags:
            print(f"    tags: {', '.join(tags) if isinstance(tags, list) else tags}")
        if desc:
            print(f"    {desc}")
        contents = r.get("contents")
        if contents:
            print(f"    contents ({len(contents)} file(s)):")
            for entry in contents:
                print(f"      {entry.get('size', 0):>10,}  {entry.get('path', '?')}")
    print()


def _web(args):
    from .webui import run

    paths = args.web_paths
    for p in paths:
        if not os.path.exists(p):
            print(f"Error: path not found: {p}", file=sys.stderr); sys.exit(1)

    run(paths, port=args.port)


def _qt(args):
    try:
        from PyQt6.QtWidgets import QApplication
    except ImportError:
        print("Error: PyQt6 is not installed. Install it with: pip install PyQt6",
              file=sys.stderr)
        sys.exit(1)

    from .qtui.main_window import MainWindow
    from .qtui import theme as theme_mod

    paths = args.qt_paths or [os.path.expanduser("~")]
    for p in paths:
        if not os.path.exists(p):
            print(f"Error: path not found: {p}", file=sys.stderr); sys.exit(1)

    app = QApplication(sys.argv[:1])
    theme_mod.apply_theme(app, dark=False)
    window = MainWindow(default_paths=paths)
    window.show()
    from PyQt6.QtCore import QThreadPool
    app.aboutToQuit.connect(lambda: QThreadPool.globalInstance().waitForDone(3000))
    sys.exit(app.exec())


def main():
    parser = argparse.ArgumentParser(
        prog="onion",
        description="Onion \U0001f9c5 — Adaptive Layered Compression with a Searchable, Self-Describing Wrapper",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    parser.add_argument("-c",  dest="compress_files",  metavar="PATH", nargs="+",
                        help="compress file(s) or directory")
    parser.add_argument("-d",  dest="decompress_file", metavar="FILE",
                        help="decompress/extract FILE.onion")
    parser.add_argument("-i",  dest="inspect_file",    metavar="FILE",
                        help="inspect archive")
    parser.add_argument("--set-meta", dest="set_meta_file", metavar="FILE",
                        help="update metadata block without recompressing")
    parser.add_argument("--verify",   dest="verify_file",   metavar="FILE",
                        help="verify HMAC-SHA256 signature")
    parser.add_argument("--unwrap", dest="unwrap_file", metavar="FILE",
                        help="restore the original file(s) from FILE.onion, then delete the "
                             "archive -- undoes the wrapper, no data lost. Distinct from --delete.")
    parser.add_argument("--delete", dest="delete_file", metavar="FILE",
                        help="permanently delete FILE.onion with NO extraction -- irreversible. "
                             "Prompts for confirmation unless --yes is given.")
    parser.add_argument("--yes", dest="yes", action="store_true",
                        help="skip the confirmation prompt for --delete (for scripting)")
    parser.add_argument("--search", dest="search_paths", metavar="PATH", nargs="+",
                        help="search .onion archives under PATH(s) by metadata")
    parser.add_argument("--web", dest="web_paths", metavar="PATH", nargs="+",
                        help="launch local web UI for browsing/searching archives under PATH(s)")
    parser.add_argument("--port", dest="port", type=int, default=8000,
                        help="port for --web (default: 8000)")
    parser.add_argument("--qt", dest="qt_paths", metavar="PATH", nargs="*",
                        help="launch the PyQt6 desktop UI for browsing/searching archives, "
                             "optionally starting at PATH(s). Requires PyQt6 (pip install PyQt6).")

    parser.add_argument("-o",  dest="output",   metavar="OUTPUT", help="output path")
    parser.add_argument("-e",  dest="encrypt",  action="store_true",
                        help="encrypt with AES-256-GCM")
    parser.add_argument("-p",  "--password", dest="password", metavar="PASSWORD",
                        help="password for encryption/decryption/signing")
    parser.add_argument("--sign-key", dest="sign_key", metavar="KEY",
                        help="HMAC signing/verification key")

    parser.add_argument("--meta", dest="meta", metavar="KEY=VALUE",
                        action="append", default=[],
                        help="metadata key=value pair (repeatable)")
    parser.add_argument("--replace", dest="replace", action="store_true",
                        help="replace metadata instead of merging (set-meta only)")
    parser.add_argument("--any", dest="any_text", metavar="TEXT",
                        help="substring match against filename/metadata (search only)")
    parser.add_argument("--no-recursive", dest="no_recursive", action="store_true",
                        help="don't recurse into subdirectories (search only)")

    parser.add_argument("--exclude", dest="exclude", metavar="PATTERN",
                        action="append", default=[],
                        help="exclude glob pattern (repeatable)")
    parser.add_argument("--no-default-ignores", dest="no_default_ignores",
                        action="store_true",
                        help="disable built-in ignore list")
    parser.add_argument("--no-audit", dest="no_audit", action="store_true",
                        help="omit audit block")
    parser.add_argument("--fast", dest="fast", action="store_true",
                        help="fast mode: use LZ4 instead of LZ77+Huffman/LZMA (requires pip install lz4)")
    parser.add_argument("--encrypt-only", dest="encrypt_only", action="store_true",
                        help="skip compression entirely, encrypt only (implies -e)")
    parser.add_argument("--no-compress", dest="no_compress", action="store_true",
                        help="store payload raw, skip compression entirely -- independent of "
                             "encryption (unlike --encrypt-only, does not require -e). The "
                             "header/TOC/META wrapper still applies, so the file stays fully "
                             "searchable via --search/-i even though it was never compressed.")
    parser.add_argument("--split-huffman", dest="split_huffman", action="store_true",
                        help="EXPERIMENTAL: use LZ77 with separate Huffman trees for literals "
                             "vs match data, instead of the normal decision tree. Pure Python, "
                             "meaningfully slower than the default (no C acceleration). NOT a "
                             "universal win: genuinely smaller on random/incompressible and "
                             "highly-repetitive data, genuinely LARGER on typical source code, "
                             "small files, and general text. Never chosen automatically -- "
                             "opt-in only. Try it and compare before relying on it.")

    args = parser.parse_args()

    if args.compress_files:   _compress(args)
    elif args.decompress_file: _decompress(args)
    elif args.inspect_file:    _inspect(args)
    elif args.set_meta_file:   _set_meta(args)
    elif args.verify_file:     _verify(args)
    elif args.unwrap_file:     _unwrap(args)
    elif args.delete_file:     _delete(args)
    elif args.search_paths:    _search(args)
    elif args.web_paths:       _web(args)
    elif args.qt_paths is not None: _qt(args)
    else:
        parser.print_help(); sys.exit(1)


if __name__ == "__main__":
    main()
