"""
toc.py — Table of Contents block
───────────────────────────────────
Uncompressed, self-describing directory listing appended after the AUDIT
block (if any) and before the META block. Lets a directory archive's file
list be read instantly on archives of any size, without decompressing the
payload — the same design principle as the META block, applied to
directory contents instead of user metadata.

Binary layout:
  [4 bytes]  Magic      b'TOC0'
  [4 bytes]  Length     uint32 BE  (byte length of UTF-8 JSON that follows)
  [N bytes]  UTF-8 JSON: list of {"path": str, "size": int} objects

Only ever written for multi-file / directory archives (compress_files()).
Plain single-stream archives (compress()) have no TOC block — there is
only one file and its name isn't part of the archive format at all in
that mode.

Note: recipe["files"] in the AUDIT block (see transformer.py) already
happens to list paths today, but that's incidental to the audit block
(compression-algorithm bookkeeping) — it's absent when --no-audit is
used, and carries no size information. TOC is independent of --no-audit
and always accompanies a directory archive.
"""

import json
import struct
from typing import Any, Dict, List, Optional, Tuple

MAGIC      = b'TOC0'
MAGIC_SIZE = 4


def pack(files: List[Tuple[str, bytes]]) -> bytes:
    """
    Serialise a list of (relative_path, data) pairs to a TOC block,
    storing only each path and its original size — never the file
    content itself.
    """
    entries = [{"path": p, "size": len(d)} for p, d in files]
    json_bytes = json.dumps(entries, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return MAGIC + struct.pack(">I", len(json_bytes)) + json_bytes


def unpack(data: bytes, offset: int = 0) -> Optional[List[Dict[str, Any]]]:
    """
    Try to read a TOC block starting at *offset* in *data*.
    Returns the list of {"path", "size"} dicts, or None if no valid TOC
    block is found there.
    """
    if offset + MAGIC_SIZE + 4 > len(data):
        return None
    if data[offset:offset + MAGIC_SIZE] != MAGIC:
        return None
    length = struct.unpack_from(">I", data, offset + MAGIC_SIZE)[0]
    start  = offset + MAGIC_SIZE + 4
    if start + length > len(data):
        return None
    try:
        return json.loads(data[start:start + length].decode('utf-8'))
    except Exception:
        return None


def block_size(data: bytes, offset: int) -> int:
    """Return total byte size of the TOC block at *offset*, or 0 if none."""
    if offset + MAGIC_SIZE + 4 > len(data):
        return 0
    if data[offset:offset + MAGIC_SIZE] != MAGIC:
        return 0
    length = struct.unpack_from(">I", data, offset + MAGIC_SIZE)[0]
    return MAGIC_SIZE + 4 + length


def is_toc(data: bytes, offset: int) -> bool:
    return (offset + MAGIC_SIZE <= len(data) and
            data[offset:offset + MAGIC_SIZE] == MAGIC)
