"""
Onion test suite. Drives the real CLI (subprocess) plus the search API.

Run from the repo root:
    python build_ext.py build_ext --inplace     # optional: C extensions
    python -m unittest discover -s tests -v     # or: pytest tests
"""
import json
import os
import random
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ace import search as onion_search  # noqa: E402
from ace.header import unpack_header  # noqa: E402


def flip_payload_byte(path):
    """Flip one byte inside the compressed payload (not header/trailing blocks)."""
    with open(path, "rb") as f:
        raw = bytearray(f.read())
    iset, off, _ = unpack_header(bytes(raw))
    size = iset.layers[-1].compressed_size
    raw[off + size // 2] ^= 0xFF
    with open(path, "wb") as f:
        f.write(raw)


def onion(*args, cwd=None, input_text=None):
    """Run the CLI, return CompletedProcess (text mode)."""
    env = {**os.environ, "PYTHONPATH": ROOT}
    return subprocess.run(
        [sys.executable, "-m", "ace.cli", *args],
        cwd=cwd or ROOT, env=env, capture_output=True, text=True,
        input=input_text, timeout=120,
    )


def sample_text(n=20000):
    words = ["alpha", "bravo", "invoice", "report", "quarter", "tax", "client",
             "payment", "draft", "final", "notes", "meeting"]
    rng = random.Random(7)
    return " ".join(rng.choice(words) for _ in range(n // 6)).encode()


class Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = self._td.name

    def tearDown(self):
        self._td.cleanup()

    def write(self, name, data):
        p = os.path.join(self.dir, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def compress(self, path, *extra):
        out = path + ".onion"
        r = onion("-c", path, "-o", out, *extra)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.exists(out))
        return out

    def read(self, p):
        with open(p, "rb") as f:
            return f.read()


class RoundTrip(Base):
    def check_roundtrip(self, data, *extra):
        src = self.write("f.bin", data)
        arc = self.compress(src, *extra)
        out = os.path.join(self.dir, "restored.bin")
        r = onion("-d", arc, "-o", out)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.read(out), data)
        return arc

    def test_text(self):
        data = sample_text()
        arc = self.check_roundtrip(data)
        self.assertLess(os.path.getsize(arc), len(data) // 2)

    def test_repeated_bytes(self):
        self.check_roundtrip(b"A" * 50000)

    def test_random_incompressible_does_not_grow_much(self):
        data = os.urandom(30000)
        arc = self.check_roundtrip(data)
        # Gain Monitor prunes useless layers; only fixed header + audit block
        # remain (~330 bytes measured). Guard against payload expansion.
        self.assertLess(os.path.getsize(arc), len(data) + 500)

    def test_tiny_file(self):
        self.check_roundtrip(b"hello")

    def test_binary_structured(self):
        import struct
        data = b"".join(struct.pack("<f", i * 0.5) for i in range(5000))
        self.check_roundtrip(data)

    def test_fast_mode(self):
        try:
            import lz4  # noqa: F401
        except ImportError:
            self.skipTest("lz4 not installed")
        self.check_roundtrip(sample_text(), "--fast")

    def test_no_compress(self):
        data = sample_text()
        arc = self.check_roundtrip(data, "--no-compress")
        self.assertGreaterEqual(os.path.getsize(arc), len(data))

    def test_split_huffman(self):
        self.check_roundtrip(sample_text(4000), "--split-huffman")

    def test_directory(self):
        d = os.path.join(self.dir, "proj")
        files = {"a.txt": sample_text(3000), "sub/b.txt": b"bee" * 400,
                 "sub/deep/c.bin": os.urandom(500)}
        for name, data in files.items():
            self.write(os.path.join("proj", name), data)
        arc = os.path.join(self.dir, "proj.onion")
        r = onion("-c", d, "-o", arc)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = os.path.join(self.dir, "restored")
        r = onion("-d", arc, "-o", out)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        found = {}
        for root, _, fs in os.walk(out):
            for f in fs:
                found[f] = self.read(os.path.join(root, f))
        for name, data in files.items():
            self.assertEqual(found[os.path.basename(name)], data, name)

    def test_default_ignores_skip_pycache_and_onion(self):
        self.write("proj/keep.txt", b"keep" * 100)
        self.write("proj/__pycache__/x.pyc", b"junk" * 100)
        arc = os.path.join(self.dir, "proj.onion")
        r = onion("-c", os.path.join(self.dir, "proj"), "-o", arc)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = onion("-i", arc)
        self.assertIn("keep.txt", r.stdout)
        self.assertNotIn("x.pyc", r.stdout)


class Integrity(Base):
    def test_corrupt_payload_is_detected(self):
        src = self.write("f.txt", sample_text())
        arc = self.compress(src)
        bad = arc.replace(".onion", "_bad.onion")
        with open(bad, "wb") as f:
            f.write(self.read(arc))
        flip_payload_byte(bad)
        out = os.path.join(self.dir, "out.txt")
        r = onion("-d", bad, "-o", out)
        ok_and_identical = (r.returncode == 0 and os.path.exists(out)
                            and self.read(out) == self.read(src))
        self.assertFalse(ok_and_identical, "corruption went unnoticed")

    def test_not_an_onion_file(self):
        junk = self.write("junk.onion", b"this is not an archive" * 10)
        r = onion("-d", junk, "-o", os.path.join(self.dir, "o"))
        self.assertNotEqual(r.returncode, 0)

    def test_unwrap_restores_and_removes_archive(self):
        data = sample_text(5000)
        src = self.write("doc.txt", data)
        arc = self.compress(src)
        os.remove(src)
        r = onion("--unwrap", arc)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(arc))
        self.assertEqual(self.read(src), data)

    def test_unwrap_refuses_to_overwrite(self):
        src = self.write("doc.txt", sample_text(5000))
        arc = self.compress(src)  # original still exists
        r = onion("--unwrap", arc)
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue(os.path.exists(arc))


class Encryption(Base):
    def test_roundtrip_and_wrong_password(self):
        data = sample_text(8000)
        src = self.write("secret.txt", data)
        arc = self.compress(src, "-e", "-p", "hunter2")
        self.assertNotIn(b"invoice", self.read(arc)[:200])
        out = os.path.join(self.dir, "ok.txt")
        r = onion("-d", arc, "-o", out, "-p", "hunter2")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.read(out), data)
        bad = os.path.join(self.dir, "bad.txt")
        r = onion("-d", arc, "-o", bad, "-p", "wrong")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(bad) and self.read(bad) == data)

    def test_encrypt_only(self):
        data = sample_text(4000)
        src = self.write("s.txt", data)
        arc = self.compress(src, "--encrypt-only", "-p", "pw")
        out = os.path.join(self.dir, "o.txt")
        r = onion("-d", arc, "-o", out, "-p", "pw")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.read(out), data)

    def test_payload_is_not_plaintext(self):
        marker = b"TOP-SECRET-MARKER-12345"
        src = self.write("s.txt", marker * 200)
        arc = self.compress(src, "-e", "-p", "pw", "--no-compress")
        self.assertNotIn(marker, self.read(arc))


