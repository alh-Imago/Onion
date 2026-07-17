"""
search.py — Metadata search across .onion archives
─────────────────────────────────────────────────────
Scans one or more paths for .onion files and matches them against metadata
filters, WITHOUT decompressing any payload. This is the "wrapper" design
principle in action: the header, audit block, TOC block, and META block
are all plain structural reads (fixed-size fields + short JSON blobs) — the
compressed file contents are only ever touched if something later chooses
to extract a match.

As of the TOC block (ace/toc.py), this now covers directory contents too:
file NAMES inside a directory archive are readable for free, on archives
of any size, with zero decompression -- including on encrypted archives,
since TOC sits outside the encrypted payload. Only actual file CONTENT
search would require decompression, and remains out of scope here.
"""

import os
import struct
from typing import Any, Dict, Iterator, List, Optional

from .header import unpack_header, unpack_audit
from .meta import unpack as meta_unpack, is_meta
from .toc import unpack as toc_unpack, is_toc, block_size as toc_block_size


def iter_onion_files(paths: List[str], recursive: bool = True) -> Iterator[str]:
    """Yield every .onion file found under the given file/directory paths."""
    for p in paths:
        if os.path.isfile(p):
            if p.lower().endswith(".onion"):
                yield p
            continue
        if not os.path.isdir(p):
            continue
        if recursive:
            for dirpath, _dirs, filenames in os.walk(p):
                for fn in filenames:
                    if fn.lower().endswith(".onion"):
                        yield os.path.join(dirpath, fn)
        else:
            for fn in sorted(os.listdir(p)):
                full = os.path.join(p, fn)
                if os.path.isfile(full) and fn.lower().endswith(".onion"):
                    yield full


def read_summary(path: str) -> Optional[Dict[str, Any]]:
    """
    Read just enough of *path* to describe it: header fields, audit
    recipe (if present), and META block (if present). Returns None if the
    file isn't a valid .onion archive. Never decompresses the payload.
    """
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None

    try:
        iset, payload_offset, has_audit = unpack_header(data)
    except (ValueError, IndexError, struct.error):
        return None

    total_payload = iset.layers[-1].compressed_size if iset.layers else 0
    trail = payload_offset + total_payload

    audit_recipe: Dict[str, Any] = {}
    if has_audit:
        audit_recipe = unpack_audit(data, trail) or {}
        if audit_recipe:
            aj_len = struct.unpack_from(">H", data, trail + 4)[0]
            trail += 4 + 2 + aj_len

    # TOC block: directory contents (path + size only), read without any
    # decompression. Present even for encrypted archives, since it sits
    # outside the encrypted payload entirely.
    contents: Optional[List[Dict[str, Any]]] = None
    if is_toc(data, trail):
        contents = toc_unpack(data, trail)
        trail += toc_block_size(data, trail)

    meta: Dict[str, Any] = {}
    if is_meta(data, trail):
        meta = meta_unpack(data, trail) or {}

    return {
        "path": path,
        "size_on_disk": os.path.getsize(path),
        "original_size": iset.original_size,
        "encrypted": iset.encrypt,
        "layer_count": len(iset.layers),
        "audit": audit_recipe,
        "contents": contents,
        "meta": meta,
    }


def _stringify(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def matches_meta_filter(summary: Dict[str, Any], key: str, value: str) -> bool:
    """
    True if summary['meta'][key] matches *value* (case-insensitive).
    If the stored value is a list (e.g. tags), match on membership OR on
    the whole list's stringified form -- so --meta tags=invoice matches
    an archive tagged ['invoice', 'q3'], and --meta tags=invoice,q3 also
    matches if you want an exact multi-tag filter.
    """
    stored = summary.get("meta", {}).get(key)
    if stored is None:
        return False
    needle = value.strip().lower()
    if isinstance(stored, list):
        haystack = [str(v).strip().lower() for v in stored]
        if "," in needle:
            wanted = [v.strip() for v in needle.split(",")]
            return all(w in haystack for w in wanted)
        return needle in haystack
    return str(stored).strip().lower() == needle


def matches_any_text(summary: Dict[str, Any], text: str) -> bool:
    """True if *text* (case-insensitive substring) appears anywhere in the
    archive's path, any metadata value, or (if present) any file path
    listed in the TOC block."""
    needle = text.strip().lower()
    if needle in os.path.basename(summary["path"]).lower():
        return True
    for v in summary.get("meta", {}).values():
        if needle in _stringify(v).lower():
            return True
    contents = summary.get("contents")
    if contents:
        for entry in contents:
            if needle in str(entry.get("path", "")).lower():
                return True
    return False


def search(
    paths: List[str],
    meta_filters: Optional[Dict[str, str]] = None,
    any_text: Optional[str] = None,
    recursive: bool = True,
) -> Iterator[Dict[str, Any]]:
    """
    Yield summary dicts (see read_summary) for every .onion archive under
    *paths* that matches ALL of *meta_filters* (key=value, AND semantics)
    AND *any_text* (if given). With no filters at all, yields every
    archive found (i.e. "list everything").
    """
    meta_filters = meta_filters or {}
    for onion_path in iter_onion_files(paths, recursive=recursive):
        summary = read_summary(onion_path)
        if summary is None:
            continue  # not a valid .onion file, skip silently
        if any(not matches_meta_filter(summary, k, v) for k, v in meta_filters.items()):
            continue
        if any_text and not matches_any_text(summary, any_text):
            continue
        yield summary


# Future work: actual file CONTENT search (not just names) would require
# decompressing the payload and is deliberately out of scope here -- the
# TOC block already covers the common "find archives containing a file
# named X" case without any decompression cost.
