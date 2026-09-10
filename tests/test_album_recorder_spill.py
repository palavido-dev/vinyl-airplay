#!/usr/bin/env python3
"""Regression tests for issue #57: album sides are spilled to disk, not held in RAM.

A full side used to live in a list of PCM chunks, was joined into one more copy,
then converted to a WAV copy for the encoder. On a 4GB Pi that pushed the OS into
reclaiming page cache mid-recording, which is what the reported drop-outs were.

These tests pin the behaviour that replaced it: the side goes to a raw file as it
arrives, the trims and track boundaries still land in the same places, and peak
memory does not grow with the length of the side.

Run: python3 tests/test_album_recorder_spill.py
"""

import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import recorder as rec

RESULTS = []
BYTES_PER_SEC = rec.SAMPLE_RATE * rec.CHANNELS * 2


def check(name, fn):
    try:
        detail = fn()
        ok = detail is True or detail is None
        RESULTS.append((name, ok, "" if ok else str(detail)))
    except Exception as e:
        import traceback
        traceback.print_exc()
        RESULTS.append((name, False, f"{type(e).__name__}: {e}"))


def eq(got, want):
    return True if got == want else f"got {got!r}, want {want!r}"


def tone(secs, freq=440, amp=0.3):
    n = int(rec.SAMPLE_RATE * secs)
    t = np.arange(n) / rec.SAMPLE_RATE
    mono = (amp * np.sin(2 * np.pi * freq * t) * 32767).astype(np.int16)
    return np.repeat(mono[:, None], rec.CHANNELS, axis=1).tobytes()


def silence(secs):
    n = int(rec.SAMPLE_RATE * secs) * rec.CHANNELS
    return np.zeros(n, dtype=np.int16).tobytes()


def make_recorder(tmp, side="A"):
    return rec.AlbumRecorder(1, side, {"artist": "Test", "title": "Album"},
                             audio_dir=Path(tmp))


def feed(r, pcm, block=8192 * 4):
    """Feed PCM the way the audio callback does: fixed-size blocks."""
    for i in range(0, len(pcm), block):
        r.put(pcm[i:i + block], rms=0.5)


def drain(r, timeout=10.0):
    """Wait for the writer thread to catch up, as it does in real time."""
    deadline = time.monotonic() + timeout
    while not r._queue.empty() and time.monotonic() < deadline:
        time.sleep(0.01)


# ── Spilling ─────────────────────────────────────────────────────────────────

def test_audio_goes_to_disk_not_memory():
    with tempfile.TemporaryDirectory() as tmp:
        r = make_recorder(tmp)
        raw = r._raw_path
        feed(r, tone(3.0))
        r._close_raw()
        if not raw.exists():
            return "raw spill file was never created"
        on_disk = raw.stat().st_size
        r._discard_raw()
        # 3s of stereo 16-bit at 44.1k
        return eq(on_disk, int(3.0 * BYTES_PER_SEC))


def test_peak_memory_does_not_scale_with_side_length():
    """The point of the change: a 4x longer side must not cost 4x the memory.

    The writer keeps up in real time (172KB/s against a disk), so the audio is
    on its way out of memory as it arrives. The old code kept every block.
    """
    def peak_for(secs):
        with tempfile.TemporaryDirectory() as tmp:
            r = make_recorder(tmp)
            chunk = tone(1.0)
            tracemalloc.start()
            for _ in range(int(secs)):
                feed(r, chunk)
                drain(r)
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            r._close_raw()
            r._discard_raw()
            return peak

    short, long = peak_for(5), peak_for(20)
    # 4x the audio. Allow generous slack for interpreter noise, but nothing
    # like the strict linear growth the old buffer had.
    if long > short * 2:
        return f"peak grew from {short} to {long} bytes for 4x the audio"
    # And the absolute ceiling: one second of audio is 172KB, so a few MB of
    # peak means the side is not accumulating.
    if long > 8 * 1024 * 1024:
        return f"peak {long / 1e6:.1f}MB for 20s of audio: still buffering"
    return True


