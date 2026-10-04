"""Unit tests for AirPlay 2 helpers (no live receivers required)."""

from pathlib import Path
from unittest.mock import MagicMock

import transports_airplay2 as ap2


def test_parse_features_and_ap2_bits():
    # SupportsUnifiedMediaControl (bit 38) → high word 0x40
    assert ap2.supports_airplay2("0x0,0x40")
    # SupportsCoreUtilsPairingAndEncryption (bit 48) → high word 0x10000
    assert ap2.supports_airplay2("0x0,0x10000")
    assert not ap2.supports_airplay2("0x200")
    assert not ap2.supports_airplay2(None)
    assert not ap2.supports_airplay2("not-a-number")


def test_supports_ptp():
    # SupportsPTP = bit 45 → high word 0x2000
    assert ap2.supports_ptp("0x0,0x2000")
    assert not ap2.supports_ptp("0x200")


def test_supports_buffered_audio():
    # SupportsBufferedAudio = bit 40 → high word 0x100
    assert ap2.supports_buffered_audio("0x0,0x100")
    assert not ap2.supports_buffered_audio("0x0,0x40")


def test_serialize_txt_skips_whitespace():
    txt = ap2.serialize_txt({
        "features": "0x1",
        "model": "AudioAccessory5,1",
        "bad key": "x",
        "ok": "no spaces",
        "skip": "has space",
    })
    assert "features=0x1" in txt
    assert "model=AudioAccessory5,1" in txt
    assert "bad key" not in txt
    assert "skip=" not in txt


def test_stable_dacp_and_active_remote(tmp_path, monkeypatch):
    monkeypatch.setattr(ap2, "CREDENTIALS_FILE", tmp_path / "creds.json")
    dacp = ap2.stable_dacp_id("AABBCCDDEEFF001122")
    assert len(dacp) == 12
    assert ap2.active_remote_id(dacp).isdigit()
    # Same id → stable
    assert ap2.stable_dacp_id("AABBCCDDEEFF001122") == dacp


def test_credentials_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(ap2, "CREDENTIALS_FILE", tmp_path / "creds.json")
    auth = "a" * 192
    ap2.set_credentials(
        "dev1", auth=auth, dacp_id="ABCDEF123456",
        address="192.168.1.10", port=7000, name="Living Room",
    )
    got = ap2.get_credentials("dev1")
    assert got["auth"] == auth
    assert got["dacp_id"] == "ABCDEF123456"
    assert ap2.stable_dacp_id("dev1") == "ABCDEF123456"
    assert ap2.get_credentials("missing") is None


def test_credentials_file_is_absolute():
    assert ap2.CREDENTIALS_FILE.is_absolute()
    assert ap2.CREDENTIALS_FILE.name == "airplay2_credentials.json"


def test_find_cliairplay_prefers_check(tmp_path, monkeypatch):
    # Point candidates at a fake binary that answers --check
    fake = tmp_path / "cliairplay"
    fake.write_text("#!/bin/sh\necho 'cliairplay v0 check'\n")
    fake.chmod(0o755)
    monkeypatch.setattr(ap2, "candidate_binary_paths", lambda: [fake])
    monkeypatch.setattr(ap2, "binary_has_ptp_cap", lambda _p: False)
    assert ap2.find_cliairplay() == str(fake)


