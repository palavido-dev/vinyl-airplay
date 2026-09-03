#!/usr/bin/env python3
"""Vinyl AirPlay: streaming sample-rate conversion for the capture path.

The whole pipeline (recorder, fingerprinting, EQ, MP3 encode, AirPlay) runs at
44100 Hz. Some USB capture dongles only open at 48000 (issue #76), and ALSA's
"plug" resampling never sees our stream because PortAudio opens the raw hw
device. So the capture stream is opened at whatever rate the card accepts and
the blocks are converted here before anything else touches them.

StreamResampler is a polyphase windowed-sinc converter that carries filter
history and fractional phase between blocks, so feeding it consecutive capture
blocks produces one continuous signal with no seams and no long-run drift (the
phase is tracked as an exact integer on the upsampled grid).
"""

from math import gcd

import numpy as np
from scipy.signal import firwin


class StreamResampler:
    """Stateful in_rate -> out_rate converter for float32 (frames, channels).

    Same filter design as scipy.signal.resample_poly (Kaiser-windowed FIR,
    cutoff at the lower Nyquist), applied incrementally: call process() with
    each capture block in order. Output block lengths vary by a frame as the
    fractional phase carries over, which is fine for every downstream consumer
    (they are all byte queues).
    """

    def __init__(self, in_rate: int, out_rate: int, channels: int, half_len_factor: int = 10):
        if in_rate <= 0 or out_rate <= 0:
            raise ValueError("rates must be positive")
        g = gcd(in_rate, out_rate)
        self.in_rate = in_rate
        self.out_rate = out_rate
        self.channels = channels
        self.up = out_rate // g      # L: 147 for 48000 -> 44100
        self.down = in_rate // g     # M: 160 for 48000 -> 44100
        self.identity = (self.up == 1 and self.down == 1)
        if self.identity:
            return

        big = max(self.up, self.down)
        half_len = half_len_factor * big
        h = firwin(2 * half_len + 1, 1.0 / big, window=("kaiser", 5.0)) * self.up
        # Polyphase bank: H[p, k] = h[k*up + p]. Output sample at upsampled
        # time t uses phase t % up and input index t // up, so
        #   y[t] = sum_k H[t % up, k] * x[t // up - k]
        taps = -(-len(h) // self.up)
        padded = np.zeros(taps * self.up, dtype=np.float32)
        padded[: len(h)] = h.astype(np.float32)
        self._bank = padded.reshape(taps, self.up).T.copy()   # (up, taps)
        self._k = np.arange(taps)
        self._taps = taps
        # History keeps the last taps-1 input frames so the first outputs of a
        # block can reach back across the block boundary.
        self._hist = np.zeros((taps - 1, channels), dtype=np.float32)
        # Next output time on the upsampled grid, relative to the start of the
        # (history + block) buffer. Starts so that x[t//up - k] never reaches
        # before the buffer: (taps-1)*up means index taps-1 with phase 0.
        self._t = (taps - 1) * self.up

    def process(self, block: np.ndarray) -> np.ndarray:
        block = np.asarray(block, dtype=np.float32)
        if block.ndim == 1:
            block = block[:, None]
        if self.identity or block.shape[0] == 0:
            return block

        buf = np.concatenate((self._hist, block), axis=0)
        n_buf = buf.shape[0]
        # Outputs whose input index stays inside the buffer:
        #   (t0 + n*down) // up <= n_buf - 1  <=>  t0 + n*down < n_buf*up
        count = max(0, -(-(n_buf * self.up - self._t) // self.down))
        if count == 0:
            out = np.zeros((0, self.channels), dtype=np.float32)
        else:
            t = self._t + np.arange(count, dtype=np.int64) * self.down
            idx = t // self.up
            phase = t % self.up
            # (count, taps, channels) gather, then per-output dot with its phase
            frames = buf[idx[:, None] - self._k[None, :]]
            out = np.einsum("nk,nkc->nc", self._bank[phase], frames).astype(np.float32, copy=False)
        # Keep the tail as history; shift the time origin with it
        keep = self._taps - 1
        consumed = n_buf - keep
        self._hist = buf[consumed:].copy()
        self._t = self._t + count * self.down - consumed * self.up
        return out


# Rates to try, in order, when the device refuses the pipeline rate. The
# device's own default rate is tried before this list.
FALLBACK_RATES = (48000, 96000, 88200, 32000, 22050, 16000)
