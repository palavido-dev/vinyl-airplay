# Hardware tiers

Vinyl Streamer’s reference build works. This doc is the plan for keeping a **Studio** SKU solid while hunting a cheaper **Essential** path — without abandoning Raspberry Pi until there’s a clear reason.

## Studio (shipping candidate)

Current personal/reference BOM from the README:

| Role | Part direction | Why |
|---|---|---|
| Compute | Raspberry Pi 5 (8GB) | Headroom for capture + encode + kiosk Chromium |
| Audio | HiFiBerry DAC2 ADC Pro | Clean Pi HAT line-in/out, fewer USB cables |
| Storage | NVMe via PCIe HAT + modest SSD | Reliable FLAC library + fingerprints |
| Display | ~10.1" IPS touch HDMI | Dedicated jukebox presence |
| Output | Existing AirPlay / Bluetooth / local paths | Already implemented |

**Ship rule:** Studio is what gets quoted as “available now” until Essential passes the checklist below.

## Essential (validation queue)

Goal: same software story, lower parts cost, acceptable audio and UX.

| Experiment | Hypothesis | Pass criteria |
|---|---|---|
| Pi 4 (4GB or 8GB) | Enough for capture + playback if Chromium load is managed | Stable 16/44.1 capture; no xruns during record+stream; UI usable |
| Cheaper / smaller SSD | Library size dominates need, not speed | Day-long burn-in; no filesystem drama; cold boot reliable |
| Class-compliant USB ADC | Drop HAT cost; reuse Scarlett-class or cheaper USB interfaces | Deterministic ALSA device after reboot; clean levels; documented cabling |
| Alternate cheaper touch | 7" or lower-cost 10" panels | Readable library grid; touch targets OK; kiosk boots cleanly |
| Headless SKU | Phone/tablet UI only; biggest BOM cut | First-run WiFi story works; buyers accept no local screen |

**Fail closed:** if a cheaper part makes recording or recognition flaky, it does not ship — even if the spreadsheet looks better.

## Platforms other than Raspberry Pi

Open in principle; not the first optimization.

| Option | Upside | Downside for this project |
|---|---|---|
| Stay on Pi | Known audio stack, HATs, install docs, community | Parts cost / availability swings |
| Other ARM SBCs (Orange/Rock/etc.) | Sometimes cheaper compute | Audio HAT / BlueALSA / display bring-up cost |
| Mini PC + USB interface | Easy USB ADCs, strong CPU | Bigger, pricier, less “appliance,” more power |
| CM4/CM5 custom carrier | Real product enclosure path | Tooling and NRE only make sense after demand is proven |

**Recommendation:** finish Essential validation on Pi first. Revisit CM/custom boards only if prebuilt volume or enclosure quality clearly needs it.

## Suggested validation order

1. Headless Pi 5 with USB ADC (software risk low, BOM lesson high)
2. Pi 4 + same USB ADC
3. Cheaper display on a known-good audio path
4. Smaller storage tier with documented library-size guidance
5. Only then consider a public Essential SKU price

## Crowdfunding gate

Do not crowdfund a BOM that has not shipped as a hand-built preorder. Kickstarter / Crowd Supply makes sense after:

- at least a few paid Studio units delivered
- Essential pass/fail written up (even if Essential is cancelled)
- packaging, shipping weight, and support load are known

See [Prebuilt](prebuilt.md) for the made-to-order offer that should run before any campaign.
