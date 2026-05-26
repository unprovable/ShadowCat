// Tests for the JS side of ShadowCat.
//
// Extracts the protocol-relevant helper functions out of shadowcat.html
// (crc32, crc32hex, bytesToBase64, base64ToBytes, parseFrame, parseFlags)
// and runs them in a Node vm. Asserts the same golden values used by the
// Python tests in tests/test_qrx1.py — if either side drifts from the spec,
// this catches it.
//
// Run with:
//     node --test tests/test_shadowcat.mjs

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { gunzipSync, gzipSync } from 'node:zlib';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const __dirname = dirname(fileURLToPath(import.meta.url));
const HTML_PATH = join(__dirname, '..', 'shadowcat.html');
const html = readFileSync(HTML_PATH, 'utf8');

// ---------------------------------------------------------------------------
// Extract a top-level `function NAME(...) { ... }` block from the HTML by
// scanning for balanced braces. The helper functions in shadowcat.html don't
// contain braces inside string literals, so plain depth counting is safe.
// ---------------------------------------------------------------------------

function extractFunction(name) {
  const needle = `function ${name}(`;
  const start = html.indexOf(needle);
  if (start < 0) throw new Error(`function ${name} not found in shadowcat.html`);
  const openBrace = html.indexOf('{', start);
  if (openBrace < 0) throw new Error(`opening brace for ${name} not found`);
  let depth = 1;
  let i = openBrace + 1;
  while (i < html.length && depth > 0) {
    const c = html[i];
    if (c === '{') depth++;
    else if (c === '}') depth--;
    i++;
  }
  if (depth !== 0) throw new Error(`unbalanced braces in ${name}`);
  return html.slice(start, i);
}

const FUNCS = ['crc32', 'crc32hex', 'bytesToBase64', 'base64ToBytes', 'parseFrame', 'parseFlags'];
// `crc32` references a module-scope `_crcTable` declared outside its body in
// shadowcat.html. Re-declare it so the extracted function works standalone.
const sourceBundle = 'var _crcTable = null;\n\n' + FUNCS.map(extractFunction).join('\n\n');

// Sandbox: provide just what the helpers touch. Node's globals already have
// Uint8Array, btoa, atob, String — but we make them explicit for clarity.
const sandbox = {
  Uint8Array,
  Uint32Array,
  btoa,
  atob,
  String,
};
vm.createContext(sandbox);
vm.runInContext(sourceBundle, sandbox);

const { crc32, crc32hex, bytesToBase64, base64ToBytes, parseFrame, parseFlags } = sandbox;

// ---------------------------------------------------------------------------
// Helpers shared with the Python golden fixture
// ---------------------------------------------------------------------------

function utf8(s) {
  return new TextEncoder().encode(s);
}

const GOLDEN_TEXT = 'Hello, ShadowCat!\n'.repeat(50);
const GOLDEN_BYTES = utf8(GOLDEN_TEXT); // 900 bytes
const GOLDEN_CRC = 'bfe4986e';
const GOLDEN_FILENAME = 'hello.txt';
const GOLDEN_CHUNK = 100;
const GOLDEN_TOTAL = 12;
const GOLDEN_FLAGS = '';
const GOLDEN_HEADER =
  `QRX1|H|${GOLDEN_TOTAL}|${GOLDEN_FLAGS}|${GOLDEN_FILENAME}|${GOLDEN_BYTES.length}|${GOLDEN_CRC}`;

