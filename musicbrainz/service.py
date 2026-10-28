"""MusicBrainz service: find the release behind each local album, then write it into files and the catalog.

Two steps, like the cleanup:
  fetch  looks albums up and stores a proposal per album (release, per-song tag changes, missing songs)
  apply  writes the chosen proposals into the files, links them to their release in the database and
         records which albums are incomplete. It journals like the cleanup, so the cleanup's Undo reverts it.
"""
import collections
import json
import os
import re

import crud
import scan
from cleanup import apply as cleanup_apply, rules, tags

from . import client, match

# Tags that tie a song to one particular edition; cleared from songs that don't belong to the album's edition.
# (Recording and artist ids identify the song itself and are kept.)
EDITION_FIELDS = ("musicbrainz_albumid", "musicbrainz_releasegroupid", "musicbrainz_releasetrackid",
                  "musicbrainz_albumartistid")
MBID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
LABELS = {"title": "Title", "artist": "Artist", "artists": "All artists", "album": "Album", "albumartist": "Album artist",
          "date": "Release date", "originaldate": "Original date", "tracknumber": "Track", "tracktotal": "Tracks on disc",
          "discnumber": "Disc", "disctotal": "Discs", "label": "Label", "catalognumber": "Catalogue no.",
          "barcode": "Barcode", "releasecountry": "Country", "releasestatus": "Status", "releasetype": "Type",
          "media": "Medium", "musicbrainz_albumid": "MB release", "musicbrainz_releasegroupid": "MB release group",
          "musicbrainz_releasetrackid": "MB track", "musicbrainz_trackid": "MB recording",
          "musicbrainz_artistid": "MB artist", "musicbrainz_albumartistid": "MB album artist"}


def _no_progress(done, total, message):
    pass


def album_key(folder, album):
    return f"{folder}\x00{album}"


def _linked(conn, songs):
    """Already applied: every song points at a release we have stored."""
    ids = {s["mb_album_id"] for s in songs}
    return len(ids) == 1 and None not in ids and conn.execute(
        "SELECT 1 FROM releases WHERE mb_release_id = ?", (next(iter(ids)),)).fetchone() is not None


def albums_to_fetch(conn, refresh=False):
    pending = {p["album_key"] for p in crud.releases.proposals(conn)}
    out = []
    for (folder, _), songs in crud.tracks.by_album(conn).items():
        key = album_key(folder, songs[0]["album"] or "")
        if refresh or (key not in pending and not _linked(conn, songs)):
            out.append((key, folder, songs))
    return out


def _search(album, artist):
    """Search in stages, from most to least specific, stopping at the first that finds anything."""
    by = None if not artist or rules.is_various(artist) else artist
    title, simple = rules.clean_album(album), match.simplify_album(rules.clean_album(album))
    stages = [("search: title + artist", title, by, False)]
    if simple != title:
        stages.append(("search: simplified title + artist", simple, by, False))
    if by:
        stages += [("search: title words + artist", simple, by, True), ("search: title only", title, None, False)]
    for how, query, query_artist, loose in stages:
        candidates = client.search_releases(query, query_artist, loose=loose)
        if candidates:
            return candidates, how
    return [], "search"


def album_facts(songs):
    """(album name, main artist) of a group of songs."""
    album = songs[0]["album"] or ""
    artist = collections.Counter(s["album_artist"] or s["artist"] for s in songs).most_common(1)[0][0]
    return album, artist


def best_match(songs, prefer=()):
    """The likeliest release for an album without fetching it: (release id, search candidate or None, how).

    Uses a release id already in the files when there is one. When the songs disagree (editions mixed
    from several downloads), a release the library is already linked to (`prefer`) wins, the one most
    songs point at first. Otherwise searches and ranks.
    """
    album, artist = album_facts(songs)
    # Only trust well-formed ids: some rips carry junk like MUSICBRAINZ_ALBUMID=Bangarang EP.
    counts = collections.Counter(s["mb_album_id"].lower() for s in songs if s["mb_album_id"] and MBID.match(s["mb_album_id"]))
    if len(counts) == 1:
        return next(iter(counts)), None, "MusicBrainz id already in the files"
    known = [rid for rid, _ in counts.most_common() if rid in prefer]
    if known:
        return known[0], None, "the edition this album is already linked to"
    candidates, how = _search(album, artist)
    if not candidates:
        return None, None, how
    years = [s["year"] for s in songs if s["year"]]
    best = max(candidates, key=lambda c: match.rank(c, album, artist, len(songs), min(years) if years else None))
    return best["id"], best, how


