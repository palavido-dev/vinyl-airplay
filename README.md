# Vinyl Streamer

**Turn your turntable into a whole-home audio system with automatic record recognition.**

Vinyl Streamer is a Raspberry Pi–powered application that captures audio from a USB audio interface (like a Focusrite Scarlett 2i2), identifies what's playing using audio fingerprinting, and streams it losslessly to AirPlay speakers throughout your home — complete with album artwork, track info, and a beautiful web UI you can control from your phone.

Drop the needle, and everything else happens automatically.

> **A note from the author:** I'm not an audiophile, and I don't pretend to be. This started as a personal project — I just wanted a simple way to play my records on speakers around the house without re-buying everything digitally. I know there are vinyl purists and audio enthusiasts who may have strong opinions about digitizing analog audio or streaming it over a network, and that's totally fair. I built this for myself because it solved a problem I had, and I'm sharing it in case it's useful to anyone else. If it's not your thing, no hard feelings.

---

## What It Does

Vinyl Streamer sits between your turntable and your speakers. It captures the analog audio via a USB DAC, identifies the record using local audio fingerprinting, and streams 16-bit/44.1kHz lossless audio to any AirPlay-compatible device on your network. A responsive web interface — designed to look and feel like a piece of vintage hi-fi furniture — lets you control playback, manage your catalog, and configure everything from any browser.

### Key Features

- **Automatic Record Recognition** — Identifies what's playing within seconds using Chromaprint audio fingerprinting against a local database. No cloud service needed after initial setup.
- **Lossless AirPlay Streaming** — Streams CD-quality audio (16-bit/44.1kHz PCM) to any AirPlay speaker or receiver on your network. Multi-room support with independent volume control.
- **Album Recording & Learning** — Record full album sides as FLAC files with automatic track boundary detection (silence-based splitting with Discogs duration fallback). Teach the system new records by playing them once.
- **Discogs Integration** — Search the Discogs database to add albums to your catalog. Automatically pulls track listings, artwork, and metadata. Just enter the artist and album name, or scan a barcode. Works without an API token for casual use.
- **Live EQ** — Real-time bass and treble shelf EQ applied before streaming, so you can dial in the sound without touching your amp.
- **Beautiful Web UI** — A warm, walnut-and-cream interface inspired by vintage hi-fi aesthetics. Fully responsive and optimized for touch — works great on a wall-mounted tablet or your phone.
- **Now Playing Display** — Shows album artwork, track name, artist, and playback progress in real time. Perfect for a dedicated display next to your turntable.
- **Idle Detection** — Automatically detects when the record ends (needle lift / run-out groove) and stops streaming gracefully.

---

## Hardware Requirements

