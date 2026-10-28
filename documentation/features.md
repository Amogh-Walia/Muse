# Features

How each part of Muse behaves. Clean up, Scrape Details, Album art, Lyrics and Undo history are sub-tabs of the **Operations** tab, which also shows the recommended order for processing new songs (see the README). For the code layout and database, see [architecture.md](architecture.md).

## Ingestion

A song is **processed** once it's in the catalog *and* at the location the cleanup would give it (`tracks.processed`, with the target in `tracks.clean_path`). The **Ingest** tab lists:

- **Not ingested:** files on disk that aren't in the catalog. **Scan now** adds them.
- **Not processed:** catalogued songs that aren't at their clean location yet. The cleanup processes them.
- Downloads still in progress (`.part`), and catalogued files that have gone missing.
- Songs waiting in the **inbox** (`MUSE_INBOX`, default `Inbox/` in the library).

**The inbox** is where your downloader should save to. Muse creates it with an empty `.ndignore`, which Navidrome honours, so nothing in it reaches Navidrome or your phone. Everything else works on inbox songs as usual: scanning, Scrape Details, album art, lyrics. Clean up then moves each song to its clean place (`Artist/Year - Album/`); inbox songs are never at their clean place, so every one moves. That is the moment it appears in Navidrome, already finished. Lyrics files and the album folder's cover and other files move with it, and songs Clean up can't place yet (an unresolved name clash) stay in the inbox. Undo moves songs back into the inbox, out of Navidrome again.

Processed flags are recomputed after every scan, cleanup, undo and MusicBrainz apply.

## Cleanup

`cleanup/rules.py` holds the regex rules:

- **Artists:** the main `ARTIST` and `ALBUMARTIST` get a single person. Everyone credited goes into a multi-valued `ARTISTS` tag. The rules split on `;`, ` / `, `feat.`/`ft.`/`featuring`, `,`, `&` and `+`, and also on `and` inside a featured list. Names in `cleanup/protected_artists.txt` (e.g. "Fitz and The Tantrums") are never split. Compilations with three or more album artists, `VA` or `Apple Music` become `Various Artists`. Artist casing is unified across the library.
- **Titles:** `(feat. X)` moves out of the title into `ARTISTS`. Decorative years are removed: `(2014)`, `[2014]`, `(Remastered 2016)`, `- 2011 Remaster`.
- **Albums:** the same, plus format junk (`[FLAC]`, `{24-96}`, `[24B-44.1kHz]`), leading `2013 - ` / `[2014] `, ` - Single`, and ` - EP` becomes ` EP`.
- **Everywhere:** curly quotes and Unicode dashes become ASCII, whitespace is tidied, and dates like `2014-00-00` become `2014`.
- **Files:** `Album Artist/Year - Album/NN - Title.ext` (`D-NN` for multi-disc), built from the cleaned tags. Lyrics (`.lrc`), covers, logs and cue sheets move with their album.
- **One release, one album:** songs carrying the same MusicBrainz release id always get one album name, album artist and year, so they share a folder, even when their album tags differ (a store's "Parvana EP" next to MusicBrainz's "Parvana"). They take those of the bigger album they're spread over, counting its songs without MusicBrainz ids too.

Keeping the catalog and files in step:
- Each tag write or move is immediately mirrored in the DB and committed: the row is re-read from the file at its new path.
- Moved tracks keep their ID.
- When a run finishes, `verify` reports any catalog/disk mismatch. The UI shows it.
- In testing, the DB after a cleanup was identical to a fresh full scan of the same files, and the same held after undo.

Safety:
- Albums whose folder still has `.part` downloads are skipped.
- **Name clashes** are files ending up with the same name in any format (`01 - Song.flac` and `01 - Song.mp3`), or a file already sitting there. Duplicates already side by side count too: the same name in one folder in different formats (`09 - Song.flac` and `09 - Song.m4a`), which nothing would otherwise move. Each clash opens a side-by-side comparison: format, lossless or not, bit depth, sample rate, bitrate, duration, size, cover art, lyrics, tag count, MusicBrainz IDs and the key tags. The best value in each row is highlighted, and the best-quality copy is marked as recommended. You pick the copy to keep. The others move to `.muse-trash/` inside the library, which scans ignore and **Undo** restores from. Delete that folder once you're sure. Undecided clashes stay where they are.
- Audio data is never rewritten; mutagen edits tags in place. Existing padding is reused, FLAC keys keep their spelling, and MP3s keep their ID3 version.
- Every apply writes a journal to the journal folder (`MUSE_JOURNAL_DIR`), and **Undo** restores every tag value and path exactly. A file can end up a few bytes larger after undo, when restored text re-encodes slightly longer.

## Scrape Details (MusicBrainz)