def _change_list(root, path, values):
    return [{"field": f, "label": LABELS.get(f, f"{f} (duplicate tag, removed)"), "old": "; ".join(old), "new": "; ".join(new)}
            for f, old, new in tags.compare(os.path.join(root, path), values)]


def build_proposal(root, songs, summary):
    """Pair an album's songs with a release and work out every tag change. No network needed.

    Paired songs get the release's tags. Songs that aren't on the release but still carry another
    edition's release ids get those edition-specific ids cleared ("detached"), so one album never
    points at several editions; their other tags are left alone.
    """
    pairs, extras, missing = match.pair_tracks(songs, summary["tracks"])
    entries = []
    for song, t, score in pairs:
        values = match.tag_values(summary, t)
        entries.append({"path": song["path"], "title": song["title"], "mb_title": t["title"],
                        "position": f"{t['disc']}-{t['track']}" if t["discs"] > 1 else str(t["track"]), "score": score,
                        "values": values, "changes": _change_list(root, song["path"], values)})
    detached = []
    for song in extras:
        if song["mb_album_id"] and song["mb_album_id"].lower() != summary["id"] and MBID.match(song["mb_album_id"]):
            values = {f: [] for f in EDITION_FIELDS}
            entries.append({"path": song["path"], "title": song["title"], "mb_title": None, "position": None,
                            "score": None, "detach": True, "values": values,
                            "changes": _change_list(root, song["path"], values)})
            detached.append(song["path"])
    return {"release": summary, "tracks": entries, "extras": [s["path"] for s in extras], "detached": detached,
            "missing": [{"disc": t["disc"], "track": t["track"], "title": rules.normalize(t["title"] or "")} for t in missing],
            "matched": len(pairs), "local": len(songs), "complete": not missing}


def _status(mb_score, payload, artist_match):
    paired = payload["matched"] / payload["local"]
    if mb_score >= 90 and paired >= 0.8 and artist_match >= 0.8:
        return "matched"
    if paired >= 0.5 and artist_match >= 0.6:
        return "low"
    return "poor"  # shown for reference, never applied


def _propose(root, songs, prefer=()):
    album, artist = album_facts(songs)
    release_id, best, how = best_match(songs, prefer)
    if not release_id:
        return "none", 0, None, {"reason": "MusicBrainz has no release with this name"}
    release = client.get_release(release_id)
    mb_score = int(best.get("score", 0)) if best else 100
    artist_match = match.artist_similarity(best, artist) if best else 1.0
    if not release:
        return "none", 0, None, {"reason": "release not found"}
    payload = build_proposal(root, songs, match.release_summary(release))
    payload.update(how=how, artist_match=round(artist_match, 2))
    return _status(mb_score, payload, artist_match), mb_score, release_id, payload


# --- keeping each album on one edition --------------------------------------------------

def _target_release(conn, songs):
    """The stored release (with full details) most of these songs point at, or None."""
    counts = collections.Counter(s["mb_album_id"].lower() for s in songs if s["mb_album_id"] and MBID.match(s["mb_album_id"]))
    for rid, _ in counts.most_common():
        summary = crud.releases.summary(conn, rid)
        if summary:
            return summary
    return None


def propose_alignments(conn, root):
    """For every album whose songs point at several editions, prepare an "align" proposal from the stored
    release it's linked to. Offline: it reads the files, never MusicBrainz. Returns how many were proposed."""
    groups = crud.tracks.by_album(conn)
    proposed = 0
    for mixed in crud.releases.mixed_albums(conn):
        # an album filed across several folders counts as one: its songs all go onto one edition,
        # and the next cleanup then brings them together in that edition's folder
        songs = [s for folder, key in mixed["parts"] for s in groups.get((folder, key), [])]
        summary = songs and _target_release(conn, songs)
        if not summary:
            continue  # not linked to a stored release yet: "Look up albums" handles it
        payload = build_proposal(root, songs, summary)
        if not any(t["changes"] for t in payload["tracks"]):
            continue
        payload.update(how="aligning with the edition this album is already linked to (no lookup needed)", artist_match=1.0)
        album, artist = album_facts(songs)
        key = album_key(mixed["folder"], album)
        crud.releases.save_proposal(conn, key, mixed["folder"], album, artist,
                                    _status(100, payload, 1.0), 100, summary["id"], payload)
        # this fix covers the album's other folders too: drop their own (now confusing) proposals
        others = {album_key(folder, next(s["album"] for s in groups[(folder, k)]) or "")
                  for folder, k in mixed["parts"] if groups.get((folder, k))} - {key}
        crud.releases.delete_proposals(conn, others)
        proposed += 1
    conn.commit()
    return proposed


