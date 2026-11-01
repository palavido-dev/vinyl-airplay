# AirPlay 2

Vinyl Streamer streams to AirPlay 2 receivers (stereo-paired HomePods, multi-room
groups, and Apple TV) through Music Assistant’s [`cliairplay`](https://github.com/music-assistant/airplay-cli)
binary. Classic AirPlay 1 / RAOP devices keep using [pyatv](https://github.com/postlund/pyatv).

## What works

| Target | Transport | Notes |
|--------|-----------|--------|
| Standalone HomePod, AirPort Express, many third-party RAOP speakers | RAOP (pyatv) | Bedroom-style units stay on RAOP |
| HomePod stereo pairs / multi-room groups | AirPlay 2 + PTP (cliairplay) | RAOP cannot drive groups |
| Apple TV (incl. HomePod-as-TV-audio) | AirPlay 2 + PTP (cliairplay) | Pair once with the on-screen PIN |
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

## Device routing

| Target | Transport | Why |
|--------|-----------|-----|
| Standalone HomePod (e.g. bedroom) | **RAOP (pyatv)** | Works today; keep it |
| Stereo pairs / multi-room groups | **AirPlay 2 + PTP** | RAOP cannot drive groups |
| Apple TV (incl. HomePod-as-TV-audio) | **AirPlay 2 + PTP** | RAOP cannot drive ATV→HomePod |

## Apple TV / speaker groups

These need native AP2 + PTP. Missing HAP pairing or `CAP_NET_BIND_SERVICE`
fails closed with a visible error — we do **not** silently fall back to RAOP
(that produces jukebox “playing” with silence).

```bash
sudo setcap 'cap_net_bind_service=+ep' /home/listen/vinyl-airplay/bin/cliairplay
sudo setcap 'cap_net_bind_service=+ep' /opt/vinyl-streamer/bin/cliairplay
getcap /home/listen/vinyl-airplay/bin/cliairplay
# Must show: cap_net_bind_service=ep

cd ~/vinyl-airplay && git pull origin cursor/airplay2-support-755b
sudo systemctl restart vinyl-airplay
```

On Play, log should show:

```text
[airplay2] <name>: native AP2 + PTP (required for Apple TV|speaker group)
[airplay2] Connecting ... timing=ptp ptp_shared=True auth=yes
[airplay2] PTP daemon started
```

## Honest limits

Standalone HomePods on RAOP are solid. **Apple TV (especially with a HomePod
as its speaker) and paired speaker groups** need a locked PTP clock from a
third-party sender. Music Assistant’s `cliairplay` is the best open path, but
if the TV never answers our clock probes the session can look healthy on the
jukebox while the TV stays blank. We now **refuse to start** that session
instead of “playing” into silence.

If ATV / groups remain unreliable on your network after `setcap` + Pair, the
practical options are:

1. Play to the **HomePod / stereo pair directly** (not via the Apple TV)
2. Keep using **standalone HomePods** on RAOP
3. Treat ATV-as-speaker as unsupported for now

That is a protocol/network limitation, not something Vinyl Streamer can paper
over with RAOP.


## License note

`cliairplay` is GPL-3. Vinyl Streamer invokes it as a separate process and does
not link against it. The binary is downloaded at install time and is not
vendored in this repository.
