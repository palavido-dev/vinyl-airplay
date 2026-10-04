# AirPlay 2

Vinyl Streamer streams to AirPlay 2 receivers (stereo-paired HomePods, multi-room
groups, and Apple TV) through Music Assistant’s [`cliairplay`](https://github.com/music-assistant/airplay-cli)
binary. Classic AirPlay 1 / RAOP devices keep using [pyatv](https://github.com/postlund/pyatv).

## What works

| Target | Transport | Notes |
|--------|-----------|--------|
| Ungrouped HomePod, AirPort Express, many third-party RAOP speakers | RAOP (pyatv) | Unchanged |
| HomePod stereo pairs / multi-room groups | AirPlay 2 (cliairplay) | Shared PTP clock |
| Apple TV (audio) | AirPlay 2 (cliairplay) | Pair once with the on-screen PIN |
| Mixed RAOP + AP2 selection | Both | Each target uses its own path; AP2 members share one PTP daemon |

## Install

`install.sh` downloads `cliairplay` into `/opt/vinyl-streamer/bin/`, grants
`CAP_NET_BIND_SERVICE` (for PTP ports UDP 319/320), and adds that capability to
the `vinyl-airplay` systemd unit.

Manual install (dev machine):

```bash
mkdir -p bin
# Pick the asset for your arch from the v0.5.5 release
curl -fsSL -o bin/cliairplay \
  https://github.com/music-assistant/airplay-cli/releases/download/v0.5.5/cliairplay-linux-x86_64
chmod +x bin/cliairplay
sudo setcap 'cap_net_bind_service=+ep' bin/cliairplay
./bin/cliairplay --check
```

Or set `CLIAIRPLAY_PATH` to an existing binary.

## Pairing

Apple TV and HomePod on the AirPlay 2 path should be paired from **Settings →
AirPlay Devices → Pair**. The Pi runs `cliairplay --pair-setup`; enter the PIN
shown on the device. Credentials are stored in
`data/airplay2_credentials.json` (not committed).

Many third-party AirPlay 2 receivers need no pairing (transient HAP).

## Apple TV notes

Jukebox “playing to Apple TV” with a **blank Apple TV** (no sound, no Now
Playing) almost always means the AirPlay 2 session fell back to NTP realtime
or never got HAP credentials. Apple TVs render nothing useful on that path.

Vinyl Streamer therefore:

1. Requires a working PTP clock (`cliairplay --ptp-daemon` binding UDP 319/320)
2. Requires HAP pairing credentials (`data/airplay2_credentials.json` next to
   the app, absolute path)
3. Falls back to **RAOP (pyatv)** when PTP or pairing is missing, so audio
   still reaches the TV instead of a fake “playing” state
4. Does **not** force `--buffered` unless the TV advertises SupportsBufferedAudio

Grant the capability on **every** `cliairplay` you might run (home checkout
and `/opt`):

```bash
sudo setcap 'cap_net_bind_service=+ep' /home/listen/vinyl-airplay/bin/cliairplay
sudo setcap 'cap_net_bind_service=+ep' /opt/vinyl-streamer/bin/cliairplay
getcap /home/listen/vinyl-airplay/bin/cliairplay
getcap /opt/vinyl-streamer/bin/cliairplay
```

Confirm the stream log shows `timing=ptp` and `ptp_shared=True` (not a
“PTP daemon exited” / “using RAOP” warning) when playing AirPlay 2 to an
Apple TV. A RAOP fallback line is expected and healthy until PTP is fixed.

## License note

`cliairplay` is GPL-3. Vinyl Streamer invokes it as a separate process and does
not link against it. The binary is downloaded at install time and is not
vendored in this repository.