def align_songs(conn, root, paths, entries, stats):
    """Re-tag individual songs to the edition the rest of their album is linked to (e.g. the copy kept
    in a name clash). Writes journal entries into `entries`, like the cleanup does."""
    by_path = {s["path"]: (key, songs) for key, songs in crud.tracks.by_album(conn).items() for s in songs}
    for path in paths:
        if path not in by_path:
            continue
        _, songs = by_path[path]
        song = next(s for s in songs if s["path"] == path)
        others = [s for s in songs if s["path"] != path]
        summary = _target_release(conn, others)
        if not summary or (song["mb_album_id"] or "").lower() == summary["id"]:
            continue
        payload = build_proposal(root, [song], summary)
        for t in payload["tracks"]:
            full = os.path.join(root, t["path"])
            if not tags.compare(full, t["values"]):
                continue
            size = os.path.getsize(full)
            before = tags.write(full, t["values"])
            entries.append({"op": "tags", "path": t["path"], "before": before, "size": size})
            scan.refresh(conn, root, t["path"])
            conn.commit()
            stats["aligned"] += 1


def fetch(db_path, refresh=False, skip=(), finished=None, progress=_no_progress):
    """Look up every album without a match yet (all of them with refresh=True) and store proposals.
    Albums in `skip` are left out; the key of each album looked up is appended to `finished`, if given."""
    conn = crud.connect(db_path)
    root = crud.get_root(conn)
    todo = [a for a in albums_to_fetch(conn, refresh) if a[0] not in skip]
    stored = {r[0] for r in conn.execute("SELECT mb_release_id FROM releases")}
    counts = collections.Counter()
    try:
        for i, (key, folder, songs) in enumerate(todo):
            album, artist = songs[0]["album"] or "", songs[0]["album_artist"] or songs[0]["artist"] or ""
            progress(i, len(todo), f"{artist} - {album}")
            try:
                status, score, release_id, payload = _propose(root, songs, prefer=stored)
            except client.MusicBrainzError as e:
                status, score, release_id, payload = "error", 0, None, {"reason": str(e)}
            crud.releases.save_proposal(conn, key, folder, album, artist, status, score, release_id, payload)
            conn.commit()
            counts[status] += 1
            if finished is not None:
                finished.append(key)
    finally:
        conn.close()
    progress(len(todo), len(todo), "Done")
    return {"albums": len(todo), **counts}


def apply(db_path, keys, progress=_no_progress):
    """Write the chosen proposals into the files and the catalog, then work out album completeness."""
    conn = crud.connect(db_path)
    root = crud.get_root(conn)
    chosen = [p for p in crud.releases.proposals(conn, set(keys)) if p["status"] in ("matched", "low")]
    work = [(p, t) for p in chosen for t in p["payload"]["tracks"]]
    entries, stats = [], collections.Counter()
    stamp = cleanup_apply.journal_stamp()
    journal_path = os.path.join(cleanup_apply.JOURNAL_DIR, f"{stamp}-musicbrainz.json")
    try:
        for i, (p, t) in enumerate(work):
            progress(i, len(work), t["path"])
            full = os.path.join(root, t["path"])
            if not os.path.exists(full):  # moved or deleted since the fetch
                stats["skipped"] += 1
                continue
            if not tags.compare(full, t["values"]):
                stats["unchanged"] += 1
                continue
            size = os.path.getsize(full)
            before = tags.write(full, t["values"])
            entries.append({"op": "tags", "path": t["path"], "before": before, "size": size})
            scan.refresh(conn, root, t["path"])
            conn.commit()
            stats["tags"] += 1
        progress(len(work), len(work), "Recording releases and missing songs…")
        for p in chosen:
            crud.releases.upsert(conn, p["payload"]["release"])
        crud.releases.recompute(conn)
        crud.releases.delete_proposals(conn, [p["album_key"] for p in chosen])
        conn.commit()
    finally:
        conn.commit()
        if entries:
            os.makedirs(cleanup_apply.JOURNAL_DIR, exist_ok=True)
            with open(journal_path, "w", encoding="utf-8") as f:
                json.dump({"root": root, "created": stamp, "kind": "musicbrainz", "stats": stats, "entries": entries},
                          f, ensure_ascii=False, indent=1)
    incomplete = len(crud.releases.incomplete(conn))
    conn.close()
    return {"albums": len(chosen), "stats": dict(stats), "incomplete_albums": incomplete,
            "journal": journal_path if entries else None}