The `musicbrainz/` service looks each album up on musicbrainz.org.

**Finding the release:**
- If the files already carry a valid release ID, it's used directly.
- Otherwise it searches in stages: title + artist, then the simplified title (`Bangarang EP` → `Bangarang`), then the title's words, then the title alone.
- Candidates are ranked on search score, artist match, track count, year and title.

**Matching:** the full tracklist is paired with your files by title, position and duration.

**Review:** proposals are stored in `mb_proposals` and reviewed in the dialog.
- *Matched*, *Check this match* or *Poor match*. Poor matches are never applied.
- Songs paired, and songs missing from your copy.
- Your files that aren't on the release.
- A per-song diff of every tag.

**Applying** writes Picard-style tags into the files and re-reads them into the catalog: title, artists, album, dates, track/disc numbers and totals, label, catalogue number, barcode, country, status, type, medium, and the release/release-group/track/recording/artist IDs.

**Completeness:** each release's tracklist is kept in `releases`. An album missing songs gets `complete = 0`. Its missing songs go into `missing_tracks` with only their name and place on the release, and they appear greyed out in the album view. Applying is journaled like the cleanup, so **Undo** reverts it.

**One album, one edition.** Albums pieced together from several downloads can end up with songs pointing at different editions of a release. Three things keep that from sticking:

- **Detection:** after every scan, cleanup, undo and MusicBrainz apply, and at startup, albums split across editions are flagged. They show a "Mixed editions" badge on the album card, a section in Scrape Details, and a "Needs attention" list on the Ingest tab.
- **Albums filed in several folders:** editions with different album names that share a MusicBrainz release group ("Delhi Belly" and "Delhi Belly: Original Motion Picture Soundtrack") count as one album. So does a **stray**: a song filed elsewhere that is one of an album's missing songs, with the same title, album name and track number (e.g. one track tagged with a different album artist and no MusicBrainz data). Title alone isn't enough: different songs often share titles.
- **Offline fix:** when one of those editions is a release the library already knows, an "align" proposal is prepared from the stored release details, with no lookup needed. Paired songs get that edition's tags. Songs not on the release have the other edition's release ids cleared; recording and artist ids stay. Completeness also matches songs by title, so a split album never shows songs it has as missing.
- **At the source:** the copy you keep in a name clash is moved into place even if unticked, and re-tagged to the edition the rest of its album is on. The clash dialog says so beforehand. "Look up albums" prefers an edition the library is already linked to over a fresh search.

MusicBrainz allows about one request a second, so a lookup takes about 2 seconds per album. Set `MUSE_MB_CONTACT` in `.env` to a URL or e-mail to include contact details in the User-Agent, as MusicBrainz asks.

## Operations: album art

The **Operations** tab lists albums with songs lacking embedded artwork. **Find artwork** gathers candidate covers into a cache (`MUSE_ART_CACHE`, named by content hash):

- **Image files in the album folder** and its `Scans`/`Artwork` subfolders, ranked by name (`cover`/`front` first, `back`/`inlay`/`CD` last).
- **Every distinct cover already embedded** in the album's songs, labelled with how many songs carry it. The most widely used comes first.
- **Online, from the Cover Art Archive** (coverartarchive.org): official covers up to 1200 px, located via the album's MusicBrainz release (it searches MusicBrainz if the files aren't linked yet).
- **Optionally, iTunes,** keeping only results whose album and artist names match.
- **An image from your drive:** **Choose image…**, or drop a file onto the album. It's checked to really be an image (JPEG/PNG/WebP/BMP/GIF, up to 25 MB).

Candidates show size, source and warnings (low resolution, not square). The likeliest front cover is pre-selected; square images win ties over wide scans.

The first visit runs the local search automatically. **Embed artwork for N albums** writes the chosen cover into each song that lacks one. Covers larger than 1200 px are scaled down with ffmpeg first. It uses FLAC picture blocks, MP3 APIC frames (keeping the ID3 version), M4A `covr` and Ogg picture tags. Optionally it also saves `cover.jpg` in the album folder when there's none, or replaces art songs already have.

A song's existing front cover is replaced, even a broken one players ignore; other pictures (back, booklet) are kept. Pictures that were there before are backed up beside the journal, so **Undo** restores them exactly. The catalog is re-read song by song, so `has_cover` and the album cards update immediately.

Journal names carry microseconds, so two runs in the same second can't overwrite each other's journal.

### Edit artwork (album page and song details)

An album's page has **✎ Edit artwork**, and so does each song's details panel. It opens one dialog for that album folder.

