"""All database access for the Muse catalog lives here.

    db.py        connect, schema (+ migrations), library root
    tracks.py    create / read / update / delete tracks and their raw tags; ingestion status
    releases.py  MusicBrainz releases, completeness, missing songs, match proposals
    artwork.py   candidate covers per album, and the artwork that was embedded
    lyrics.py    lyrics status per song, and LRCLIB results waiting for review
    merges.py    merge suggestions marked as fine
    remake.py    playlist-style albums looked up song by song, and the releases they may go to
"""
from .db import connect, get_meta, get_root, set_meta, set_root  # noqa: F401
from . import artwork, lyrics, merges, releases, remake, tracks  # noqa: F401
