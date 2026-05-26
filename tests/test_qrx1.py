"""
Tests for the QRX1 protocol module + the encode/decode tools.

Run with:
    pytest -q

The QR-rendering roundtrip tests are skipped automatically if the
`qrcode` / `pyzbar` / `PIL` libraries (or system zbar) are not installed.
"""

from __future__ import annotations

import base64
import random
from pathlib import Path

import pytest

from qrx1 import Header, Receiver, build_frames, crc32hex, parse_frame


# ---------------------------------------------------------------------------
# Golden fixture — the same bytes are asserted against by the Node tests too.
# If these values change, the JS side must change in lockstep.
# ---------------------------------------------------------------------------

GOLDEN_BYTES = b"Hello, ShadowCat!\n" * 50  # 900 bytes
GOLDEN_CRC = "bfe4986e"
GOLDEN_FILENAME = "hello.txt"
GOLDEN_CHUNK = 100
GOLDEN_TOTAL = 12  # ceil(base64_len(900) / 100) == ceil(1200 / 100)
GOLDEN_HEADER = f"QRX1|H|{GOLDEN_TOTAL}|{GOLDEN_FILENAME}|{len(GOLDEN_BYTES)}|{GOLDEN_CRC}"


# ---------------------------------------------------------------------------
# crc32hex
# ---------------------------------------------------------------------------

class TestCrc32Hex:
    def test_empty(self):
        assert crc32hex(b"") == "00000000"

    def test_abc(self):
        # zlib.crc32(b"abc") == 0x352441C2
        assert crc32hex(b"abc") == "352441c2"

    def test_known_blob(self):
        assert crc32hex(GOLDEN_BYTES) == GOLDEN_CRC

    def test_always_8_chars_lowercase(self):
        for blob in [b"", b"\x00", b"\xff" * 1000, b"x"]:
            h = crc32hex(blob)
            assert len(h) == 8
            assert h == h.lower()


# ---------------------------------------------------------------------------
# build_frames
# ---------------------------------------------------------------------------

class TestBuildFrames:
    def test_header_matches_golden(self):
        frames = build_frames(GOLDEN_BYTES, GOLDEN_FILENAME, GOLDEN_CHUNK)
        assert frames[0] == GOLDEN_HEADER

    def test_total_count(self):
        frames = build_frames(GOLDEN_BYTES, GOLDEN_FILENAME, GOLDEN_CHUNK)
        assert len(frames) == GOLDEN_TOTAL + 1  # header + data

    def test_data_frames_are_indexed_from_1(self):
        frames = build_frames(GOLDEN_BYTES, GOLDEN_FILENAME, GOLDEN_CHUNK)
        for i, f in enumerate(frames[1:], start=1):
            assert f.startswith(f"QRX1|D|{i}|")

    def test_data_payloads_concat_to_full_base64(self):
        frames = build_frames(GOLDEN_BYTES, GOLDEN_FILENAME, GOLDEN_CHUNK)
        payloads = [f.split("|", 3)[3] for f in frames[1:]]
        assert "".join(payloads) == base64.b64encode(GOLDEN_BYTES).decode()

    def test_chunk_at_boundary(self):
        # base64 of 75 bytes is exactly 100 chars (no padding pad-pad)
        # 75 raw bytes -> 100 base64 chars, so chunk=100 yields exactly 1 chunk.
        raw = b"x" * 75
        frames = build_frames(raw, "x.bin", 100)
        assert len(frames) == 2  # header + 1
        assert frames[0].split("|")[2] == "1"

    def test_chunk_just_over_boundary(self):
        # 76 raw bytes -> 104 base64 chars (with padding), needs 2 chunks at chunk=100
        raw = b"x" * 76
        frames = build_frames(raw, "x.bin", 100)
        assert len(frames) == 3
        assert frames[0].split("|")[2] == "2"

    def test_empty_file_still_has_one_data_chunk(self):
        frames = build_frames(b"", "empty.bin", 100)
        assert len(frames) == 2
        assert frames[0] == "QRX1|H|1|empty.bin|0|00000000"
        assert frames[1] == "QRX1|D|1|"

    def test_rejects_filename_with_pipe(self):
        with pytest.raises(ValueError, match="separator"):
            build_frames(b"x", "weird|name.bin", 100)

    @pytest.mark.parametrize("bad", [49, 0, -1, 2001, 100_000])
    def test_rejects_out_of_range_chunk(self, bad):
        with pytest.raises(ValueError, match=r"\[50, 2000\]"):
            build_frames(b"x", "x.bin", bad)


