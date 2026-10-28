"""Album artwork: candidate covers per album, and what was embedded."""
import datetime


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def add_candidate(conn, album_key, source, label, sha, mime, width, height, size, url=None, rank=50):
    conn.execute("""INSERT INTO art_candidates (album_key, source, label, sha, mime, width, height, bytes, url, rank, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (album_key, sha) DO UPDATE SET rank = min(rank, excluded.rank)""",
                 (album_key, source, label, sha, mime, width, height, size, url, rank, _now()))


def candidates(conn, album_key=None):
    sql = "SELECT * FROM art_candidates" + (" WHERE album_key = ?" if album_key is not None else "")
    # best guess first: by rank, then square images (a cover) before wide scans, then larger
    rows = conn.execute(sql + " ORDER BY album_key, rank, abs(width - height) * 10 > max(width, height), width * height DESC",
                        (album_key,) if album_key is not None else ())
    return [dict(r) for r in rows]


def clear_candidates(conn, album_key, sources):
    conn.executemany("DELETE FROM art_candidates WHERE album_key = ? AND source = ?", [(album_key, s) for s in sources])


def record(conn, album_key, folder, album, candidate, songs):
    conn.execute("INSERT OR REPLACE INTO artwork VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (album_key, folder, album, candidate["source"], candidate["sha"], candidate["width"],
                  candidate["height"], songs, _now()))


def forget(conn, album_keys):
    conn.executemany("DELETE FROM artwork WHERE album_key = ?", [(k,) for k in album_keys])


def applied(conn):
    return {r["album_key"]: dict(r) for r in conn.execute("SELECT * FROM artwork")}


def prune_candidates(conn, live_keys):
    """Drop candidates of albums that no longer exist (renamed or moved since). Returns how many."""
    stale = [k for (k,) in conn.execute("SELECT DISTINCT album_key FROM art_candidates") if k not in live_keys]
    conn.executemany("DELETE FROM art_candidates WHERE album_key = ?", [(k,) for k in stale])
    return len(stale)


def referenced_images(conn):
    """Cached image names (shas) still offered as a candidate or recorded as embedded."""
    return {r[0] for r in conn.execute("SELECT sha FROM art_candidates UNION SELECT sha FROM artwork")}
