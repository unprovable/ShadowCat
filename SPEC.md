# ShadowCat / QRX1 Protocol Specification

ShadowCat moves a file from one device to another using a sequence of QR codes
displayed on one screen and scanned by another camera. There is no back
channel — the sender just loops a sequence of frames forever, and the receiver
collects them until it has them all.

This document specifies the **QRX1** wire protocol used inside each QR.

---

## 1. Topology

```
   +--------------------+                          +--------------------+
   |     SENDER         |                          |     RECEIVER       |
   |  (any browser /    |    optical channel       |  (any browser /    |
   |   shadowcat.html)  |   ====================>  |   shadowcat.html)  |
   |                    |   QR frames @ N fps      |                    |
   |  loops:            |   one-way, no ACKs       |  ingests frames,   |
   |   [H, D1, D2, ...] |                          |  dedupes by idx,   |
   |   forever          |                          |  verifies CRC32    |
   +--------------------+                          +--------------------+
           |                                                 ^
           |                                                 |
           +-------------- screen --> camera ----------------+
```

Key properties:

- **One-way.** The receiver never talks back. The sender does not know what's
  been received.
- **Loop-forever.** The sender cycles through `[header, chunk_1, ..., chunk_N]`
  at a chosen FPS. Missed frames are caught on the next pass.
- **Idempotent ingest.** The receiver tracks which chunk indices it has and
  drops duplicates. Order does not matter.
- **No retransmit signaling.** "Retransmit" is just letting the loop come back
  around (or manually displaying a single frame via the sender's "Show frame"
  control).

---

## 2. Frame format

Every QR encodes a single UTF-8 text string of one of two types.

### 2.1 Header frame (type `H`)

```
+--------+---+--------+-----------+------------+-------------+
| "QRX1" | H | total  | filename  | sizeBytes  | crc32hex    |
+--------+---+--------+-----------+------------+-------------+
   ^      ^     ^          ^           ^             ^
   |      |     |          |           |             |
   |      |     |          |           |             8-char lowercase hex CRC32
   |      |     |          |           |             of the raw file bytes
   |      |     |          |           |
   |      |     |          |           total raw file size in bytes (decimal)
   |      |     |          |
   |      |     |          original filename, no '|' character allowed
   |      |     |
   |      |     number of data chunks that follow (decimal, >= 1)
   |      |
   |      type tag: 'H' = header
   |
   protocol version tag, literal "QRX1"
```

Literal example:

```
QRX1|H|14|hello.txt|6713|9a3f0c21
```

### 2.2 Data frame (type `D`)

```
+--------+---+-----+----------------------------+
| "QRX1" | D | idx | base64 chunk               |
+--------+---+-----+----------------------------+
                ^               ^
                |               |
                |               a slice of the file's base64 encoding;
                |               the alphabet (A-Z, a-z, 0-9, '+', '/', '=')
                |               contains no '|'
                |
                1-indexed chunk number, 1..total
```

Literal example:

```
QRX1|D|1|SGVsbG8sIFNoYWRvd0NhdCEgVGhpcyBp...
```

### 2.3 Field separator

The separator is a single ASCII `|` (0x7C). Because the base64 alphabet does
not contain `|`, parsing is exactly `text.split('|')` — no escaping, no
quoting.

The filename also must not contain `|`. The reference HTML does not enforce
this; a malicious or unusual filename will corrupt the header. Implementations
SHOULD reject or sanitize filenames containing `|`.

---

## 3. Encoding pipeline (sender)

