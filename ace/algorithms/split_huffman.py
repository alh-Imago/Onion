"""
split_huffman.py — LZ77 + split-stream Huffman coding (experimental, opt-in only)
─────────────────────────────────────────────────────────────────────────────
A standalone alternative to the plain "LZ77 → Huffman" pipeline (two
independent layers sharing one combined byte-stream frequency table).
This algorithm keeps its own LZ77 tokenizer and Huffman-codes the
literal/match-length stream with its OWN tree, separate from the plain
Huffman module -- the same architectural pattern LZMA already uses as a
standalone alternative rather than a modification of LZ77 or Huffman.

STATUS: pure Python only (no C extension) -- meaningfully slower than the
existing C-accelerated LZ77+Huffman path. NEVER selected automatically by
the Strategist; only used when explicitly requested (--split-huffman /
the web UI checkbox), by design, given the cost/benefit tradeoff below.

Measured against the current C-accelerated LZ77+Huffman pipeline across six
representative data types (see points.md / README for the full table this
came from) -- results are genuinely MIXED, not a universal win:
  - Random/incompressible data:  ~5% smaller
  - JSON-like structured data:   ~1% smaller
  - Highly repetitive patterns:  ~56% smaller (adaptive distance mode helps most here)
  - Real source code:            ~4% LARGER
  - Small files (<1KB):          ~30-40% LARGER (fixed header overhead dominates)
  - General structured log text: ~6% LARGER
There is no single data characteristic that reliably predicts a win ahead
of time without just trying it -- hence: opt-in, compare-and-decide,
never a silent default.

── Design ─────────────────────────────────────────────────────────────────
1. Hash-chain LZ77 tokenizer (tokenize()): indexes 3-byte prefixes with a
   chain of prior positions, checking only real candidates instead of a
   brute-force full-window scan. Bounded by MAX_CHAIN per position -- this
   is what fixes a genuine hang (not just slowness) that a naive
   brute-force pure-Python matcher hit on highly repetitive input
   (confirmed directly: a naive matcher failed to complete in 60s on 20KB
   of a repeated byte; this tokenizer does the same work in 0.003s).

2. Literal/length stream: one combined alphabet (symbols 0-255 = literal
   bytes, 256-511 = match-length codes) with ONE canonical Huffman tree --
   this is the part of split-stream Huffman that reliably helps, since
   literals and match lengths are both drawn from genuinely skewed
   distributions worth exploiting.

3. Distance stream: NOT naively Huffman-coded directly. An earlier attempt
   at this Huffman-coded raw distance values one-for-one and made files
   dramatically LARGER (confirmed: -61% on a 154KB test) because general
   text has a wide, near-flat spread of match distances -- Huffman-coding
   thousands of near-unique values costs more in header overhead than it
   saves. Real deflate avoids this with bucketed distance codes + raw
   extra bits; this implementation takes a simpler adaptive approach
   instead: try both a fixed-width raw encoding and a Huffman-coded
   encoding of the actual distance values, keep whichever is smaller, and
   record a 1-byte mode flag. Cheap to compute both since the distance
   list is already in hand from tokenization.
"""

import struct
import heapq
from collections import Counter
from typing import Dict, List, Tuple, Optional

WINDOW_SIZE   = 32768
MAX_MATCH_LEN = 258
MIN_MATCH_LEN = 3
MAX_CHAIN     = 64   # candidates checked per position -- bounds worst-case runtime

DIST_MODE_RAW     = 0
DIST_MODE_HUFFMAN = 1


# ── Hash-chain LZ77 tokenizer ─────────────────────────────────────────────────

