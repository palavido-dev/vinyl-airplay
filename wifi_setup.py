#!/usr/bin/env python3
"""
WiFi Setup Portal for Vinyl Streamer.

Captive portal for headless first-time WiFi configuration. When the Pi is
already on the LAN (the usual case for http://vinyl.local/), the portal
redirects to the player instead of trapping the browser on this page.
"""

import asyncio
import contextlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

# Configuration
HOTSPOT_SSID = "VinylStreamer-Setup"
SETUP_PORT = int(os.environ.get("VINYL_WIFI_PORT", "80"))
PLAYER_PORT = int(os.environ.get("VINYL_PLAYER_PORT", "8080"))
CONFIG_FILE = Path("/opt/vinyl-streamer/wifi_config.json")
WPA_SUPPLICANT_CONF = Path("/etc/wpa_supplicant/wpa_supplicant.conf")
# Written by the player app (user listen) and read by this root process.
FORCE_FILE = Path(os.environ.get(
    "VINYL_WIFI_FORCE_FILE",
    "/opt/vinyl-streamer/wifi_setup_force",
))
FORCE_TTL_SECONDS = 15 * 60
_HOST_RE = re.compile(r"\A[A-Za-z0-9._-]{1,253}\Z")
_IWLIST_ESCAPED_SSID = re.compile(r"(?:\\x[0-9a-fA-F]{2})+")

app = FastAPI()

# Store state
wifi_state = {
    "hotspot_active": False,
    "connected": False,
    "error": None,
    "force_until": 0.0,
}

def load_wifi_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {"ssid": None, "psk": None}

def save_wifi_config(config: dict):
    """Persist Wi-Fi reminder config (PSK optional). Mode 600 when possible."""
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Do not duplicate the PSK on disk when wpa_supplicant already holds it.
    safe = {"ssid": config.get("ssid"), "configured_at": time.time()}
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(safe, indent=2) + "\n")
    os.replace(tmp, CONFIG_FILE)
    with contextlib.suppress(OSError):
        os.chmod(CONFIG_FILE, 0o600)

def _run_text(argv: list[str], timeout: int = 5) -> str | None:
    """Stdout of a probe command, or None when it is missing or fails."""
    if not argv or shutil.which(argv[0]) is None:
        return None
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout or ""


def _usable_scan_ssid(ssid: str) -> bool:
    """True for a network name worth showing or treating as a real association.

    iwlist prints hidden or corrupt SSIDs as a quoted run of ``\\x00`` escapes.
    Those are not networks the user can join, and selecting one breaks connect.
    """
    if not ssid or not ssid.strip() or ssid == HOTSPOT_SSID:
        return False
    if not _valid_ssid(ssid):
        return False
    escaped = "\\x00" in ssid.lower() or _IWLIST_ESCAPED_SSID.fullmatch(ssid)
    return not escaped


def _nmcli_wifi_connected(output: str) -> bool:
    """True when NetworkManager has a wifi device joined to a real SSID."""
    for raw in output.splitlines():
        # nmcli -t escapes embedded colons as \:
        line = raw.replace("\\:", "\u0001")
        parts = line.split(":")
        if len(parts) < 3:
            continue
        kind = parts[0].replace("\u0001", ":").strip().lower()
        state = parts[1].replace("\u0001", ":").strip().lower()
        name = ":".join(parts[2:]).replace("\u0001", ":").strip()
        if kind == "wifi" and state == "connected" and _usable_scan_ssid(name):
            return True
    return False


def _iw_link_associated(output: str) -> bool:
    if "connected to" not in output.lower():
        return False
    match = re.search(r"(?im)^ssid:\s*(.+?)\s*$", output)
    if not match:
        return False
    return _usable_scan_ssid(match.group(1).strip())


