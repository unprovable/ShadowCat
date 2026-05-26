#!/usr/bin/env python3
"""
qrx1_decode.py — decode ShadowCat / QRX1 QR frames back into a file.

Inputs can be:
    * a single image file (PNG / JPG / etc.) — one QR
    * a directory of images — all are scanned, header + data chunks
      get assembled
    * a video file (.mp4 / .mov / .avi / ...) — every frame is sampled;
      duplicates are ignored, so a recording of the sender's loop works
    * a live camera index (e.g. `0`) when --camera is set — scans until
      the file is complete or you Ctrl-C

Dependencies:
    pip install pyzbar pillow opencv-python

(`pyzbar` requires the system `zbar` library — `brew install zbar` on macOS,
or `apt install libzbar0` on Debian/Ubuntu.)

Usage:
    python qrx1_decode.py frames/                  # directory of PNGs
    python qrx1_decode.py one_frame.png            # single QR (header or data)
    python qrx1_decode.py recording.mp4            # video file
    python qrx1_decode.py 0 --camera               # live webcam
    python qrx1_decode.py frames/ -o out_dir/      # choose output dir
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Iterable, Iterator

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qrx1 import Receiver  # noqa: E402

try:
    from PIL import Image
except ImportError:
    sys.stderr.write("error: Pillow is required.  pip install pillow\n")
    sys.exit(2)

try:
    from pyzbar.pyzbar import decode as zbar_decode
except ImportError:
    sys.stderr.write(
        "error: pyzbar is required.\n"
        "       pip install pyzbar\n"
        "       and install system zbar (brew install zbar / apt install libzbar0)\n"
    )
    sys.exit(2)


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}


# ---------------------------------------------------------------------------
# Frame sources: each yields raw strings from QR decodes
# ---------------------------------------------------------------------------

def decode_image(img: Image.Image) -> Iterator[str]:
    for sym in zbar_decode(img):
        try:
            yield sym.data.decode("utf-8")
        except UnicodeDecodeError:
            continue


def texts_from_path(path: Path) -> Iterator[str]:
    if path.is_dir():
        files = sorted(
            p for p in path.iterdir() if p.suffix.lower() in IMAGE_EXTS
        )
        for f in files:
            try:
                with Image.open(f) as img:
                    yield from decode_image(img)
            except Exception as e:
                sys.stderr.write(f"warn: failed to read {f}: {e}\n")
        return

    if path.is_file() and path.suffix.lower() in VIDEO_EXTS:
        yield from texts_from_video(str(path))
        return

    if path.is_file() and path.suffix.lower() in IMAGE_EXTS:
        with Image.open(path) as img:
            yield from decode_image(img)
        return

    raise SystemExit(f"error: don't know how to read {path}")


def texts_from_video(source) -> Iterator[str]:
    try:
        import cv2  # type: ignore
    except ImportError:
        raise SystemExit(
            "error: opencv-python is required for video / camera input.\n"
            "       pip install opencv-python"
        )

    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        raise SystemExit(f"error: could not open video source: {source}")
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            img = Image.fromarray(rgb)
            yield from decode_image(img)
    finally:
        cap.release()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="Decode QRX1 QR frames into a file.")
    p.add_argument(
        "input",
        help="path to image / directory of images / video file, "
        "or a camera index (with --camera)",
    )
    p.add_argument(
        "-o", "--output-dir",
        type=Path,
        default=Path("."),
        help="directory to write the reassembled file into (default .)",
    )
    p.add_argument(
        "-O", "--output-file",
        type=Path,
        default=None,
        help="explicit output path; overrides --output-dir + header filename",
    )
    p.add_argument(
        "--camera",
        action="store_true",
        help="treat input as a camera index (int) and read frames live",
    )
    p.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="print each parsed frame as it is ingested",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="overwrite existing output file",
    )
    args = p.parse_args()

    recv = Receiver(verbose=args.verbose)

    if args.camera:
        try:
            cam_index = int(args.input)
        except ValueError:
            sys.stderr.write("error: --camera requires an integer input\n")
            return 1
        source: Iterable[str] = texts_from_video(cam_index)
    else:
        source = texts_from_path(Path(args.input))

    done = False
    for text in source:
        if recv.ingest(text):
            done = True
            break

    if not done:
        if recv.header is None:
            sys.stderr.write("error: no QRX1 header was found in the input.\n")
            return 1
        miss = recv.missing()
        sys.stderr.write(
            f"error: incomplete. got {recv.count}/{len(recv.chunks)} chunks "
            f"for {recv.header.name!r}. missing {len(miss)}: "
            f"{miss[:20]}{'...' if len(miss) > 20 else ''}\n"
        )
        return 1

    try:
        raw = recv.assemble()
    except ValueError as e:
        sys.stderr.write(f"error: {e}\n")
        return 1

    assert recv.header is not None
    if args.output_file is not None:
        out_path = args.output_file
    else:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        # strip any path separators that might have been smuggled in
        safe_name = Path(recv.header.name).name or "shadowcat_output.bin"
        out_path = args.output_dir / safe_name

    if out_path.exists() and not args.force:
        sys.stderr.write(
            f"error: {out_path} exists; pass --force or use --output-file to overwrite.\n"
        )
        return 1

    out_path.write_bytes(raw)
    flag_note = f", flags={recv.header.flags!r}" if recv.header.flags else ""
    wire_note = (
        f" (decompressed from {recv.header.size} wire bytes)"
        if recv.header.has_flag("gz")
        else ""
    )
    print(
        f"OK: wrote {out_path} ({len(raw)} bytes{wire_note}, "
        f"crc32={recv.header.crc}{flag_note})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
