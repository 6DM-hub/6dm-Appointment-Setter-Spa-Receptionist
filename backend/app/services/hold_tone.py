"""A short, quiet waiting tone for slow provider lookups.

One chunk is about two seconds: a soft chime, then silence. The media bridge
queues at most one chunk at a time and drops it the moment the lookup ends.
"""
from __future__ import annotations

import math

_RATE = 8000
_CLIP = 32635
_BIAS = 0x84


def linear_to_ulaw(sample: int) -> int:
    sign = 0x80 if sample < 0 else 0
    magnitude = -sample if sample < 0 else sample
    if magnitude > _CLIP:
        magnitude = _CLIP
    magnitude += _BIAS
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not (magnitude & mask):
        exponent -= 1
        mask >>= 1
    mantissa = (magnitude >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa)) & 0xFF


def soft_hold_chunk() -> bytes:
    """Quiet two-note chime plus silence. Low amplitude so it stays in the background."""
    samples = bytearray()
    for i in range(int(_RATE * 0.18)):
        env = min(1.0, i / 200) * max(0.0, 1 - i / (_RATE * 0.18))
        wave = math.sin(2 * math.pi * 523.25 * i / _RATE)
        wave += 0.4 * math.sin(2 * math.pi * 659.25 * i / _RATE)
        samples.append(linear_to_ulaw(int(wave * env * 1800)))
    samples.extend(b"\xff" * int(_RATE * 1.8))
    return bytes(samples)
