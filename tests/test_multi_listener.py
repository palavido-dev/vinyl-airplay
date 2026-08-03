#!/usr/bin/env python3
"""Regression tests for issue #49: multiple simultaneous listeners.

Covers the three things that were broken or undefined:
  1. LiveMP3Broadcaster.stop() left its client registry populated, so
     /live.mp3 generators spun forever and the listener badge stuck.
  2. A browser stream abandoned after its first feed kept an ffmpeg alive
     until the whole stream or player stopped.
  3. A second device could not join audio already playing; it either got
     "Already streaming" or silently killed the first listener.

Run: python3 tests/test_multi_listener.py
"""

import asyncio
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from audio_mp3 import LiveMP3Broadcaster
from player import Player

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


# ── 1. Listener registry is cleared on stop ──────────────────────────────────

def test_stop_clears_clients():
    b = LiveMP3Broadcaster()
    b.enabled = True
    cid = b.register_client()
    assert cid is not None
    if b.listener_count() != 1:
        return f"expected 1 listener, got {b.listener_count()}"
    b.stop()
    # The badge must clear...
    if b.listener_count() != 0:
        return f"listener_count stuck at {b.listener_count()} after stop()"
    # ...and the in-flight generator must be told to exit (None, not b"")
    return eq(b.get_chunk(cid), None)


def test_cap_refuses_extra_clients():
    b = LiveMP3Broadcaster()
    b.enabled = False  # keep ffmpeg out of a unit test
    ids = [b.register_client() for _ in range(b.MAX_CLIENTS)]
    if any(i is None for i in ids):
        return "a client under the cap was refused"
    if b.register_client() is not None:
        return "a client over the cap was accepted"
    return eq(b.listener_count(), b.MAX_CLIENTS)


def test_unregister_frees_a_slot():
    b = LiveMP3Broadcaster()
    b.enabled = False
    ids = [b.register_client() for _ in range(b.MAX_CLIENTS)]
    b.unregister_client(ids[0])
    return eq(b.register_client() is not None, True)


# ── 2. Player sinks can be added and removed mid-playback ────────────────────

class FakeSink:
    def __init__(self):
        self.chunks = []

    def put(self, pcm):
        self.chunks.append(pcm)

    def stop(self):
        pass


def test_player_add_remove_stream():
    first = FakeSink()
    p = Player(eq=None, streams=[first])
    joiner = FakeSink()

    if p.add_stream(joiner) is not True:
        return "joining an idle player should succeed"
    if p.add_stream(joiner) is not False:
        return "joining twice should be refused"
    if list(p.streams) != [first, joiner]:
        return f"unexpected sink list: {p.streams}"

    # The original listener must survive the join
    for s in p.streams:
        s.put(b"pcm")
    if not (first.chunks and joiner.chunks):
        return "both sinks should receive audio after a join"

    if p.remove_stream(joiner) is not True:
        return "leaving should succeed"
    if p.remove_stream(joiner) is not False:
        return "leaving twice should be refused"
    return eq(list(p.streams), [first])


def test_player_sink_list_is_swapped_not_mutated():
    """The feed thread iterates self.streams directly, so a join must not
    mutate the list an iteration is already walking."""
    first = FakeSink()
    p = Player(eq=None, streams=[first])
    before = p.streams
    p.add_stream(FakeSink())
    if before is p.streams:
        return "add_stream mutated the list in place instead of swapping it"
    return eq(before, [first])


def test_concurrent_joins_are_serialised():
    p = Player(eq=None, streams=[])
    sinks = [FakeSink() for _ in range(25)]
    barrier = threading.Barrier(len(sinks))

    def join(s):
        barrier.wait()
        p.add_stream(s)

    threads = [threading.Thread(target=join, args=(s,)) for s in sinks]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    return eq(len(p.streams), len(sinks))


# ── 3. Joined capture sinks never outlive the stream they joined ─────────────

def test_capture_join_requires_running_capture():
    import streaming

    cap = streaming.CaptureManager()
    sink = FakeSink()
    # Nothing is capturing, so there is nothing to join
    if asyncio.run(cap.attach_extra(sink)) is not False:
        return "joining with no capture open should be refused"
    return eq(cap.joined, [])