def _iwconfig_has_association(output: str) -> bool:
    """True when any wireless iface is joined to a real SSID.

    ``iwconfig`` always prints ``no wireless extensions`` for lo and eth0, and
    a second iface (p2p) often says ``Not-Associated`` while wlan0 is up.
    Those strings must not mark the Pi offline.
    """
    blocks = re.split(r"\n(?=\S)", output.strip())
    for block in blocks:
        low = block.lower()
        if "no wireless extensions" in low:
            continue
        if "not-associated" in low or "unassociated" in low:
            continue
        quoted = re.search(r'essid:"([^"]*)"', block, re.IGNORECASE)
        if quoted:
            ssid = quoted.group(1).strip()
        else:
            bare = re.search(r"essid:(\S+)", block, re.IGNORECASE)
            if not bare:
                continue
            ssid = bare.group(1).strip()
            if ssid.lower() in {"off/any", "off"}:
                continue
        if _usable_scan_ssid(ssid):
            return True
    return False


def _safe_iface(name: str) -> str | None:
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]{0,14}", name or ""):
        return name
    return None


def _wifi_iface() -> str | None:
    text = _run_text(["nmcli", "-t", "-f", "DEVICE,TYPE", "device"])
    if text:
        for raw in text.splitlines():
            device, _, kind = raw.replace("\\:", "\u0001").partition(":")
            device = device.replace("\u0001", ":").strip()
            if kind.replace("\u0001", ":").strip().lower() == "wifi":
                safe = _safe_iface(device)
                if safe:
                    return safe
    text = _run_text(["iw", "dev"])
    if text:
        match = re.search(r"Interface\s+(\S+)", text)
        if match:
            safe = _safe_iface(match.group(1))
            if safe:
                return safe
    return _safe_iface("wlan0")


def check_wifi_connection() -> bool:
    """True when Wi-Fi is associated to something other than the setup AP."""
    nm = _run_text(["nmcli", "-t", "-f", "TYPE,STATE,CONNECTION", "device"])
    if nm and _nmcli_wifi_connected(nm):
        return True
    iface = _wifi_iface()
    if iface:
        link = _run_text(["iw", "dev", iface, "link"])
        if link and _iw_link_associated(link):
            return True
    iwconfig = _run_text(["iwconfig"])
    return bool(iwconfig and _iwconfig_has_association(iwconfig))


def _proc_net_route_has_default(text: str) -> bool:
    """True when /proc/net/route lists an IPv4 default route (destination 0)."""
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 2 and fields[1] == "00000000":
            return True
    return False


def _read_proc_net_route() -> str:
    try:
        return Path("/proc/net/route").read_text()
    except OSError:
        return ""


def _ipv4_default_route() -> bool:
    route = _run_text(["ip", "-4", "route", "show", "default"])
    if route and route.strip():
        return True
    # `ip` is optional. The kernel table is enough, and it is what is
    # actually present when SSH to vinyl.local already works.
    return _proc_net_route_has_default(_read_proc_net_route())


def has_usable_network() -> bool:
    """True when this Pi can already be reached on the LAN.

    A real Wi-Fi association counts, and so does any IPv4 default route
    (Ethernet, or Wi-Fi that the wireless tools describe messily). The setup
    AP alone does not install a default route, so first-time setup still
    shows the form.
    """
    if check_wifi_connection():
        return True
    return _ipv4_default_route()


def _network_manager_running() -> bool:
    # nmcli exits non-zero when NetworkManager is not running.
    return _run_text(["nmcli", "-t", "-f", "STATE", "general"]) is not None


def request_forced_setup() -> None:
    """Show the setup form on the next visit, even if the Pi is already online.

    The player service calls this before starting the portal so Reconfigure
    WiFi still opens the form. The flag expires so a forgotten reconfigure
    cannot trap http://vinyl.local/ on this page.
    """
    deadline = time.time() + FORCE_TTL_SECONDS
    wifi_state["force_until"] = deadline
    try:
        if not FORCE_FILE.parent.is_dir():
            return
        tmp = FORCE_FILE.with_name(FORCE_FILE.name + ".tmp")
        tmp.write_text(f"{deadline}\n")
        os.replace(tmp, FORCE_FILE)
        with contextlib.suppress(OSError):
            os.chmod(FORCE_FILE, 0o644)
    except OSError:
        pass


