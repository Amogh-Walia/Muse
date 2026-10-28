"""Make songs singles: each its own one-song release by its main artist, tagged as a single.

A single is stored the way MusicBrainz-tagged singles already are: album named after the song, track 1 of 1,
release type "single" (Navidrome and most players list those apart from albums), and no MusicBrainz release
ids from wherever the song came from. Clean up's usual layout then gives it its own folder,
Artist/Year - Title/. A song taken out of a playlist-style album also loses the playlist's picture, which
isn't its own (Album art can then give it one). Journaled: one Undo reverses it.
"""
import collections
import json
import os

import crud
import scan

from . import apply as applier, rules, tags

def _no_progress(done, total, message):
    pass


def single_values(song, released):
    """The tags that make `song` a single. `released`: a release date from the file when it has no date."""
    title = rules.clean_title(song["title"] or os.path.splitext(song["filename"])[0])[0]
    artists = rules.split_artists([song["artist"]]) if song["artist"] else []
    main = artists[0] if artists else song["album_artist"]
    date = song["year"] and str(song["year"])
    date = rules.clean_date(released) if (released and not date) else date
    values = {"album": [title], "albumartist": [main] if main else [], "tracknumber": ["1"], "tracktotal": ["1"],
              "discnumber": ["1"], "disctotal": ["1"], "releasetype": ["single"],
              "musicbrainz_albumid": [], "musicbrainz_releasegroupid": [], "musicbrainz_releasetrackid": [],
              "musicbrainz_albumartistid": []}
    if date:
        values["date"] = [date]
    return values


def _songs(conn, track_ids):
    wanted = set(track_ids)
    return [s for group in crud.tracks.by_album(conn).values() for s in group if s["id"] in wanted]


def apply(db_path, track_ids, progress=_no_progress):
    """Make the songs `track_ids` singles, then move each into its own folder."""
    from musicbrainz import remake  # its playlist detection and picture removal
    conn = crud.connect(db_path)
    root = crud.get_root(conn)
    songs = _songs(conn, track_ids)
    if not songs:
        conn.close()
        raise LookupError("no such songs")
    released = crud.tracks.tag_lookup(conn, ("releasetime",))
    playlists = {a["folder"] for a in remake.candidates(conn) if a["playlist"]}
    entries, stats = [], collections.Counter()
    stamp = applier.journal_stamp()
    backup = f"{stamp}-single-backup"
    try:
        for i, song in enumerate(songs):
            progress(i, len(songs) + 1, song["path"])
            full = os.path.join(root, song["path"])
            values = single_values(song, released.get(song["id"], {}).get("releasetime"))
            size = os.path.getsize(full)
            before = tags.write(full, values)
            entries.append({"op": "tags", "path": song["path"], "before": before, "size": size})
            if song["folder"] in playlists:  # the playlist's picture isn't the single's
                stats["art_removed"] += remake._strip_art(root, song["path"], backup, entries)
            scan.refresh(conn, root, song["path"])
            conn.commit()
            stats["singles"] += 1
        progress(len(songs), len(songs) + 1, "Moving each into its own folder…")
        stats["moved"] = applier.move_into_place(conn, root, [s["path"] for s in songs], entries)
        for folder in {s["folder"] for s in songs} & playlists:  # a playlist left without songs: its cover goes too
            stats["folders_emptied"] += applier.trash_leftovers(root, folder, stamp, entries)
        crud.releases.recompute(conn)
    finally:
        conn.commit()
        journal = None
        if entries:
            os.makedirs(applier.JOURNAL_DIR, exist_ok=True)
            journal = os.path.join(applier.JOURNAL_DIR, f"{stamp}-single.json")
            with open(journal, "w", encoding="utf-8") as f:
                json.dump({"root": root, "created": stamp, "kind": "single", "stats": stats, "entries": entries,
                           **({"backup": backup} if stats["art_removed"] else {})}, f, ensure_ascii=False, indent=1)
        conn.close()
    return {"stats": dict(stats), "journal": journal}