| Component | What I Use | Notes |
|---|---|---|
| **Raspberry Pi** | [CanaKit Pi 5 Starter Kit (8GB)](https://www.amazon.com/dp/B0CRSNCJ6Y) | Developed and tested on a Pi 5 8GB. Other models may work but are untested. |
| **USB Audio Interface** | Focusrite Scarlett 2i2 (4th Gen) | Any class-compliant USB DAC with line-level input works. I'm planning to switch to a [HiFiBerry DAC2 ADC](https://www.hifiberry.com/shop/boards/hifiberry-dac2-adc/) HAT for a cleaner single-board setup. |
| **NVMe SSD** | [Geekworm X1005 PCIe HAT](https://www.amazon.com/dp/B0DTH2Y1WN) + [Silicon Power 256GB NVMe](https://www.amazon.com/dp/B08QBJ2YMG) | Stores FLAC recordings and the fingerprint database. Much faster and more reliable than SD card storage, especially with a growing catalog. |
| **Touchscreen** | [ROADOM 10.1" IPS Touch Display (1024x600)](https://www.amazon.com/dp/B0CSQGZ91P) | Runs the web UI in Chromium kiosk mode for a dedicated now-playing display next to the turntable. |
| **Turntable** | Any with line-level output | If your turntable has a built-in preamp, connect directly. Otherwise, run it through a phono preamp first. |
| **AirPlay Speakers** | Any AirPlay (1) compatible speaker or receiver | HomePod (ungrouped), AirPort Express, AirPlay-enabled AVR, etc. See note below. |

**A note on AirPlay compatibility:** Vinyl Streamer uses AirPlay (RAOP), not AirPlay 2. This means Apple TVs are not supported, and HomePods that are grouped into stereo pairs or multi-room groups won't work — once grouped, they require AirPlay 2. Individual, ungrouped HomePods, AirPort Express units, and most third-party AirPlay receivers work well.

### Wiring

```
Turntable ──▶ (Phono Preamp if needed) ──▶ USB Audio Interface (line in) ──▶ Raspberry Pi (USB)
                                                                                    │
                                                                              USB SSD (storage)
                                                                                    │
                                                                              WiFi / Ethernet
                                                                                    │
                                                                            AirPlay Speakers
```

---

## Software Requirements

- **Raspberry Pi OS** (Bookworm or later, 64-bit recommended)
- **Python 3.10+**
- **ffmpeg** (for FLAC encoding and audio decoding)
- **fpcalc** (Chromaprint CLI — for audio fingerprinting)
- **PortAudio** (for `sounddevice` audio capture)

### Python Dependencies

- `numpy` — Audio signal processing and EQ
- `sounddevice` — USB audio capture
- `pyatv` — AirPlay device discovery and streaming
- `fastapi` + `uvicorn` — Web server and API
- `Pillow` — Album artwork processing
- `requests` — Discogs API communication

---

## Installation

### Quick Start

```bash
# Clone the repository
git clone https://github.com/yourusername/vinyl-streamer.git
cd vinyl-streamer

# Install system dependencies
sudo apt update
sudo apt install -y python3-pip ffmpeg libchromaprint-tools portaudio19-dev libasound2-dev

# Install Python dependencies
pip install numpy sounddevice pyatv fastapi uvicorn pillow requests jinja2 python-multipart --break-system-packages

# Run it
python3 main.py
```

The web UI will be available at `http://<your-pi-ip>:8000`.

### Running as a Service

To start Vinyl Streamer automatically on boot:

```bash
sudo tee /etc/systemd/system/vinyl-streamer.service > /dev/null <<EOF
[Unit]
Description=Vinyl Streamer
After=network-online.target sound.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$(pwd)
ExecStart=/usr/bin/python3 main.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable vinyl-streamer
sudo systemctl start vinyl-streamer
```

---

## Setup & First Use

1. **Connect your hardware** — Plug your turntable (via preamp if needed) into your USB audio interface, and plug the interface into the Pi.

2. **Open the web UI** — Navigate to `http://<your-pi-ip>:8000` from any device on your network.

3. **Add an album to your catalog** — Use the search to find your record on Discogs. Select the correct pressing to import the full track listing and artwork. No API token is required for basic use — if you hit rate limits with heavy use, you can optionally add a Discogs personal access token in Settings (free at [discogs.com/settings/developers](https://www.discogs.com/settings/developers)).

4. **Teach the system your record** — Start a recording session for the album. Play Side A all the way through — the system will automatically detect track boundaries and build a fingerprint database. Flip and repeat for Side B.

5. **Play your records** — From now on, just drop the needle. Vinyl Streamer will recognize the record within seconds, display the album info, and start streaming to your selected AirPlay speakers automatically.

---

## How It Works

### Audio Fingerprinting

Vinyl Streamer uses [Chromaprint](https://acoustid.org/chromaprint) to generate audio fingerprints — compact representations of the acoustic content of your music. During the initial "learning" phase, it captures overlapping fingerprint windows as each track plays and stores them in a local SQLite database. On subsequent plays, it samples the incoming audio every few seconds and matches it against the local database. All matching happens on-device with no internet required after the initial catalog setup.

### Recording & Track Splitting

When recording an album side, the system captures the full side as a continuous FLAC file while simultaneously detecting track boundaries. It uses a dual approach: primary silence-based gap detection (listening for the quiet grooves between tracks), with Discogs track duration data as a fallback for records with short or unclear gaps. Each detected track is fingerprinted independently for future recognition.

### Streaming

Audio is captured at 16-bit/44.1kHz (CD quality) from the USB interface, processed through the real-time EQ stage, and streamed via the RAOP (Remote Audio Output Protocol) to AirPlay devices using [pyatv](https://github.com/postlund/pyatv). The system supports streaming to multiple AirPlay speakers simultaneously with independent volume control.

---

## Project Structure

```
vinyl-streamer/
├── main.py          # FastAPI server, AirPlay streaming, audio pipeline, web API
├── catalog.py       # Album catalog, Chromaprint fingerprinting, Discogs integration
├── recorder.py      # Vinyl recording, silence detection, track boundary splitting
├── player.py        # FLAC playback engine with track navigation
├── templates/
│   └── index.html   # Web UI (single-page application)
├── settings.json    # User configuration (auto-created)
└── data/            # SQLite database, album artwork, FLAC recordings
```

---

## Roadmap

- [ ] **Bluetooth speaker support** — Stream to Bluetooth A2DP speakers/headphones in addition to AirPlay
- [ ] **Unified device manager** — Single UI for managing both AirPlay and Bluetooth output devices
- [ ] **WiFi setup portal** — Captive portal for headless first-time WiFi configuration
- [ ] **Flashable Pi image** — Pre-built SD card image for zero-config setup
- [ ] **One-line install script** — `curl | bash` installer for existing Pi setups

---

## Support This Project

This project represents a significant investment of time, effort, and hardware testing costs. If you find it useful and want to support continued development, donations are greatly appreciated.

- **[Donate via PayPal](https://paypal.me/palavido)**
- **[Sponsor on GitHub](https://github.com/sponsors/palavido-dev)** *(coming soon — pending approval)*

---

## License

MIT License — see [LICENSE](LICENSE) for details.

---

*Built with love for the ritual of playing records.*
