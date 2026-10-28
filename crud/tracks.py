"""Create, read, update and delete tracks (and their raw tags)."""
import os

# Columns written from a probed file; `id` is managed by SQLite.
COLUMNS = ("path", "folder", "filename", "ext", "size", "mtime", "duration", "codec", "sample_rate", "bit_depth",
           "channels", "bitrate", "has_cover", "title", "artist", "album", "album_artist", "track_no", "track_total",
           "disc_no", "disc_total", "date", "year", "genre", "composer", "mb_album_id", "mb_release_track_id")
PUBLIC = ", ".join(("id",) + tuple(c for c in COLUMNS if c != "mtime")
                   + ("clean_path", "processed", "navidrome_id", "navidrome_album_id", "lyrics", "lyrics_source"))
SORTABLE = {"title", "artist", "album", "year", "genre", "duration", "track_no", "folder", "filename", "bitrate", "size"}


def _path_fields(path):
    return {"path": path, "folder": os.path.dirname(path), "filename": os.path.basename(path),
            "ext": os.path.splitext(path)[1].lower().lstrip(".")}


# --- create / update -------------------------------------------------------------

def upsert(conn, row, raw_tags):
    """Insert or update the track at row['path'], replacing its raw tags. Keeps the id of an existing row."""
    row = {**row, **_path_fields(row["path"])}
    values = [row.get(c) for c in COLUMNS]
    existing = conn.execute("SELECT id FROM tracks WHERE path = ?", (row["path"],)).fetchone()
    if existing:
        track_id = existing[0]
        conn.execute(f"UPDATE tracks SET {', '.join(f'{c} = ?' for c in COLUMNS)} WHERE id = ?", values + [track_id])
        conn.execute("DELETE FROM tags WHERE track_id = ?", (track_id,))
    else:
        cur = conn.execute(f"INSERT INTO tracks ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})", values)
        track_id = cur.lastrowid
    conn.executemany("INSERT INTO tags VALUES (?, ?, ?)", [(track_id, k, str(v)) for k, v in raw_tags.items()])
    return track_id


def move(conn, old_path, new_path):
    """Point a track at its new location, keeping its id. Returns False if old_path isn't catalogued."""
    fields = _path_fields(new_path)
    cur = conn.execute("UPDATE tracks SET path = ?, folder = ?, filename = ?, ext = ? WHERE path = ?",
                       (fields["path"], fields["folder"], fields["filename"], fields["ext"], old_path))
    return cur.rowcount > 0


# --- delete ----------------------------------------------------------------------

def delete(conn, path):
    conn.execute("DELETE FROM tracks WHERE path = ?", (path,))


# --- read ------------------------------------------------------------------------

def file_index(conn):
    """{path: (size, mtime)} for change detection."""
    return {r["path"]: (r["size"], r["mtime"]) for r in conn.execute("SELECT path, size, mtime FROM tracks")}


def get(conn, track_id):
    row = conn.execute(f"SELECT {PUBLIC} FROM tracks WHERE id = ?", (track_id,)).fetchone()
    if row is None:
        return None
    # AcousticBrainz analysis tags (AB:*) can number in the hundreds; list them last
    tags = conn.execute("SELECT key, value FROM tags WHERE track_id = ? ORDER BY key LIKE 'AB:%', lower(key)", (track_id,))
    return {**dict(row), "tags": [dict(t) for t in tags]}


def search(conn, q=None, folder=None, sort=None, direction="asc"):
    where, args = [], []
    if folder is not None:
        where.append("folder = ?")
        args.append(folder)
    if q:
        where.append("(title LIKE ? OR artist LIKE ? OR album LIKE ? OR album_artist LIKE ? OR genre LIKE ? OR path LIKE ?)")
        args += [f"%{q}%"] * 6
    sort = sort if sort in SORTABLE else None
    order = f"{sort} IS NULL, {sort} COLLATE NOCASE {'DESC' if direction == 'desc' else 'ASC'}, " if sort else ""
    order += "folder, disc_no, track_no, filename"
    sql = f"SELECT {PUBLIC} FROM tracks {'WHERE ' + ' AND '.join(where) if where else ''} ORDER BY {order}"
    return [dict(r) for r in conn.execute(sql, args)]


