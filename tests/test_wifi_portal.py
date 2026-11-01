"""Wi-Fi portal: already-online Pi must open the player, not the setup form.

The setup page used to stay bound to port 80 because iwconfig always prints
"no wireless extensions" for lo/eth0 (and often "Not-Associated" for a second
iface). Connect then crashed with Errno 2 because Raspberry Pi OS no longer
ships dhclient. Skip sent the browser to the laptop's localhost.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

import wifi_setup as wifi

# The portal must not try to start hostapd while these tests boot the app.
os.environ["VINYL_WIFI_DISABLE_HOTSPOT"] = "1"

IWCONFIG_ONLINE = """\
wlan0     IEEE 802.11  ESSID:"TheRanch"
          Mode:Managed  Frequency:5.18 GHz  Access Point: AA:BB:CC:DD:EE:FF

p2p-dev-wlan0  IEEE 802.11  ESSID:off/any
          Mode:Managed  Access Point: Not-Associated

lo        no wireless extensions.

eth0      no wireless extensions.
"""

IWCONFIG_OFFLINE = """\
wlan0     IEEE 802.11  ESSID:off/any
          Mode:Managed  Access Point: Not-Associated

lo        no wireless extensions.

eth0      no wireless extensions.
"""

NM_ONLINE = (
    "wifi:connected:TheRanch\n"
    "ethernet:unavailable:\n"
    "loopback:connected (externally):lo\n"
)
NM_OFFLINE = "wifi:disconnected:\nloopback:connected (externally):lo\n"


def test_iwconfig_noise_does_not_hide_a_real_association():
    # The old check was: "not-associated" not in output and "no wireless" not in output.
    # Both strings are in this output while wlan0 is on TheRanch.
    assert "not-associated" in IWCONFIG_ONLINE.lower()
    assert "no wireless" in IWCONFIG_ONLINE.lower()
    assert wifi._iwconfig_has_association(IWCONFIG_ONLINE) is True
    assert wifi._iwconfig_has_association(IWCONFIG_OFFLINE) is False


def test_nmcli_wifi_connected_ignores_loopback():
    assert wifi._nmcli_wifi_connected(NM_ONLINE) is True
    assert wifi._nmcli_wifi_connected(NM_OFFLINE) is False
    hotspot = "wifi:connected:VinylStreamer-Setup\n"
    assert wifi._nmcli_wifi_connected(hotspot) is False


def test_hidden_null_ssid_is_not_a_network():
    assert wifi._usable_scan_ssid("TheRanch") is True
    assert wifi._usable_scan_ssid("TheCasita") is True
    # iwlist prints a corrupt BSS as the literal characters \x00 repeated.
    assert wifi._usable_scan_ssid("\\x00" * 8) is False
    assert wifi._usable_scan_ssid("\\x00\\x00") is False
    assert wifi._usable_scan_ssid("") is False
    assert wifi._usable_scan_ssid("VinylStreamer-Setup") is False


def test_check_wifi_uses_iwconfig_when_nmcli_is_absent():
    def fake_run(argv, timeout=5):
        if argv == ["iwconfig"]:
            return IWCONFIG_ONLINE
        return None

    with patch.object(wifi, "_run_text", fake_run):
        assert wifi.check_wifi_connection() is True


def test_default_route_counts_as_already_online():
    def fake_run(argv, timeout=5):
        if argv[:4] == ["ip", "-4", "route", "show"]:
            return "default via 192.168.50.1 dev wlan0 proto dhcp\n"
        return None

    with patch.object(wifi, "_run_text", fake_run):
        assert wifi.has_usable_network() is True


def test_no_route_and_no_association_is_offline():
    def fake_run(argv, timeout=5):
        if argv == ["iwconfig"]:
            return IWCONFIG_OFFLINE
        return ""

    with (
        patch.object(wifi, "_run_text", fake_run),
        patch.object(wifi, "_read_proc_net_route", lambda: "Iface Destination\neth0 00001EAC 00000000 0001\n"),
    ):
        assert wifi.check_wifi_connection() is False
        assert wifi.has_usable_network() is False


def test_proc_net_route_counts_when_ip_is_missing():
    table = (
        "Iface\tDestination\tGateway\tFlags\n"
        "enp0s3\t00000000\t01001EAC\t0003\n"
        "docker0\t000011AC\t00000000\t0001\n"
    )
    assert wifi._proc_net_route_has_default(table) is True
    assert wifi._proc_net_route_has_default("Iface Destination\neth0 00001EAC 00000000 0001\n") is False
    with (
        patch.object(wifi, "_run_text", lambda argv, timeout=5: None),
        patch.object(wifi, "_read_proc_net_route", lambda: table),
    ):
        assert wifi.has_usable_network() is True


def test_player_redirect_stays_on_the_host_you_opened():
    assert wifi.player_redirect_url("vinyl.local") == "http://vinyl.local:8080/"
    assert wifi.player_redirect_url("vinyl.local:80") == "http://vinyl.local:8080/"
    assert wifi.player_redirect_url("192.168.50.20") == "http://192.168.50.20:8080/"
    assert wifi.player_redirect_url("[fe80::1]") == "http://[fe80::1]:8080/"
    assert wifi.player_redirect_url("evil.example/steal") == "http://127.0.0.1:8080/"
    assert wifi.player_redirect_url("") == "http://127.0.0.1:8080/"


def test_online_root_redirects_to_the_player():
    with (
        patch.object(wifi, "should_show_setup_page", lambda: False),
        TestClient(wifi.app) as client,
    ):
        response = client.get(
            "/",
            headers={"host": "vinyl.local"},
            follow_redirects=False,
        )
    assert response.status_code == 302
    assert response.headers["location"] == "http://vinyl.local:8080/"
    assert response.headers["cache-control"] == "no-store"


def test_setup_form_points_at_this_host_not_localhost():
    with (
        patch.object(wifi, "should_show_setup_page", lambda: True),
        TestClient(wifi.app) as client,
    ):
        response = client.get("/", headers={"host": "vinyl.local"})
    assert response.status_code == 200
    body = response.text
    assert "WiFi Setup" in body
    assert "Open player" in body
    assert "playerUrl" in body
    assert "location.hostname" in body
    assert "http://localhost:8080" not in body
    assert response.headers["cache-control"] == "no-store"


def test_force_flag_shows_the_form_while_online():
    with tempfile.TemporaryDirectory() as tmp:
        flag = Path(tmp) / "wifi_setup_force"
        with (
            patch.object(wifi, "FORCE_FILE", flag),
            patch.object(wifi, "has_usable_network", lambda: True),
        ):
            wifi.wifi_state["force_until"] = 0.0
            assert wifi.should_show_setup_page() is False
            wifi.request_forced_setup()
            assert flag.is_file()
            assert wifi.should_show_setup_page() is True
            wifi.clear_forced_setup()
            assert wifi.should_show_setup_page() is False
            assert not flag.exists()


def test_dhcp_command_does_not_call_a_missing_dhclient():
    assert wifi.dhcp_command("wlan0", set()) is None
    assert wifi.dhcp_command("wlan0", {"nmcli"})[:2] == ["nmcli", "device"]
    # Prefer the client this image actually has. Never invent dhclient.
    argv = wifi.dhcp_command("wlan0", {"dhcpcd", "nmcli"})
    assert argv[0] == "dhcpcd"
    assert all(part != "dhclient" for part in argv)
    unsafe = wifi.dhcp_command("wlan0;rm -rf", {"dhclient"})
    assert unsafe == ["dhclient", "-1", "wlan0"]


def test_nmcli_connect_does_not_touch_dhclient_or_wpa_conf():
    with tempfile.TemporaryDirectory() as tmp:
        conf = Path(tmp) / "wifi_config.json"
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, stdout="connected\n", stderr="")

        with (
            patch.object(wifi, "CONFIG_FILE", conf),
            patch.object(wifi, "FORCE_FILE", Path(tmp) / "force"),
            patch.object(wifi, "_network_manager_running", lambda: True),
            patch.object(wifi, "stop_hotspot", lambda: None),
            patch.object(wifi, "_wifi_iface", lambda: "wlan0"),
            patch.object(wifi.subprocess, "run", fake_run),
        ):
            assert wifi.connect_to_network("TheRanch", "secret-pass") is True

        assert calls
        assert calls[0][:5] == ["nmcli", "device", "wifi", "connect", "TheRanch"]
        assert "secret-pass" in calls[0]
        assert all(cmd[0] != "dhclient" for cmd in calls)
        assert all(cmd[0] != "wpa_cli" for cmd in calls)
        saved = json.loads(conf.read_text())
        assert saved["ssid"] == "TheRanch"
        assert "secret-pass" not in conf.read_text()


def test_nmcli_failure_does_not_echo_the_password():
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="Secrets were required, but not provided: secret-pass"
        )

    with (
        patch.object(wifi, "_network_manager_running", lambda: True),
        patch.object(wifi, "stop_hotspot", lambda: None),
        patch.object(wifi, "_wifi_iface", lambda: "wlan0"),
        patch.object(wifi.subprocess, "run", fake_run),
    ):
        assert wifi.connect_to_network("TheRanch", "secret-pass") is False
    assert "secret-pass" not in (wifi.wifi_state["error"] or "")
    assert "********" in wifi.wifi_state["error"]


def test_wpa_fallback_survives_missing_dhclient():
    with tempfile.TemporaryDirectory() as tmp:
        wpa = Path(tmp) / "wpa_supplicant.conf"
        calls = []

        def fake_which(name):
            if name == "wpa_cli":
                return "/usr/sbin/wpa_cli"
            return None

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with (
            patch.object(wifi, "WPA_SUPPLICANT_CONF", wpa),
            patch.object(wifi, "CONFIG_FILE", Path(tmp) / "wifi_config.json"),
            patch.object(wifi, "FORCE_FILE", Path(tmp) / "force"),
            patch.object(wifi, "_network_manager_running", lambda: False),
            patch.object(wifi, "_wifi_iface", lambda: "wlan0"),
            patch.object(wifi, "check_wifi_connection", lambda: True),
            patch.object(wifi.shutil, "which", fake_which),
            patch.object(wifi.subprocess, "run", fake_run),
        ):
            assert wifi.connect_to_network("TheCasita", "other-secret") is True

        assert calls == [["wpa_cli", "reconfigure"]]
        text = wpa.read_text()
        assert 'ssid="TheCasita"' in text
        assert "dhclient" not in text


def main():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print("ok", test.__name__)
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    main()
