"""Merge albums by hand: when one album shows up as several (editions, a stray song with another album
artist, a name variant), give all their songs one album's details and optionally move them into one folder.

The album you keep provides the album-level tags: name, album artist, dates and MusicBrainz release
identity. Songs from the other albums take those values; track-level tags are left alone, except a
MusicBrainz release-track id pointing at another edition, which is cleared. One journal covers the
tag writes and the moves, so a single Undo reverses the merge.
"""
import collections
import json
import os

import crud
import scan

from . import apply as applier, plan as planner, rules, tags
from .apply import JOURNAL_DIR, LYRICS_SIDECARS, _carry_leftovers, _move, _move_track

ALBUM_FIELDS = ("album", "albumartist", "date", "originaldate",
                "musicbrainz_albumid", "musicbrainz_releasegroupid", "musicbrainz_albumartistid")


def _no_progress(done, total, message):
    pass


def album_key(folder, album):
    return f"{folder}\x00{album or ''}"


def _groups(conn):
    return {album_key(folder, songs[0]["album"]): songs for (folder, _), songs in crud.tracks.by_album(conn).items()}


def albums(conn):
    """Every album (folder + album name) with a few facts to recognise it by."""
    out = []
    for key, songs in _groups(conn).items():
        artists = collections.Counter(s["album_artist"] or s["artist"] for s in songs)
        years = [s["year"] for s in songs if s["year"]]
        out.append({"key": key, "folder": songs[0]["folder"], "album": songs[0]["album"],
                    "artist": artists.most_common(1)[0][0], "year": min(years) if years else None, "songs": len(songs),
                    "musicbrainz": any(s["mb_album_id"] for s in songs), "cover_id": next((s["id"] for s in songs if s["has_cover"]), None)})
    return sorted(out, key=lambda a: ((a["artist"] or "").lower(), (a["album"] or "").lower()))


def _base(name):
    return rules.clean_album(name or "").lower().replace(" ", "")


def suggestions(conn):
    """Albums that look like one album split up: what the automatic check finds (editions, strays), plus
    same name in different folders, and same artist where one name extends the other ("Delhi Belly" /
    "Delhi Belly: Original Motion Picture Soundtrack"). Sets of albums marked as fine are left out."""
    groups = _groups(conn)
    keys_of = {(folder, (songs[0]["album"] or "").lower()): key for key, songs in groups.items() for folder in [songs[0]["folder"]]}
    found = []
    for m in crud.releases.mixed_albums(conn):
        keys = [keys_of[tuple(p)] for p in m["parts"] if tuple(p) in keys_of]
        if len(keys) > 1:
            found.append({"keys": keys, "reason": "songs point at different editions, or a stray song belongs to it"})
    info = albums(conn)
    by_name = collections.defaultdict(list)
    for a in info:
        by_name[_base(a["album"])].append(a)
    for name, same in by_name.items():
        if len(same) > 1:
            found.append({"keys": [a["key"] for a in same], "reason": "same album name in different folders"})
    by_artist = collections.defaultdict(list)
    for a in info:
        by_artist[(a["artist"] or "").lower()].append(a)
    for same in by_artist.values():
        for a in same:
            for b in same:
                ka, kb = _base(a["album"]), _base(b["album"])
                if a is not b and len(ka) >= 4 and kb.startswith(ka) and kb != ka:
                    found.append({"keys": [a["key"], b["key"]], "reason": f"“{b['album']}” looks like a longer name for “{a['album']}”"})
    ignored = crud.merges.ignored(conn)
    seen, out = set(), []
    for f in found:
        sig = frozenset(f["keys"])
        if len(sig) > 1 and sig not in seen and not any(sig < other for other in (frozenset(g["keys"]) for g in found)) \
                and crud.merges.signature(sig) not in ignored:
            seen.add(sig)
            out.append({**f, "keys": sorted(sig), "albums": [a for a in info if a["key"] in sig]})
    return out


def ignore(conn, keys, reason=None):
    """Mark a set of albums as fine to keep apart, so it isn't suggested again."""
    by_key = {a["key"]: a for a in albums(conn)}
    if len(set(keys)) < 2:
        raise ValueError("pick at least two albums")
    described = [{k: by_key[key][k] for k in ("album", "artist", "folder", "year", "songs")} if key in by_key
                 else {"album": key.split("\x00")[-1], "folder": key.split("\x00")[0]} for key in keys]
    crud.merges.ignore(conn, keys, described, reason)