def albums(conn):
    """One row per folder, with the best track to take cover art from and MusicBrainz completeness."""
    rows = conn.execute("""
        SELECT t.folder,
               coalesce(min(CASE WHEN t.has_cover THEN t.id END), min(t.id)) cover_id,
               group_concat(DISTINCT t.album) albums,
               coalesce(max(t.album_artist), max(t.artist)) artist,
               min(t.year) year,
               count(*) tracks,
               sum(t.duration) duration,
               sum(t.size) size,
               group_concat(DISTINCT t.ext) formats,
               max(t.bit_depth) bit_depth,
               max(t.sample_rate) sample_rate,
               max(t.navidrome_album_id) navidrome_album_id,
               sum(t.processed) processed,
               (SELECT count(DISTINCT g.value) FROM tracks t2 JOIN tags g ON g.track_id = t2.id
                    WHERE t2.folder = t.folder AND lower(g.key) IN ('musicbrainz_albumid', 'musicbrainz album id')
                    AND g.value LIKE '________-____-____-____-____________') editions,
               (SELECT min(complete) FROM releases WHERE mb_release_id IN
                    (SELECT mb_album_id FROM tracks t2 WHERE t2.folder = t.folder)) complete,
               (SELECT sum(missing_count) FROM releases WHERE mb_release_id IN
                    (SELECT mb_album_id FROM tracks t2 WHERE t2.folder = t.folder)) missing
        FROM tracks t
        GROUP BY t.folder ORDER BY lower(artist), year, t.folder""")
    return [dict(r) for r in rows]


RELEASE_TYPE_KEYS = ("releasetype", "musicbrainz_albumtype", "release type", "musicbrainz album type")


def release_kinds(conn):
    """{folder: "single" or "ep"} from the songs' release type tags (or "EP" ending the album name);
    folders not listed are albums."""
    kinds = {}
    marks = ", ".join("?" * len(RELEASE_TYPE_KEYS))
    for folder, value in conn.execute(f"""SELECT t.folder, lower(g.value) FROM tracks t JOIN tags g ON g.track_id = t.id
                                          WHERE lower(g.key) IN ({marks})""", RELEASE_TYPE_KEYS):
        parts = {p.strip() for p in value.replace("/", ";").split(";")}
        if "single" in parts:
            kinds[folder] = "single"
        elif "ep" in parts and kinds.get(folder) != "single":
            kinds[folder] = "ep"
    for folder, album in conn.execute("SELECT folder, min(album) FROM tracks GROUP BY folder"):
        if folder not in kinds and album and album.rstrip().lower().endswith(" ep"):
            kinds[folder] = "ep"
    return kinds


def album_folders(conn, album_artist):
    """The folders holding albums by `album_artist`."""
    return [r[0] for r in conn.execute("SELECT DISTINCT folder FROM tracks WHERE lower(album_artist) = lower(?)", (album_artist,))]


def appearances(conn, artist=None):
    """Songs on albums by someone else: {artist: {folder: songs}} (main artist of the song vs album artist)."""
    sql = """SELECT artist, folder, count(*) FROM tracks WHERE artist IS NOT NULL AND album_artist IS NOT NULL
             AND lower(artist) <> lower(album_artist)""" + (" AND lower(artist) = lower(?)" if artist else "") + " GROUP BY artist, folder"
    out = {}
    for name, folder, songs in conn.execute(sql, (artist,) if artist else ()):
        out.setdefault(name, {})[folder] = songs
    return out


def album_artists(conn):
    """Every album artist in the library as spelled in the tags, with its MusicBrainz artist id when the files
    have one: [(name, id or None)], most used first."""
    return [tuple(r) for r in conn.execute("""
        SELECT t.album_artist, g.value FROM tracks t
        LEFT JOIN tags g ON g.track_id = t.id AND lower(g.key) IN ('musicbrainz_albumartistid', 'musicbrainz album artist id')
        WHERE t.album_artist IS NOT NULL GROUP BY t.album_artist, g.value ORDER BY count(*) DESC""")]


def tag_lookup(conn, keys):
    """{track id: {key: first value}} for raw tags named `keys` (lower case), whatever case the file used."""
    out = {}
    marks = ", ".join("?" * len(keys))
    for track_id, key, value in conn.execute(f"SELECT track_id, lower(key), value FROM tags WHERE lower(key) IN ({marks})", keys):
        out.setdefault(track_id, {}).setdefault(key, value)
    return out


def by_album(conn):
    """Library songs grouped the way the cleanup groups them: (folder, album tag)."""
    groups = {}
    for r in conn.execute(f"SELECT {PUBLIC} FROM tracks ORDER BY folder, disc_no, track_no, filename"):
        groups.setdefault((r["folder"], (r["album"] or "").lower()), []).append(dict(r))
    return groups


# --- ingestion status ------------------------------------------------------------

