/*
 * Seconded — AudioWorklet processor that tees the mic/system mix to the
 * main thread for WAV chunking + /ingest POSTs.
 *
 * Why AudioWorklet instead of createScriptProcessor: the legacy ScriptProcessor
 * runs its handler on the *main thread* — every 256 ms chunk does the int16
 * conversion and meter math there, janking the UI, and browsers throttle
 * background tabs' main-thread audio, dropping transcript chunks when the tab
 * is not focused. AudioWorklet runs on the audio rendering thread.
 *
 * Accumulates 4096 frames (256 ms at the 16 kHz context rate — same chunk
 * size the old ScriptProcessor used) before posting one message, so the main
 * thread does one int16 conversion + /ingest POST per chunk, not per 128-frame
 * quantum.
 *
 * Copyright (C) 2026 Seconded contributors
 * SPDX-part: GNU AGPL v3+ (see LICENSE) — same terms as the rest of Seconded.
 */
const CHUNK = 4096;

class PCMTee extends AudioWorkletProcessor {
  constructor() {
    super();
    this.acc = new Float32Array(CHUNK);
    this.n = 0;
  }
  process(inputs) {
    const input = inputs[0];
    if (input && input.length) {
      const ch = input[0];
      let off = 0;
      while (off < ch.length) {
        const take = Math.min(CHUNK - this.n, ch.length - off);
        this.acc.set(ch.subarray(off, off + take), this.n);
        this.n += take; off += take;
        if (this.n === CHUNK) {
          // The input buffer is only valid during process() — hand the main
          // thread a copy (structured clone).
          this.port.postMessage({ pcm: this.acc });
          this.acc = new Float32Array(CHUNK);
          this.n = 0;
        }
      }
    }
    // Leave the (unused) output silent and keep returning true so the
    // graph pulls until the main thread disconnects us.
    return true;
  }
}
registerProcessor('pcm-tee', PCMTee);
