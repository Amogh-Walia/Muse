#!/usr/bin/env python3
"""Scan a music folder with ffprobe and catalog every audio file into SQLite.

Usage: python3 scan.py [MUSIC_ROOT] [--db PATH] [--full]

MUSIC_ROOT defaults to MUSE_LIBRARY (see .env.example), then to the folder the database remembers.

Rescans are incremental: files whose size and mtime are unchanged are skipped,
and rows for files that no longer exist are removed. --full forces a re-probe.
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import config
import crud

AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".aiff", ".aif", ".wma", ".ape", ".wv"}
# Where the cleanup puts duplicates you chose to discard; never catalogued.
TRASH_DIR = ".muse-trash"

# lowercase tag key -> candidate column; first match wins
ALIASES = {
    "title": ["title"],
    "artist": ["artist", "performer", "ensemble"],
    "album": ["album"],
    "album_artist": ["album_artist", "album artist", "albumartist"],
    "date": ["date", "originaldate", "year", "originalyear", "tdat", "retail date"],
    "genre": ["genre"],
    "composer": ["composer"],
    # FLAC/Ogg spell these as Vorbis keys; ID3 (TXXX) and MP4 (freeform) use Picard's descriptive names
    "mb_album_id": ["musicbrainz_albumid", "musicbrainz album id"],
    "mb_release_track_id": ["musicbrainz_releasetrackid", "musicbrainz release track id"],
}


def probe(full_path):
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", full_path],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        return None
    return json.loads(out.stdout)


def split_num(value):
    """'3/12' -> (3, 12); '03' -> (3, None); 'A1' -> (None, None)."""
    if not value:
        return None, None
    head, _, tail = str(value).partition("/")
    def to_int(s):
        s = s.strip()
        return int(s) if s.isdigit() else None
    return to_int(head), to_int(tail)


def extract(data):
    fmt = data.get("format", {})
    streams = data.get("streams", [])
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})

    # Tags can live on the container or (e.g. Ogg) on the audio stream.
    raw = dict(audio.get("tags", {}))
    raw.update(fmt.get("tags", {}))
    lower = {k.lower(): v for k, v in raw.items()}

    row = {}
    for col, keys in ALIASES.items():
        row[col] = next((lower[k] for k in keys if lower.get(k)), None)

    row["track_no"], row["track_total"] = split_num(lower.get("track") or lower.get("tracknumber"))
    row["disc_no"], row["disc_total"] = split_num(lower.get("disc") or lower.get("discnumber"))
    row["track_total"] = row["track_total"] or split_num(lower.get("tracktotal") or lower.get("totaltracks"))[0]
    row["disc_total"] = row["disc_total"] or split_num(lower.get("disctotal") or lower.get("totaldiscs"))[0]
    year = (row["date"] or "")[:4]
    row["year"] = int(year) if year.isdigit() and year != "0000" else None

    bits = audio.get("bits_per_raw_sample") or audio.get("bits_per_sample")
    row.update(
        duration=float(fmt["duration"]) if fmt.get("duration") else None,
        codec=audio.get("codec_name"),
        sample_rate=int(audio["sample_rate"]) if audio.get("sample_rate") else None,
        bit_depth=int(bits) if bits and int(bits) > 0 else None,
        channels=audio.get("channels"),
        bitrate=int(fmt["bit_rate"]) if fmt.get("bit_rate") else None,
        has_cover=int(any(s.get("disposition", {}).get("attached_pic") for s in streams)),
    )
    return row, raw


def read_file(root, rel):
    """Probe one file and return (row, raw_tags) ready for crud.tracks.upsert, or None if ffprobe fails."""
    full = os.path.join(root, rel)
    st = os.stat(full)
    data = probe(full)
    if data is None:
        return None
    row, raw = extract(data)
    row.update(path=rel, size=st.st_size, mtime=st.st_mtime)
    return row, raw


def refresh(conn, root, rel):
    """Re-read a single file into the catalog (after its tags or location changed)."""
    result = read_file(root, rel)
    if result is None:
        raise OSError(f"ffprobe failed: {rel}")
    return crud.tracks.upsert(conn, *result)


def _no_progress(done, total, message):
    pass


def resolve_root(db_path, explicit=None):
    """The library folder to use: an explicit one, else MUSE_LIBRARY, else the one the database remembers."""
    root = explicit or config.LIBRARY
    if not root and os.path.exists(db_path):
        conn = crud.connect(db_path, readonly=True)
        root = crud.get_root(conn)
        conn.close()
    if not root:
        raise SystemExit("No music library configured: set MUSE_LIBRARY in .env (see .env.example) or pass the folder.")
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        raise SystemExit(f"The music library folder doesn't exist: {root}")
    return root


def partial_downloads(root):
    """Files still being downloaded (.part), which aren't ingested until they finish."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != TRASH_DIR)
        out += [os.path.relpath(os.path.join(dirpath, n), root) for n in sorted(filenames) if n.endswith(".part")]
    return out


def scan(root, db_path, full=False, progress=_no_progress):
    """Bring the catalog in line with the disk. Returns counts of files found, probed, removed, failed."""
    root = os.path.abspath(root)
    conn = crud.connect(db_path)
    crud.set_root(conn, root)
    progress(0, 0, "Looking for new and changed files…")

    known = crud.tracks.file_index(conn)
    found, todo = set(), []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != TRASH_DIR)
        for name in sorted(filenames):
            if os.path.splitext(name)[1].lower() not in AUDIO_EXTS:
                continue
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root)
            st = os.stat(path)
            found.add(rel)
            if full or known.get(rel) != (st.st_size, st.st_mtime):
                todo.append(rel)

    gone = set(known) - found
    for rel in gone:
        crud.tracks.delete(conn, rel)

    print(f"{len(found)} audio files, {len(todo)} to probe, {len(gone)} removed", file=sys.stderr)
    failed = 0
    with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as pool:
        for i, (rel, result) in enumerate(zip(todo, pool.map(lambda r: read_file(root, r), todo)), 1):
            progress(i, len(todo), rel)
            if result is None:
                failed += 1
                print(f"  ffprobe failed: {rel}", file=sys.stderr)
                continue
            crud.tracks.upsert(conn, *result)
            if i % 100 == 0:
                print(f"  {i}/{len(todo)}", file=sys.stderr)
                conn.commit()
    crud.set_meta(conn, "last_scan", datetime.datetime.now().isoformat(timespec="seconds"))
    conn.commit()
    conn.close()
    print(f"done ({failed} failed)", file=sys.stderr)
    return {"found": len(found), "probed": len(todo) - failed, "removed": len(gone), "failed": failed}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", nargs="?", help="music folder (default: MUSE_LIBRARY)")
    ap.add_argument("--db", default=config.DB_PATH, help="catalog database, created if missing (default: MUSE_DB)")
    ap.add_argument("--full", action="store_true", help="re-probe every file")
    args = ap.parse_args()
    args.root = resolve_root(args.db, args.root)
    scan(args.root, args.db, args.full)
    try:  # processed flags need the cleanup planner (mutagen); skipped on a plain python3
        from cleanup import status
    except ImportError:
        return
    conn = crud.connect(args.db)
    status.refresh(conn, os.path.abspath(args.root))
    conn.close()


if __name__ == "__main__":
    main()
