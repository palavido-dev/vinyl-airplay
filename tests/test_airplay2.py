"""Unit tests for AirPlay 2 helpers (no live receivers required)."""

from pathlib import Path

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


def test_find_cliairplay_prefers_check(tmp_path, monkeypatch):
    # Point candidates at a fake binary that answers --check
    fake = tmp_path / "cliairplay"
    fake.write_text("#!/bin/sh\necho 'cliairplay v0 check'\n")
    fake.chmod(0o755)
    monkeypatch.setattr(ap2, "candidate_binary_paths", lambda: [fake])
    assert ap2.find_cliairplay() == str(fake)


def test_cmd_escape_flattens_newlines():
    assert "\n" not in ap2._cmd_escape("a\nb\rc")


def test_binary_asset_name_known():
    name = ap2._binary_asset_name()
    assert name is None or name.startswith("cliairplay-")