```
   raw file bytes
        |
        |  1. compute crc32hex = lowercase hex CRC32 (IEEE 802.3 poly 0xEDB88320,
        |     init 0xFFFFFFFF, xor-out 0xFFFFFFFF) of the raw bytes
        v
   crc32hex                              raw file bytes
                                              |
                                              |  2. base64-encode (standard,
                                              |     with '=' padding)
                                              v
                                         base64 string  S
                                              |
                                              |  3. split S into fixed-length
                                              |     character slices of length
                                              |     chunkLen (default 500;
                                              |     allowed range 50..2000 in
                                              |     reference impl)
                                              v
                          slices: S[0..L], S[L..2L], ..., last slice may be shorter
                                              |
                                              |  4. total = number of slices
                                              v
        +-----------------------------+   +------------------------------------+
        | header = QRX1|H|<total>|... |   | data_i = QRX1|D|<i>|<slice_{i-1}>  |
        +-----------------------------+   +------------------------------------+
                       \                            /
                        \                          /
                         v                        v
                          frames = [header, data_1, data_2, ..., data_total]
                                              |
                                              v
                                  loop forever at FPS, rendering one QR each tick
```

Notes:
- `chunkLen` is measured in **base64 characters**, not raw bytes. 500 base64
  chars ≈ 375 raw bytes per frame.
- The header is re-shown once per loop, so a receiver that misses the very
  first frame still gets it on the next pass.

---

## 4. Decoding pipeline (receiver)

```
   QR scan tick
        |
        v
   text  ----> parseFrame(text) ----> {type:'H', total, name, size, crc}
                       |              {type:'D', idx, data}
                       |              null   (not a QRX1 frame; ignored)
                       v
              +----------------+
              | Header arrives |
              +----------------+
                       |
        new header (different crc OR different total)?
                       |
                yes -->+--> reset state:
                       |      recvHeader  := f
                       |      recvChunks  := array of size f.total, all empty
                       |      recvCount   := 0
                       |
                no  -->+--> ignore (idempotent dedupe by crc+total)

              +----------------+
              | Data arrives   |
              +----------------+
                       |
                       v
              if no header yet            -> drop (cannot place chunk)
              if idx < 1 or idx > total   -> drop (out of range)
              if recvChunks[idx-1] set    -> drop (duplicate)
              else                         -> store, recvCount += 1

              when recvCount == total:
                  reassembled = base64decode(concat(recvChunks))
                  assert len(reassembled) == recvHeader.size
                  assert crc32hex(reassembled) == recvHeader.crc
                  emit file
```

---

## 5. Invariants & error handling

| # | Invariant                                                                              |
|---|----------------------------------------------------------------------------------------|
| 1 | A frame begins with the literal ASCII prefix `QRX1|`. Anything else is ignored.        |
| 2 | The second field is either `H` or `D`. Other values are ignored.                       |
| 3 | `total` (header) and `idx` (data) are decimal integers with no leading sign.           |
| 4 | `idx` is 1-indexed and bounded by `1 <= idx <= total`.                                 |
| 5 | `crc32hex` is exactly 8 lowercase hex characters.                                      |
| 6 | The base64 alphabet used is the standard one (A-Z, a-z, 0-9, `+`, `/`, `=`). No `|`.   |
| 7 | A new header with a CRC or total different from the current one resets receiver state. |
| 8 | Duplicate data frames (same `idx`) MUST be ignored, not overwritten.                   |

If the final CRC check fails the receiver should surface an error rather than
silently writing a corrupt file. The reference HTML shows a red status banner
in that case.

---

## 6. Sizing & throughput

- **Per-frame payload.** A data frame's text is `"QRX1|D|<idx>|" + slice`. The
  framing overhead is small (≈ 10–12 chars) compared to a 500-char slice.
- **Throughput.** At `chunkLen` base64 chars and `fps` frames/sec, the raw
  goodput is approximately `(chunkLen * 3/4) * fps` bytes/sec, ignoring
  framing overhead and missed frames. For example, 500 chars × 3 fps ≈
  1.1 KB/s base64 ≈ 0.83 KB/s raw.
- **QR capacity.** A higher ECC level (`L < M < Q < H`) gives the QR more
  resilience to glare, motion blur, and dirty lenses, at the cost of needing
  a denser QR for the same payload — and therefore a larger screen or closer
  camera. If rendering fails with "code length overflow", lower `chunkLen` or
  lower the ECC level.

---

## 7. Versioning

The literal prefix `QRX1` is the protocol version. A future incompatible
version would use a different tag (e.g. `QRX2`) and receivers should ignore
frames whose prefix they do not recognize.