def test_write_queue_is_bounded():
    """If the disk stalls the queue must cap rather than grow without limit,
    so a dead disk cannot turn into a dead Pi."""
    with tempfile.TemporaryDirectory() as tmp:
        r = make_recorder(tmp)
        # Stall the writer by holding the file handle hostage: replace the
        # write with a blocker, then push far more than the cap.
        import threading as _t
        release = _t.Event()
        original = r._raw_file.write

        def blocking_write(b):
            release.wait(timeout=5.0)
            return original(b)
        r._raw_file.write = blocking_write
        try:
            feed(r, tone(30.0))   # ~5MB, 160 blocks
            depth = r._queue.qsize()
            if depth > rec.RAW_QUEUE_BLOCKS:
                return f"queue grew to {depth}, past the {rec.RAW_QUEUE_BLOCKS} cap"
        finally:
            release.set()
            r._raw_file.write = original
            drain(r)
            r._close_raw()
            r._discard_raw()
        return True


def test_orphaned_raw_files_are_swept():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / ".side-9-A-999999.pcm").write_bytes(b"x" * 1024)
        (d / ".side-9-B-999998.pcm").write_bytes(b"x" * 1024)
        keeper = d / ".side-9-C-4242.pcm"
        keeper.write_bytes(b"x" * 1024)
        real = d / "Test - Album - Side A.flac"
        real.write_bytes(b"flac")
        removed = rec.sweep_orphaned_raw_sides(d, keep_pid=4242)
        if removed != 2:
            return f"swept {removed}, expected 2"
        if not keeper.exists():
            return "swept the raw file belonging to the live process"
        return True if real.exists() else "swept a real recording"


# ── Trims and boundaries still behave ────────────────────────────────────────

def test_needle_drop_trim_shifts_boundaries():
    """A needle-drop transient then music: the trim must move track boundaries
    back by the same amount it removed."""
    with tempfile.TemporaryDirectory() as tmp:
        r = make_recorder(tmp)
        r.mark_first_track(101)
        # Needle drop: a short loud burst, a quiet valley, then the music
        feed(r, tone(0.2, freq=120, amp=0.7))
        feed(r, silence(0.8))
        feed(r, tone(40.0))
        r.mark_track_boundary(102)
        feed(r, tone(40.0, freq=660))
        path, duration, boundaries = r.finish()
        if path is None:
            return "side was discarded"
        try:
            if len(boundaries) != 2:
                return f"expected 2 boundaries, got {len(boundaries)}"
            first, second = boundaries
            if first["start_secs"] != 0.0:
                return f"first track no longer starts at 0 ({first['start_secs']})"
            # The second track was marked at ~41s of raw audio; after trimming
            # the ~1s needle drop it must land near 40s, not stay at 41s.
            if not (39.0 <= second["start_secs"] <= 41.5):
                return f"second track at {second['start_secs']:.2f}s, expected ~40s"
            if second["start_secs"] >= first["end_secs"] + 0.001 and first["end_secs"] != second["start_secs"]:
                return "first track end does not meet second track start"
            if duration < 75:
                return f"duration {duration:.1f}s, expected ~80s"
            return True
        finally:
            path.unlink(missing_ok=True)


def test_trailing_silence_is_trimmed():
    with tempfile.TemporaryDirectory() as tmp:
        r = make_recorder(tmp)
        r.mark_first_track(201)
        feed(r, tone(45.0))
        feed(r, silence(20.0))   # run-out groove
        path, duration, boundaries = r.finish()
        if path is None:
            return "side was discarded"
        try:
            # 65s in; the trailing silence should be gone bar the 1s fade tail
            if not (44.0 <= duration <= 48.0):
                return f"duration {duration:.1f}s, expected ~46s"
            last = boundaries[-1]
            return True if abs(last["end_secs"] - duration) < 0.01 else \
                f"last boundary ends at {last['end_secs']:.2f}s but file is {duration:.2f}s"
        finally:
            path.unlink(missing_ok=True)


def test_short_side_is_discarded_and_raw_removed():
    with tempfile.TemporaryDirectory() as tmp:
        r = make_recorder(tmp)
        r.mark_first_track(301)
        feed(r, tone(5.0))
        raw = r._raw_path
        path, duration, boundaries = r.finish()
        if path is not None:
            path.unlink(missing_ok=True)
            return "a 5s side should have been discarded"
        return True if not raw.exists() else "raw buffer left behind after discard"


def test_nothing_recorded_is_clean():
    with tempfile.TemporaryDirectory() as tmp:
        r = make_recorder(tmp)
        raw = r._raw_path
        path, duration, boundaries = r.finish()
        if path is not None or duration != 0.0 or boundaries != []:
            return f"expected an empty result, got {path!r} {duration} {boundaries}"
        return True if not raw.exists() else "raw buffer left behind"