// Same logic as the JS `prepareFrames` / `recomputeWire` pair in
// shadowcat.html, factored out so we can test the frame-building independently
// of DOM state. `compress` here uses Node's sync gzip rather than the browser's
// CompressionStream — the wire format is identical (RFC 1952), so anything
// that round-trips here will also round-trip through CompressionStream.
function buildFrames(rawBytes, filename, chunkLen, opts) {
  opts = opts || {};
  if (filename.indexOf('|') >= 0) {
    throw new Error(`filename contains '|': ${filename}`);
  }
  if (chunkLen < 50 || chunkLen > 2000) {
    throw new Error(`chunk size out of range: ${chunkLen}`);
  }
  const crc = crc32hex(rawBytes);
  let payload = rawBytes;
  let flags = '';
  if (opts.compress) {
    const gz = new Uint8Array(gzipSync(Buffer.from(rawBytes)));
    if (gz.length < rawBytes.length) {
      payload = gz;
      flags = 'gz';
    }
  }
  const b64 = bytesToBase64(payload);
  const total = Math.max(1, Math.ceil(b64.length / chunkLen));
  const frames = [`QRX1|H|${total}|${flags}|${filename}|${payload.length}|${crc}`];
  for (let i = 0; i < total; i++) {
    frames.push(`QRX1|D|${i + 1}|${b64.substring(i * chunkLen, (i + 1) * chunkLen)}`);
  }
  return frames;
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test('extracts 6 helper functions from shadowcat.html', () => {
  for (const name of FUNCS) {
    assert.equal(typeof sandbox[name], 'function', `${name} should be a function`);
  }
});

test('crc32hex matches the golden fixture', () => {
  assert.equal(crc32hex(GOLDEN_BYTES), GOLDEN_CRC);
});

test('crc32hex of empty buffer is 00000000', () => {
  assert.equal(crc32hex(new Uint8Array(0)), '00000000');
});

test('crc32hex of "abc" matches zlib reference', () => {
  assert.equal(crc32hex(utf8('abc')), '352441c2');
});

test('bytesToBase64 / base64ToBytes are inverses', () => {
  const samples = [
    new Uint8Array(0),
    new Uint8Array([0, 1, 2, 3, 254, 255]),
    utf8('The quick brown fox jumps over the lazy dog'),
  ];
  for (const s of samples) {
    const b64 = bytesToBase64(s);
    const round = base64ToBytes(b64);
    assert.deepEqual(Array.from(round), Array.from(s));
  }
});

test('parseFrame parses a valid header (with empty flags)', () => {
  const h = { ...parseFrame(GOLDEN_HEADER) };
  assert.deepEqual(h, {
    type: 'H',
    total: GOLDEN_TOTAL,
    flags: '',
    name: GOLDEN_FILENAME,
    size: GOLDEN_BYTES.length,
    crc: GOLDEN_CRC,
  });
});

test('parseFrame parses a header with the gz flag', () => {
  const h = parseFrame('QRX1|H|3|gz|file.bin|42|deadbeef');
  assert.equal(h.type, 'H');
  assert.equal(h.flags, 'gz');
  assert.equal(h.name, 'file.bin');
  assert.equal(h.size, 42);
  assert.equal(h.crc, 'deadbeef');
});

test('parseFrame parses a valid data frame', () => {
  const d = { ...parseFrame('QRX1|D|7|SGVsbG8=') };
  assert.deepEqual(d, { type: 'D', idx: 7, data: 'SGVsbG8=' });
});

test('parseFrame lowercases the CRC', () => {
  const h = parseFrame('QRX1|H|1||x|0|ABCDEF12');
  assert.equal(h.crc, 'abcdef12');
});

test('parseFrame rejects invalid frames', () => {
  const bad = [
    '',
    'garbage',
    'QRX2|H|1||x|0|00000000', // wrong version
    'qrx1|H|1||x|0|00000000', // wrong case
    'QRX1|H|1|x|0|00000000',  // old (no-flags) format — now invalid
    'QRX1|H|1||x|0',           // truncated header
    'QRX1|D|1',                // truncated data
    'QRX1|',                   // just prefix
  ];
  for (const t of bad) {
    assert.equal(parseFrame(t), null, `should reject: ${JSON.stringify(t)}`);
  }
});

test('parseFlags returns non-empty tokens only', () => {
  // parseFlags returns an Array from the vm sandbox's primordials, so spread
  // it into a fresh Array in this realm before comparing (otherwise strict
  // deepEqual fails on differing Array.prototype identities).
  assert.deepEqual([...parseFlags('')], []);
  assert.deepEqual([...parseFlags('gz')], ['gz']);
  assert.deepEqual([...parseFlags('gz,future')], ['gz', 'future']);
  assert.deepEqual([...parseFlags(',')], []);
  assert.deepEqual([...parseFlags(undefined)], []);
  assert.deepEqual([...parseFlags(null)], []);
});

test('buildFrames matches the golden header and total', () => {
  const frames = buildFrames(GOLDEN_BYTES, GOLDEN_FILENAME, GOLDEN_CHUNK);
  assert.equal(frames[0], GOLDEN_HEADER);
  assert.equal(frames.length, GOLDEN_TOTAL + 1);
});

test('buildFrames data payloads reconcatenate to the full base64', () => {
  const frames = buildFrames(GOLDEN_BYTES, GOLDEN_FILENAME, GOLDEN_CHUNK);
  const payloads = frames.slice(1).map(f => f.split('|', 4)[3]);
  const joined = payloads.join('');
  assert.equal(joined, bytesToBase64(GOLDEN_BYTES));
});

test('buildFrames data frames are 1-indexed sequentially', () => {
  const frames = buildFrames(GOLDEN_BYTES, GOLDEN_FILENAME, GOLDEN_CHUNK);
  frames.slice(1).forEach((f, i) => {
    assert.ok(f.startsWith(`QRX1|D|${i + 1}|`), `frame ${i} prefix wrong: ${f.slice(0, 20)}`);
  });
});

test('roundtrip: buildFrames -> parseFrame -> reassemble -> match input', () => {
  const frames = buildFrames(GOLDEN_BYTES, GOLDEN_FILENAME, GOLDEN_CHUNK);
  const h = parseFrame(frames[0]);
  assert.equal(h.type, 'H');
  assert.equal(h.flags, '');
  const chunks = new Array(h.total);
  for (const f of frames.slice(1)) {
    const d = parseFrame(f);
    assert.equal(d.type, 'D');
    chunks[d.idx - 1] = d.data;
  }
  const reassembled = base64ToBytes(chunks.join(''));
  assert.equal(reassembled.length, h.size);
  assert.equal(crc32hex(reassembled), h.crc);
  assert.deepEqual(Array.from(reassembled), Array.from(GOLDEN_BYTES));
});

test('roundtrip: empty file still produces exactly one data chunk', () => {
  const frames = buildFrames(new Uint8Array(0), 'empty.bin', 100);
  assert.equal(frames.length, 2);
  assert.equal(frames[0], 'QRX1|H|1||empty.bin|0|00000000');
  assert.equal(frames[1], 'QRX1|D|1|');
});

// ---------------------------------------------------------------------------
// Compression
// ---------------------------------------------------------------------------

const COMPRESSIBLE = utf8('The quick brown fox jumps over the lazy dog.\n'.repeat(120));
// 5400 bytes of highly repetitive text — gzip should crush it
const COMPRESSIBLE_CRC = crc32hex(COMPRESSIBLE);

test('compress=true emits the gz flag and shrinks the wire size', () => {
  const frames = buildFrames(COMPRESSIBLE, 'fox.txt', 200, { compress: true });
  const h = parseFrame(frames[0]);
  assert.equal(h.flags, 'gz');
  assert.ok(h.size < COMPRESSIBLE.length, `expected shrink, got ${h.size} vs ${COMPRESSIBLE.length}`);
  assert.equal(h.crc, COMPRESSIBLE_CRC); // CRC is over the original
  assert.equal(h.name, 'fox.txt');       // filename unchanged
});

test('compress=true round-trips via gzip back to the original bytes', () => {
  const frames = buildFrames(COMPRESSIBLE, 'fox.txt', 2000, { compress: true });
  const h = parseFrame(frames[0]);
  const chunks = new Array(h.total);
  for (const f of frames.slice(1)) {
    const d = parseFrame(f);
    chunks[d.idx - 1] = d.data;
  }
  const wire = base64ToBytes(chunks.join(''));
  assert.equal(wire.length, h.size);
  const decompressed = new Uint8Array(gunzipSync(Buffer.from(wire)));
  assert.equal(crc32hex(decompressed), h.crc);
  assert.deepEqual(Array.from(decompressed), Array.from(COMPRESSIBLE));
});

test('compress=true falls back to uncompressed when gzip would grow the payload', () => {
  const tiny = utf8('hi');
  const frames = buildFrames(tiny, 'hi.txt', 100, { compress: true });
  const h = parseFrame(frames[0]);
  assert.equal(h.flags, '', `expected fallback to empty flags, got ${JSON.stringify(h.flags)}`);
  assert.equal(h.size, tiny.length);
});

test('compress=false leaves flags empty even on compressible input', () => {
  const frames = buildFrames(COMPRESSIBLE, 'fox.txt', 200, { compress: false });
  const h = parseFrame(frames[0]);
  assert.equal(h.flags, '');
  assert.equal(h.size, COMPRESSIBLE.length);
});

// Note (split('|', 4) limit semantics): JS's split with a limit DROPS extra
// fields, it doesn't keep them as a tail like Python does. Base64 never
// contains '|' so this doesn't matter for valid frames, but we sanity-check
// to document the assumption.
test('base64 alphabet does not contain the field separator', () => {
  const all = bytesToBase64(new Uint8Array(Array.from({ length: 256 }, (_, i) => i)));
  assert.equal(all.indexOf('|'), -1);
});
