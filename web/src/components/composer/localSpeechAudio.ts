/**
 * Pure, DOM-free helpers for the local speech-to-text engine's audio path.
 *
 * The local engine captures raw microphone samples in the browser and streams
 * them to the runtime (`runtime.stt.begin/append/finish/cancel`), which decodes
 * int16 little-endian mono PCM at the announced rate.  Everything between "a
 * `Float32Array` arrived from Web Audio" and "a base64 wire chunk is ready" is a
 * decision this module owns, so `node --test` can exercise it without a browser:
 *
 * - **`encodeBase64`** turns bytes into standard base64 over `Uint8Array` rather
 *   than reaching for `btoa` (or the Node `Buffer`), so the module stays
 *   host-independent -- the same reason `runtime-client/attachments.ts` carries
 *   its own codec.
 * - **`floatToPcm16`** / **`pcm16ToBytes`** / **`pcmChunkToBase64`** are the one
 *   conversion from Web Audio's `[-1, 1]` floats to the wire's signed 16-bit
 *   little-endian samples; the clamp is what keeps a hot signal from wrapping
 *   around to the opposite sign.
 * - **`resampleTo16k`** is the fallback for an `AudioContext` that refuses the
 *   requested 16 kHz (most desktops only offer 44.1/48 kHz): a bounded linear
 *   interpolation, not a guess that the rate will always be honoured.
 * - **`createSampleBatcher`** turns a stream of arbitrary-sized sample buffers
 *   into fixed ~600 ms chunks with a carry-over buffer, and clamps the chunk to
 *   the wire's 64 KiB bound so a caller can never hand `runtime.stt.append` a
 *   payload the decoder would reject.
 */

/** The rate `runtime.stt.begin` announces; every chunk is encoded at this rate. */
export const LOCAL_STT_SAMPLE_RATE = 16000;

/** Target chunk length: ~600 ms of speech, small enough to keep latency low. */
export const LOCAL_STT_CHUNK_MS = 600;

/** Samples in one target chunk at the announced rate (9600 at 16 kHz). */
export const LOCAL_STT_CHUNK_SAMPLES = (LOCAL_STT_SAMPLE_RATE * LOCAL_STT_CHUNK_MS) / 1000;

/** The wire bound on one decoded chunk, in bytes (mirrors the service's 64 KiB). */
export const LOCAL_STT_MAX_CHUNK_BYTES = 64 * 1024;

/** Samples in one chunk at the byte bound (32768 = 2.048 s of int16 mono). */
export const LOCAL_STT_MAX_CHUNK_SAMPLES = LOCAL_STT_MAX_CHUNK_BYTES / 2;

// --- base64 codec (DOM-free, Buffer-free) ------------------------------------

const B64_ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';

/**
 * Encode one byte range as standard base64 (no line breaks, padded).
 *
 * Mirrors `runtime-client/attachments.ts` on purpose: this module must run
 * under `node --test` and in the browser alike, so it carries its own codec
 * instead of the host `btoa`.
 */
export function encodeBase64(bytes: Uint8Array): string {
  let out = '';
  const length = bytes.length;
  let index = 0;
  for (; index + 2 < length; index += 3) {
    const n = (bytes[index] << 16) | (bytes[index + 1] << 8) | bytes[index + 2];
    out +=
      B64_ALPHABET[(n >> 18) & 63] +
      B64_ALPHABET[(n >> 12) & 63] +
      B64_ALPHABET[(n >> 6) & 63] +
      B64_ALPHABET[n & 63];
  }
  const remainder = length - index;
  if (remainder === 1) {
    const n = bytes[index] << 16;
    out += B64_ALPHABET[(n >> 18) & 63] + B64_ALPHABET[(n >> 12) & 63] + '==';
  } else if (remainder === 2) {
    const n = (bytes[index] << 16) | (bytes[index + 1] << 8);
    out +=
      B64_ALPHABET[(n >> 18) & 63] +
      B64_ALPHABET[(n >> 12) & 63] +
      B64_ALPHABET[(n >> 6) & 63] +
      '=';
  }
  return out;
}

// --- float32 -> int16 little-endian PCM --------------------------------------

/**
 * Convert `[-1, 1]` float samples to signed 16-bit PCM.
 *
 * Web Audio hands back floats in `[-1, 1]`, but a clipped or synthesized signal
 * can exceed that; clamping keeps `-1.2` from wrapping to a loud positive value.
 * The negative half is scaled by `0x8000` and the positive by `0x7fff` so both
 * endpoints map to the exact `Int16` limits.
 */