def clear_forced_setup() -> None:
    wifi_state["force_until"] = 0.0
    with contextlib.suppress(OSError):
        FORCE_FILE.unlink()


def forced_setup_active() -> bool:
    now = time.time()
    if float(wifi_state.get("force_until") or 0) > now:
        return True
    try:
        raw = FORCE_FILE.read_text().strip()
    except OSError:
        return False
    try:
        deadline = float(raw)
    except ValueError:
        deadline = 0.0
    if deadline > now:
        wifi_state["force_until"] = deadline
        return True
    clear_forced_setup()
    return False


def should_show_setup_page() -> bool:
    if forced_setup_active():
        return True
    return not has_usable_network()


def player_redirect_url(host_header: str) -> str:
    """Player URL on the same host the browser used to reach this portal."""
    raw = (host_header or "").strip()
    if raw.startswith("["):
        end = raw.find("]")
        host = raw[1:end] if end > 1 else ""
        if not re.fullmatch(r"[0-9A-Fa-f:]{2,}", host):
            host = "127.0.0.1"
        return f"http://[{host}]:{PLAYER_PORT}/"
    host = raw.split(":", 1)[0].strip().rstrip(".")
    if not _HOST_RE.fullmatch(host) or ".." in host:
        host = "127.0.0.1"
    return f"http://{host}:{PLAYER_PORT}/"


def dhcp_command(iface: str, available: set[str]) -> list[str] | None:
    """DHCP client argv for this OS, or None when nothing is installed.

    Raspberry Pi OS Bookworm and newer use NetworkManager and do not ship
    ``dhclient``. Calling a missing binary aborts connect with Errno 2.
    ``nmcli`` is only the fallback here: when NetworkManager is running,
    connect goes through ``nmcli device wifi connect`` and does not ask
    for a lease itself.
    """
    safe = _safe_iface(iface) or "wlan0"
    if "dhcpcd" in available:
        return ["dhcpcd", "-n", safe]
    if "dhclient" in available:
        return ["dhclient", "-1", safe]
    if "networkctl" in available:
        return ["networkctl", "reconfigure", safe]
    if "nmcli" in available:
        return ["nmcli", "device", "reapply", safe]
    return None


def _request_dhcp(iface: str) -> None:
    available = {
        name for name in ("nmcli", "dhcpcd", "dhclient", "networkctl")
        if shutil.which(name)
    }
    argv = dhcp_command(iface, available)
    if not argv:
        return
    try:
        subprocess.run(argv, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return


def _redact(text: str, secret: str) -> str:
    cleaned = (text or "Connection failed").strip()
    if secret:
        cleaned = cleaned.replace(secret, "********")
    line = cleaned.splitlines()[-1] if cleaned else "Connection failed"
    return line[:300]

def start_hotspot():
    if os.environ.get("VINYL_WIFI_DISABLE_HOTSPOT") == "1":
        return
    try:
        subprocess.run(["systemctl", "start", "hostapd"], check=True, timeout=10)
        subprocess.run(["systemctl", "start", "dnsmasq"], check=True, timeout=10)
        wifi_state["hotspot_active"] = True
    except Exception as e:
        wifi_state["error"] = str(e)

def stop_hotspot():
    try:
        subprocess.run(["systemctl", "stop", "hostapd"], timeout=10)
        subprocess.run(["systemctl", "stop", "dnsmasq"], timeout=10)
        wifi_state["hotspot_active"] = False
    except Exception:
        pass

def scan_networks() -> list:
    try:
        iface = _wifi_iface() or "wlan0"
        result = subprocess.run(
            ["sudo", "iwlist", iface, "scan"],
            capture_output=True,
            text=True,
            timeout=15
        )
        networks = []
        lines = result.stdout.split('\n')
        current_network = {}

        for line in lines:
            line = line.strip()
            if "ESSID:" in line:
                ssid = line.split("ESSID:")[1].strip().strip('"')
                if _usable_scan_ssid(ssid):
                    current_network["ssid"] = ssid
            elif "Signal level=" in line:
                try:
                    signal = int(line.split("Signal level=")[1].split()[0])
                    current_network["signal"] = signal
                except (IndexError, ValueError):
                    pass
            elif line.startswith("Cell"):
                if current_network.get("ssid"):
                    networks.append(current_network)
                current_network = {}

        if current_network.get("ssid"):
            networks.append(current_network)

        seen = set()
        unique_networks = []
        for net in sorted(networks, key=lambda x: x.get("signal", 0), reverse=True):
            ssid = net.get("ssid", "")
            if ssid and ssid not in seen:
                seen.add(ssid)
                unique_networks.append(net)

        return unique_networks[:20]
    except Exception:
        return []

def _escape_wpa_string(value: str) -> str:
    """Escape a value for a wpa_supplicant quoted string (no raw newlines)."""
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "")
        .replace("\r", "")
        .replace("\x00", "")
    )