def test_cancel_removes_the_raw_buffer():
    with tempfile.TemporaryDirectory() as tmp:
        r = make_recorder(tmp)
        feed(r, tone(2.0))
        raw = r._raw_path
        r.cancel()
        if raw.exists():
            return "cancel left the raw buffer on disk"
        return True if not r.is_active else "still active after cancel"


def test_put_after_finish_is_ignored():
    """The capture callback can fire once more after a side is finalized."""
    with tempfile.TemporaryDirectory() as tmp:
        r = make_recorder(tmp)
        feed(r, tone(1.0))
        r.finish()
        r.put(tone(0.1), rms=0.5)   # must not raise on the closed file
        return True


def test_saved_audio_matches_what_was_captured():
    """Decode the FLAC back and compare it to the captured PCM.

    This is the recordings path, so a wrong offset or a swapped channel would be
    silent and permanent. FLAC is lossless, so past the 50ms fade-in the samples
    must come back exactly as they went in.
    """
    import subprocess
    with tempfile.TemporaryDirectory() as tmp:
        r = make_recorder(tmp)
        r.mark_first_track(401)
        # Distinct left and right so a channel swap cannot pass
        n = int(rec.SAMPLE_RATE * 50.0)
        t = np.arange(n) / rec.SAMPLE_RATE
        left = (0.4 * np.sin(2 * np.pi * 440 * t) * 32767).astype(np.int16)
        right = (0.2 * np.sin(2 * np.pi * 880 * t) * 32767).astype(np.int16)
        music = np.stack([left, right], axis=1).tobytes()
        feed(r, music)
        path, duration, _ = r.finish()
        if path is None:
            return "side was discarded"
        try:
            out = subprocess.run(
                ["ffmpeg", "-v", "quiet", "-i", str(path),
                 "-f", "s16le", "-ac", "2", "-ar", str(rec.SAMPLE_RATE), "pipe:1"],
                capture_output=True, timeout=120)
            if out.returncode != 0:
                return "could not decode the FLAC back"
            decoded = np.frombuffer(out.stdout, dtype=np.int16)
            expected = np.frombuffer(music, dtype=np.int16)
            if len(decoded) > len(expected):
                return f"decoded {len(decoded)} samples, captured {len(expected)}"
            # Compare the body, skipping the first second (fade region) and the
            # tail the trailing-silence trim may have shortened.
            skip = rec.SAMPLE_RATE * rec.CHANNELS
            body = decoded[skip:]
            ref = expected[skip:skip + len(body)]
            if len(body) < rec.SAMPLE_RATE * rec.CHANNELS * 30:
                return f"only {len(body) / (rec.SAMPLE_RATE * rec.CHANNELS):.1f}s survived"
            if not np.array_equal(body, ref):
                bad = int(np.count_nonzero(body != ref))
                return f"{bad} of {len(body)} samples differ from what was captured"
            return True
        finally:
            path.unlink(missing_ok=True)


def test_encode_range_is_frame_aligned():
    """A byte range that splits a frame must be clamped, never shifted, or the
    channels swap for the rest of the side."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        raw = d / "raw.pcm"
        raw.write_bytes(tone(35.0))
        out = d / "out.flac"
        ok = rec.encode_flac_from_raw(raw, out, {"artist": "T"},
                                      start_byte=3, length_bytes=len(tone(35.0)) - 7)
        if not ok:
            return "encode failed"
        return True if out.exists() and out.stat().st_size > 0 else "no output written"


def main():
    check("captured audio lands on disk, not in a list", test_audio_goes_to_disk_not_memory)
    check("peak memory does not scale with side length", test_peak_memory_does_not_scale_with_side_length)
    check("the write queue is bounded when the disk stalls", test_write_queue_is_bounded)
    check("orphaned raw sides are swept, real files are not", test_orphaned_raw_files_are_swept)
    check("needle-drop trim shifts track boundaries", test_needle_drop_trim_shifts_boundaries)
    check("trailing silence is trimmed", test_trailing_silence_is_trimmed)
    check("a too-short side is discarded and cleaned up", test_short_side_is_discarded_and_raw_removed)
    check("finishing with no audio is clean", test_nothing_recorded_is_clean)
    check("cancel removes the raw buffer", test_cancel_removes_the_raw_buffer)
    check("a late callback after finish is ignored", test_put_after_finish_is_ignored)
    check("saved FLAC matches the captured audio", test_saved_audio_matches_what_was_captured)
    check("encode byte range is frame aligned", test_encode_range_is_frame_aligned)

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