export function floatToPcm16(samples: Float32Array): Int16Array {
  const out = new Int16Array(samples.length);
  for (let index = 0; index < samples.length; index += 1) {
    const value = samples[index];
    const clamped = value <= -1 ? -1 : value >= 1 ? 1 : value;
    out[index] = Math.round(clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff);
  }
  return out;
}

/** The little-endian bytes of signed 16-bit PCM (two bytes per sample). */
export function pcm16ToBytes(samples: Int16Array): Uint8Array {
  const bytes = new Uint8Array(samples.length * 2);
  for (let index = 0; index < samples.length; index += 1) {
    const value = samples[index];
    bytes[index * 2] = value & 0xff;
    bytes[index * 2 + 1] = (value >> 8) & 0xff;
  }
  return bytes;
}

/** One chunk of float samples as base64 int16 little-endian mono PCM. */
export function pcmChunkToBase64(samples: Float32Array): string {
  return encodeBase64(pcm16ToBytes(floatToPcm16(samples)));
}

// --- resampling --------------------------------------------------------------

/**
 * Resample float samples to the announced 16 kHz by linear interpolation.
 *
 * The `AudioContext` often refuses the requested rate (44.1 or 48 kHz on most
 * desktops), so this is the normal path, not an edge case.  The output length
 * is bounded by the input length; an empty input or a non-positive rate yields
 * an empty result rather than a thrown error.
 */
export function resampleTo16k(samples: Float32Array, inputRate: number): Float32Array {
  if (samples.length === 0 || !Number.isFinite(inputRate) || inputRate <= 0) {
    return new Float32Array(0);
  }
  if (inputRate === LOCAL_STT_SAMPLE_RATE) return samples.slice();
  const outputLength = Math.max(
    1,
    Math.round((samples.length * LOCAL_STT_SAMPLE_RATE) / inputRate),
  );
  const out = new Float32Array(outputLength);
  // Input samples consumed per output sample (>= 1 when downsampling).
  const step = inputRate / LOCAL_STT_SAMPLE_RATE;
  for (let index = 0; index < outputLength; index += 1) {
    const position = index * step;
    const left = Math.floor(position);
    const right = Math.min(left + 1, samples.length - 1);
    const fraction = position - left;
    const a = left < samples.length ? samples[left] : 0;
    const b = samples[right];
    out[index] = a + (b - a) * fraction;
  }
  return out;
}

// --- chunk batching ----------------------------------------------------------

/** A streaming sample buffer that emits fixed-size chunks with carry-over. */
export interface SampleBatcher {
  /**
   * Append samples and return every complete chunk this push completed.
   *
   * A push larger than one chunk is split into as many chunks as it fills; the
   * remainder stays in the carry buffer for the next push or for `flush`.
   */
  push(samples: Float32Array): Float32Array[];
  /** The carry-over samples not yet part of a chunk, or `null` when empty. */
  flush(): Float32Array | null;
  /** How many samples are buffered but not yet emitted. */
  pending(): number;
}

/** Clamp a requested chunk size to `[1, LOCAL_STT_MAX_CHUNK_SAMPLES]`. */
function clampChunkSamples(value: number): number {
  if (!Number.isFinite(value)) return LOCAL_STT_CHUNK_SAMPLES;
  const rounded = Math.floor(value);
  if (rounded < 1) return 1;
  if (rounded > LOCAL_STT_MAX_CHUNK_SAMPLES) return LOCAL_STT_MAX_CHUNK_SAMPLES;
  return rounded;
}

/**
 * Create a batcher that emits chunks of `chunkSamples` (default ~600 ms).
 *
 * The size is clamped to the 64 KiB wire bound, so no caller -- however large a
 * chunk it asks for -- can hand `runtime.stt.append` an oversized payload.
 */
export function createSampleBatcher(chunkSamples: number = LOCAL_STT_CHUNK_SAMPLES): SampleBatcher {
  const size = clampChunkSamples(chunkSamples);
  let carry: number[] = [];
  return {
    push(samples: Float32Array): Float32Array[] {
      const chunks: Float32Array[] = [];
      let offset = 0;
      if (carry.length > 0) {
        const need = size - carry.length;
        const take = Math.min(need, samples.length);
        for (let index = 0; index < take; index += 1) carry.push(samples[index]);
        offset = take;
        if (carry.length === size) {
          chunks.push(Float32Array.from(carry));
          carry = [];
        }
      }
      while (samples.length - offset >= size) {
        chunks.push(samples.slice(offset, offset + size));
        offset += size;
      }
      for (; offset < samples.length; offset += 1) carry.push(samples[offset]);
      return chunks;
    },
    flush(): Float32Array | null {
      if (carry.length === 0) return null;
      const tail = Float32Array.from(carry);
      carry = [];
      return tail;
    },
    pending(): number {
      return carry.length;
    },
  };
}
