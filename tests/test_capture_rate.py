#!/usr/bin/env python3
"""Regression tests for issue #76: capture devices that refuse 44100 Hz.

A USB dongle that only opens at 48000 used to fail every stream start with
"Invalid sample rate [PaErrorCode -9997]". The capture path now negotiates
the rate the card accepts and resamples back to the pipeline rate.

Run: python3 tests/test_capture_rate.py
"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sounddevice as sd

import streaming
from audio_resample import StreamResampler

RESULTS = []


def check(name, fn):
    try:
        detail = fn()
        ok = detail is True or detail is None
        RESULTS.append((name, ok, "" if ok else str(detail)))
    except Exception as e:
        RESULTS.append((name, False, f"{type(e).__name__}: {e}"))


def eq(got, want):
    return True if got == want else f"got {got!r}, want {want!r}"


# ── Resampler ────────────────────────────────────────────────────────────────

def _tone(rate, secs, freq, channels=2):
    t = np.arange(int(rate * secs)) / rate
    x = 0.5 * np.sin(2 * np.pi * freq * t).astype(np.float32)
    return np.repeat(x[:, None], channels, axis=1)


def test_resampler_length_has_no_drift():
    x = _tone(48000, 5.0, 1000)
    r = StreamResampler(48000, 44100, 2)
    out = np.concatenate([r.process(x[i:i + 8192]) for i in range(0, len(x), 8192)])
    return eq(len(out), int(len(x) * 44100 / 48000))


def test_resampler_is_seamless_across_blocks():
    """Chopping the input into arbitrary block sizes must give the same
    signal as one big block: no clicks at the seams."""
    x = _tone(48000, 2.0, 3000)
    whole = StreamResampler(48000, 44100, 2).process(x)
    r = StreamResampler(48000, 44100, 2)
    rng = np.random.default_rng(76)
    parts, i = [], 0
    while i < len(x):
        k = int(rng.integers(1, 6000))
        parts.append(r.process(x[i:i + k]))
        i += k
    chopped = np.concatenate(parts)
    n = min(len(whole), len(chopped))
    diff = float(np.max(np.abs(whole[:n] - chopped[:n])))
    return True if diff < 1e-5 else f"seam error {diff}"


def test_resampler_preserves_tone():
    """A 5 kHz tone at 48k comes out as a 5 kHz tone at 44.1k at the same level."""
    x = _tone(48000, 3.0, 5000, channels=1)
    y = StreamResampler(48000, 44100, 1).process(x)[44100:44100 * 3, 0]
    spec = np.abs(np.fft.rfft(y * np.hanning(len(y))))
    freqs = np.fft.rfftfreq(len(y), 1 / 44100)
    peak = float(freqs[spec.argmax()])
    level = float(np.sqrt(np.mean(y ** 2)))
    if abs(peak - 5000) > 2:
        return f"peak at {peak} Hz"
    return True if abs(level - 0.5 / np.sqrt(2)) < 0.01 else f"rms {level}"


def test_resampler_identity_passthrough():
    x = _tone(44100, 0.1, 440)
    return eq(StreamResampler(44100, 44100, 2).process(x).shape, x.shape)


# ── Rate negotiation (sounddevice stubbed) ───────────────────────────────────

class _FakeSD:
    """Stand-in for the sounddevice module: a device that accepts only `rates`."""

    PortAudioError = sd.PortAudioError

    def __init__(self, rates, default=48000):
        self.rates = set(rates)
        self.default = default
        self.probed = []

    def check_input_settings(self, device=None, samplerate=None, channels=None, dtype=None):
        self.probed.append(samplerate)
        if samplerate not in self.rates:
            raise sd.PortAudioError("Invalid sample rate [PaErrorCode -9997]")

    def query_devices(self, device=None, kind=None):
        return {"default_samplerate": float(self.default), "max_input_channels": 2}


def _with_fake_sd(fake, fn):
    real = streaming.sd
    streaming.sd = fake
    streaming._capture_rates.clear()
    try:
        return fn()
    finally:
        streaming.sd = real
        streaming._capture_rates.clear()


def test_negotiate_prefers_pipeline_rate():
    fake = _FakeSD(rates={44100, 48000})
    rate = _with_fake_sd(fake, lambda: streaming._negotiate_capture_rate(3, 2))
    return eq((rate, fake.probed), (44100, [44100]))


def test_negotiate_falls_back_to_native_rate():
    fake = _FakeSD(rates={48000}, default=48000)
    rate = _with_fake_sd(fake, lambda: streaming._negotiate_capture_rate(3, 2))
    return eq(rate, 48000)


def test_negotiate_tries_common_rates_when_default_lies():
    """Some drivers report a default rate they will not actually open."""
    fake = _FakeSD(rates={96000}, default=44100)
    rate = _with_fake_sd(fake, lambda: streaming._negotiate_capture_rate(3, 2))
    return eq(rate, 96000)


def test_negotiate_result_is_cached():
    fake = _FakeSD(rates={48000})

    def run():
        a = streaming._negotiate_capture_rate(3, 2)
        n = len(fake.probed)
        b = streaming._negotiate_capture_rate(3, 2)
        return (a, b, len(fake.probed) - n)
    return eq(_with_fake_sd(fake, run), (48000, 48000, 0))


def test_negotiate_gives_up_gracefully():
    """No probe passes: return the pipeline rate so the open reports the real
    PortAudio error rather than masking it."""
    fake = _FakeSD(rates=set())
    rate = _with_fake_sd(fake, lambda: streaming._negotiate_capture_rate(3, 2))
    return eq(rate, 44100)


def test_resampling_callback_delivers_pipeline_rate():
    """The wrapped callback must hand downstream 44100-rate audio: 48000
    frames in per second means 44100 frames out per second."""
    got = []
    inner = lambda data, frames, t, status: got.append(frames)  # noqa: E731
    cb = streaming._resampling_callback(inner, StreamResampler(48000, 44100, 2))
    block = np.zeros((8192, 2), dtype=np.float32)
    for _ in range(60):   # 60 * 8192 = 491520 frames = 10.24 s at 48 kHz
        cb(block, 8192, None, None)
    return eq(sum(got), int(60 * 8192 * 44100 / 48000))


def main():
    check("resampler output length has no drift", test_resampler_length_has_no_drift)
    check("resampler is seamless across block boundaries", test_resampler_is_seamless_across_blocks)
    check("resampler preserves a tone's pitch and level", test_resampler_preserves_tone)
    check("resampler is a passthrough at equal rates", test_resampler_identity_passthrough)
    check("negotiation prefers 44100 when supported", test_negotiate_prefers_pipeline_rate)
    check("negotiation falls back to the card's native rate", test_negotiate_falls_back_to_native_rate)
    check("negotiation tries common rates when the default lies", test_negotiate_tries_common_rates_when_default_lies)
    check("negotiated rate is cached per device", test_negotiate_result_is_cached)
    check("negotiation returns 44100 when nothing probes clean", test_negotiate_gives_up_gracefully)
    check("resampling callback delivers 44100-rate frame counts", test_resampling_callback_delivers_pipeline_rate)

    failed = 0
    for name, ok, detail in RESULTS:
        if not ok:
            failed += 1
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    print(f"\nAll {len(RESULTS)} checks passed" if not failed
          else f"\n{failed} of {len(RESULTS)} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
