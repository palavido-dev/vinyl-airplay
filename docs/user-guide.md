# User Guide

A feature by feature tour of Vinyl Streamer's web UI and the common workflows you'll use day to day.

> **Looking for a quick overview?** See [Getting Started](getting-started.md). **Need to know exactly what every button does?** See the [Reference](reference.md).

---

## Contents

- [Library view](#library-view)
- [Shelves view](#shelves-view)
- [Searching](#searching)
- [Sorting and grouping](#sorting-and-grouping)
- [Multi select](#multi-select)
- [Shuffle](#shuffle)
- [Album detail modal](#album-detail-modal)
- [Playback and Now Playing](#playback-and-now-playing)
- [Output picker](#output-picker)
- [Queue](#queue)
- [Playlists](#playlists)
- [Add a record](#add-a-record)
- [Recording and learning](#recording-and-learning)
- [Stats](#stats)
- [Equalizer](#equalizer)
- [Settings](#settings)
- [Keyboard shortcuts](#keyboard-shortcuts)
- [Screensaver](#screensaver)
- [Common workflows](#common-workflows)

---

## Library view

![Library grid](images/01-library-grid.png)

The Library is the default view. It shows every album in your catalog as a grid of cover cards with title and artist underneath. Albums you have recorded (and can play back without the turntable) show an amber border and a small indicator; unrecorded albums are slightly dimmed.

Each card has a heart (favorite), an info button, and a quick add to queue button. Tap the cover itself to play the album, or tap the info button to open the [album detail modal](#album-detail-modal).

**Header buttons left to right:**

1. **Shelves / Library toggle**: switches between the curated Shelves view and the flat Library grid. Either button also exits the full screen Now Playing hero when something is playing.
2. **Sort**: opens the [sort and group panel](#sorting-and-grouping).
3. **Multi select**: puts the grid into [multi select mode](#multi-select) so you can batch play, queue, favorite, or delete.
4. **Stats**: opens the [Listening Statistics](#stats) modal.
5. **Playlists**: slides out the [playlists panel](#playlists).
6. **Shuffle**: tap for an instant library wide shuffle. See [Shuffle](#shuffle).
7. **Add**: opens the [Add a record](#add-a-record) form.
8. **Settings**: opens the [Settings](#settings) modal.

The status dot on the left side of the row below the header shows what the player is doing: Idle, Playing, Recording, or Streaming live vinyl.

## Shelves view

![Shelves home](images/02-shelves-home.png)

Shelves is a curated home screen that groups your records into horizontal rows: **Recently Played**, **Recently Added**, **Most Played**, **Unplayed**, **Favorites**, **Top Rated**, and **Genres**. Each row has a **See All** link on the right to expand that row into a full grid.

Shelves is a great starting point if you have a large collection and want inspiration rather than a flat alphabetical list. Tap the **Library** button in the header at any time to go back to the full grid.

## Searching

The search bar at the top searches across artist, album, genre, year, label, and your album notes. Results filter live as you type. Clear the search to go back to the full library.

Search also works from the touchscreen: tap the search bar and an on screen keyboard slides up (handy in kiosk mode where you may not have a physical keyboard).

Press `F` anywhere to jump to the search bar.

## Sorting and grouping

![Sort panel](images/08-sort.png)

Tap the sort button in the header to open the sort panel. You can choose:

- **Sort by**: Artist, Title, Year, Recent, Newest, Favorites, Rating
- **Group by**: Genre, Artist, or Favorites
- **Filter**: Not Recorded, Recorded (hide recorded or hide unrecorded albums)

The panel closes automatically when you tap outside it. Sort preferences persist across sessions.

## Multi select

![Multi select](images/09-multiselect.png)

Tap the checkmark in the header to enter multi select mode. Every card grows a checkbox in its top left corner, and a green action bar slides in at the bottom. Tap cards to toggle them, then pick an action from the bar:

- **Play**: queues every selected album and starts playback immediately.
- **Queue**: adds every selected album to the current queue without starting playback.
- **Favorite**: marks every selected album as a favorite.
- **Delete**: permanently removes the selected albums from the catalog (with an undo toast).
- **Cancel**: exits multi select without doing anything.

Long pressing a card in normal mode is another way to enter multi select with that card already selected.

## Shuffle

Tap the shuffle button in the header to add every recorded album in your collection to the queue in random order and start playback. It is the fastest way to turn the Pi into background music. If something is already playing, shuffle replaces the queue with the new shuffled list.

## Album detail modal

![Album detail](images/03-album-detail.png)

Tap the small `i` button on any album card to open the album detail modal. Everything about that record lives here:

- **Cover art**: tap to edit (upload a replacement if the Discogs art is wrong) or let the app fetch a new one.
- **Title and artist**: tap either to edit inline.
- **Meta chips**: year, genre, and label. Tap a chip to edit.
- **Favorite heart**: toggle favorite.
- **Star rating**: tap a star to rate the album 1 to 5.
- **Notes**: a free text field for pressing details, condition, where you bought it, anything you want to remember. Tap **Add Note** or the existing note to edit.
- **Track listing**: side A, side B, and so on. Tap any track to start playing from that exact point. Next to each track is a small edit button for fixing titles and a duration.
- **Recorded audio panel**: lists every FLAC recording tied to this album, with the option to re fingerprint, delete, or edit track boundaries.
- **Action row**: **Play**, **Queue**, **Learn / Record**, and a kebab menu with **Re fingerprint**, **Edit track boundaries**, **Delete album**, and more.

Close the modal by tapping the X, tapping outside, or pressing Escape.

## Playback and Now Playing

When you start playback, the Now Playing hero fills the main area with large artwork, the current track title, the artist, and transport controls.

![Now playing hero](images/11-nowplaying-hero.png)

The transport row has (left to right): an **expand** button that only shows when you have collapsed the hero, previous track, play/pause, next track, the current track timer, a progress bar you can scrub, the remaining time, and a volume slider. Below the transport is a **Browse Library** button that shrinks the hero into a compact bar and shows the library grid again so you can find something else to play without stopping the current record.

![Library during playback](images/12-nowplaying-browse-mode.png)

In this collapsed mode the now playing bar stays at the bottom of the screen. To send the hero back full screen, tap the bar's title area, tap the expand button in the transport, or click either **Shelves** or **Library** in the header (the buttons double as "exit browse mode" shortcuts because they always return you to a clean view).

Tapping the cover art in the Now Playing bar opens the current album's [detail modal](#album-detail-modal).

## Output picker

The first time you start playback (or when you tap **Switch Output** from the transport), the output picker asks where to send the audio. It lists every known device in three groups:

- **This Device**: plays through the browser you are currently using. Good for listening on headphones at your desk.
- **AirPlay**: every HomePod, AirPort Express, or AirPlay receiver the Pi has discovered on your network. You can pick more than one for multi room.
- **Bluetooth**: any A2DP speaker or headphones you have paired in Settings. Only one Bluetooth device can stream at a time.
- **Local**: speakers plugged directly into the Pi's audio output.

Tap a device to select or deselect it, then tap outside the picker to confirm and start playing. The last used combination is remembered for the next session. You can rename or hide devices from this picker so your list stays clean.

## Queue

Every album you tap with something already playing is added to the queue. Open the queue panel (press `Q` or tap the queue button in the Now Playing bar) to see what is coming up next. The panel lets you:

- Drag to reorder sides.
- Remove individual items.
- Clear the whole queue.
- Save the current queue as a playlist (see [Playlists](#playlists)).

## Playlists

![Playlists panel](images/07-playlists.png)

The Playlists panel slides out from the left. It has three tabs:

- **Albums**: ordered collections of complete albums. Play from the beginning, shuffle within the playlist, or edit the order.
- **Songs**: ordered collections of individual tracks.
- **Smart**: smart playlists generated by rules like "most played in the last month" or "unrecorded rock albums". Edit the rules or create new ones with the Smart Playlist Builder.

Tap **+ New Playlist** at the bottom to create a new playlist from scratch, or save the current queue from the queue panel.

## Add a record

![Add record](images/04-add-record.png)

Tap **+** in the header to add a new album. The form has three modes:

- **Search Discogs**: type artist and album, pick from the search results, review the track listing on the next step, and save. Artwork and metadata come across automatically. This is the default and by far the easiest path.
- **Scan Barcode**: uses your phone's camera to scan a UPC and look up the matching release on Discogs. Camera access requires HTTPS, so use `https://<your-pi-ip>:8443` from your phone after installing the CA certificate from Settings (see [Mobile Access](#mobile-access-and-certificates)).
- **Enter Manually**: for bootlegs, self released records, or anything Discogs does not have. Fill in title, artist, year, and track listing by hand.

Once saved, the album appears in your catalog immediately (unrecorded, with a dim border).

## Recording and learning

Once an album is in your catalog, you need to **record** each side so Vinyl Streamer can play it back without the turntable and recognize it when you drop the needle again.

From the album detail modal, tap **Record**. A panel slides down with:

- A countdown timer that waits for you to drop the needle (you can skip it).
- A live color coded level meter (green/yellow/red) so you can see whether your input levels are too hot.
- A big **Stop** button.
- A **Flip side** prompt that appears when the recorder detects the end of side A.

The recorder captures the full side as a FLAC file while it is running. As soon as the side ends, it runs silence detection to split the continuous audio into tracks and uses the Discogs track listing to snap boundaries to the right places. Every track is then fingerprinted so the Pi can identify it on future plays.

After all sides are recorded, the album gets an amber border in the library to mark it as recorded, and you can play it back from the library or from the album detail's **Play** button.

If a split lands in the wrong spot, open the album, find the track, tap the edit button, and adjust the boundaries manually in seconds.

## Stats

![Stats modal](images/06-stats.png)

The Listening Statistics modal (header bar chart icon) shows:

- **Total plays**, **total tracks**, and **listening hours** across your entire collection.
- **Most Played Albums** with play counts and last played dates.
- **Most Played Tracks** (individual songs, not whole albums).
- **Genre breakdown** if you have tagged your records.

Useful for finding out which records you actually listen to versus the ones you just own.

## Equalizer

![EQ](images/10-eq.png)

Vinyl Streamer applies a real time 5 band EQ before streaming. Open the EQ panel from Settings or tap the EQ button in the Now Playing transport to adjust bass and treble on the fly. Presets include Flat, Jazz, Rock, Hip Hop, Electronic, Vocal, Classical, Bass Boost, Warm, and Bright. Custom curves are saved automatically.

Everything is applied in the digital domain using 64 bit float shelving filters before the audio reaches AirPlay, Bluetooth, the browser, or the local DAC. For details on what "lossless" means when EQ is involved, see [Audio Quality](../README.md#audio-quality-and-lossless) in the README.

## Settings

![Settings](images/05-settings.png)

Settings is the big modal with everything else. It is divided into collapsible groups.

### Audio

- **AirPlay Devices**: scan, pair, rename, hide, and test AirPlay endpoints.
- **Bluetooth Speakers**: scan, pair, connect, and disconnect A2DP devices.
- **Vinyl Streaming**: **Start Streaming** kicks off the live vinyl pipeline on demand. **Auto stream when needle detected** makes it automatic.
- **Default device**: which audio device the Pi uses as its local output.
- **Audio Input**: pick the capture device (HiFiBerry, Scarlett, USB mic, and so on) and adjust input gain.
- **Crossfade**: 0 to 2 seconds of equal power crossfade between album sides, or 0 for pure gapless.

### Library

- **Discogs sync**: backfill missing Discogs IDs and sync metadata.
- **Fetch missing artwork**: batch download covers for any albums without art.
- **Find duplicates**: open the Duplicates modal to clean up the catalog.
- **Download catalog**: export a SQLite copy and a JSON manifest of your collection for backup.

### Personalization

- **App name**: the text in the top left corner ("The Listening Room" by default).
- **Accent color** and **density**: if you want to theme the app.

### System

- **Mobile access and certificates**: generate and download the mkcert CA for HTTPS access on your phone. See [Mobile access and certificates](#mobile-access-and-certificates) below.
- **Check for updates**: pull the latest release, install it, and restart the service. Rolls back automatically on failure.
- **Backup settings** and **Restore settings**: export or import `settings.json`.
- **Storage**: see free space and configure where recordings live.

### Mobile access and certificates

Vinyl Streamer runs an HTTPS server on port 8443 alongside the plain HTTP server on 8080 so that phone features like the barcode scanner can request camera permission. The app uses [mkcert](https://github.com/FiloSottile/mkcert) to generate a local Certificate Authority and a trusted certificate for the Pi.

From the Mobile Access section of Settings, tap **Download Certificate** and follow the instructions for your platform (iOS installs it as a profile, Android installs via the security settings). After the CA is trusted, `https://<your-pi-ip>:8443` loads without a browser warning and the barcode scanner works.

## Keyboard shortcuts

Vinyl Streamer has a small set of shortcuts designed for kiosk mode and desktop browsers. Press `?` to show the list:

| Key | Action |
|---|---|
| Space | Play / pause |
| N | Next track |
| Up / Down | Volume |
| Q | Toggle queue panel |
| P | Toggle playlists panel |
| S | Open settings |
| F | Focus search bar |
| Escape | Close the current modal or panel |
| ? | Show keyboard shortcuts |

## Screensaver

After a configurable idle period, Vinyl Streamer fades into a full screen Now Playing screensaver: a spinning vinyl disc, track title and artist, a progress bar, the current side indicator, and a simple animated EQ visualization. Designed for the dedicated touchscreen so it looks good glanced at from across the room.

Tap anywhere to wake it.

---

## Common workflows

### Adding and recording a new album

1. Tap **+** in the header.
2. Type the artist and album, pick the right Discogs result, save.
3. Open the album from the library.
4. Tap **Record**.
5. Drop the needle on side A.
6. When the side ends, flip and let the recorder continue (or tap Stop).
7. Confirm track boundaries in the detail modal if anything landed wrong.

The album now plays back from the FLAC whenever you tap it.

### Playing live vinyl without recording

1. Open Settings and enable **Auto stream when needle detected** (or leave it on).
2. Drop the needle.
3. Vinyl Streamer identifies the record if it has been learned before, otherwise streams it as "Live input" until you decide to record it.

### Multi room listening

1. Tap a recorded album or start playback from the Now Playing transport.
2. On the output picker, tap every AirPlay device you want to use.
3. Tap outside to confirm.
4. The Pi streams to all of them in sync, with per device volume available from the Now Playing transport.

One Bluetooth device can run alongside the AirPlay group if you want to include a Bluetooth headphone in the mix.

### Building and saving a playlist

1. Tap a few albums from the grid to queue them (multi select helps here).
2. Open the queue panel.
3. Rearrange with drag handles, remove items you do not want.
4. Tap **Save as playlist** at the bottom of the queue.
5. Give it a name. It now shows up in the Playlists panel.

### Shuffling the whole recorded collection

1. Tap the shuffle button in the header.
2. Pick an output.
3. Enjoy the ride.

### Backing up your catalog

1. Open Settings then the **Library** group.
2. Tap **Download Catalog Database** for a SQLite copy.
3. Tap **Download JSON Manifest** for a plain text inventory of your records.
4. Pair with `rsync` to a NAS for automated FLAC backup.

---

## Where next

- **[Reference](reference.md)**: every button, field, and API route in one place.
- **[README](../README.md)**: hardware, install, architecture, audio paths.
- **[Getting Started](getting-started.md)**: the 5 minute intro if you need to orient yourself again.