def _valid_ssid(ssid: str) -> bool:
    if not ssid or len(ssid) > 32:
        return False
    # Reject control characters that could break config parsing.
    return not re.search(r"[\x00-\x1f\x7f]", ssid)


def _mark_connected(ssid: str) -> None:
    save_wifi_config({"ssid": ssid})
    clear_forced_setup()
    wifi_state["connected"] = True
    wifi_state["error"] = None


def _connect_nmcli(ssid: str, password: str) -> bool:
    """Join via NetworkManager. This is Raspberry Pi OS Bookworm and newer."""
    stop_hotspot()
    argv = ["nmcli", "device", "wifi", "connect", ssid]
    iface = _wifi_iface()
    if iface:
        argv.extend(["ifname", iface])
    if password:
        argv.extend(["password", password])
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=45)
    except subprocess.TimeoutExpired:
        wifi_state["error"] = "Connection timeout"
        return False
    except OSError as exc:
        wifi_state["error"] = f"Required tool is not installed: {exc.filename or 'nmcli'}"
        return False
    if result.returncode != 0:
        wifi_state["error"] = _redact(result.stderr or result.stdout, password)
        return False
    _mark_connected(ssid)
    return True


def _connect_wpa(ssid: str, password: str) -> bool:
    """Join via wpa_supplicant on images that are not running NetworkManager."""
    safe_ssid = _escape_wpa_string(ssid)
    safe_psk = _escape_wpa_string(password or "")

    # Preserve global wpa_supplicant options; only replace network blocks.
    conf_path = WPA_SUPPLICANT_CONF
    global_lines = [
        "ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev",
        "update_config=1",
        "country=US",
    ]
    if conf_path.exists():
        raw = conf_path.read_text()
        # Keep non-network preamble lines when present.
        preamble = []
        in_network = False
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped.startswith("network={"):
                in_network = True
                continue
            if in_network:
                if stripped == "}":
                    in_network = False
                continue
            if stripped and not stripped.startswith("#"):
                preamble.append(line.rstrip())
        if preamble:
            global_lines = preamble

    if password:
        network_block = (
            "network={\n"
            f'    ssid="{safe_ssid}"\n'
            f'    psk="{safe_psk}"\n'
            "    key_mgmt=WPA-PSK\n"
            "}\n"
        )
    else:
        network_block = (
            "network={\n"
            f'    ssid="{safe_ssid}"\n'
            "    key_mgmt=NONE\n"
            "}\n"
        )

    content = "\n".join(global_lines) + "\n\n" + network_block
    tmp = conf_path.with_suffix(".conf.tmp")
    tmp.write_text(content)
    os.replace(tmp, conf_path)
    with contextlib.suppress(OSError):
        os.chmod(conf_path, 0o600)

    if shutil.which("wpa_cli") is None:
        wifi_state["error"] = "Required tool is not installed: wpa_cli"
        return False
    subprocess.run(["wpa_cli", "reconfigure"], timeout=10, capture_output=True, text=True)
    _request_dhcp(_wifi_iface() or "wlan0")

    # Called via asyncio.to_thread() from the FastAPI endpoint, so a
    # blocking sleep here does not block the event loop.
    for _attempt in range(30):
        if check_wifi_connection():
            _mark_connected(ssid)
            return True
        time.sleep(1)

    wifi_state["error"] = "Connection timeout"
    return False


