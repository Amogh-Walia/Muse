"""Connection handling and schema for the Muse catalog."""
import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id           INTEGER PRIMARY KEY,
    path         TEXT UNIQUE NOT NULL,   -- relative to root
    folder       TEXT NOT NULL,
    filename     TEXT NOT NULL,
    ext          TEXT NOT NULL,
    size         INTEGER,
    mtime        REAL,
    -- audio properties
    duration     REAL,
    codec        TEXT,
    sample_rate  INTEGER,
    bit_depth    INTEGER,
    channels     INTEGER,
    bitrate      INTEGER,
    has_cover    INTEGER,
    -- common tags, normalized from whatever key casing the file uses
    title        TEXT,
    artist       TEXT,
    album        TEXT,
    album_artist TEXT,
    track_no     INTEGER,
    track_total  INTEGER,
    disc_no      INTEGER,
    disc_total   INTEGER,
    date         TEXT,
    year         INTEGER,
    genre        TEXT,
    composer     TEXT,
    -- MusicBrainz links, read from the file's tags
    mb_album_id         TEXT,
    mb_release_track_id TEXT,
    -- ingestion pipeline: where the cleanup wants this file, and whether it's there
    clean_path   TEXT,
    processed    INTEGER NOT NULL DEFAULT 0,
    -- Navidrome's ids for this song and its album (playback, "Open in Navidrome")
    navidrome_id       TEXT,
    navidrome_album_id TEXT,
    -- lyrics: 'synced' / 'plain' / NULL, and where the best copy is ('lrc' / 'txt' / 'embedded')
    lyrics        TEXT,
    lyrics_source TEXT
);
CREATE INDEX IF NOT EXISTS tracks_mb_album ON tracks(mb_album_id);
-- every raw tag exactly as stored in the file
CREATE TABLE IF NOT EXISTS tags (
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    key      TEXT NOT NULL,
    value    TEXT
);
CREATE INDEX IF NOT EXISTS tags_track ON tags(track_id);
CREATE INDEX IF NOT EXISTS tags_key ON tags(key);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

-- MusicBrainz releases that library songs are linked to, with completeness
CREATE TABLE IF NOT EXISTS releases (
    mb_release_id       TEXT PRIMARY KEY,
    mb_release_group_id TEXT,
    title         TEXT,
    artist        TEXT,
    date          TEXT,
    original_date TEXT,
    country       TEXT,
    label         TEXT,
    barcode       TEXT,
    track_count   INTEGER,
    disc_count    INTEGER,
    tracklist     TEXT,     -- JSON: every track on the release
    summary       TEXT,     -- JSON: full release details, so songs can be aligned to it without a lookup
    local_count   INTEGER,  -- library songs linked to it
    missing_count INTEGER,
    complete      INTEGER,  -- 1 all tracks present, 0 some missing
    fetched_at    TEXT
);
-- Songs on a release that aren't in the library: only the name (and its place on the release) is known.
CREATE TABLE IF NOT EXISTS missing_tracks (
    id              INTEGER PRIMARY KEY,
    mb_release_id   TEXT NOT NULL REFERENCES releases(mb_release_id) ON DELETE CASCADE,
    title           TEXT,
    disc_no         INTEGER,
    track_no        INTEGER,
    mb_release_track_id TEXT
);
-- MusicBrainz matches waiting for review, one per local album
CREATE TABLE IF NOT EXISTS mb_proposals (
    album_key     TEXT PRIMARY KEY,
    folder        TEXT,
    album         TEXT,
    artist        TEXT,
    status        TEXT,     -- matched | low | none | error
    score         INTEGER,
    mb_release_id TEXT,
    payload       TEXT,     -- JSON: release summary, per-song tag changes, missing songs, extras
    fetched_at    TEXT
);

-- Cover images found for an album (files next to the songs, sibling songs, Cover Art Archive, iTunes,
-- or picked from the user's drive). The image itself lives in the artwork cache, named by sha.
CREATE TABLE IF NOT EXISTS art_candidates (
    id         INTEGER PRIMARY KEY,
    album_key  TEXT NOT NULL,
    source     TEXT,      -- folder | embedded | coverartarchive | itunes | upload
    label      TEXT,
    sha        TEXT NOT NULL,
    mime       TEXT,
    width      INTEGER,
    height     INTEGER,
    bytes      INTEGER,
    url        TEXT,
    rank       INTEGER,   -- lower is a better guess for "the front cover"
    created_at TEXT,
    UNIQUE (album_key, sha)
);
-- Lyrics found on LRCLIB, waiting for review (one per song; kind 'none' remembers a miss)
CREATE TABLE IF NOT EXISTS lyrics_candidates (
    track_id   INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
    kind       TEXT,      -- synced | plain | instrumental | none
    how        TEXT,      -- exact match | search
    lrclib_id  INTEGER,
    synced     TEXT,
    plain      TEXT,
    duration   REAL,
    fetched_at TEXT
);
-- Artwork embedded per album by the Operations tab
CREATE TABLE IF NOT EXISTS artwork (
    album_key  TEXT PRIMARY KEY,
    folder     TEXT,
    album      TEXT,
    source     TEXT,
    sha        TEXT,
    width      INTEGER,
    height     INTEGER,
    songs      INTEGER,
    applied_at TEXT
);
-- Merge suggestions marked "this is fine" (signature: the album keys, sorted, joined by \x01)
CREATE TABLE IF NOT EXISTS merge_ignored (
    signature  TEXT PRIMARY KEY,
    albums     TEXT,
    reason     TEXT,
    ignored_at TEXT
);
"""

# Columns added after the first release; existing databases get them on open.
ADDED_COLUMNS = {
    "tracks": [("mb_album_id", "TEXT"), ("mb_release_track_id", "TEXT"), ("clean_path", "TEXT"),
               ("processed", "INTEGER NOT NULL DEFAULT 0"), ("navidrome_id", "TEXT"), ("navidrome_album_id", "TEXT"),
               ("lyrics", "TEXT"), ("lyrics_source", "TEXT")],
    "releases": [("summary", "TEXT")],
}


def migrate(conn):
    for table, columns in ADDED_COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not have:  # brand-new database: SCHEMA creates the table with every column
            continue
        for name, decl in columns:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    conn.executescript(SCHEMA)
    conn.commit()


def connect(path, readonly=False):
    """Open the catalog. Writable connections create the schema if needed."""
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    else:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        conn = sqlite3.connect(path, timeout=30)  # a background Navidrome re-link may be writing
        # WAL lets the web app keep reading while a scan or cleanup writes.
        conn.execute("PRAGMA journal_mode = WAL")
        migrate(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def get_meta(conn, key):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))


def get_root(conn):
    return get_meta(conn, "root")


def set_root(conn, root):
    set_meta(conn, "root", root)
