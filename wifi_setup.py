#!/usr/bin/env python3
"""
WiFi Setup Portal for Vinyl Streamer
Captive portal for headless first-time WiFi configuration
"""

import asyncio
import json
import subprocess
import os
import signal
import sys
from pathlib import Path
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

# Configuration
HOTSPOT_SSID = "VinylStreamer-Setup"
SETUP_PORT = 80
CONFIG_FILE = Path("/opt/vinyl-streamer/wifi_config.json")

app = FastAPI()

# Store state
wifi_state = {
    "hotspot_active": False,
    "connected": False,
    "error": None
}

def load_wifi_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {"ssid": None, "psk": None}

def save_wifi_config(config: dict):
    CONFIG_FILE.write_text(json.dumps(config, indent=2))

def check_wifi_connection() -> bool:
    try:
        result = subprocess.run(
            ["iwconfig"],
            capture_output=True,
            text=True,
            timeout=5
        )
        output = result.stdout.lower()
        return "not-associated" not in output and "no wireless" not in output
    except Exception:
        return False

def start_hotspot():
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
        result = subprocess.run(
            ["sudo", "iwlist", "wlan0", "scan"],
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
                if ssid:
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
    except Exception as e:
        return []

def connect_to_network(ssid: str, password: str = "") -> bool:
    try:
        config_content = f'''network={{
    ssid="{ssid}"
    psk="{password}"
    key_mgmt=WPA-PSK
}}'''

        with open("/etc/wpa_supplicant/wpa_supplicant.conf", "w") as f:
            f.write(config_content)

        subprocess.run(
            ["wpa_cli", "reconfigure"],
            timeout=10
        )

        subprocess.run(
            ["dhclient", "wlan0"],
            timeout=15
        )

        for attempt in range(30):
            if check_wifi_connection():
                save_wifi_config({"ssid": ssid, "psk": password})
                wifi_state["connected"] = True
                wifi_state["error"] = None
                return True
            await asyncio.sleep(1)

        wifi_state["error"] = "Connection timeout"
        return False
    except Exception as e:
        wifi_state["error"] = str(e)
        return False

async def check_main_app_ready() -> bool:
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get("http://localhost:8080", timeout=aiohttp.ClientTimeout(total=5)) as response:
                return response.status == 200
    except Exception:
        return False

@app.get("/", response_class=HTMLResponse)
async def wifi_setup_page():
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
                <button class="btn-secondary" onclick="skipSetup()">Skip</button>
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
                    window.location.href = 'http://localhost:8080';
                } else {
                    const error = await response.json();
                    showStatus('Connection failed: ' + error.detail, 'error');
                }
            } catch (e) {
                showStatus('Connection error: ' + e.message, 'error');
            }
        }

        function skipSetup() {
            showStatus('Skipping setup...', 'connecting');
            setTimeout(() => {
                window.location.href = 'http://localhost:8080';
            }, 1000);
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

        document.addEventListener('DOMContentLoaded', scanNetworks);
        setInterval(scanNetworks, 30000);
    </script>
</body>
</html>"""

@app.get("/api/wifi/scan")
async def scan_wifi():
    networks = scan_networks()
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
    if not check_wifi_connection():
        start_hotspot()
    else:
        sys.exit(0)

def main():
    uvicorn.run(app, host="0.0.0.0", port=SETUP_PORT)

if __name__ == "__main__":
    main()