def tokenize(data: bytes) -> List[Tuple]:
    """Returns a list of ('L', byte) or ('M', length, offset) tuples."""
    n = len(data)
    tokens = []
    head: Dict[bytes, int] = {}
    prev: Dict[int, int] = {}

    pos = 0
    while pos < n:
        best_len, best_off = 0, 0
        if pos + MIN_MATCH_LEN <= n:
            key = data[pos:pos + 3]
            candidate = head.get(key)
            chain_checked = 0
            window_start = max(0, pos - WINDOW_SIZE)
            while candidate is not None and candidate >= window_start and chain_checked < MAX_CHAIN:
                length = 0
                max_possible = min(MAX_MATCH_LEN, n - pos)
                while length < max_possible and data[candidate + length] == data[pos + length]:
                    length += 1
                if length > best_len:
                    best_len, best_off = length, pos - candidate
                    if best_len >= MAX_MATCH_LEN:
                        break
                candidate = prev.get(candidate)
                chain_checked += 1

        if best_len >= MIN_MATCH_LEN:
            key = data[pos:pos + 3]
            prev[pos] = head.get(key)
            head[key] = pos
            tokens.append(('M', best_len, best_off))
            pos += best_len
        else:
            if pos + 3 <= n:
                key = data[pos:pos + 3]
                prev[pos] = head.get(key)
                head[key] = pos
            tokens.append(('L', data[pos]))
            pos += 1
    return tokens


def detokenize(tokens: List[Tuple]) -> bytes:
    out = bytearray()
    for tok in tokens:
        if tok[0] == 'L':
            out.append(tok[1])
        else:
            _, length, offset = tok
            start = len(out) - offset
            for k in range(length):
                out.append(out[start + k])
    return bytes(out)


# ── Generic canonical Huffman over an arbitrary int alphabet ─────────────────
# (Distinct from ace/algorithms/huffman.py, which is hardcoded to byte-range
# symbols. This one handles the literal/length alphabet (0-511) and, when
# selected, the distance alphabet (0-32767) too.)

class _Node:
    __slots__ = ("freq", "symbol", "left", "right")
    def __init__(self, freq, symbol=None, left=None, right=None):
        self.freq, self.symbol, self.left, self.right = freq, symbol, left, right
    def __lt__(self, other): return self.freq < other.freq


def _build_lengths(symbols: List[int]) -> Dict[int, int]:
    counts = Counter(symbols)
    if len(counts) == 1:
        return {next(iter(counts)): 1}
    heap = [_Node(f, s) for s, f in counts.items()]
    heapq.heapify(heap)
    while len(heap) > 1:
        a, b = heapq.heappop(heap), heapq.heappop(heap)
        heapq.heappush(heap, _Node(a.freq + b.freq, left=a, right=b))
    lengths: Dict[int, int] = {}
    def walk(node, d):
        if node.symbol is not None:
            lengths[node.symbol] = max(d, 1); return
        walk(node.left, d + 1); walk(node.right, d + 1)
    walk(heap[0], 0)
    return lengths


def _canonical_codes(lengths: Dict[int, int]) -> Dict[int, Tuple[int, int]]:
    by_length = sorted(lengths.items(), key=lambda kv: (kv[1], kv[0]))
    codes: Dict[int, Tuple[int, int]] = {}
    code, prev_len = 0, (by_length[0][1] if by_length else 0)
    for symbol, length in by_length:
        code <<= (length - prev_len)
        codes[symbol] = (code, length)
        code += 1
        prev_len = length
    return codes


def _huffman_encode(symbols: List[int]) -> Tuple[bytes, bytes]:
    """Returns (header, payload). Symbols must fit in 16 bits (0-65535)."""
    if not symbols:
        return b"", b""
    lengths = _build_lengths(symbols)
    codes = _canonical_codes(lengths)
    bits, nbits, stream = 0, 0, bytearray()
    for s in symbols:
        code, L = codes[s]
        bits = (bits << L) | code; nbits += L
        while nbits >= 8:
            nbits -= 8
            stream.append((bits >> nbits) & 0xFF)
    if nbits:
        stream.append((bits << (8 - nbits)) & 0xFF)
    header = struct.pack(">H", len(lengths))
    for symbol in sorted(lengths):
        header += struct.pack(">HB", symbol, lengths[symbol])
    return header, bytes(stream)


