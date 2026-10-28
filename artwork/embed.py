"""Embed a front cover into audio files, and snapshot/restore their pictures for undo."""
import base64

import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, ID3NoHeaderError
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

from cleanup.tags import _keep_padding, _padding_for_size

FRONT_COVER = 3


def _picture(data, mime, width, height):
    pic = Picture()
    pic.type, pic.mime, pic.desc, pic.data = FRONT_COVER, mime, "Cover", data
    pic.width, pic.height, pic.depth = width, height, 24
    return pic


def _open(path):
    lower = path.lower()
    if lower.endswith(".mp3"):
        try:
            return "id3", ID3(path, translate=False)
        except ID3NoHeaderError:
            tags = ID3()
            tags.version = (2, 4, 0)
            return "id3", tags
    f = mutagen.File(path)
    if isinstance(f, FLAC):
        return "flac", f
    if isinstance(f, MP4):
        return "mp4", f
    if isinstance(f, (OggVorbis, OggOpus)):
        return "ogg", f
    raise ValueError("embedding art isn't supported for this format")


def snapshot(path):
    """Every picture currently embedded: [{data, mime, type, desc}]."""
    kind, f = _open(path)
    if kind == "flac":
        return [{"data": p.data, "mime": p.mime, "type": p.type, "desc": p.desc, "width": p.width, "height": p.height}
                for p in f.pictures]
    if kind == "id3":
        return [{"data": p.data, "mime": p.mime, "type": int(p.type), "desc": p.desc} for p in f.getall("APIC")]
    if kind == "mp4":
        return [{"data": bytes(c), "mime": "image/png" if c.imageformat == MP4Cover.FORMAT_PNG else "image/jpeg",
                 "type": FRONT_COVER, "desc": ""} for c in (f.tags or {}).get("covr", [])]
    out = []
    for b64 in (f.tags or {}).get("metadata_block_picture", []):
        p = Picture(base64.b64decode(b64))
        out.append({"data": p.data, "mime": p.mime, "type": p.type, "desc": p.desc, "width": p.width, "height": p.height})
    return out


def _write(path, kind, f, pictures, padding):
    """Replace all embedded pictures with `pictures` and save."""
    if kind == "flac":
        f.clear_pictures()
        for p in pictures:
            pic = _picture(p["data"], p["mime"], p.get("width", 0), p.get("height", 0))
            pic.type, pic.desc = p["type"], p["desc"]
            f.add_picture(pic)
        f.save(padding=padding)
    elif kind == "id3":
        f.delall("APIC")
        for p in pictures:
            f.add(APIC(encoding=3, mime=p["mime"], type=p["type"], desc=p["desc"], data=p["data"]))
        major = f.version[1] if f.version[1] in (3, 4) else 4
        f.save(path, v2_version=major, padding=padding)
    elif kind == "mp4":
        if f.tags is None:
            f.add_tags()
        covers = [MP4Cover(p["data"], MP4Cover.FORMAT_PNG if p["mime"] == "image/png" else MP4Cover.FORMAT_JPEG)
                  for p in pictures]
        if covers:
            f.tags["covr"] = covers
        elif "covr" in f.tags:
            del f.tags["covr"]
        f.save(padding=padding)
    else:
        if f.tags is None:
            f.add_tags()
        encoded = []
        for p in pictures:
            pic = _picture(p["data"], p["mime"], p.get("width", 0), p.get("height", 0))
            pic.type, pic.desc = p["type"], p["desc"]
            encoded.append(base64.b64encode(pic.write()).decode("ascii"))
        if encoded:
            f.tags["metadata_block_picture"] = encoded
        elif "metadata_block_picture" in f.tags:
            del f.tags["metadata_block_picture"]
        f.save(padding=padding)


def embed(path, data, mime, width, height, replace=False):
    """Set the front cover, keeping other pictures (back, booklet…) unless replace. Returns what was there before.

    An existing front-cover picture is always replaced: songs only get here when no usable cover was
    found, which includes broken ones (e.g. a picture block with no MIME type that players ignore).
    """
    kind, f = _open(path)
    before = snapshot(path)
    keep = [] if replace else [p for p in before if p["type"] != FRONT_COVER]
    cover = {"data": data, "mime": mime, "type": FRONT_COVER, "desc": "Cover", "width": width, "height": height}
    _write(path, kind, f, keep + [cover], _keep_padding)
    return before


def restore(path, pictures, size=None):
    """Put back exactly `pictures` (as snapshot() returned them); with `size`, aim for the original file size."""
    kind, f = _open(path)
    _write(path, kind, f, pictures, _padding_for_size(path, size) if size else _keep_padding)
