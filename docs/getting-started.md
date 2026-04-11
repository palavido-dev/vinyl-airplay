# Getting Started

A 5 minute tour of Vinyl Streamer: what it is, what you can do with it, and where to look next.

> **New to the project?** Start here, then jump to the [User Guide](user-guide.md) once you want a proper feature tour. For exhaustive button by button detail, see the [Reference](reference.md).

---

## What is Vinyl Streamer?

Vinyl Streamer is a self hosted jukebox for your vinyl collection. It runs on a Raspberry Pi, captures audio from your turntable through a line level ADC, and does three things with it:

1. **Recognizes** the record using local audio fingerprinting (no cloud, no account).
2. **Streams** the live audio to AirPlay, Bluetooth, and browser outputs around your house.
3. **Records** each album side as a lossless FLAC so you can play it back later without touching the vinyl.

Once you have taught the Pi a record, you can play the physical vinyl or play the recording. Both appear as the same album in the catalog, and both stream to the same speakers. Your records stay on the shelf unless you feel like dropping the needle.

---

## Your first 10 minutes

The installer handles the boring parts. After you run `install.sh`, here is what a first visit to the web UI looks like.

### 1. Open the app

Point any browser on your network at `http://<your-pi-ip>:8080` or use the touchscreen directly. You land on the Library view.

![Library grid](images/01-library-grid.png)

The top bar shows the app name, album count, search, a Shelves/Library toggle, and a row of header buttons (sort, multi select, stats, playlists, shuffle, add, settings). The main area is a grid of albums. Every record you add shows up here.

### 2. Add a record

Tap the `+` button in the header. The Add Record form opens with three tabs: search Discogs by artist and title, scan a barcode with your phone camera, or enter the details manually.

![Add record](images/04-add-record.png)

Most of the time you type the artist and title, pick the right Discogs result, and save. Discogs provides the track listing, artwork, year, and label, so the album is fully populated before you have even touched the turntable.

### 3. Teach the Pi the record

Open the album you just added and tap **Record**. The Pi starts capturing audio from the line input and watches for silences between tracks. Drop the needle on side A. When the needle lifts, flip the record and keep going. When you are done, the full side is saved as a FLAC, each track is fingerprinted, and the album is officially "recorded".

![Album detail](images/03-album-detail.png)

From then on, that album has a green halo in the library grid to mark it as recorded.

### 4. Play it

Tap any recorded album. If nothing is currently playing, an output picker asks where to send the audio: the Pi itself, an AirPlay speaker like a HomePod, a paired Bluetooth speaker, or "This Device" (the browser you are using). Pick one, and playback starts immediately. You can also pick more than one AirPlay target for multi room.

Alternatively, drop the needle and play the vinyl. The Pi recognizes the record within a few seconds and starts streaming the live audio to the same outputs. Same controls, same transport, same album detail.

### 5. Browse while it plays

When something is playing, the Now Playing hero fills the main area.

![Now playing hero](images/11-nowplaying-hero.png)

Tap **Browse Library** (or either header toggle) to shrink the now playing view into a compact bar at the bottom and get the full grid back. Tap the bar, or the expand button in the transport controls, to send the hero full screen again.

![Library during playback](images/12-nowplaying-browse-mode.png)

---

## Where things live

These four screens cover 90% of normal use:

- **[Library](user-guide.md#library-view)** is the grid of every album in your collection. Search, sort, and shuffle live in the header. Tap a card to play, tap the small `i` to see details.
- **[Shelves](user-guide.md#shelves-view)** is a curated home view: Recently Played, Recently Added, Most Played, Unplayed, Favorites.
- **[Album detail](user-guide.md#album-detail-modal)** is where you record, learn, edit tracks, rate, favorite, and write notes.
- **[Settings](user-guide.md#settings)** is where you configure AirPlay, Bluetooth, EQ, storage, updates, and everything else.

---

## What to read next

- Want a guided tour of every feature? Go to the **[User Guide](user-guide.md)**.
- Looking for what a specific button does? Go to the **[Reference](reference.md)**.
- Want to know how the hardware is wired up or how it works under the hood? The top level **[README](../README.md)** covers hardware, audio paths, and the architecture.

---

## Quick glossary

A few terms you will see all over the docs:

- **Catalog**: the list of every album the app knows about, whether recorded or not.
- **Learn / Teach**: the act of fingerprinting a record so the Pi can recognize it later.
- **Record**: capturing the audio of a side into a FLAC file.
- **Recorded album**: an album that has at least one recorded side. These can be played back without the turntable.
- **Jukebox mode**: playing a recorded album from the FLAC file instead of the live vinyl.
- **Live vinyl**: the Pi streaming what is currently on the turntable, in real time.
- **Output**: any destination that can play audio: an AirPlay device, a Bluetooth speaker, the local speakers on the Pi, or "This Device" (the browser).