# ---------------------------------------------------------------------------
# parse_frame
# ---------------------------------------------------------------------------

class TestParseFrame:
    def test_valid_header(self):
        kind, h = parse_frame(GOLDEN_HEADER)
        assert kind == "H"
        assert h == Header(GOLDEN_TOTAL, GOLDEN_FILENAME, len(GOLDEN_BYTES), GOLDEN_CRC)

    def test_valid_data(self):
        kind, idx, data = parse_frame("QRX1|D|7|SGVsbG8=")
        assert kind == "D"
        assert idx == 7
        assert data == "SGVsbG8="

    def test_lowercases_crc(self):
        _, h = parse_frame("QRX1|H|1|x|0|ABCDEF12")
        assert h.crc == "abcdef12"

    def test_data_chunk_can_be_empty(self):
        kind, idx, data = parse_frame("QRX1|D|3|")
        assert (kind, idx, data) == ("D", 3, "")

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "garbage",
            "QRX2|H|1|x|0|00000000",  # wrong version tag
            "qrx1|H|1|x|0|00000000",  # wrong case
            "QRX1|X|1|x|0|00000000",  # unknown type
            "QRX1|H|1|x|0",            # truncated header
            "QRX1|D|1",                # truncated data
            "QRX1|H|notanumber|x|0|00000000",
            "QRX1|D|notanumber|payload",
        ],
    )
    def test_rejects_invalid(self, text):
        assert parse_frame(text) is None


# ---------------------------------------------------------------------------
# Receiver
# ---------------------------------------------------------------------------

