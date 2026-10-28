# Muse

**Muse: My Unruly Songs, Edited.** A self-hosted web app that catalogs a folder of music files in SQLite, cleans up messy tags and file names, matches albums against MusicBrainz, and fills in missing cover art. Every change it makes to your files can be reviewed beforehand and undone.

## Features

- **Catalog:** every audio file (FLAC, MP3, M4A/ALAC, Ogg, Opus, WAV, …) with its full tags and audio properties, browsable by album and track, with cover art.
- **Ingest:** see which files on disk aren't catalogued yet and which songs haven't been organized, and scan with one click.
- **Inbox:** new downloads land in an inbox folder inside the library that Navidrome doesn't see, so songs reach your phone only once they're processed.
- **Import / Export:**
  - **Import** moves or copies a whole folder of music from anywhere on this machine (another disk, a USB stick) into the inbox, keeping its folder structure. Songs, lyrics, covers, cue sheets and rip logs come along, and Undo puts moved files back where they came from.
  - **Export** copies what you pick to another folder, such as a USB stick or a folder to burn to a DVD. You pick from a collapsible artist → album → song tree, choosing whole artists, whole albums or single songs. They keep the library's `Artist/Year - Album/` structure, lyrics files and covers. Names can be made safe for FAT/exFAT and Windows, free space is checked first, and running it again skips files already copied, so an interrupted export carries on.
