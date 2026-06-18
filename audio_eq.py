#!/usr/bin/env python3
"""Vinyl AirPlay: biquad EQ (bass/treble shelves + 5-band parametric).

Extracted from main.py. Audio constants are kept local here (matching
player.py / recorder.py) so this module has no import back into main.
"""

import math
import threading
from typing import ClassVar

import numpy as np
from scipy.signal import lfilter

SAMPLE_RATE = 44100
CHANNELS    = 2


def _shelf_coeffs(freq, gain_db, shelf_type, fs=SAMPLE_RATE, Q=0.707):
    A      = 10 ** (gain_db / 40.0)
    w0     = 2 * math.pi * freq / fs
    alpha  = math.sin(w0) / (2 * Q)
    cos_w0 = math.cos(w0)
    if shelf_type == 'low':
        b0 =    A*((A+1)-(A-1)*cos_w0+2*math.sqrt(A)*alpha)
        b1 =  2*A*((A-1)-(A+1)*cos_w0)
        b2 =    A*((A+1)-(A-1)*cos_w0-2*math.sqrt(A)*alpha)
        a0 =      (A+1) +(A-1)*cos_w0+2*math.sqrt(A)*alpha
        a1 =  -2 *((A-1)+(A+1)*cos_w0)
        a2 =      (A+1) +(A-1)*cos_w0-2*math.sqrt(A)*alpha
    else:
        b0 =    A*((A+1)+(A-1)*cos_w0+2*math.sqrt(A)*alpha)
        b1 = -2*A*((A-1)+(A+1)*cos_w0)
        b2 =    A*((A+1)+(A-1)*cos_w0-2*math.sqrt(A)*alpha)
        a0 =      (A+1) -(A-1)*cos_w0+2*math.sqrt(A)*alpha
        a1 =   2 *((A-1)-(A+1)*cos_w0)
        a2 =      (A+1) -(A-1)*cos_w0-2*math.sqrt(A)*alpha
    b = np.array([b0,b1,b2], dtype=np.float64)/a0
    a = np.array([a0,a1,a2], dtype=np.float64)/a0
    return b, a


def _peak_coeffs(freq, gain_db, fs=SAMPLE_RATE, Q=1.0):
    """Peaking EQ biquad coefficients for a parametric band."""
    if gain_db == 0.0:
        return np.array([1.0, 0.0, 0.0]), np.array([1.0, 0.0, 0.0])
    A      = 10 ** (gain_db / 40.0)
    w0     = 2 * math.pi * freq / fs
    alpha  = math.sin(w0) / (2 * Q)
    cos_w0 = math.cos(w0)
    b0 =  1 + alpha * A
    b1 = -2 * cos_w0
    b2 =  1 - alpha * A
    a0 =  1 + alpha / A
    a1 = -2 * cos_w0
    a2 =  1 - alpha / A
    b = np.array([b0, b1, b2], dtype=np.float64) / a0
    a = np.array([a0, a1, a2], dtype=np.float64) / a0
    return b, a


def _apply_biquad(x, b, a, z):
    # Direct Form II transposed IIR with carried state. Identical math to the old
    # per-sample Python loop, but run in C by scipy's lfilter, which keeps EQ off
    # the audio callback's critical path on the Pi (that per-sample loop was the
    # main cause of input overflows when EQ was engaged).
    y, zf = lfilter(b, a, x, axis=0, zi=z)
    z[:] = zf
    return y


class EQ:
    BASS_FREQ = 250
    TREBLE_FREQ = 8000
    # Parametric band center frequencies
    BAND_FREQS: ClassVar[list[int]] = [60, 250, 1000, 3500, 10000]
    BAND_NAMES: ClassVar[list[str]] = ["sub", "low_mid", "mid", "upper_mid", "air"]

    def __init__(self, bass_db=0.0, treble_db=0.0, volume=80, bands=None):
        self._lock = threading.Lock()
        self._bass_db = bass_db
        self._treble_db = treble_db
        self._volume = int(np.clip(volume, 0, 100))
        # 5-band parametric: [sub_60, low_mid_250, mid_1k, upper_mid_3.5k, air_10k]
        self._bands = list(bands or [0.0, 0.0, 0.0, 0.0, 0.0])
        self._z_bass = np.zeros((2, CHANNELS))
        self._z_treble = np.zeros((2, CHANNELS))
        self._z_bands = [np.zeros((2, CHANNELS)) for _ in range(5)]
        self._b_bands = [None] * 5
        self._a_bands = [None] * 5
        self._update_coeffs()

    def _update_coeffs(self):
        self._b_bass,self._a_bass=_shelf_coeffs(self.BASS_FREQ,self._bass_db,'low')
        self._b_treble,self._a_treble=_shelf_coeffs(self.TREBLE_FREQ,self._treble_db,'high')
        for i, (freq, gain) in enumerate(zip(self.BAND_FREQS, self._bands, strict=False)):
            if i == 0:  # sub-bass: low shelf
                self._b_bands[i], self._a_bands[i] = _shelf_coeffs(freq, gain, 'low')
            elif i == 4:  # air: high shelf
                self._b_bands[i], self._a_bands[i] = _shelf_coeffs(freq, gain, 'high')
            else:  # mid bands: peaking
                self._b_bands[i], self._a_bands[i] = _peak_coeffs(freq, gain)

    def set_eq(self, bass_db, treble_db):
        with self._lock:
            if bass_db!=self._bass_db or treble_db!=self._treble_db:
                self._bass_db=float(np.clip(bass_db,-12,12))
                self._treble_db=float(np.clip(treble_db,-12,12))
                self._update_coeffs()
                self._z_bass = np.zeros((2, CHANNELS))
                self._z_treble = np.zeros((2, CHANNELS))

    def set_bands(self, bands: list):
        """Set 5-band parametric EQ gains. bands: list of 5 floats in dB."""
        with self._lock:
            changed = False
            for i in range(5):
                v = float(np.clip(bands[i] if i < len(bands) else 0, -12, 12))
                if v != self._bands[i]:
                    self._bands[i] = v
                    changed = True
            if changed:
                self._update_coeffs()
                self._z_bands = [np.zeros((2, CHANNELS)) for _ in range(5)]

    def set_volume(self, volume):
        with self._lock:
            self._volume=int(np.clip(volume,0,100))

    @property
    def values(self):
        return self._bass_db, self._treble_db, self._volume

    @property
    def band_values(self):
        return list(self._bands)

    def process(self, x):
        with self._lock:
            gain=self._volume/100.0
            flat_basic=(self._bass_db==0.0 and self._treble_db==0.0)
            flat_bands=all(b==0.0 for b in self._bands)
            if gain == 1.0 and flat_basic and flat_bands:
                return x
            x64=x.astype(np.float64)*gain
            if not flat_basic:
                x64=_apply_biquad(x64,self._b_bass,self._a_bass,self._z_bass)
                x64=_apply_biquad(x64,self._b_treble,self._a_treble,self._z_treble)
            if not flat_bands:
                for i in range(5):
                    if self._bands[i] != 0.0:
                        x64=_apply_biquad(x64,self._b_bands[i],self._a_bands[i],self._z_bands[i])
            return np.clip(x64,-1.0,1.0).astype(np.float32)