class TestReceiver:
    def _frames(self):
        return build_frames(GOLDEN_BYTES, GOLDEN_FILENAME, GOLDEN_CHUNK)

    def test_in_order_roundtrip(self):
        r = Receiver()
        frames = self._frames()
        for i, f in enumerate(frames):
            done = r.ingest(f)
            assert done == (i == len(frames) - 1)
        assert r.assemble() == GOLDEN_BYTES

    def test_out_of_order_roundtrip_after_header(self):
        r = Receiver()
        frames = self._frames()
        header, data = frames[0], frames[1:]
        random.Random(42).shuffle(data)
        r.ingest(header)
        completed_at = None
        for k, f in enumerate(data):
            if r.ingest(f) and completed_at is None:
                completed_at = k
        assert completed_at is not None
        assert r.assemble() == GOLDEN_BYTES

    def test_receiver_joins_mid_loop(self):
        """Sender loops [H, D1..DN] forever. Receiver tunes in mid-loop and
        must wait until the header comes back around to start collecting."""
        r = Receiver()
        frames = self._frames()
        # tune in at the 4th frame of the first loop, then the loop repeats
        stream = frames[4:] + frames + frames
        completed = False
        for f in stream:
            if r.ingest(f):
                completed = True
                break
        assert completed
        assert r.assemble() == GOLDEN_BYTES

    def test_duplicates_are_idempotent(self):
        r = Receiver()
        frames = self._frames()
        r.ingest(frames[0])
        r.ingest(frames[1])
        snap = list(r.chunks)
        count = r.count
        r.ingest(frames[1])  # duplicate
        r.ingest(frames[1])  # duplicate
        assert r.chunks == snap
        assert r.count == count

    def test_data_before_header_is_dropped(self):
        r = Receiver()
        frames = self._frames()
        assert r.ingest(frames[1]) is False  # no header yet
        assert r.header is None
        assert r.count == 0

    def test_out_of_range_idx_is_dropped(self):
        r = Receiver()
        frames = self._frames()
        r.ingest(frames[0])
        bad = f"QRX1|D|{GOLDEN_TOTAL + 5}|junk"
        r.ingest(bad)
        assert r.count == 0
        bad2 = "QRX1|D|0|junk"
        r.ingest(bad2)
        assert r.count == 0

    def test_garbage_frames_dont_reset_state(self):
        r = Receiver()
        frames = self._frames()
        r.ingest(frames[0])
        r.ingest(frames[1])
        r.ingest("not a qrx1 frame at all")
        r.ingest("QRX2|H|1|x|0|00000000")
        assert r.count == 1
        assert r.header is not None

    def test_header_reset_on_different_crc(self):
        r = Receiver()
        r.ingest(GOLDEN_HEADER)
        r.ingest("QRX1|D|1|AAAA")
        # different CRC and different file: receiver should reset
        new_header = "QRX1|H|3|other.bin|10|deadbeef"
        r.ingest(new_header)
        assert r.header is not None
        assert r.header.crc == "deadbeef"
        assert r.header.total == 3
        assert r.count == 0
        assert all(c is None for c in r.chunks)

    def test_same_header_is_noop(self):
        r = Receiver()
        r.ingest(GOLDEN_HEADER)
        r.ingest("QRX1|D|1|AAAA")
        before = (r.header, list(r.chunks), r.count)
        r.ingest(GOLDEN_HEADER)  # same header again
        assert (r.header, r.chunks, r.count) == before

    def test_assemble_before_complete_raises(self):
        r = Receiver()
        r.ingest(GOLDEN_HEADER)
        with pytest.raises(ValueError, match="not complete"):
            r.assemble()

    def test_missing_lists_indices(self):
        r = Receiver()
        frames = self._frames()
        r.ingest(frames[0])
        r.ingest(frames[3])
        r.ingest(frames[7])
        miss = r.missing()
        assert 3 not in miss  # frames[3] is data idx 3
        assert 7 not in miss
        assert sorted(miss + [3, 7]) == list(range(1, GOLDEN_TOTAL + 1))


# ---------------------------------------------------------------------------
# End-to-end roundtrip through PNG QR images (optional; needs qrcode + pyzbar)
# ---------------------------------------------------------------------------

def _have_qr_stack() -> bool:
    try:
        import qrcode  # noqa: F401
        from PIL import Image  # noqa: F401
        from pyzbar.pyzbar import decode  # noqa: F401
    except Exception:
        return False
    return True


@pytest.mark.skipif(
    not _have_qr_stack(),
    reason="needs qrcode + pyzbar + PIL (and system zbar) installed",
)
def test_full_qr_image_roundtrip(tmp_path: Path):
    """Render every frame to a PNG, scan each PNG, reassemble, compare."""
    import qrcode
    from PIL import Image
    from pyzbar.pyzbar import decode as zbar_decode

    raw = (b"The quick brown fox jumps over the lazy dog.\n" * 30)
    frames = build_frames(raw, "fox.txt", 200)

    for i, text in enumerate(frames):
        qr = qrcode.QRCode(
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=6,
            border=4,
        )
        qr.add_data(text)
        qr.make(fit=True)
        qr.make_image(fill_color="black", back_color="white").save(
            tmp_path / f"f_{i:04d}.png"
        )

    r = Receiver()
    for path in sorted(tmp_path.glob("f_*.png")):
        with Image.open(path) as img:
            for sym in zbar_decode(img):
                r.ingest(sym.data.decode("utf-8"))

    assert r.assemble() == raw