def set_clean_paths(conn, clean):
    """Record where each song belongs ({path: clean path}; songs not listed are already right).

    A song is *processed* once it's catalogued and sitting at its clean location.
    """
    rows = [(clean.get(p, p), clean.get(p, p), p) for (p,) in conn.execute("SELECT path FROM tracks")]
    conn.executemany("UPDATE tracks SET clean_path = ?, processed = (? = path) WHERE path = ?", rows)


# Spellings of the MusicBrainz release ids in the raw tags: Picard's Vorbis keys, and the descriptive
# form some taggers (and ID3/MP4) use. A file can carry both, sometimes with different values.
ALBUM_ID_KEYS = ("musicbrainz_albumid", "musicbrainz album id")
RELEASE_TRACK_ID_KEYS = ("musicbrainz_releasetrackid", "musicbrainz release track id")


def backfill_mb_ids(conn):
    """Fill missing MusicBrainz ids from the stored raw tags (songs catalogued before these columns existed
    are never re-read by an incremental scan). Returns how many songs changed."""
    changed = 0
    for column, keys in (("mb_album_id", ALBUM_ID_KEYS), ("mb_release_track_id", RELEASE_TRACK_ID_KEYS)):
        cur = conn.execute(f"""UPDATE tracks SET {column} = (
                                   SELECT value FROM tags WHERE track_id = tracks.id AND lower(key) IN (?, ?)
                                   ORDER BY lower(key) = ? DESC LIMIT 1)
                               WHERE {column} IS NULL AND EXISTS (
                                   SELECT 1 FROM tags WHERE track_id = tracks.id AND lower(key) IN (?, ?))""",
                           (*keys, keys[0], *keys))
        changed += cur.rowcount
    return changed


def recording_ids(conn):
    """{MusicBrainz recording id: track id}, from the raw tags (FLAC/Ogg MUSICBRAINZ_TRACKID, MP4/ID3 text forms)."""
    rows = conn.execute("""SELECT value, track_id FROM tags
                           WHERE lower(key) IN ('musicbrainz_trackid', 'musicbrainz track id')""")
    return {value: track_id for value, track_id in rows}


def set_navidrome_ids(conn, links):
    """Replace every song's Navidrome ids with `links` ({track id: (song id, album id)})."""
    conn.execute("UPDATE tracks SET navidrome_id = NULL, navidrome_album_id = NULL")
    conn.executemany("UPDATE tracks SET navidrome_id = ?, navidrome_album_id = ? WHERE id = ?",
                     [(song, album, tid) for tid, (song, album) in links.items()])


def navidrome_counts(conn, hidden=None):
    """Songs and how many are linked to Navidrome, leaving out the folder `hidden` from it (the inbox)."""
    if not hidden:
        return dict(conn.execute("SELECT count(*) songs, count(navidrome_id) linked FROM tracks").fetchone())
    prefix = hidden.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%"
    return dict(conn.execute("SELECT count(*) songs, count(navidrome_id) linked FROM tracks WHERE path NOT LIKE ? ESCAPE '\\'",
                             (prefix,)).fetchone())


def unprocessed(conn):
    """Songs checked and found away from their clean location (never-checked songs aren't listed)."""
    return [dict(r) for r in conn.execute(
        f"SELECT {PUBLIC} FROM tracks WHERE NOT processed AND clean_path IS NOT NULL ORDER BY folder, disc_no, track_no, filename")]


def needs_status(conn):
    """True if some songs have never had their processed status worked out (e.g. right after an upgrade)."""
    return conn.execute("SELECT 1 FROM tracks WHERE clean_path IS NULL LIMIT 1").fetchone() is not None


def stats(conn):
    row = conn.execute("""SELECT count(*) tracks, count(DISTINCT folder) folders,
        count(DISTINCT lower(coalesce(album_artist, artist))) artists,
        sum(duration) duration, sum(size) size FROM tracks""").fetchone()
    formats = conn.execute("SELECT ext, count(*) n FROM tracks GROUP BY ext ORDER BY n DESC")
    return {**dict(row), "formats": [dict(r) for r in formats]}


def verify(conn, root, audio_exts, skip_dirs=()):
    """Compare the catalog with the disk: rows without a file, files without a row, and rows out of date."""
    on_disk = {}
    for dirpath, dirnames, names in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for name in names:
            if os.path.splitext(name)[1].lower() in audio_exts:
                full = os.path.join(dirpath, name)
                st = os.stat(full)
                on_disk[os.path.relpath(full, root)] = (st.st_size, st.st_mtime)
    known = file_index(conn)
    return {
        "missing": sorted(set(known) - set(on_disk)),
        "untracked": sorted(set(on_disk) - set(known)),
        "stale": sorted(p for p in set(known) & set(on_disk) if known[p] != on_disk[p]),
    }
