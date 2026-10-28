"""Merge suggestions the user marked as fine: they aren't suggested again."""
import datetime
import json

SEPARATOR = "\x01"


def signature(keys):
    """One id for a set of albums, whatever order they come in."""
    return SEPARATOR.join(sorted(set(keys)))


def ignored(conn):
    return {r["signature"]: {"keys": r["signature"].split(SEPARATOR), "albums": json.loads(r["albums"]),
                             "reason": r["reason"], "ignored_at": r["ignored_at"]}
            for r in conn.execute("SELECT * FROM merge_ignored ORDER BY ignored_at DESC")}


def ignore(conn, keys, albums, reason):
    """albums: a short description of each album, so the ignored list stays readable after files move."""
    conn.execute("INSERT OR REPLACE INTO merge_ignored VALUES (?, ?, ?, ?)",
                 (signature(keys), json.dumps(albums, ensure_ascii=False), reason,
                  datetime.datetime.now().isoformat(timespec="seconds")))


def unignore(conn, keys):
    conn.execute("DELETE FROM merge_ignored WHERE signature = ?", (signature(keys),))
