"""Read and write tags in place with mutagen, disturbing the file as little as possible.

Only the fields the cleanup and the MusicBrainz service touch are handled. Each format has a small
backend so that:
  * FLAC/Ogg keep each key's original spelling ('ARTIST' stays 'ARTIST'),
  * MP3 keeps its ID3 version (v2.3 stays v2.3, frames like TDAT survive),
  * existing padding is reused, so files keep their size and aren't rewritten when tags fit.
"""
import os

try:
    import mutagen
    from mutagen._vorbis import VCommentDict
    from mutagen.easymp4 import EasyMP4Tags
    from mutagen.id3 import ID3, ID3NoHeaderError, TXXX, UFID, Frames
except ImportError as e:  # pragma: no cover
    raise ImportError("the cleanup needs mutagen: run with .venv/bin/python (see README)") from e

# Picard's convention for "every credited artist": a multi-valued ARTISTS tag.
EasyMP4Tags.RegisterFreeformKey("artists", "ARTISTS")

FIELDS = ("title", "artist", "artists", "album", "albumartist", "date")
EXTRA = ("tracknumber", "discnumber", "tracktotal", "disctotal", "totaldiscs", "originaldate")
# Written by the MusicBrainz service, named as Picard names them in Vorbis comments.
MB_FIELDS = ("musicbrainz_albumid", "musicbrainz_releasegroupid", "musicbrainz_releasetrackid", "musicbrainz_trackid",
             "musicbrainz_artistid", "musicbrainz_albumartistid", "label", "catalognumber", "barcode",
             "releasecountry", "releasestatus", "releasetype", "media")

# Picard's ID3 frames for the MusicBrainz fields (the recording id lives in a UFID frame).
ID3_MB = {"musicbrainz_albumid": "TXXX:MusicBrainz Album Id", "musicbrainz_releasegroupid": "TXXX:MusicBrainz Release Group Id",
          "musicbrainz_releasetrackid": "TXXX:MusicBrainz Release Track Id", "musicbrainz_trackid": "UFID:http://musicbrainz.org",
          "musicbrainz_artistid": "TXXX:MusicBrainz Artist Id", "musicbrainz_albumartistid": "TXXX:MusicBrainz Album Artist Id",
          "label": "TPUB", "catalognumber": "TXXX:CATALOGNUMBER", "barcode": "TXXX:BARCODE",
          "releasecountry": "TXXX:MusicBrainz Album Release Country", "releasestatus": "TXXX:MusicBrainz Album Status",
          "releasetype": "TXXX:MusicBrainz Album Type", "media": "TMED"}
# mutagen's easy MP4 already knows some MusicBrainz atoms under other names; register the rest.
MP4_ALIASES = {"releasestatus": "musicbrainz_albumstatus", "releasetype": "musicbrainz_albumtype"}
for _key, _atom in {"musicbrainz_releasegroupid": "MusicBrainz Release Group Id",
                    "musicbrainz_releasetrackid": "MusicBrainz Release Track Id", "label": "LABEL",
                    "catalognumber": "CATALOGNUMBER", "barcode": "BARCODE", "originaldate": "ORIGINALDATE",
                    "media": "MEDIA"}.items():
    EasyMP4Tags.RegisterFreeformKey(_key, _atom)
# Vorbis comments in the wild also spell album artist these ways; they are removed when it's rewritten.
ALBUMARTIST_ALIASES = ("album artist", "album_artist")
# Other spellings of the same field in Vorbis comments, left by other taggers. They're removed whenever the
# field is written, so a file never carries two disagreeing values (players may read either one).
VORBIS_ALIASES = {
    "albumartist": ALBUMARTIST_ALIASES,
    "musicbrainz_albumid": ("musicbrainz album id",),
    "musicbrainz_releasegroupid": ("musicbrainz release group id",),
    "musicbrainz_releasetrackid": ("musicbrainz release track id",),
    "musicbrainz_trackid": ("musicbrainz track id",),
    "musicbrainz_artistid": ("musicbrainz artist id",),
    "musicbrainz_albumartistid": ("musicbrainz album artist id",),
    "releasecountry": ("musicbrainz album release country",),
    "releasestatus": ("musicbrainz album status", "musicbrainz_albumstatus"),
    "releasetype": ("musicbrainz album type", "musicbrainz_albumtype"),
}


def _keep_padding(info):
    return info.padding if info.padding >= 0 else info.get_default_padding()


def _padding_for_size(path, target_size):
    """Padding that brings the file back to target_size bytes (used by undo).

    Restored values can encode a few bytes longer than the originals (e.g. ID3 text re-encoded as
    UTF-16), in which case the file ends up as close as possible: no padding at all.
    """
    current = os.path.getsize(path)

    def padding(info):
        return max(0, info.padding - (current - target_size))
    return padding


class _Vorbis:
    """FLAC, Ogg Vorbis/Opus: free-form, case-insensitive keys."""

    def __init__(self, f):
        self.f, self.tags = f, f.tags

    def spelling(self, key):
        return next((k for k, _ in list.__iter__(self.tags) if k.lower() == key), None)

    def get(self, key):
        return [str(v) for v in self.tags.get(key, [])]

    def write(self, field, values, before):
        spelled = self.spelling(field)
        target = spelled or field.upper()
        before[target] = self.get(field) if spelled else None
        for alias in VORBIS_ALIASES.get(field, ()):
            alias_spelled = self.spelling(alias)
            if alias_spelled:
                before[alias_spelled] = self.get(alias)
                del self.tags[alias]
        if spelled:
            del self.tags[field]
        if values:
            self.tags[target] = list(values)

    def restore(self, key, values):
        if key in self.tags:
            del self.tags[key]
        if values is not None:
            self.tags[key] = values

    def save(self, padding=_keep_padding):
        self.f.save(padding=padding)