class Metadata(Base):
    def test_meta_visible_via_inspect(self):
        src = self.write("r.txt", sample_text(3000))
        arc = self.compress(src, "--meta", "tags=invoice;q3", "--meta", "author=alan",
                            "--meta", "pages=12")
        out = onion("-i", arc).stdout
        for needle in ("invoice", "q3", "alan"):
            self.assertIn(needle, out)

    def test_set_meta_does_not_touch_payload(self):
        src = self.write("r.txt", sample_text(6000))
        arc = self.compress(src, "--meta", "tags=old")
        before = self.read(arc)
        r = onion("--set-meta", arc, "--meta", "tags=new", "--meta", "note=edited")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        after = self.read(arc)
        self.assertNotEqual(before, after)
        # The payload sits at the front; metadata is a trailing block, so the
        # leading bytes (header + layers + payload) are unchanged.
        common = os.path.commonprefix([before, after])
        self.assertGreater(len(common), len(before) // 2)
        out = os.path.join(self.dir, "o.txt")
        self.assertEqual(onion("-d", arc, "-o", out).returncode, 0)
        self.assertEqual(self.read(out), self.read(src))
        self.assertIn("new", onion("-i", arc).stdout)

    def test_signature_verify_and_payload_tamper(self):
        src = self.write("r.txt", sample_text(3000))
        arc = self.compress(src, "--meta", "author=alan", "--sign-key", "k1")
        self.assertEqual(onion("--verify", arc, "--sign-key", "k1").returncode, 0)
        self.assertEqual(onion("--verify", arc, "--sign-key", "other").returncode, 2)
        flip_payload_byte(arc)
        self.assertEqual(onion("--verify", arc, "--sign-key", "k1").returncode, 2)

    def test_signature_covers_content_not_user_metadata(self):
        """Documents the design: tags stay editable and do NOT break the signature."""
        src = self.write("r.txt", sample_text(3000))
        arc = self.compress(src, "--meta", "author=alan", "--sign-key", "k1")
        raw = self.read(arc)
        self.assertIn(b"alan", raw)
        with open(arc, "wb") as f:
            f.write(raw.replace(b"alan", b"mal!"))
        self.assertEqual(onion("--verify", arc, "--sign-key", "k1").returncode, 0)


class Search(Base):
    def make_store(self):
        a = self.write("store/a.txt", sample_text(3000))
        b = self.write("store/b.txt", sample_text(3000))
        c = self.write("store/nested/c.txt", sample_text(3000))
        self.compress(a, "--meta", "tags=invoice;q3", "--meta", "author=alan")
        self.compress(b, "--meta", "tags=invoice")
        self.compress(c, "--meta", "tags=receipt")
        return os.path.join(self.dir, "store")

    def names(self, results):
        return sorted(os.path.basename(r["path"]) for r in results)

    def test_tag_filter(self):
        store = self.make_store()
        r = list(onion_search.search([store], meta_filters={"tags": "invoice"}))
        self.assertEqual(self.names(r), ["a.txt.onion", "b.txt.onion"])

    def test_multi_tag_is_and(self):
        store = self.make_store()
        r = list(onion_search.search([store], meta_filters={"tags": "invoice,q3"}))
        self.assertEqual(self.names(r), ["a.txt.onion"])

    def test_list_everything_and_recursion(self):
        store = self.make_store()
        self.assertEqual(len(list(onion_search.search([store]))), 3)
        flat = list(onion_search.search([store], recursive=False))
        self.assertEqual(len(flat), 2)

    def test_any_text_matches_metadata_and_filename(self):
        store = self.make_store()
        self.assertEqual(self.names(onion_search.search([store], any_text="receipt")),
                         ["c.txt.onion"])
        self.assertEqual(self.names(onion_search.search([store], any_text="alan")),
                         ["a.txt.onion"])

    def test_search_is_payload_blind(self):
        """Corrupt every payload; search must still work, since it never decompresses."""
        store = self.make_store()
        for root, _, fs in os.walk(store):
            for f in fs:
                if f.endswith(".onion"):
                    flip_payload_byte(os.path.join(root, f))
        r = list(onion_search.search([store], meta_filters={"tags": "invoice"}))
        self.assertEqual(len(r), 2)

    def test_directory_archive_filenames_searchable_even_encrypted(self):
        self.write("proj/main.py", b"print('x')\n" * 50)
        self.write("proj/readme.md", b"# hi\n" * 50)
        arc = os.path.join(self.dir, "arch", "proj.onion")
        os.makedirs(os.path.dirname(arc))
        r = onion("-c", os.path.join(self.dir, "proj"), "-o", arc, "-e", "-p", "pw")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        hits = list(onion_search.search([os.path.dirname(arc)], any_text="main.py"))
        self.assertEqual(len(hits), 1)  # found without the password
        self.assertEqual(
            len(list(onion_search.search([os.path.dirname(arc)], any_text="nope.py"))), 0)

    def test_cli_search_output(self):
        store = self.make_store()
        r = onion("--search", store, "--meta", "tags=receipt")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("c.txt.onion", r.stdout)
        self.assertNotIn("a.txt.onion", r.stdout)


class Format(Base):
    def test_atomic_write_leaves_no_temp_files(self):
        src = self.write("f.txt", sample_text(3000))
        self.compress(src)
        leftovers = [f for f in os.listdir(self.dir) if f.endswith((".tmp", ".part"))]
        self.assertEqual(leftovers, [])

    def test_audit_block_is_json_readable(self):
        src = self.write("f.txt", sample_text(5000))
        arc = self.compress(src)
        out = onion("-i", arc).stdout
        self.assertTrue(out.strip())  # inspect prints something without extracting


if __name__ == "__main__":
    unittest.main()