- **Cleanup:** regex rules give each song one main artist, move `feat.` credits out of titles, and strip decorative years and `[FLAC]`-style junk. Files are then renamed into `Artist/Year - Album/NN - Title.ext`. You preview every change per album and song before applying. Duplicate files are compared side by side (format, bit depth, sample rate, bitrate, art, lyrics) so you choose which to keep.
- **Scrape Details:** finds the release on MusicBrainz behind each album, writes Picard-style tags and IDs, flags incomplete albums and lists their missing songs, and keeps albums assembled from several downloads on a single edition.
- **Remake Various Artists:** "albums" that are really playlists (like Apple Music's *… Essentials*) are split back into the songs' real albums. Each song is looked up on its own by its ISRC (or title, artist and length) and goes to the release it first came out on, joining that album when it's already in your library. Songs you already have are flagged for Clean up to compare.
- **Playback:** play songs and albums from the page. It streams through [Navidrome](https://www.navidrome.org/) when set up, which also serves Subsonic apps on phones and players, or straight from disk otherwise.
- **Lyrics:** shows each song's lyrics status, fills gaps with synced lyrics from [LRCLIB](https://lrclib.net) saved as `.lrc` files, tidies orphaned lyrics files, and scrolls synced lyrics along with the player.
- **Album art:** finds covers in album folders, in the songs themselves, on the Cover Art Archive and optionally iTunes, or takes an image from your drive, and embeds the one you pick. **✎ Edit artwork** on an album's page (or a song's details) changes the cover of the whole album or of chosen songs.
- **Safe by design:**
  - Audio data is never rewritten.
  - Every operation is journaled, and one click undoes it.
  - Duplicates go to a trash folder instead of being deleted.
  - The catalog is updated file by file, so it always matches the disk.

## Requirements

- Python 3.10 or newer
- [FFmpeg](https://ffmpeg.org/) (`ffprobe` and `ffmpeg` on your `PATH`)
- [mutagen](https://pypi.org/project/mutagen/), installed from `requirements.txt`

## Quick start

```sh
git clone <this repository> muse && cd muse
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # then set MUSE_LIBRARY to your music folder
.venv/bin/python app.py       # open http://127.0.0.1:8800
```

On first start Muse creates its SQLite database and scans the library. Later scans only read new or changed files: click **Scan now** on the Ingest tab, or run `python3 scan.py`.

## Processing new songs

The **Operations** tab walks you through it, one sub-tab per step:

Point your downloader at the **`Inbox/`** folder in your library. Navidrome doesn't see it (it holds a `.ndignore`), so new songs reach Navidrome and your phone only once they're finished.

1. **Scan** (Ingest tab → *Scan now*): add new downloads to the catalog once they've finished downloading.
2. **Scrape Details:** look the albums up on MusicBrainz and apply the matches.
3. **Album art:** add covers to albums without one.
4. **Lyrics:** fill in missing lyrics and tidy leftover lyrics files.
5. **Clean up:** keep the best copy of any duplicates, fix tags and file names, and move the songs out of the inbox into `Artist/Year - Album/`. This releases them to Navidrome.

Got a folder of music somewhere else, like another disk or a USB stick? **Import / Export → Import** brings it into the inbox in one go, ready for these steps.

Scrape Details comes first because it corrects album and artist names, which art, lyrics and folder names are all based on. Clean up comes last, so each song moves once, already finished: its lyrics files and the album's cover file move with it. Every step previews its changes, and the **Undo history** sub-tab reverses them, newest first.

## Configuration

Settings are read from the environment, or from a `.env` file in the project folder (copy [`.env.example`](.env.example)). Relative paths are relative to the project folder.

| Variable | Default | Purpose |
|---|---|---|
| `MUSE_LIBRARY` | (required) | Your music folder. Muse reads and organizes the files in it. |
| `MUSE_DB` | `data/muse.db` | The SQLite catalog. Created if it doesn't exist. |
| `MUSE_JOURNAL_DIR` | `data/journal` | Undo journals. |
| `MUSE_ART_CACHE` | `data/artwork-cache` | Downloaded and uploaded cover images. |
| `MUSE_UNDO_DAYS` | `30` | How long a change stays undoable. After that, its undo record and the duplicates it moved to `.muse-trash/` are deleted. `0` keeps everything. |
| `MUSE_INBOX` | `Inbox` | Folder inside the library where downloads land, hidden from Navidrome until Clean up moves songs into place. Empty: no inbox. |
| `MUSE_STOP_UNWATCHED` | `120` | Seconds after which a lookup (Scrape Details, artwork, lyrics) stops by itself if no Muse page is open, keeping what it found. `0`: never. |
| `MUSE_HOST` | `127.0.0.1` | Address the web app listens on. |
| `MUSE_PORT` | `8800` | Port the web app listens on. |
| `MUSE_MB_CONTACT` | (empty) | A URL or e-mail for the MusicBrainz User-Agent, which MusicBrainz asks for. |
| `NAVIDROME_URL`, `NAVIDROME_USER`, `NAVIDROME_PASSWORD` | (empty) | Optional [Navidrome](documentation/navidrome-setup.md) server for playback. Muse rescans it after every change and links songs to it. The password stays on the server. |
| `NAVIDROME_PUBLIC_URL` | `NAVIDROME_URL` | Address your browser uses for "Open in Navidrome" links. |
| `NAVIDROME_SERVICE` | `navidrome` | systemd user service Muse starts if Navidrome isn't answering. Empty: don't try. |
| `MUSE_COLOR_<NAME>`, `MUSE_COLOR_<NAME>_DARK` | see `config.py` | Accent colours (`#rrggbb`) for light and dark mode. Names: `ACCENT`, `LINK`, `OK`, `WARN`, `DANGER`. Set them from the **Appearance** menu in the app, which saves them here. |

Keep `MUSE_HOST` on `127.0.0.1` unless you put Muse behind authentication: it can rename and retag your files.

## Command line

```sh
python3 scan.py [FOLDER] [--full]          # update the catalog from the disk
.venv/bin/python -m cleanup                # preview the cleanup in the terminal
.venv/bin/python -m cleanup --apply        # apply it (--no-tags / --no-rename to limit it)
.venv/bin/python -m cleanup --undo         # undo the most recent change
.venv/bin/python -m cleanup --history      # undo history, trash, and when they expire (--prune to expire now)
.venv/bin/python -m unittest cleanup.test_rules cleanup.test_retention navidrome.test_service lyrics.test_files   # tests
```

## Documentation

- [Features](documentation/features.md): how ingestion, the cleanup, MusicBrainz and album art work, in detail
- [Architecture](documentation/architecture.md): code layout, database, journals and undo, jobs
- [Navidrome setup guide](documentation/navidrome-setup.md): playback through Navidrome and Subsonic apps