def test_find_cliairplay_prefers_setcap_binary(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    capped = tmp_path / "capped"
    for p in (plain, capped):
        p.write_text("#!/bin/sh\necho 'cliairplay v0 check'\n")
        p.chmod(0o755)
    monkeypatch.setattr(ap2, "candidate_binary_paths", lambda: [plain, capped])
    monkeypatch.setattr(
        ap2, "binary_has_ptp_cap", lambda p: Path(p) == capped
    )
    assert ap2.find_cliairplay() == str(capped)


def test_cmd_escape_flattens_newlines():
    assert "\n" not in ap2._cmd_escape("a\nb\rc")


def test_binary_asset_name_known():
    name = ap2._binary_asset_name()
    assert name is None or name.startswith("cliairplay-")


def _fake_apple_tv(identifier="atv1", name="Living Room TV", has_raop=True):
    conf = MagicMock()
    conf.identifier = identifier
    conf.name = name
    conf.address = "192.168.1.50"
    services = {}

    def get_service(proto):
        return services.get(proto)

    conf.get_service.side_effect = get_service
    if has_raop:
        from pyatv.const import Protocol
        raop = MagicMock()
        raop.port = 7000
        raop.properties = {"ft": "0x0,0x40"}
        services[Protocol.RAOP] = raop
        airplay = MagicMock()
        airplay.port = 7000
        airplay.properties = {"features": "0x0,0x2040", "model": "AppleTV14,1"}
        services[Protocol.AirPlay] = airplay
    return conf


def test_resolve_falls_back_to_raop_without_ptp(monkeypatch):
    conf = _fake_apple_tv()
    monkeypatch.setattr(ap2, "find_cliairplay", lambda: "/bin/fake-cliairplay")
    monkeypatch.setattr(ap2, "conf_is_apple_tv", lambda _c: True)
    monkeypatch.setattr(ap2, "conf_is_homepod", lambda _c: False)
    monkeypatch.setattr(ap2, "conf_has_raop", lambda _c: True)
    monkeypatch.setattr(ap2, "get_credentials", lambda _id: {"auth": "a" * 192})
    monkeypatch.setattr(
        ap2.PtpDaemon, "acquire", classmethod(lambda cls, _b: False)
    )
    monkeypatch.setattr(ap2.PtpDaemon, "release", classmethod(lambda cls: None))

    raop, streams = ap2.resolve_airplay_outputs(
        [{"id": "atv1", "name": "Living Room TV"}],
        [conf],
        volume=50,
        binary="/bin/fake-cliairplay",
    )
    assert streams == []
    assert raop == [conf]


def test_resolve_falls_back_to_raop_when_unpaired(monkeypatch):
    conf = _fake_apple_tv()
    monkeypatch.setattr(ap2, "find_cliairplay", lambda: "/bin/fake-cliairplay")
    monkeypatch.setattr(ap2, "conf_is_apple_tv", lambda _c: True)
    monkeypatch.setattr(ap2, "conf_is_homepod", lambda _c: False)
    monkeypatch.setattr(ap2, "conf_has_raop", lambda _c: True)
    monkeypatch.setattr(ap2, "get_credentials", lambda _id: None)
    monkeypatch.setattr(
        ap2.PtpDaemon, "acquire", classmethod(lambda cls, _b: True)
    )
    monkeypatch.setattr(ap2.PtpDaemon, "release", classmethod(lambda cls: None))

    raop, streams = ap2.resolve_airplay_outputs(
        [{"id": "atv1"}],
        [conf],
        volume=50,
        binary="/bin/fake-cliairplay",
    )
    assert streams == []
    assert raop == [conf]


def test_build_stream_requires_auth_for_apple(monkeypatch):
    conf = _fake_apple_tv()
    monkeypatch.setattr(ap2, "conf_is_apple_tv", lambda _c: True)
    monkeypatch.setattr(ap2, "conf_is_homepod", lambda _c: False)
    monkeypatch.setattr(ap2, "get_credentials", lambda _id: None)
    monkeypatch.setattr(ap2, "conf_airplay_properties", lambda _c: {})
    monkeypatch.setattr(ap2, "conf_features_value", lambda _c: "0x0,0x2040")
    monkeypatch.setattr(ap2, "conf_airplay_port", lambda _c: 7000)
    try:
        ap2.build_stream_for_conf(
            conf, volume=50, binary="/bin/fake", ptp_available=True
        )
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "pairing" in str(e).lower() or "HAP" in str(e)


def test_build_stream_does_not_force_buffered_without_feature(monkeypatch):
    conf = _fake_apple_tv()
    monkeypatch.setattr(ap2, "conf_is_apple_tv", lambda _c: True)
    monkeypatch.setattr(ap2, "conf_is_homepod", lambda _c: False)
    monkeypatch.setattr(
        ap2, "get_credentials",
        lambda _id: {"auth": "b" * 192, "dacp_id": "ABCDEF123456"},
    )
    monkeypatch.setattr(ap2, "conf_airplay_properties", lambda _c: {
        "features": "0x0,0x2040",  # AP2 + PTP, no buffered bit
    })
    monkeypatch.setattr(ap2, "conf_features_value", lambda _c: "0x0,0x2040")
    monkeypatch.setattr(ap2, "conf_airplay_port", lambda _c: 7000)
    stream = ap2.build_stream_for_conf(
        conf, volume=50, binary="/bin/fake", ptp_available=True
    )
    assert stream.force_buffered is False
    assert stream.timing == "ptp"
    assert stream.require_ptp is True
    assert stream.auth == "b" * 192
