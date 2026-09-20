/**
 * Offline tests for the local speech engine's pure audio helpers.
 *
 * No browser, no socket, no microphone: `node --test` exercises every decision
 * the local engine makes between Web Audio and the wire.  What is pinned here:
 *
 * - the base64 codec is a DOM-free round trip that matches the canonical
 *   `runtime-client/attachments.ts` decoder (the module deliberately does not
 *   use `btoa`);
 * - float samples become signed 16-bit little-endian PCM with the endpoints
 *   clamped, so a hot signal cannot wrap to the opposite sign;
 * - resampling to 16 kHz is a bounded linear interpolation for the common case
 *   where the `AudioContext` refuses the requested rate;
 * - the batcher carries a partial chunk over between pushes and can never emit a
 *   chunk larger than the 64 KiB wire bound.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  LOCAL_STT_CHUNK_SAMPLES,
  LOCAL_STT_MAX_CHUNK_BYTES,
  LOCAL_STT_MAX_CHUNK_SAMPLES,
  LOCAL_STT_SAMPLE_RATE,
  createSampleBatcher,
  encodeBase64,
  floatToPcm16,
  pcm16ToBytes,
  pcmChunkToBase64,
  resampleTo16k,
} from '../src/components/composer/localSpeechAudio.ts';
import { decodeBase64 } from '../src/runtime-client/attachments.ts';

/** Read one little-endian signed 16-bit sample. */
function int16At(bytes: Uint8Array, index: number): number {
  const value = bytes[index * 2] | (bytes[index * 2 + 1] << 8);
  return value >= 0x8000 ? value - 0x10000 : value;
}

// --- base64 codec -----------------------------------------------------------

test('the base64 encoder matches the canonical decoder', () => {
  assert.equal(encodeBase64(new Uint8Array([])), '');
  assert.equal(encodeBase64(new Uint8Array([0x66])), 'Zg==');
  assert.equal(encodeBase64(new Uint8Array([0x66, 0x6f])), 'Zm8=');
  assert.equal(encodeBase64(new Uint8Array([0x66, 0x6f, 0x6f])), 'Zm9v');
  assert.equal(encodeBase64(new Uint8Array([0x66, 0x6f, 0x6f, 0x62, 0x61, 0x72])), 'Zm9vYmFy');

  const odd = new Uint8Array(4097);
  for (let index = 0; index < odd.length; index += 1) odd[index] = (index * 37) & 255;
  assert.deepEqual(decodeBase64(encodeBase64(odd)), odd, 'a non-multiple-of-three length round-trips');
});

// --- float32 -> int16 PCM ----------------------------------------------------

test('float samples become clamped signed 16-bit PCM', () => {
  const pcm = floatToPcm16(new Float32Array([0, 1, -1, 0.5, -0.5, 2, -2]));
  assert.deepEqual(Array.from(pcm), [0, 32767, -32768, 16384, -16384, 32767, -32768]);
});

test('signed 16-bit PCM is written little-endian', () => {
  const bytes = pcm16ToBytes(new Int16Array([0x0102, -2]));
  assert.deepEqual(Array.from(bytes), [0x02, 0x01, 0xfe, 0xff]);
});

test('a float chunk round-trips through base64 back to its samples', () => {
  const samples = new Float32Array([0, 1, -1, 0.25]);
  const bytes = decodeBase64(pcmChunkToBase64(samples));
  assert.equal(bytes.length, samples.length * 2);
  assert.equal(int16At(bytes, 0), 0);
  assert.equal(int16At(bytes, 1), 32767);
  assert.equal(int16At(bytes, 2), -32768);
  assert.equal(int16At(bytes, 3), Math.round(0.25 * 0x7fff));
});

// --- resampling --------------------------------------------------------------

test('a 16 kHz stream is passed through unchanged', () => {
  const input = new Float32Array([0.1, 0.2, 0.3]);
  const output = resampleTo16k(input, LOCAL_STT_SAMPLE_RATE);
  assert.notEqual(output, input, 'the caller must not share the capture buffer');
  assert.deepEqual(Array.from(output), Array.from(input));
});

