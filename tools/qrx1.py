"""
QRX1 protocol primitives — pure Python, no QR/image dependencies.

This module contains everything you need to build, parse, and reassemble
QRX1 frames as defined in SPEC.md. The encoder and decoder scripts add
QR rendering and image/video input on top of these primitives.
"""

from __future__ import annotations

import base64
import gzip
import zlib
from typing import Optional


# Flags that this implementation knows how to interpret. A receiver MUST
# refuse to decode a payload that carries an unknown flag — silently
# ignoring it would risk handing the user corrupt bytes.
KNOWN_FLAGS = frozenset({"gz"})


def crc32hex(data: bytes) -> str:
    """Lowercase 8-char hex CRC32 of `data` (zlib / IEEE 802.3)."""
    return f"{zlib.crc32(data) & 0xFFFFFFFF:08x}"


def _parse_flags(flags: str) -> list[str]:
    """Split a flags field into a list of non-empty tokens."""
    return [t for t in flags.split(",") if t]


def build_frames(
    raw: bytes,
    filename: str,
    chunk_len: int = 500,
    *,
    compress: bool = False,
) -> list[str]:
    """Return [header, data_1, data_2, ...] for `raw` bytes.

    If `compress=True`, gzip the payload first; if the gzipped result is not
    smaller than the original, fall back to the raw bytes with empty flags
    (so the receiver never gets a transfer that's bigger than it had to be).
    The CRC is always computed over the *original* (decompressed) bytes, and
    the filename is left untouched — no '.gz' suffix.
    """
    if "|" in filename:
        raise ValueError(
            f"filename contains '|' which is the QRX1 field separator: {filename!r}"
        )
    if not (50 <= chunk_len <= 2000):
        raise ValueError(f"chunk size must be in [50, 2000], got {chunk_len}")

    crc = crc32hex(raw)

    payload = raw
    flags = ""
    if compress:
        gz = gzip.compress(raw, compresslevel=9, mtime=0)
        if len(gz) < len(raw):
            payload = gz
            flags = "gz"

    b64 = base64.b64encode(payload).decode("ascii")
    total = max(1, (len(b64) + chunk_len - 1) // chunk_len)

    header = f"QRX1|H|{total}|{flags}|{filename}|{len(payload)}|{crc}"
    frames = [header]
    for i in range(total):
        frames.append(f"QRX1|D|{i + 1}|{b64[i * chunk_len : (i + 1) * chunk_len]}")
    return frames


class Header:
    __slots__ = ("total", "flags", "name", "size", "crc")

    def __init__(self, total: int, flags: str, name: str, size: int, crc: str):
        self.total = total
        self.flags = flags
        self.name = name
        self.size = size
        self.crc = crc

    def __repr__(self) -> str:
        return (
            f"Header(total={self.total!r}, flags={self.flags!r}, "
            f"name={self.name!r}, size={self.size!r}, crc={self.crc!r})"
        )

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Header)
            and self.total == other.total
            and self.flags == other.flags
            and self.name == other.name
            and self.size == other.size
            and self.crc == other.crc
        )

    def has_flag(self, name: str) -> bool:
        return name in _parse_flags(self.flags)


def parse_frame(text: str):
    """Return ('H', Header) | ('D', idx, data) | None."""
    if not text.startswith("QRX1|"):
        return None
    parts = text.split("|")
    try:
        if parts[1] == "H" and len(parts) >= 7:
            return (
                "H",
                Header(
                    int(parts[2]),
                    parts[3],
                    parts[4],
                    int(parts[5]),
                    parts[6].lower(),
                ),
            )
        if parts[1] == "D" and len(parts) >= 4:
            return ("D", int(parts[2]), parts[3])
    except (ValueError, IndexError):
        return None
    return None


class Receiver:
    """Stateful frame ingester. Mirrors the reference HTML's behaviour."""

    def __init__(self, verbose: bool = False):
        self.header: Optional[Header] = None
        self.chunks: list[Optional[str]] = []
        self.count = 0
        self.verbose = verbose

    def ingest(self, text: str) -> bool:
        """Feed one QR's text. Returns True iff the file is fully received."""
        parsed = parse_frame(text)
        if parsed is None:
            return self._complete()

        if parsed[0] == "H":
            h: Header = parsed[1]
            same = (
                self.header is not None
                and self.header.crc == h.crc
                and len(self.chunks) == h.total
            )
            if not same:
                self.header = h
                self.chunks = [None] * h.total
                self.count = 0
                if self.verbose:
                    flag_str = f" flags={h.flags!r}" if h.flags else ""
                    print(
                        f"[header] name={h.name!r} size={h.size}{flag_str} "
                        f"chunks={h.total} crc={h.crc}"
                    )
            return self._complete()

        _, idx, data = parsed
        if self.header is None:
            return False
        if idx < 1 or idx > len(self.chunks):
            return False
        if self.chunks[idx - 1] is not None:
            return self._complete()
        self.chunks[idx - 1] = data
        self.count += 1
        if self.verbose:
            print(
                f"[data]   chunk {idx}/{self.header.total} "
                f"({self.count} stored)"
            )
        return self._complete()

    def _complete(self) -> bool:
        return self.header is not None and self.count == len(self.chunks)

    def missing(self) -> list[int]:
        return [i + 1 for i, c in enumerate(self.chunks) if c is None]

    def assemble(self) -> bytes:
        if self.header is None or not self._complete():
            raise ValueError("receiver is not complete")

        flags = _parse_flags(self.header.flags)
        unknown = [f for f in flags if f not in KNOWN_FLAGS]
        if unknown:
            raise ValueError(
                f"unknown header flags: {','.join(unknown)} "
                f"(this build knows only: {','.join(sorted(KNOWN_FLAGS))})"
            )

        b64 = "".join(self.chunks)  # type: ignore[arg-type]
        payload = base64.b64decode(b64)
        if len(payload) != self.header.size:
            raise ValueError(
                f"size mismatch: got {len(payload)} bytes, "
                f"header says {self.header.size}"
            )

        if "gz" in flags:
            try:
                raw = gzip.decompress(payload)
            except (OSError, EOFError, zlib.error) as e:
                raise ValueError(f"gzip decompression failed: {e}") from e
        else:
            raw = payload

        got = crc32hex(raw)
        if got != self.header.crc:
            raise ValueError(
                f"CRC mismatch: got {got}, header says {self.header.crc}"
            )
        return raw