def _huffman_decode(header: bytes, payload: bytes, num_symbols: int) -> List[int]:
    if num_symbols == 0:
        return []
    n = struct.unpack_from(">H", header, 0)[0]
    offset = 2
    lengths: Dict[int, int] = {}
    for _ in range(n):
        symbol, length = struct.unpack_from(">HB", header, offset)
        offset += 3
        lengths[symbol] = length
    codes = _canonical_codes(lengths)
    decode_map = {(L, c): s for s, (c, L) in codes.items()}
    max_len = max(lengths.values()) if lengths else 0
    out: List[int] = []
    bits, nbits, i = 0, 0, 0
    while len(out) < num_symbols:
        while nbits < max_len and i < len(payload):
            bits = (bits << 8) | payload[i]; i += 1; nbits += 8
        matched = False
        for L in range(1, min(nbits, max_len) + 1):
            candidate = (bits >> (nbits - L)) & ((1 << L) - 1)
            if (L, candidate) in decode_map:
                out.append(decode_map[(L, candidate)])
                nbits -= L
                matched = True
                break
        if not matched:
            raise ValueError("split_huffman: corrupt bitstream (no matching code)")
    return out


# ── Public: compress / decompress ────────────────────────────────────────────

def compress(data: bytes) -> bytes:
    if not data:
        return b""
    tokens = tokenize(data)
    lit_len_symbols: List[int] = []
    dist_symbols: List[int] = []
    for tok in tokens:
        if tok[0] == 'L':
            lit_len_symbols.append(tok[1])
        else:
            _, length, offset = tok
            lit_len_symbols.append(256 + (length - MIN_MATCH_LEN))
            dist_symbols.append(offset - 1)

    ll_header, ll_payload = _huffman_encode(lit_len_symbols)

    # Adaptive distance encoding: try both, keep whichever is smaller.
    raw_dist = b"".join(struct.pack(">H", d) for d in dist_symbols)
    if dist_symbols:
        huff_dist_header, huff_dist_payload = _huffman_encode(dist_symbols)
        huff_dist_total = len(huff_dist_header) + len(huff_dist_payload)
    else:
        huff_dist_header, huff_dist_payload, huff_dist_total = b"", b"", 0

    if dist_symbols and huff_dist_total < len(raw_dist):
        dist_mode = DIST_MODE_HUFFMAN
        dist_blob = (struct.pack(">II", len(huff_dist_header), len(huff_dist_payload))
                     + huff_dist_header + huff_dist_payload)
    else:
        dist_mode = DIST_MODE_RAW
        dist_blob = raw_dist

    out = struct.pack(">IIIB", len(lit_len_symbols), len(ll_header), len(ll_payload), dist_mode)
    out += ll_header + ll_payload
    out += struct.pack(">I", len(dist_symbols)) + dist_blob
    return out


def decompress(data: bytes) -> bytes:
    if not data:
        return b""
    n_ll, ll_h_len, ll_p_len, dist_mode = struct.unpack_from(">IIIB", data, 0)
    off = 13
    ll_header = data[off:off + ll_h_len]; off += ll_h_len
    ll_payload = data[off:off + ll_p_len]; off += ll_p_len
    n_dist = struct.unpack_from(">I", data, off)[0]; off += 4

    if dist_mode == DIST_MODE_HUFFMAN:
        d_h_len, d_p_len = struct.unpack_from(">II", data, off); off += 8
        d_header = data[off:off + d_h_len]; off += d_h_len
        d_payload = data[off:off + d_p_len]; off += d_p_len
        dist_symbols = _huffman_decode(d_header, d_payload, n_dist)
    else:
        dist_symbols = [struct.unpack_from(">H", data, off + i * 2)[0] for i in range(n_dist)]

    lit_len_symbols = _huffman_decode(ll_header, ll_payload, n_ll)
    out = bytearray()
    dist_idx = 0
    for sym in lit_len_symbols:
        if sym < 256:
            out.append(sym)
        else:
            length = (sym - 256) + MIN_MATCH_LEN
            offset = dist_symbols[dist_idx] + 1
            dist_idx += 1
            start = len(out) - offset
            if start < 0:
                raise ValueError(f"split_huffman: invalid back-reference (offset={offset})")
            for k in range(length):
                out.append(out[start + k])
    return bytes(out)


def backend_info() -> str:
    return "pure Python (no C extension -- experimental, opt-in only)"