def _target_values(root, songs):
    """The album-level tags of the album being kept: for each field, the value most of its songs have."""
    counts = {f: collections.Counter() for f in ALBUM_FIELDS}
    for s in songs:
        values = tags.read_fields(os.path.join(root, s["path"]), ALBUM_FIELDS)
        for f in ALBUM_FIELDS:
            counts[f][tuple(values[f])] += 1
    return {f: list(c.most_common(1)[0][0]) if c else [] for f, c in counts.items()}


def preview(conn, root, keys, target_key):
    """What merging would change, song by song, plus warnings."""
    groups = _groups(conn)
    if target_key not in keys or len(set(keys)) < 2 or any(k not in groups for k in keys):
        raise ValueError("pick at least two albums, one of them to keep")
    target = _target_values(root, groups[target_key])
    target_release = (target["musicbrainz_albumid"] or [None])[0]
    songs, positions = [], collections.defaultdict(list)
    for key in keys:
        for s in groups[key]:
            values = {f: v for f, v in target.items()}
            current = tags.read_fields(os.path.join(root, s["path"]), ("musicbrainz_albumid", "musicbrainz_releasetrackid"))
            if current["musicbrainz_releasetrackid"] and (current["musicbrainz_albumid"] or [None])[0] != target_release:
                values["musicbrainz_releasetrackid"] = []  # it points at a track on another edition
            changes = [{"field": f, "old": "; ".join(old), "new": "; ".join(new)}
                       for f, old, new in tags.compare(os.path.join(root, s["path"]), values)]
            songs.append({"path": s["path"], "title": s["title"], "album": s["album"], "from_kept": key == target_key,
                          "disc": s["disc_no"] or 1, "track": s["track_no"], "values": values, "changes": changes})
            if s["track_no"]:
                positions[(s["disc_no"] or 1, s["track_no"])].append(s["title"])
    warnings = [f"Disc {d}, track {t} is used by {len(titles)} songs ({', '.join(titles)}): Clean up will show them as "
                f"duplicates to compare." for (d, t), titles in sorted(positions.items()) if len(titles) > 1]
    return {"target": {"key": target_key, "album": (target["album"] or [""])[0], "artist": (target["albumartist"] or [""])[0],
                       "date": (target["date"] or [""])[0], "musicbrainz": target_release},
            "songs": songs, "warnings": warnings}


def apply(db_path, keys, target_key, move=True, progress=_no_progress):
    """Merge: retag every song of the chosen albums, then (with move) bring them together in one folder."""
    conn = crud.connect(db_path)
    root = crud.get_root(conn)
    result = preview(conn, root, keys, target_key)
    entries, stats = [], collections.Counter()
    stamp = applier.journal_stamp()
    work = [s for s in result["songs"] if s["changes"]]
    try:
        for i, s in enumerate(work):
            progress(i, len(work) + 1, s["path"])
            full = os.path.join(root, s["path"])
            size = os.path.getsize(full)
            before = tags.write(full, {c["field"]: s["values"][c["field"]] for c in s["changes"] if c["field"] in s["values"]})
            entries.append({"op": "tags", "path": s["path"], "before": before, "size": size})
            scan.refresh(conn, root, s["path"])
            conn.commit()
            stats["retagged"] += 1
        if move:
            progress(len(work), len(work) + 1, "Bringing the songs together in one folder…")
            merged = {s["path"] for s in result["songs"]}
            for album in planner.build(root)["albums"]:
                for t in album["tracks"]:
                    if t["path"] in merged and t["new_path"] != t["path"] and not t["clash"] \
                            and not os.path.exists(os.path.join(root, t["new_path"])):
                        _move_track(conn, root, t["path"], t["new_path"], entries)
                        for ext in LYRICS_SIDECARS:
                            sidecar = os.path.splitext(t["path"])[0] + ext
                            if os.path.exists(os.path.join(root, sidecar)):
                                _move(root, sidecar, os.path.splitext(t["new_path"])[0] + ext, entries, "extra")
                        scan.refresh(conn, root, t["new_path"])
                        conn.commit()
                        stats["moved"] += 1
            _carry_leftovers(root, entries)
        crud.releases.recompute(conn)
    finally:
        conn.commit()
        journal = None
        if entries:
            os.makedirs(JOURNAL_DIR, exist_ok=True)
            journal = os.path.join(JOURNAL_DIR, f"{stamp}-merge.json")
            with open(journal, "w", encoding="utf-8") as f:
                json.dump({"root": root, "created": stamp, "kind": "merge", "stats": stats, "entries": entries}, f,
                          ensure_ascii=False, indent=1)
        conn.close()
    return {"stats": dict(stats), "journal": journal, "warnings": result["warnings"]}
