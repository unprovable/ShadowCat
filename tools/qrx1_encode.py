#!/usr/bin/env python3
"""
qrx1_encode.py — generate ShadowCat / QRX1 QR frames from a file.

Produces a directory of PNG frames named:

    frame_0000.png   <- header  (QRX1|H|...)
    frame_0001.png   <- data chunk 1
    frame_0002.png   <- data chunk 2
    ...

These are byte-for-byte compatible with what shadowcat.html cycles through.
Feed them to any QR reader (or to qrx1_decode.py) to reassemble the file.

Dependencies:
    pip install qrcode[pil]

Usage:
    python qrx1_encode.py path/to/file
    python qrx1_encode.py path/to/file --chunk 500 --ecc M --out frames/
    python qrx1_encode.py path/to/file --gif animated.gif --fps 3
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qrx1 import build_frames  # noqa: E402

try:
    import qrcode
    from qrcode.constants import (
        ERROR_CORRECT_L,
        ERROR_CORRECT_M,
        ERROR_CORRECT_Q,
        ERROR_CORRECT_H,
    )
except ImportError:
    sys.stderr.write(
        "error: the 'qrcode' library is required.\n"
        "       install with:  pip install qrcode[pil]\n"
    )
    sys.exit(2)


ECC_MAP = {
    "L": ERROR_CORRECT_L,
    "M": ERROR_CORRECT_M,
    "Q": ERROR_CORRECT_Q,
    "H": ERROR_CORRECT_H,
}


def render_qr(text: str, ecc: int, box_size: int, border: int):
    qr = qrcode.QRCode(
        version=None,
        error_correction=ecc,
        box_size=box_size,
        border=border,
    )
    qr.add_data(text)
    qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="white").convert("RGB")


def main() -> int:
    p = argparse.ArgumentParser(description="Generate QRX1 QR frames from a file.")
    p.add_argument("file", type=Path, help="input file to encode")
    p.add_argument(
        "--chunk",
        type=int,
        default=500,
        help="base64 chars per data frame (50..2000, default 500)",
    )
    p.add_argument(
        "--ecc",
        choices=list(ECC_MAP.keys()),
        default="M",
        help="QR error correction level (default M)",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=Path("frames"),
        help="output directory for PNG frames (default ./frames)",
    )
    p.add_argument("--box-size", type=int, default=8, help="QR pixel module size")
    p.add_argument("--border", type=int, default=4, help="QR quiet zone (modules)")
    p.add_argument(
        "--gif",
        type=Path,
        default=None,
        help="also write an animated GIF cycling through frames",
    )
    p.add_argument(
        "--fps",
        type=float,
        default=3.0,
        help="GIF playback rate (default 3 fps); only relevant with --gif",
    )
    p.add_argument(
        "--dump-frames",
        action="store_true",
        help="also print the raw QRX1 text of each frame to stdout",
    )
    p.add_argument(
        "--gzip",
        action="store_true",
        help="gzip the payload before chunking; falls back to uncompressed "
        "automatically if the gzipped size is not smaller",
    )
    args = p.parse_args()

    if not args.file.is_file():
        sys.stderr.write(f"error: not a file: {args.file}\n")
        return 1

    raw = args.file.read_bytes()
    try:
        frames = build_frames(raw, args.file.name, args.chunk, compress=args.gzip)
    except ValueError as e:
        sys.stderr.write(f"error: {e}\n")
        return 1

    args.out.mkdir(parents=True, exist_ok=True)
    ecc = ECC_MAP[args.ecc]

    header_parts = frames[0].split("|")
    flags = header_parts[3]
    wire_size = int(header_parts[5])
    crc = header_parts[6]
    if args.gzip and flags == "gz":
        pct = 100 * (1 - wire_size / max(1, len(raw)))
        compression_note = f", gzipped {len(raw)} -> {wire_size} bytes ({pct:.1f}% smaller)"
    elif args.gzip:
        compression_note = f", gzip skipped (would have grown the payload)"
    else:
        compression_note = ""

    print(
        f"{args.file.name}: {len(raw)} bytes{compression_note}, crc32={crc}, "
        f"{len(frames) - 1} data chunks, {len(frames)} total frames "
        f"(chunk={args.chunk} chars, ecc={args.ecc})"
    )

    images = []
    for i, text in enumerate(frames):
        img = render_qr(text, ecc, args.box_size, args.border)
        path = args.out / f"frame_{i:04d}.png"
        img.save(path)
        if args.dump_frames:
            print(f"{path}: {text}")
        if args.gif:
            images.append(img)

    print(f"wrote {len(frames)} PNG frames to {args.out}/")

    if args.gif:
        duration_ms = max(1, int(round(1000.0 / args.fps)))
        images[0].save(
            args.gif,
            save_all=True,
            append_images=images[1:],
            duration=duration_ms,
            loop=0,
            disposal=2,
        )
        print(f"wrote {args.gif} ({len(images)} frames @ {args.fps} fps)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
