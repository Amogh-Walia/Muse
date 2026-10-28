"""Where a song's lyrics are, what kind they are, and reading them for the player.

Lyrics can live in three places, all of which Navidrome and most players read:
  * `<song>.lrc` next to the audio file: usually synced ([mm:ss.xx] timestamps per line);
  * `<song>.txt` next to it: plain text;
  * embedded in the file's tags (LYRICS / UNSYNCEDLYRICS; ID3 USLT shows up as `lyrics-xxx`).
Synced beats plain wherever it is.
"""
import collections
import os
import re

SIDECARS = (".lrc", ".txt")
EMBEDDED_KEYS = ("lyrics", "unsyncedlyrics", "unsynced lyrics")
STAMP = re.compile(r"\[(\d{1,3}):(\d{1,2}(?:[.:]\d{1,3})?)\]")
SYNCED = re.compile(r"^\s*\[\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?\]", re.M)
OFFSET = re.compile(r"^\s*\[offset:\s*([+-]?\d+)\s*\]", re.M | re.I)


def is_synced(text):
    return bool(text and SYNCED.search(text))


def _read(path):
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def embedded_by_track(conn, track_ids=None):
    """{track id: [lyrics text]} from the raw tags in the catalog."""
    rows = conn.execute("""SELECT track_id, value FROM tags
                           WHERE lower(key) IN ('lyrics', 'unsyncedlyrics', 'unsynced lyrics') OR lower(key) LIKE 'lyrics-%'""")
    out = collections.defaultdict(list)
    for track_id, value in rows:
        if (track_ids is None or track_id in track_ids) and value and value.strip():
            out[track_id].append(value)
    return out


def sources(root, path, embedded=()):
    """Every copy of a song's lyrics: [{text, synced, source}], best first."""
    stem = os.path.join(root, os.path.splitext(path)[0])
    found = []
    for ext in SIDECARS:
        text = _read(stem + ext) if os.path.exists(stem + ext) else None
        if text and text.strip():
            found.append({"text": text, "synced": is_synced(text), "source": ext.lstrip(".")})
    for text in embedded:
        found.append({"text": text, "synced": is_synced(text), "source": "embedded"})
    return sorted(found, key=lambda f: (not f["synced"], ["lrc", "txt", "embedded"].index(f["source"])))


def best(root, path, embedded=()):
    found = sources(root, path, embedded)
    return found[0] if found else None


def status_for_all(conn, root):
    """{track id: (kind, source)} with kind 'synced' / 'plain' / None, for every catalogued song."""
    embedded = embedded_by_track(conn)
    out = {}
    for track_id, path in conn.execute("SELECT id, path FROM tracks"):
        b = best(root, path, embedded.get(track_id, ()))
        out[track_id] = ((("synced" if b["synced"] else "plain"), b["source"]) if b else (None, None))
    return out


def parse(text):
    """Synced lyrics as [(seconds, line)], sorted; lines with several timestamps appear once per stamp."""
    m = OFFSET.search(text)
    offset = int(m.group(1)) / 1000 if m else 0.0  # positive offset: lyrics show earlier
    lines = []
    for raw in text.splitlines():
        stamps = STAMP.findall(raw)
        if not stamps:
            continue
        words = STAMP.sub("", raw).strip()
        for minutes, seconds in stamps:
            lines.append((max(0.0, int(minutes) * 60 + float(seconds.replace(":", ".")) - offset), words))
    return sorted(lines, key=lambda line: line[0])


# --- orphans --------------------------------------------------------------------------

def _title_key(name):
    """'06 - Heartbeat' / '1-06 Heartbeat' / 'Heartbeat' -> 'heartbeat'."""
    name = re.sub(r"^\s*(\d+[-.]?)?\d+\s*[-.]?\s*", "", name)
    return re.sub(r"[\W_]+", "", name.lower())


def orphans(conn, root, skip_dirs=(".muse-trash",)):
    """`.lrc` files with no song of the same name next to them, each with a suggested fix:
    reattach to the one song it fits that has no lyrics, else move to the trash."""
    songs = conn.execute("SELECT id, path, title, lyrics FROM tracks").fetchall()
    stems = {os.path.splitext(s["path"])[0] for s in songs}
    by_title = collections.defaultdict(list)
    for s in songs:
        by_title[_title_key(s["title"] or os.path.splitext(os.path.basename(s["path"]))[0])].append(s)
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in skip_dirs)
        for name in sorted(filenames):
            if not name.lower().endswith(".lrc"):
                continue
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            if os.path.splitext(rel)[0] in stems:
                continue
            matches = by_title.get(_title_key(os.path.splitext(name)[0]), [])
            same_folder = [s for s in matches if os.path.dirname(s["path"]) == os.path.dirname(rel)]
            fits = [s for s in (same_folder or matches) if not s["lyrics"]]
            if len(fits) == 1:
                target = os.path.splitext(fits[0]["path"])[0] + ".lrc"
                out.append({"path": rel, "action": "reattach", "target": target, "song": fits[0]["title"],
                            "reason": f"fits “{fits[0]['title']}”, which has no lyrics"})
            else:
                reason = ("the song it belonged to already has lyrics" if matches
                          else "no song in the library matches it")
                out.append({"path": rel, "action": "trash", "target": None, "song": None, "reason": reason})
    return out
