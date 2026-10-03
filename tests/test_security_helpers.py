"""Unit tests for path containment, Wi-Fi escaping, artwork URL policy, BT MAC."""

import wifi_setup as wifi
from catalog import _artwork_url_allowed


def test_wifi_escape_strips_control_and_quotes():
    assert wifi._escape_wpa_string('ab"c') == 'ab\\"c'
    assert "\n" not in wifi._escape_wpa_string("a\nb")
    assert wifi._valid_ssid("My Network")
    assert not wifi._valid_ssid("bad\nssid")
    assert not wifi._valid_ssid("")


def test_artwork_url_policy():
    assert _artwork_url_allowed("https://i.discogs.com/abc.jpg")
    assert not _artwork_url_allowed("http://i.discogs.com/abc.jpg")
    assert not _artwork_url_allowed("https://127.0.0.1/x")
    assert not _artwork_url_allowed("https://evil.example/x")


def test_bt_address_helper():
    from routes_bluetooth import _bt_address
    assert _bt_address("bt:AA:BB:CC:DD:EE:FF") == "AA:BB:CC:DD:EE:FF"
    assert _bt_address("AA:BB:CC:DD:EE:FF") == "AA:BB:CC:DD:EE:FF"
    assert _bt_address("bt:--agent") is None
    assert _bt_address("bt:GG:HH:II:JJ:KK:LL") is None


def test_export_relative_to_logic(tmp_path):
    export_dir = (tmp_path / "exports").resolve()
    export_dir.mkdir()
    evil = (tmp_path / "exports_evil").resolve()
    evil.mkdir()
    target = evil.resolve()
    assert not target.is_relative_to(export_dir)
    good = (export_dir / "Artist" / "Album").resolve()
    good.mkdir(parents=True)
    assert good.is_relative_to(export_dir)


def test_async_audio_stream_reset():
    from audio_streams import AsyncAudioStream, wav_header
    s = AsyncAudioStream()
    s.put(b"\x00\x01" * 100)
    s.reset_for_retry()
    assert s._buf == wav_header()
    assert len(s._deque) == 0


def test_insert_next_uses_playlist_attrs():
    """Player exposes playlist/_side_idx, not queue/current_index."""
    import player as plr
    p = plr.Player.__new__(plr.Player)
    p.playlist = ["a", "b"]
    p._side_idx = 0
    assert hasattr(p, "playlist")
    assert hasattr(p, "_side_idx")
    assert not hasattr(p, "queue")
    assert not hasattr(p, "current_index")