class _ID3:
    """MP3: raw frames, loaded untranslated so the file's ID3 version and frames are kept."""

    def __init__(self, path):
        self.path = path
        try:
            self.tags = ID3(path, translate=False)
        except ID3NoHeaderError:
            self.tags = ID3()
            self.tags.version = (2, 4, 0)
        v24 = self.tags.version >= (2, 4, 0)
        self.frames = {"title": "TIT2", "artist": "TPE1", "album": "TALB", "albumartist": "TPE2",
                       "artists": "TXXX:ARTISTS", "date": "TDRC" if v24 else "TYER",
                       "originaldate": "TDOR" if v24 else "TORY", "tracknumber": "TRCK", "discnumber": "TPOS",
                       **ID3_MB}

    def get(self, key):
        frame = self.tags.get(self.frames.get(key, key))
        if frame is None:
            return []
        if isinstance(frame, UFID):
            return [frame.data.decode("utf-8", "replace")]
        return [str(t) for t in frame.text] if hasattr(frame, "text") else []

    def write(self, field, values, before):
        fid = self.frames[field]
        before[fid] = self.get(fid) if fid in self.tags else None
        self.restore(fid, list(values) or None)

    def restore(self, fid, values):
        self.tags.delall(fid)
        if values is not None:
            if fid.startswith("UFID:"):
                self.tags.add(UFID(owner=fid[5:], data=values[0].encode()))
            elif fid.startswith("TXXX:"):
                self.tags.add(TXXX(encoding=3, desc=fid[5:], text=values))
            else:
                self.tags.add(Frames[fid](encoding=3, text=values))

    def save(self, padding=_keep_padding):
        major = self.tags.version[1] if self.tags.version[1] in (3, 4) else 4
        self.tags.save(self.path, v2_version=major, padding=padding)


class _Easy:
    """MP4/M4A and anything else mutagen's 'easy' layer understands."""

    def __init__(self, f):
        self.f, self.tags = f, f.tags

    def get(self, key):
        try:
            return [str(v) for v in self.tags[MP4_ALIASES.get(key, key)]]
        except (KeyError, ValueError):
            return []

    def write(self, field, values, before):
        field = MP4_ALIASES.get(field, field)
        before[field] = self.get(field) if field in self.tags else None
        self.restore(field, list(values) or None)

    def restore(self, key, values):
        if key in self.tags:
            del self.tags[key]
        if values is not None:
            self.tags[key] = values

    def save(self, padding=_keep_padding):
        self.f.save(padding=padding)


def _open(path):
    if path.lower().endswith(".mp3"):
        return _ID3(path)
    f = mutagen.File(path)
    if f is None:
        raise ValueError(f"unsupported file: {path}")
    if f.tags is None:
        f.add_tags()
    if isinstance(f.tags, VCommentDict):
        return _Vorbis(f)
    return _Easy(mutagen.File(path, easy=True))


def read(path):
    """Return {field: [values]} for FIELDS plus numbering and original date."""
    t = _open(path)
    out = {k: t.get(k) for k in FIELDS + EXTRA}
    if not out["albumartist"] and isinstance(t, _Vorbis):
        out["albumartist"] = next((t.get(a) for a in ALBUMARTIST_ALIASES if t.get(a)), [])
    return out


def read_fields(path, fields):
    """{field: [values]} for any fields this module knows how to write (MusicBrainz ids included)."""
    t = _open(path)
    return {f: t.get(f) for f in fields}


def _fold_totals(t, changes):
    """ID3 and MP4 keep totals inside the number ('3/12'); Vorbis has separate TRACKTOTAL/DISCTOTAL."""
    if isinstance(t, _Vorbis):
        return dict(changes)
    changes = dict(changes)
    for number, total in (("tracknumber", "tracktotal"), ("discnumber", "disctotal")):
        count = changes.pop(total, None)
        if number in changes and count:
            changes[number] = [f"{changes[number][0]}/{count[0]}"]
    return changes


def compare(path, changes):
    """[(field, current values, new values)] for the fields that would actually change.

    Other spellings of a field that writing it would remove (see VORBIS_ALIASES) are listed under
    their own key, e.g. ("MusicBrainz Album Id", [old id], []).
    """
    t = _open(path)
    folded = _fold_totals(t, changes)
    out = [(f, t.get(f), list(v)) for f, v in folded.items() if t.get(f) != list(v)]
    if isinstance(t, _Vorbis):
        for field in folded:
            for alias in VORBIS_ALIASES.get(field, ()):
                spelled = t.spelling(alias)
                if spelled:
                    out.append((spelled, t.get(alias), []))
    return out


def write(path, changes):
    """Apply {field: [values] or []} and return the previous raw values (by raw key) for undo."""
    t = _open(path)
    before = {}
    for field, values in _fold_totals(t, changes).items():
        t.write(field, values, before)
    t.save()
    return before


def restore(path, before, size=None):
    """Put back the values `write` returned; with `size`, also the file's original byte size."""
    t = _open(path)
    for key, values in before.items():
        t.restore(key, values)
    t.save(padding=_padding_for_size(path, size) if size else _keep_padding)
