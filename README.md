[![tests](https://github.com/unprovable/ShadowCat/actions/workflows/tests.yml/badge.svg)](https://github.com/unprovable/ShadowCat/actions/workflows/tests.yml) 

# ShadowCat

A fully offline, single-file HTML page for moving data between two devices via QR codes — intended for old phones whose radios (BLE, NFC, etc.) are dead but whose cameras and browsers still work.

Click here to use it: https://shadowcat.online/shadowcat.html

## Tabs

- **Generate** — encode text into a single QR code.
- **Scan** — decode a single QR via the camera.
- **Send file** — pick a file, choose chunk size / FPS / ECC, optionally **Compress (gzip)**, hit Start. Compression auto-falls-back when the gzipped payload isn't smaller (e.g. for PDF/JPG/MP4). Cycles through `[header, chunk1…chunkN]` forever at the chosen FPS. Pause / Resume / Stop.
- **Start from** — begin the loop at a chosen frame index; it then continues forward and wraps back to the header normally.
- **Show frame** + **Show** / **−** / **+** — display exactly one frame static, for resending a specific missing chunk. The number matches the chunk index shown in the receiver's missing-chunks grid (0 = header).
- **Receive file** — start the camera and point at the sender. Header autodetects, progress bar fills in, missing-chunks grid shows which ones haven't arrived yet. When complete, the file's CRC is verified and a Download button appears.

## Protocol

- Header: `QRX1|H|<total>|<flags>|<filename>|<sizeBytes>|<crc32hex>`
- Data: `QRX1|D|<idx>|<base64chunk>` (1-indexed)
- `<flags>` is a comma-separated list, empty by default. Defined flags: `gz` = payload is gzipped.
- `<sizeBytes>` is the on-wire size (gzipped when `gz` is set); `<crc32hex>` is always over the original bytes.
- Base64 alphabet has no `|`, so parsing is just `split('|')`.
- Receiver tracks chunks by index, ignores duplicates, dedupes header by CRC. Unknown flags are refused.

See [SPEC.md](SPEC.md) for the full wire format, frame diagrams, and
invariants. A reference encoder and decoder live in [`tools/`](tools/):

```
pip install qrcode[pil] pyzbar pillow opencv-python
python tools/qrx1_encode.py myfile.bin --out frames/
python tools/qrx1_encode.py myfile.bin --gzip --out frames/   # gzip the payload
python tools/qrx1_decode.py frames/ -o out/
```

## Tests

Both sides assert against a shared golden fixture (known input bytes → known
CRC and header). If the JS or Python implementation drifts from the spec,
its tests break.

```
# Python — pytest, zero deps; the optional QR-image roundtrip is skipped
# unless qrcode/pyzbar/PIL are installed.
python -m pytest tests/test_qrx1.py

# HTML — Node's built-in runner; extracts protocol helpers straight out of
# shadowcat.html and exercises them in a vm. No npm install needed.
node --test tests/test_shadowcat.mjs
```

## Practical notes for old phones

- Camera needs HTTPS or localhost — `file://` won't grant `getUserMedia` permission. Serve with `python3 -m http.server 8000` and visit `http://<your-laptop-ip>:8000/qrcode.html` over the local network. iOS Safari additionally requires HTTPS for cross-device access — for a LAN setup, `caddy` or a self-signed cert helps.
- If render fails on a frame ("code length overflow"), drop chunk size or drop ECC level.
- 500 chars × 3 fps ≈ 1.1 KB/s base64 ≈ 0.83 KB/s raw. A 100 KB file is roughly 2 minutes per loop; receiver typically needs 1-2 loops.
- If old devices struggle to decode: lower FPS, raise ECC to Q, shrink chunk to ~300 chars — produces smaller, less dense QRs.

## Related

Doing data transfer with QR Codes is a well trodden idea. Here are some references of other projects that may be useful for erudition or expansion:

* IP-Over-QR Codes - https://hackaday.com/2016/11/22/ip-over-qr-codes/ 
