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

An Apple TV that uses a **paired HomePod as its audio output** is AirPlay 2
only. Classic RAOP (and `airplay2-compat`) cannot drive that path — the jukebox
will look like it is playing while the TV stays blank.

Vinyl Streamer therefore **requires** native AP2 + PTP for Apple TV / HomePod:

1. Working PTP clock (`cliairplay` binding UDP 319/320 via `CAP_NET_BIND_SERVICE`)
2. HAP pairing credentials (Settings → Pair — PIN on the TV)
3. Fail closed with a visible error if either is missing (no silent RAOP fallback)

```bash
# On the Pi — do both trees if you have them:
sudo setcap 'cap_net_bind_service=+ep' /home/listen/vinyl-airplay/bin/cliairplay
sudo setcap 'cap_net_bind_service=+ep' /opt/vinyl-streamer/bin/cliairplay
getcap /home/listen/vinyl-airplay/bin/cliairplay
# Must show: cap_net_bind_service=ep

cd ~/vinyl-airplay && git pull origin cursor/airplay2-support-755b
sudo systemctl restart vinyl-airplay   # or your unit for this tree
```

Confirm in the log when you hit Play:

```text
[airplay2] <name>: native AP2 + PTP (required for ATV/HomePod)
[airplay2] Connecting ... timing=ptp ptp_shared=True auth=yes
[airplay2] PTP daemon started
```

If you see `PTP clock unavailable` or `pair from Settings first`, fix that —
do not expect RAOP to rescue ATV→HomePod audio.

```bash
journalctl -u vinyl-airplay -n 100 --no-pager | egrep -i 'airplay2|PTP|setcap|pair|error'
ls -la ~/vinyl-airplay/data/airplay2_credentials.json /opt/vinyl-streamer/data/airplay2_credentials.json 2>/dev/null
```


## License note

`cliairplay` is GPL-3. Vinyl Streamer invokes it as a separate process and does
not link against it. The binary is downloaded at install time and is not
vendored in this repository.