def test_capture_join_and_leave():
    import streaming

    cap = streaming.CaptureManager()
    cap._stream = object()  # pretend capture is open
    a, b = FakeSink(), FakeSink()

    async def scenario():
        first = await cap.attach_extra(a)
        dup = await cap.attach_extra(a)
        second = await cap.attach_extra(b)
        left = await cap.detach_extra(a)
        return first, dup, second, left

    first, dup, second, left = asyncio.run(scenario())
    if not (first and second and left) or dup is not False:
        return f"attach/detach results wrong: {first},{dup},{second},{left}"
    if cap.joined != [b]:
        return f"expected only the second joiner to remain, got {cap.joined}"
    # A joined listener must NOT hold the capture device open on its own
    return eq(len(cap._tokens), 0)


def test_joined_sinks_do_not_hold_capture_open():
    """The refcount that keeps the ALSA device open is the token set. A
    joined listener is deliberately absent from it, so the capture still
    closes when the last real consumer detaches."""
    import streaming

    cap = streaming.CaptureManager()
    cap._stream = object()
    asyncio.run(cap.attach_extra(FakeSink()))
    return eq(bool(cap._tokens), False)


# ── 4. Browser stream reaping thresholds are sane ────────────────────────────

def test_reap_windows_are_ordered():
    from audio_streams import BrowserMP3Stream as B
    if not (B.WATCHDOG_TICK_SECS < B.STARTUP_GRACE_SECS < B.IDLE_REAP_SECS):
        return (f"tick={B.WATCHDOG_TICK_SECS} grace={B.STARTUP_GRACE_SECS} "
                f"idle={B.IDLE_REAP_SECS} should be increasing")
    # iOS reopens connections between tracks; too tight a window would reap a
    # listener that is merely reconnecting.
    return eq(B.IDLE_REAP_SECS >= 60, True)


def test_abandoned_stream_is_reaped():
    """Drive the real watchdog with the timers compressed."""
    from audio_streams import BrowserMP3Stream

    class Fast(BrowserMP3Stream):
        WATCHDOG_TICK_SECS = 0.05
        STARTUP_GRACE_SECS = 0.2
        IDLE_REAP_SECS = 0.3

        def __init__(self):
            # Skip ffmpeg entirely: this exercises the watchdog, not encoding
            self.stream_id = "test-stream-id"
            self._stop = threading.Event()
            self._fed = False
            self._clients = {}
            self._clients_lock = threading.Lock()
            self._client_seq = 0
            self._had_client = False
            self._last_client_at = time.monotonic()
            self._proc = None
            threading.Thread(target=self._watchdog, daemon=True).start()

    s = Fast()
    cid = s.register_client()   # a listener connects
    s.put(b"")                  # and audio starts flowing
    s._fed = True
    time.sleep(0.4)
    if s.is_stopped():
        return "a stream with a live listener was reaped"
    s.unregister_client(cid)    # listener goes away
    deadline = time.time() + 3
    while time.time() < deadline and not s.is_stopped():
        time.sleep(0.05)
    if not s.is_stopped():
        return "an abandoned stream was never reaped"
    return True


def main():
    check("stop() clears the listener registry", test_stop_clears_clients)
    check("listener cap refuses extra clients", test_cap_refuses_extra_clients)
    check("unregister frees a slot", test_unregister_frees_a_slot)
    check("player sinks can join and leave", test_player_add_remove_stream)
    check("joining swaps the sink list, never mutates", test_player_sink_list_is_swapped_not_mutated)
    check("concurrent joins do not lose sinks", test_concurrent_joins_are_serialised)
    check("cannot join when nothing is capturing", test_capture_join_requires_running_capture)
    check("capture sinks can join and leave", test_capture_join_and_leave)
    check("joined sinks never hold the capture open", test_joined_sinks_do_not_hold_capture_open)
    check("reap windows are ordered and iOS-safe", test_reap_windows_are_ordered)
    check("abandoned browser stream is reaped", test_abandoned_stream_is_reaped)

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
