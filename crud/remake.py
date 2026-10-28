"""Playlist-style albums looked up song by song ("Remake Various Artists"), and the releases they may go to."""
import datetime
import json


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def save_song(conn, track_id, isrc, status, how, options):
    """A song's lookup result. Looking it up again keeps a choice made by hand."""
    conn.execute("""INSERT INTO remake_songs (track_id, isrc, status, how, options, fetched_at) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT (track_id) DO UPDATE SET isrc = excluded.isrc, status = excluded.status, how = excluded.how,
                    options = excluded.options, fetched_at = excluded.fetched_at""",
                 (track_id, isrc, status, how, json.dumps(options, ensure_ascii=False), _now()))


def songs(conn):
    """{track id: {isrc, status, how, options, chosen}} for every song looked up."""
    return {r["track_id"]: {**dict(r), "options": json.loads(r["options"] or "[]")}
            for r in conn.execute("SELECT * FROM remake_songs")}


def choose(conn, track_id, choice):
    """A release id, "keep" (leave the song as it is) or None (the best match)."""
    conn.execute("UPDATE remake_songs SET chosen = ? WHERE track_id = ?", (choice, track_id))


def forget(conn, track_ids):
    conn.executemany("DELETE FROM remake_songs WHERE track_id = ?", [(t,) for t in track_ids])


def save_release(conn, release_id, summary):
    conn.execute("INSERT OR REPLACE INTO remake_releases VALUES (?, ?, ?)",
                 (release_id, json.dumps(summary, ensure_ascii=False), _now()))


def release(conn, release_id):
    row = conn.execute("SELECT summary FROM remake_releases WHERE release_id = ?", (release_id,)).fetchone()
    return json.loads(row[0]) if row else None
