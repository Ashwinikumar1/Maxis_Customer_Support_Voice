// Plays PCM16 audio from the model (AudioContext runs at 24 kHz) via a ring buffer.
// Post an ArrayBuffer to enqueue audio, or {command: "clear"} to flush on interruption.
class PcmPlayer extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buffer = new Float32Array(24000 * 180); // 3 minutes
    this.write = 0;
    this.read = 0;
    this.port.onmessage = (e) => {
      if (e.data && e.data.command === "clear") {
        this.read = this.write;
        return;
      }
      const pcm = new Int16Array(e.data);
      for (let i = 0; i < pcm.length; i++) {
        this.buffer[this.write] = pcm[i] / 32768;
        this.write = (this.write + 1) % this.buffer.length;
        if (this.write === this.read) this.read = (this.read + 1) % this.buffer.length;
      }
    };
  }

  process(_inputs, outputs) {
    const out = outputs[0][0];
    for (let i = 0; i < out.length; i++) {
      if (this.read === this.write) {
        out[i] = 0;
      } else {
        out[i] = this.buffer[this.read];
        this.read = (this.read + 1) % this.buffer.length;
      }
    }
    return true;
  }
}

registerProcessor("pcm-player", PcmPlayer);
