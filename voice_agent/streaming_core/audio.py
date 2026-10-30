"""Audio conversion for telephony channels (Python 3.13+ removed audioop).

Genesys Audio Connector streams G.711 mu-law (PCMU) 8 kHz mono. Gemini Live
takes PCM16 16 kHz and returns PCM16 24 kHz. A Genesys adapter would do:

    inbound : pcm16_16k = resample_pcm16(ulaw_to_pcm16(frame), 8000, 16000)
    outbound: frame     = pcm16_to_ulaw(resample_pcm16(pcm16_24k, 24000, 8000))

Resampling is stateless per chunk (linear interpolation up, block averaging
down): fine for speech. TODO: use a stateful polyphase resampler (e.g. soxr)
if audio quality testing shows artefacts at chunk boundaries.
"""

import sys
from array import array

_BIAS = 0x84
_CLIP = 32635


def _ulaw_decode(u: int) -> int:
  u = ~u & 0xFF
  exponent, mantissa = (u >> 4) & 0x07, u & 0x0F
  sample = (((mantissa << 3) + _BIAS) << exponent) - _BIAS
  return -sample if u & 0x80 else sample


def _ulaw_encode(sample: int) -> int:
  sign = 0x80 if sample < 0 else 0
  magnitude = min(-sample if sample < 0 else sample, _CLIP) + _BIAS
  exponent, mask = 7, 0x4000
  while exponent > 0 and not magnitude & mask:
    exponent -= 1
    mask >>= 1
  mantissa = (magnitude >> (exponent + 3)) & 0x0F
  return ~(sign | (exponent << 4) | mantissa) & 0xFF


_DECODE = array("h", (_ulaw_decode(u) for u in range(256)))
_ENCODE = bytes(_ulaw_encode(s - 65536 if s > 32767 else s) for s in range(65536))  # by s & 0xFFFF


def _samples(pcm16: bytes) -> array:
  samples = array("h")
  samples.frombytes(pcm16[: len(pcm16) // 2 * 2])
  if sys.byteorder == "big":  # PCM16 on the wire is little-endian
    samples.byteswap()
  return samples


def _to_bytes(samples: array) -> bytes:
  if sys.byteorder == "big":
    samples.byteswap()
  return samples.tobytes()


def ulaw_to_pcm16(ulaw: bytes) -> bytes:
  """G.711 mu-law bytes -> PCM16 little-endian (same sample rate)."""
  return _to_bytes(array("h", (_DECODE[b] for b in ulaw)))


def pcm16_to_ulaw(pcm16: bytes) -> bytes:
  """PCM16 little-endian -> G.711 mu-law bytes (same sample rate)."""
  return bytes(_ENCODE[s & 0xFFFF] for s in _samples(pcm16))


def resample_pcm16(pcm16: bytes, src_rate: int, dst_rate: int) -> bytes:
  """Resamples mono PCM16 little-endian audio from src_rate to dst_rate."""
  if src_rate == dst_rate or not pcm16:
    return pcm16
  x = _samples(pcm16)
  if src_rate % dst_rate == 0:  # integer downsample (24k -> 8k): average each block
    f = src_rate // dst_rate
    out = array("h", (sum(x[i:i + f]) // f for i in range(0, len(x) - f + 1, f)))
  else:  # linear interpolation (8k -> 16k, and any other ratio)
    n_out = len(x) * dst_rate // src_rate
    step = src_rate / dst_rate
    last = len(x) - 1
    out = array("h")
    for j in range(n_out):
      pos = j * step
      i = int(pos)
      nxt = x[i + 1] if i < last else x[last]
      out.append(int(x[i] + (nxt - x[i]) * (pos - i)))
  return _to_bytes(out)