- **Choose a cover:** the folder's images and every different picture embedded in its songs (gathered when the dialog opens), plus **Search online** (Cover Art Archive, optionally iTunes) and **Choose image…** from your drive.
- **Put it on:**
  - **the whole album**, which covers every song and the folder's cover file. An existing `cover.jpg` (or `folder.jpg`, `front.jpg`) is set aside and replaced, since Navidrome shows the folder's file for the album.
  - **only some songs**, with each song's current picture shown next to it. From a song's panel, that song comes pre-ticked.
- **What it replaces:** whatever art the chosen songs had. Songs in formats that can't hold pictures (WAV, for instance) are marked and skipped.
- **Undo:** each change is one entry in Undo history, which puts back exactly the pictures and cover file there were.

Changing a song's artwork changes the file, so phone apps that keep songs offline download it again.

## Lyrics

Each song's lyrics status is kept in the catalog: **synced** (timed lines), **plain**, or none. It also records where the best copy is: a `.lrc` file next to the song, a `.txt` file, or embedded in the tags. Synced beats plain wherever it is. The status is refreshed at startup and after every job, because lyrics files can appear without the song changing.

- **Finding lyrics** (Operations → Lyrics): songs without lyrics, and optionally songs with only plain lyrics, are looked up on [LRCLIB](https://lrclib.net), a free community database. Muse first asks for an exact match on artist, title, album and length. Failing that, it searches and keeps only results whose title and artist match and whose length is within 2 seconds, so a live take or radio edit doesn't get the album version's lyrics. Misses are remembered and not asked again unless you tick "Ask again". Requests are kept to about one a second.
- **Saving:** chosen lyrics are saved next to the song: synced ones as `<song>.lrc`, plain ones as `<song>.txt`. Navidrome, Subsonic apps and most players read both, and the audio file isn't touched. It's journaled, so Undo removes them, or restores a file that was replaced.
- **Duplicates keep their lyrics:** when you resolve a name clash, a kept copy without lyrics, or with only plain ones, gets the discarded copy's better lyrics saved next to it. The comparison dialog says so beforehand. Copies side by side share one `.lrc` (it's named after the song without its extension), and it stays with the copy you keep.
- **Orphaned lyrics files:** `.lrc` files with no song of the same name, usually left behind by renames, are listed. Each comes with a suggested fix: reattach it to the one song it fits that has no lyrics, or move it to `.muse-trash/`.
- **In the player:** the ♪ button shows the playing song's lyrics. Synced lines scroll and highlight with the music, and clicking a line jumps there.

## Merge albums

Operations → Merge albums joins albums that show up as several: different editions, a stray song tagged with another album artist, or a name variant. You pick the albums and choose whose details to keep. Every song then takes that album's name, album artist, dates and MusicBrainz identity, and can optionally move into one folder. One Undo reverses the whole merge.

Suggestions come from three checks:

- **Mixed editions and strays** (the same check that flags them elsewhere): songs from one release group spread over different MusicBrainz editions or folders, or a song whose title, album name and track number fill a gap in another album.
- **Same name:** albums with the same name, once years, edition notes and spacing are cleaned off, sitting in different folders.
- **Longer name, same artist:** one album's name starts with another's ("Delhi Belly" and "Delhi Belly: Original Motion Picture Soundtrack").

The last check also catches real sequels and editions ("The Marshall Mathers LP 2"). **This is fine, ignore** stops a suggestion coming back. It applies to that exact set of albums, so a new album joining the group brings the suggestion back. Ignored suggestions are listed under the suggestions, each with **Suggest again**.

## Remake Various Artists

Some "albums" are really playlists, such as Apple Music's *… Essentials* or *Radio Hits*. Each song carries the right title and artist, but the album is the playlist, so everything files under `Various Artists/`. Operations → **Remake Various Artists** sends each song back to its own album.

- **Which albums are offered:**
  - Albums whose songs carry an Apple Music playlist id (`ITUNESALBUMID` starting `pl.`) or are named like a playlist (*Essentials*, *Playlist*, *Radio Hits*). These are pre-selected.
  - Albums filed as Various Artists that aren't linked to MusicBrainz, which you can tick too.
- **How songs are looked up:** each song is looked up on MusicBrainz by its **ISRC**, a code for the recording that most store-bought files carry. Without an ISRC, or when MusicBrainz doesn't know it, the lookup searches by title and artist and checks the length (within 8 seconds). That's two requests a song, at MusicBrainz's one a second. The lookup can be stopped and continued, and results are kept, so it isn't repeated.
- **How the album is chosen:** every release the recording is on is an option, ranked:
  1. one already in your library (same release group, or an album of the same name);
  2. not a compilation, live album or remix;
  3. official, and an album before an EP or single;
  4. the earliest, so a film song goes to its soundtrack rather than a later re-release;
  5. shared with the other songs looked up together.

  The best one is chosen. A dropdown offers the others (one edition per release group) and "Leave this song as it is".
