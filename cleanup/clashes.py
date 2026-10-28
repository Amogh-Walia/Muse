"""Describe the files in a name clash side by side, so the user can pick which one to keep."""
import os
from concurrent.futures import ThreadPoolExecutor

import scan
from lyrics import files

LOSSLESS_CODECS = {"flac", "alac", "ape", "wavpack", "tta", "mlp", "truehd"}


def details(root, path):
    """Format, quality and tag facts about one file (probed fresh, so it works for uncatalogued files too)."""
    result = scan.read_file(root, path)
    if result is None:
        return {"path": path, "error": "could not read file"}
    row, raw = result
    keys = {k.lower() for k in raw}
    embedded = [str(v) for k, v in raw.items() if k.lower() in files.EMBEDDED_KEYS or k.lower().startswith("lyrics-")]
    found = files.best(root, path, embedded)
    lyrics = found and {"kind": "synced" if found["synced"] else "plain", "source": found["source"]}
    codec = row.get("codec") or ""
    return {
        "path": path,
        "ext": os.path.splitext(path)[1].lower().lstrip("."),
        "codec": codec,
        "lossless": codec in LOSSLESS_CODECS or codec.startswith("pcm_"),
        "bit_depth": row.get("bit_depth"),
        "sample_rate": row.get("sample_rate"),
        "bitrate": row.get("bitrate"),
        "channels": row.get("channels"),
        "duration": row.get("duration"),
        "size": row.get("size"),
        "has_cover": bool(row.get("has_cover")),
        "lyrics": lyrics["kind"] if lyrics else None,           # synced | plain | None
        "lyrics_source": lyrics["source"] if lyrics else None,  # lrc | txt | embedded
        "musicbrainz": any(k.startswith("musicbrainz_") for k in keys),
        "tag_count": len(raw),
        "title": row.get("title"),
        "artist": row.get("artist"),
        "album": row.get("album"),
        "year": row.get("year"),
        "mb_album_id": row.get("mb_album_id"),
    }


def _score(d):
    """Higher is better: lossless first, then resolution, then extras."""
    if "error" in d:
        return (-1,)
    return (d["lossless"], d["bit_depth"] or 0, d["sample_rate"] or 0, d["bitrate"] or 0,
            {"synced": 2, "plain": 1}.get(d["lyrics"], 0), d["has_cover"], d["musicbrainz"], d["tag_count"])


def describe(root, groups, align_to=lambda group: None):
    """Attach per-file details and a recommended keeper to each clash group.

    `align_to(group)` names the release the destination album is linked to (if any): the kept copy is
    re-tagged to it, which the dialog says up front.
    """
    paths = [m["path"] for g in groups for m in g["members"]]
    with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as pool:
        info = dict(zip(paths, pool.map(lambda p: details(root, p), paths)))
    out = []
    for g in groups:
        members = [{**info[m["path"]], "role": m["role"]} for m in g["members"]]
        best = max(members, key=_score)
        out.append({"target": g["target"], "kind": g.get("kind", "incoming"), "members": members,
                    "recommended": best["path"], "align_to": align_to(g)})
    return out