test('a refused AudioContext rate is resampled by linear interpolation', () => {
  // 32 samples at 32 kHz -> 16 samples at 16 kHz: every other input sample.
  const ramp = new Float32Array(32);
  for (let index = 0; index < ramp.length; index += 1) ramp[index] = index;
  const down = resampleTo16k(ramp, 32000);
  assert.equal(down.length, 16);
  assert.deepEqual(Array.from(down), Array.from({ length: 16 }, (_, index) => index * 2));

  // 2 samples at 8 kHz -> 4 samples at 16 kHz, interpolated between the two.
  const up = resampleTo16k(new Float32Array([0, 1]), 8000);
  assert.equal(up.length, 4);
  assert.deepEqual(Array.from(up), [0, 0.5, 1, 1]);

  const flat = resampleTo16k(new Float32Array(10).fill(0.5), 44100);
  assert.equal(flat.length, Math.round((10 * LOCAL_STT_SAMPLE_RATE) / 44100));
  for (const value of flat) assert.equal(value, 0.5, 'a constant signal stays constant');
});

test('an empty stream or a nonsense rate yields an empty result, not a throw', () => {
  assert.equal(resampleTo16k(new Float32Array(0), 48000).length, 0);
  assert.equal(resampleTo16k(new Float32Array([1, 2]), 0).length, 0);
  assert.equal(resampleTo16k(new Float32Array([1, 2]), Number.NaN).length, 0);
});

// --- chunk batching ----------------------------------------------------------

test('the batcher carries a partial chunk over between pushes', () => {
  const batcher = createSampleBatcher(4);
  assert.deepEqual(batcher.push(new Float32Array([1, 2, 3])), []);
  assert.equal(batcher.pending(), 3);
  const first = batcher.push(new Float32Array([4, 5, 6]));
  assert.equal(first.length, 1);
  assert.deepEqual(Array.from(first[0]), [1, 2, 3, 4]);
  assert.equal(batcher.pending(), 2, 'the leftover stays buffered');
  const second = batcher.push(new Float32Array([7, 8]));
  assert.deepEqual(Array.from(second[0]), [5, 6, 7, 8]);
  assert.equal(batcher.pending(), 0);
  assert.equal(batcher.flush(), null, 'nothing is left after an exact boundary');
});

test('a large push is split into whole chunks and a remainder', () => {
  const batcher = createSampleBatcher(4);
  const chunks = batcher.push(new Float32Array(Array.from({ length: 10 }, (_, index) => index)));
  assert.deepEqual(
    chunks.map((chunk) => chunk.length),
    [4, 4],
  );
  assert.deepEqual(Array.from(batcher.flush() ?? new Float32Array()), [8, 9]);
});

test('the chunk size is clamped to the 64 KiB wire bound', () => {
  const batcher = createSampleBatcher(1_000_000);
  const samples = new Float32Array(LOCAL_STT_MAX_CHUNK_SAMPLES + 10);
  const chunks = batcher.push(samples);
  assert.equal(chunks.length, 1);
  assert.equal(chunks[0].length, LOCAL_STT_MAX_CHUNK_SAMPLES, 'a huge request is clamped');
  assert.equal(batcher.pending(), 10);
});

test('a chunk never exceeds the decoded byte bound', () => {
  assert.equal(LOCAL_STT_SAMPLE_RATE, 16000);
  assert.equal(LOCAL_STT_MAX_CHUNK_BYTES, 64 * 1024);
  assert.equal(LOCAL_STT_MAX_CHUNK_SAMPLES, LOCAL_STT_MAX_CHUNK_BYTES / 2);
  assert.equal(LOCAL_STT_CHUNK_SAMPLES, 9600, 'the default target is ~600 ms at 16 kHz');

  const full = decodeBase64(pcmChunkToBase64(new Float32Array(LOCAL_STT_MAX_CHUNK_SAMPLES)));
  assert.equal(full.length, LOCAL_STT_MAX_CHUNK_BYTES);

  // Even a caller that asks for the largest possible chunk cannot cross the bound.
  const batcher = createSampleBatcher(Number.MAX_SAFE_INTEGER);
  for (const chunk of batcher.push(new Float32Array(LOCAL_STT_MAX_CHUNK_SAMPLES * 2))) {
    assert.ok(chunk.length * 2 <= LOCAL_STT_MAX_CHUNK_BYTES);
  }
});