- **Clashes are flagged before anything changes:**
  - **Joining an album you have:** the song files under that album's own name, album artist and dates, so it lands in the same folder. That holds when it's the very same release too, whose name in your files may differ from MusicBrainz's ("Parvana EP" / "Parvana").
  - **You already have the song** in that album: it's flagged. It's still remade, but it can't move onto the existing file, so it waits for Clean up, which shows both copies to compare and keeps one.
  - **Two songs remade into the same album under the same title**, for example the same song in two playlists: flagged the same way.
  - **Artist spelling:** an artist already in the library keeps its spelling. It's matched by MusicBrainz artist id, else by name, so a release crediting "Vishal & Shekhar" doesn't open a second folder next to `Vishal-Shekhar`. For joint credits, the main (first) artist is used.
- **Remake:**
  - The chosen songs get the release's tags (album, album artist, dates, track and disc numbers, MusicBrainz ids) and move into place like Clean up, with their lyrics files.
  - Their **artwork is taken out**: it's the playlist's cover (every song of an Apple playlist carries the same picture), not their album's. Without art, their new albums show up in **Album art** as albums to give a cover, from the Cover Art Archive or from songs of that album you already have. The old pictures are kept for Undo.
  - A playlist folder left without songs has its leftovers (the playlist's cover) moved to the trash.
  - It's one entry in Undo history, and the lookups are kept, so an undone remake can be redone without looking up again.
  - Songs MusicBrainz doesn't have stay where they are.

## Import / Export

**Import** brings a folder of music into the inbox in one go. Pick the folder with the folder picker, which shows mounted drives such as USB sticks, then **Check this folder** to see how many songs it holds and where they'll go.
- **Where it lands:** `<inbox>/<folder name>/`, with the folder's own structure underneath.
- **What comes along:** songs and the files that belong with them (lyrics, covers, cue sheets, rip logs, playlists). Anything else stays where it was.
- **Move or copy:** moving takes the files out of the source (a quick rename on the same disk; on another disk they're copied, then removed). Copying leaves the source as it was.
- **What's skipped:** files already in the inbox are left alone.
- **Afterwards:** the import scans the new songs into the catalog. It's one entry in Undo history, which puts moved files back where they came from and removes copies.
- **Stopping:** an import can be stopped; whatever was already brought in stays, and Continue carries on.
- **Refused folders:** a folder inside the library, or one containing it.

**Export** copies music out of the library. Choose from a collapsible tree of artists → albums → songs, then pick a destination folder and copy.
- **Choosing:** ticking an artist chooses all its albums, ticking an album chooses all its songs, and a half-filled box means only some are chosen. An album's songs are loaded when you open it, so long libraries stay quick. Search narrows the tree to matching artists and albums, opening them. "Select all shown" chooses everything matching the search.
- **A single song** comes with its own lyrics files and its album's cover, cue sheet and other shared files. Other songs' lyrics stay behind.
- **Structure:** albums keep the library's structure (`Artist/Year - Album/…`) with their lyrics files and covers.
- **Safe names:** on by default, this replaces the characters FAT/exFAT USB sticks, DVDs and Windows reject (`: ? * " < > | \`), so `Delhi Belly: Soundtrack` becomes `Delhi Belly - Soundtrack`.
- **Before copying:** free space is checked first. Files are copied under a temporary name and renamed when complete, so a stopped or failed export never leaves half files.
- **Running it again** skips files already there with the same size, so it carries on an interrupted export or tops up a stick with newly added albums.
- **The library isn't changed**, so exports aren't in Undo history. A destination inside the library is refused, since its copies would be catalogued as duplicates.

Imports and exports can take a while. Like other jobs they carry on if you leave the page, and they don't stop when no page is watching (lookups do).

## Long-running jobs

Scans, lookups and changes run on the server and keep going if you switch tabs, reload, or close the page. While one runs, the header shows it with its progress (click it to go to the job) and a Stop button for lookups. Its progress bar reappears wherever you return to it, even from another device. Only one job runs at a time; the buttons of a pane whose job is running are disabled.

Lookups (Scrape Details, Album art, Lyrics) can be stopped at any point, and keep everything found so far for review: **Continue** carries on with the rest. A lookup nobody is watching (no Muse page open for `MUSE_STOP_UNWATCHED` seconds, 2 minutes by default) stops by itself the same way; the next time you open that page it says so and offers Continue. Jobs that change your files always run to the end.

## Appearance

The **Appearance** button in the header sets the five accent colours (accent, links, success, warning, danger), each for light and dark mode; your system setting picks which. The page previews changes as you pick them. Save writes them to `.env` as `MUSE_COLOR_<NAME>` and `MUSE_COLOR_<NAME>_DARK`, leaving the rest of the file as it was. A colour left at its default isn't written, and Reset to defaults removes them all.
