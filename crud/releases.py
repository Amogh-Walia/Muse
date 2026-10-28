"""MusicBrainz releases, album completeness, missing songs and match proposals."""
import collections
import datetime
import json
import re


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


# --- releases & completeness -------------------------------------------------------

# A well-formed MusicBrainz id (some rips carry junk like "Bangarang EP" in MUSICBRAINZ_ALBUMID).
VALID_ID_SQL = "{col} LIKE '________-____-____-____-____________' AND length({col}) = 36"


def upsert(conn, release):
    """Store a release: summary columns, its tracklist (list of {id, disc, track, title}) and full details."""
    conn.execute("""INSERT OR REPLACE INTO releases (mb_release_id, mb_release_group_id, title, artist, date, original_date,
                    country, label, barcode, track_count, disc_count, tracklist, fetched_at, summary)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                 (release["id"], release.get("release_group_id"), release.get("title"), release.get("artist"),
                  release.get("date"), release.get("original_date"), release.get("country"), release.get("label"),
                  release.get("barcode"), release.get("track_count"), release.get("disc_count"),
                  json.dumps(release["tracklist"], ensure_ascii=False), _now(), json.dumps(release, ensure_ascii=False)))


def summary(conn, release_id):
    """Full stored details of a release, or None if they weren't kept: releases applied before details were
    stored, or from a match found before they included every track ("tracks")."""
    row = conn.execute("SELECT summary FROM releases WHERE mb_release_id = ?", (release_id,)).fetchone()
    details = json.loads(row[0]) if row and row[0] else None
    return details if details and "tracks" in details else None


ALBUM_ID_KEYS = ("musicbrainz_albumid", "musicbrainz album id")
RELEASE_GROUP_KEYS = ("musicbrainz_releasegroupid", "musicbrainz release group id")


def _ids_by_track(conn, keys):
    valid = VALID_ID_SQL.format(col="value")
    out = collections.defaultdict(set)
    for track_id, value in conn.execute(f"SELECT track_id, value FROM tags WHERE lower(key) IN (?, ?) AND {valid}", keys):
        out[track_id].add(value.lower())
    return out


def _strays(conn, by_album):
    """{album part: {stray parts}}: songs filed in another folder that are one of the album's missing songs."""
    out = collections.defaultdict(set)
    missing = conn.execute("""SELECT m.mb_release_id, m.title, m.track_no FROM missing_tracks m""").fetchall()
    if not missing:
        return out
    linked = collections.defaultdict(set)  # release -> parts whose songs are linked to it
    for t_id, rid, folder, album_key in conn.execute("SELECT id, mb_album_id, folder, lower(album) FROM tracks WHERE mb_album_id IS NOT NULL"):
        linked[rid.lower()].add((folder, album_key))
    for m in missing:
        homes = linked.get(m["mb_release_id"].lower(), set())
        names = {_title_key(album_key) for _, album_key in homes}
        for part, songs in by_album.items():
            if part in homes or _title_key(part[1]) not in names:
                continue
            if any(_title_key(t["title"]) == _title_key(m["title"]) and t["track_no"] in (None, m["track_no"]) for t in songs):
                out[sorted(homes)[0]].add(part)
    return out


def mixed_albums(conn):
    """Albums whose songs point at more than one MusicBrainz release (different editions of one album).

    Two shapes are caught:
      * within one folder: songs pieced together from several downloads (every spelling of the
        release-id tag counts, so a file carrying two disagreeing ids is caught too);
      * across folders: editions with different album names ("Delhi Belly" and "Delhi Belly: Original
        Motion Picture Soundtrack") that the cleanup filed apart; they share an album artist and a
        MusicBrainz release group.
    Returns [{folder, album, parts: [[folder, album_key]], editions: [{id, title, date, stored, has_summary,
    songs, count}]}], the edition with most songs first.
    """
    release_ids = _ids_by_track(conn, ALBUM_ID_KEYS)
    groups_of = _ids_by_track(conn, RELEASE_GROUP_KEYS)
    tracks = conn.execute("""SELECT id, folder, lower(album) AS album_key, album, title, track_no,
                                    lower(coalesce(album_artist, artist, '')) AS artist
                             FROM tracks ORDER BY folder, disc_no, track_no""").fetchall()
    by_album, by_group = collections.defaultdict(list), collections.defaultdict(list)
    for t in tracks:
        by_album[(t["folder"], t["album_key"])].append(t)
        for rg in groups_of.get(t["id"], ()):
            by_group[(t["artist"], rg)].append(t)

    candidates = []  # (album parts, songs)
    for group in by_group.values():
        parts = sorted({(t["folder"], t["album_key"]) for t in group})
        if len(parts) > 1:
            candidates.append((parts, [t for part in parts for t in by_album[part]]))
    # Strays: a song filed elsewhere that is one of an album's missing songs (same title, same album name,
    # same track number), e.g. one track tagged with a different album artist and no MusicBrainz data.
    for main_part, stray_parts in _strays(conn, by_album).items():
        merged = next((c for c in candidates if main_part in c[0]), None)
        parts = sorted(set(merged[0] if merged else [main_part]) | stray_parts)
        if merged:
            candidates.remove(merged)
        candidates.append((parts, [t for part in parts for t in by_album[part]]))
    covered = {part for parts, _ in candidates for part in parts}
    candidates += [([part], songs) for part, songs in by_album.items() if part not in covered]

    out = []
    for parts, songs in candidates:
        by_release = collections.defaultdict(list)
        for t in songs:
            for rid in release_ids.get(t["id"], ()) or ("",):  # "": a song with no MusicBrainz data at all
                if t["title"] not in by_release[rid]:
                    by_release[rid].append(t["title"])
        by_release.pop("", None) if len(parts) == 1 else None  # untagged songs only matter when filed apart
        if len(by_release) < 2:
            continue
        editions = []
        for rid, titles in sorted(by_release.items(), key=lambda item: -len(item[1])):
            rel = conn.execute("SELECT title, date, summary IS NOT NULL FROM releases WHERE mb_release_id = ?", (rid,)).fetchone()
            rid = rid or None
            editions.append({"id": rid, "title": rel[0] if rel else None, "date": rel[1] if rel else None,
                             "stored": rel is not None, "has_summary": bool(rel and rel[2]), "songs": titles, "count": len(titles)})
        name = collections.Counter(t["album"] for t in songs).most_common(1)[0][0]
        out.append({"folder": parts[0][0], "album": name, "parts": [list(p) for p in parts], "editions": editions})
    return out


def _title_key(title):
    """'L.E.S.' and 'LES', 'Heartbeat (OLIVER remix)' and 'Heartbeat (Oliver remix)' compare equal."""
    return re.sub(r"[\W_]+", "", (title or "").lower())


def recompute(conn):
    """Re-derive completeness and the missing-song list of every release.

    A release track counts as present when a song of the same album (same folder and album name as the
    songs linked to the release) carries its MusicBrainz track id, or has the same title. Albums assembled
    from several downloads often point at different editions of one release; matching titles keeps those
    from showing songs that are there as missing. Releases no song points at any more are dropped.
    """
    for rel in conn.execute("SELECT mb_release_id, tracklist FROM releases").fetchall():
        rid = rel["mb_release_id"]
        linked = conn.execute("SELECT DISTINCT folder, lower(album) FROM tracks WHERE mb_album_id = ?", (rid,)).fetchall()
        conn.execute("DELETE FROM missing_tracks WHERE mb_release_id = ?", (rid,))
        if not linked:
            conn.execute("DELETE FROM releases WHERE mb_release_id = ?", (rid,))
            continue
        songs = [s for folder, album in linked for s in conn.execute(
            "SELECT title, mb_release_track_id FROM tracks WHERE folder = ? AND lower(album) IS ?", (folder, album))]
        tracklist = json.loads(rel["tracklist"] or "[]")
        on_release = {t["id"] for t in tracklist}
        have_ids = {s["mb_release_track_id"] for s in songs}
        # songs not already matched by id can stand in for a track by title
        have_titles = collections.Counter(_title_key(s["title"]) for s in songs if s["mb_release_track_id"] not in on_release)
        missing = []
        for t in tracklist:
            if t["id"] in have_ids:
                continue
            key = _title_key(t["title"])
            if have_titles[key] > 0:  # each local song accounts for one release track
                have_titles[key] -= 1
                continue
            missing.append(t)
        conn.executemany("INSERT INTO missing_tracks (mb_release_id, title, disc_no, track_no, mb_release_track_id) "
                         "VALUES (?, ?, ?, ?, ?)", [(rid, t["title"], t["disc"], t["track"], t["id"]) for t in missing])
        conn.execute("UPDATE releases SET local_count = ?, missing_count = ?, complete = ? WHERE mb_release_id = ?",
                     (len(tracklist) - len(missing), len(missing), int(not missing), rid))


def incomplete(conn):
    """Incomplete releases, each with its missing songs."""
    out = []
    for r in conn.execute("SELECT * FROM releases WHERE complete = 0 ORDER BY lower(artist), date").fetchall():
        rel = {k: r[k] for k in r.keys() if k != "tracklist"}
        rel["missing"] = [dict(m) for m in conn.execute(
            "SELECT title, disc_no, track_no FROM missing_tracks WHERE mb_release_id = ? ORDER BY disc_no, track_no",
            (r["mb_release_id"],))]
        out.append(rel)
    return out


def missing_for_folder(conn, folder):
    """Missing songs of the releases the songs in `folder` belong to."""
    return [dict(r) for r in conn.execute("""
        SELECT m.*, r.title AS album, r.artist AS album_artist FROM missing_tracks m
        JOIN releases r ON r.mb_release_id = m.mb_release_id
        WHERE m.mb_release_id IN (SELECT DISTINCT mb_album_id FROM tracks WHERE folder = ?)
        ORDER BY m.disc_no, m.track_no""", (folder,))]


# --- match proposals (fetched, waiting for review) -----------------------------------

def save_proposal(conn, key, folder, album, artist, status, score, release_id, payload):
    conn.execute("INSERT OR REPLACE INTO mb_proposals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (key, folder, album, artist, status, score, release_id, json.dumps(payload, ensure_ascii=False), _now()))


def proposals(conn, keys=None):
    rows = conn.execute("SELECT * FROM mb_proposals ORDER BY lower(artist), lower(album)").fetchall()
    out = [{**dict(r), "payload": json.loads(r["payload"] or "{}")} for r in rows]
    return [p for p in out if keys is None or p["album_key"] in keys]


def delete_proposals(conn, keys):
    conn.executemany("DELETE FROM mb_proposals WHERE album_key = ?", [(k,) for k in keys])


def prune_proposals(conn, live_keys):
    """Drop proposals of albums that no longer exist (renamed or moved since). Returns how many."""
    stale = [k for (k,) in conn.execute("SELECT album_key FROM mb_proposals") if k not in live_keys]
    delete_proposals(conn, stale)
    return len(stale)
