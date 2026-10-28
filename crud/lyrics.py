"""Lyrics status per song, and LRCLIB results waiting for review."""
import datetime


def set_status(conn, statuses):
    """statuses: {track id: (kind, source)} with kind 'synced' / 'plain' / None."""
    conn.executemany("UPDATE tracks SET lyrics = ?, lyrics_source = ? WHERE id = ?",
                     [(kind, source, tid) for tid, (kind, source) in statuses.items()])


def coverage(conn):
    row = conn.execute("""SELECT count(*) songs, sum(lyrics = 'synced') synced, sum(lyrics = 'plain') plain,
                                 sum(lyrics IS NULL) missing FROM tracks""").fetchone()
    return {k: row[k] or 0 for k in row.keys()}


def save_candidate(conn, track_id, found):
    """Remember what LRCLIB had for a song (`found` None records a miss, so it isn't asked again)."""
    found = found or {"kind": "none"}
    conn.execute("INSERT OR REPLACE INTO lyrics_candidates VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (track_id, found["kind"], found.get("how"), found.get("lrclib_id"), found.get("synced"),
                  found.get("plain"), found.get("duration"), datetime.datetime.now().isoformat(timespec="seconds")))


def candidates(conn, track_ids=None):
    rows = conn.execute("""SELECT c.*, t.path, t.folder, t.album, t.title, t.artist, t.disc_no, t.track_no,
                                  t.duration AS song_duration, t.lyrics AS current
                           FROM lyrics_candidates c JOIN tracks t ON t.id = c.track_id
                           ORDER BY lower(t.folder), t.disc_no, t.track_no""").fetchall()
    return [dict(r) for r in rows if track_ids is None or r["track_id"] in track_ids]


def asked(conn):
    return {r[0] for r in conn.execute("SELECT track_id FROM lyrics_candidates")}


def forget_candidates(conn, track_ids):
    conn.executemany("DELETE FROM lyrics_candidates WHERE track_id = ?", [(t,) for t in track_ids])
