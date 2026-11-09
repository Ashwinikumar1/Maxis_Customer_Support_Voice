// Captures mic audio (AudioContext runs at 16 kHz) and posts ~100 ms PCM16 chunks.
class PcmRecorder extends AudioWorkletProcessor {
  constructor() {
    super();
    this.chunk = new Int16Array(1600); // 100 ms @ 16 kHz
    this.offset = 0;
  }

  process(inputs) {
    const input = inputs[0] && inputs[0][0];
    if (!input) return true;
    for (let i = 0; i < input.length; i++) {
      const s = Math.max(-1, Math.min(1, input[i]));
      this.chunk[this.offset++] = s < 0 ? s * 0x8000 : s * 0x7fff;
      if (this.offset === this.chunk.length) {
        this.port.postMessage(this.chunk.buffer, [this.chunk.buffer]);
        this.chunk = new Int16Array(1600);
        this.offset = 0;
      }
    }
    return true;
  }
}

registerProcessor("pcm-recorder", PcmRecorder);