def connect_to_network(ssid: str, password: str = "") -> bool:
    try:
        if not _valid_ssid(ssid):
            wifi_state["error"] = "Invalid SSID"
            return False
        if len(password) > 63:
            wifi_state["error"] = "Password too long"
            return False
        if re.search(r"[\x00-\x1f\x7f]", password or ""):
            wifi_state["error"] = "Invalid password characters"
            return False

        # Never rewrite wpa_supplicant.conf while NetworkManager owns the
        # radio, and never call dhclient — Bookworm/Trixie do not ship it.
        if _network_manager_running():
            return _connect_nmcli(ssid, password)
        return _connect_wpa(ssid, password)
    except Exception as e:
        wifi_state["error"] = _redact(str(e), password or "")
        return False

async def check_main_app_ready() -> bool:
    try:
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=5)
        async with (
            aiohttp.ClientSession() as session,
            session.get("http://localhost:8080", timeout=timeout) as response,
        ):
            return response.status == 200
    except Exception:
        return False

@app.middleware("http")
async def _no_store(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/", response_class=HTMLResponse)
async def wifi_setup_page(request: Request):
    # Already on the LAN: http://vinyl.local/ is the player, not this form.
    if not await asyncio.to_thread(should_show_setup_page):
        response = RedirectResponse(
            player_redirect_url(request.headers.get("host", "")),
            status_code=302,
        )
        response.headers["Cache-Control"] = "no-store"
        return response
    return """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="theme-color" content="#3e2723">
    <title>Vinyl Streamer WiFi Setup</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Libre+Baskerville:ital,wght@0,400;0,700;1,400&family=DM+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --walnut:    #3E2723;
            --walnut-lt: #5D4037;
            --leather:   #795548;
            --amber:     #D4A24E;
            --amber-dk:  #B8862D;
            --cream:     #F5EBD8;
            --paper:     #EDE0CA;
            --paper-dk:  #DDD0B8;
            --ink:       #2C1810;
            --muted:     #7A6652;
            --sage:      #6B8F71;
            --sage-dk:   #557A5A;
            --rust:      #C1553A;
            --teal:      #4A8B83;
        }
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        body {
            font-family: 'DM Sans', sans-serif;
            background: var(--walnut);
            color: var(--ink);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 1rem;
        }
        .container {
            background: linear-gradient(180deg, #EDE0CA 0%, #F5EBD8 30%, #F0E4CE 100%);
            border: 3px solid var(--walnut-lt);
            border-radius: 12px;
            box-shadow: 0 0 0 2px var(--walnut), 0 2px 12px rgba(0,0,0,0.4);
            padding: 2rem;
            max-width: 500px;
            width: 100%;
        }
        .header {
            text-align: center;
            margin-bottom: 2rem;
        }
        .brand {
            font-family: 'Libre Baskerville', serif;
            font-size: 1.8rem;
            font-weight: 700;
            color: var(--amber);
            margin-bottom: 0.5rem;
            text-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }
        .subtitle {
            color: var(--muted);
            font-size: 0.9rem;
        }
        .form-group {
            margin-bottom: 1.5rem;
        }
        .form-group label {
            display: block;
            margin-bottom: 0.5rem;
            color: var(--ink);
            font-weight: 600;
            font-size: 0.9rem;
        }
        .network-list {
            max-height: 200px;
            overflow-y: auto;
            border: 1px solid rgba(0,0,0,0.1);
            border-radius: 6px;
            background: white;
        }
        .network-item {
            padding: 0.8rem;
            border-bottom: 1px solid rgba(0,0,0,0.05);
            cursor: pointer;
            transition: background 0.15s;
            display: flex;
            align-items: center;
            gap: 0.8rem;
        }
        .network-item:last-child {
            border-bottom: none;
        }
        .network-item:active {
            background: rgba(212,162,78,0.1);
        }
        .signal-strength {
            font-size: 1.2rem;
        }
        .network-ssid {
            flex: 1;
            font-weight: 500;
            color: var(--ink);
        }
        input[type="text"],
        input[type="password"] {
            width: 100%;
            padding: 0.7rem;
            border: 1px solid rgba(0,0,0,0.2);
            border-radius: 6px;
            font-size: 1rem;
            font-family: inherit;
            color: var(--ink);
            background: white;
        }
        input:focus {
            outline: none;
            border-color: var(--amber);
            box-shadow: 0 0 0 2px rgba(212,162,78,0.2);
        }
        .button-group {
            display: flex;
            gap: 0.8rem;
            margin-top: 1.5rem;
        }
        button {
            flex: 1;
            padding: 0.8rem 1.5rem;
            border: none;
            border-radius: 6px;
            font-size: 0.95rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
            font-family: inherit;
        }
        .btn-primary {
            background: var(--amber);
            color: var(--ink);
        }
        .btn-primary:active {
            background: var(--amber-dk);
            transform: scale(0.98);
        }
        .btn-secondary {
            background: rgba(0,0,0,0.1);
            color: var(--ink);
        }
        .btn-secondary:active {
            background: rgba(0,0,0,0.15);
        }
        .status {
            padding: 0.8rem;
            border-radius: 6px;
            margin-top: 1rem;
            text-align: center;
            font-weight: 500;
            display: none;
        }
        .status.connecting {
            background: rgba(212,162,78,0.2);
            color: var(--amber-dk);
            display: block;
        }
        .status.error {
            background: rgba(193,85,58,0.2);
            color: var(--rust);
            display: block;
        }
        .status.success {
            background: rgba(107,143,113,0.2);
            color: var(--sage-dk);
            display: block;
        }
        .spinner {
            display: inline-block;
            width: 1rem;
            height: 1rem;
            border: 2px solid rgba(212,162,78,0.3);
            border-top-color: var(--amber);
            border-radius: 50%;
            animation: spin 1s linear infinite;
        }
        @keyframes spin {
            to { transform: rotate(360deg); }
        }
        .section-title {
            color: var(--ink);
            font-weight: 600;
            margin-bottom: 1rem;
            font-size: 0.9rem;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="brand">Vinyl Streamer</div>
            <div class="subtitle">WiFi Setup</div>
        </div>

        <div id="scanSection">
            <div class="form-group">
                <label class="section-title">Available Networks</label>
                <div class="network-list" id="networkList">
                    <div class="network-item" style="justify-content: center; color: var(--muted);">
                        <div class="spinner"></div> Scanning...
                    </div>
                </div>
            </div>

            <div class="form-group">
                <label for="ssidInput">Network SSID</label>
                <input type="text" id="ssidInput" placeholder="Enter network name">
            </div>

            <div class="form-group">
                <label for="passwordInput">Password</label>
                <input type="password" id="passwordInput" placeholder="Leave blank for open networks">
            </div>

            <div class="button-group">
                <button class="btn-secondary" onclick="skipSetup()">Open player</button>
                <button class="btn-primary" onclick="connectWiFi()">Connect</button>
            </div>
        </div>

        <div id="statusMessage" class="status"></div>
    </div>

    <script>
        async function scanNetworks() {
            try {
                const response = await fetch('/api/wifi/scan');
                const networks = await response.json();
                displayNetworks(networks);
            } catch (e) {
                console.error('Scan failed:', e);
            }
        }

        function displayNetworks(networks) {
            const list = document.getElementById('networkList');
            list.innerHTML = '';

            if (networks.length === 0) {
                list.innerHTML = '<div class="network-item" style="justify-content: center; color: var(--muted);">No networks found</div>';
                return;
            }

            networks.forEach(net => {
                const item = document.createElement('div');
                item.className = 'network-item';
                const signal = net.signal || 0;
                let bars = '📶';
                if (signal < -80) bars = '📶';
                else if (signal < -60) bars = '📶📶';
                else bars = '📶📶📶';

                item.innerHTML = `
                    <div class="signal-strength">${bars}</div>
                    <div class="network-ssid">${escapeHtml(net.ssid)}</div>
                `;
                item.onclick = () => selectNetwork(net.ssid);
                list.appendChild(item);
            });
        }

        function selectNetwork(ssid) {
            document.getElementById('ssidInput').value = ssid;
            document.getElementById('passwordInput').focus();
        }

        async function connectWiFi() {
            const ssid = document.getElementById('ssidInput').value.trim();
            const password = document.getElementById('passwordInput').value;

            if (!ssid) {
                showStatus('Please enter a network name', 'error');
                return;
            }

            showStatus('Connecting to ' + escapeHtml(ssid) + '...', 'connecting');

            try {
                const response = await fetch('/api/wifi/connect', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ ssid, password })
                });

                if (response.ok) {
                    showStatus('Connected! Redirecting...', 'success');
                    await new Promise(r => setTimeout(r, 2000));
                    window.location.href = playerUrl();
                } else {
                    const error = await response.json();
                    showStatus('Connection failed: ' + error.detail, 'error');
                }
            } catch (e) {
                showStatus('Connection error: ' + e.message, 'error');
            }
        }

        function playerUrl() {
            // The portal is on port 80. The player is on the same host, port 8080.
            // localhost would be the phone or laptop, not the Pi.
            var host = window.location.hostname || 'vinyl.local';
            return 'http://' + host + ':8080/';
        }

        async function noteIfOnline() {
            try {
                const response = await fetch('/api/wifi/status');
                const data = await response.json();
                if (data.online) {
                    showStatus('Already on the network. Open the player to continue.', 'success');
                }
            } catch (e) {
                console.error('Status check failed:', e);
            }
        }

        function skipSetup() {
            showStatus('Opening player...', 'connecting');
            fetch('/api/wifi/skip', { method: 'POST' })
                .catch(function() {})
                .finally(function() {
                    window.location.href = playerUrl();
                });
        }

        function showStatus(msg, type) {
            const status = document.getElementById('statusMessage');
            status.textContent = msg;
            status.className = 'status ' + type;
        }

        function escapeHtml(text) {
            const map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' };
            return text.replace(/[&<>"']/g, m => map[m]);
        }

        document.addEventListener('DOMContentLoaded', function() {
            scanNetworks();
            noteIfOnline();
        });
        setInterval(scanNetworks, 30000);
    </script>
</body>
</html>"""

@app.get("/api/wifi/status")
async def wifi_status():
    online = await asyncio.to_thread(has_usable_network)
    return JSONResponse({"online": online})


@app.post("/api/wifi/skip")
async def skip_wifi():
    await asyncio.to_thread(clear_forced_setup)
    return JSONResponse({"status": "ok"})


@app.get("/api/wifi/scan")
async def scan_wifi():
    networks = await asyncio.to_thread(scan_networks)
    return JSONResponse(networks)

@app.post("/api/wifi/connect")
async def connect_wifi(request: Request):
    try:
        data = await request.json()
        ssid = data.get("ssid", "").strip()
        password = data.get("password", "")

        if not ssid:
            return JSONResponse({"detail": "SSID required"}, status_code=400)

        success = await asyncio.to_thread(connect_to_network, ssid, password)

        if success:
            return JSONResponse({"status": "connected"})
        else:
            error_msg = wifi_state.get("error", "Unknown error")
            return JSONResponse({"detail": error_msg}, status_code=400)
    except Exception as e:
        return JSONResponse({"detail": str(e)}, status_code=500)

@app.on_event("startup")
async def startup():
    # Stay up either way. When the Pi is already online this process is the
    # port-80 redirect to the player. Starting the hotspot then would knock
    # a working connection off the LAN.
    if await asyncio.to_thread(has_usable_network):
        wifi_state["connected"] = True
        return
    await asyncio.to_thread(start_hotspot)

def main():
    uvicorn.run(app, host="0.0.0.0", port=SETUP_PORT)

if __name__ == "__main__":
    main()
