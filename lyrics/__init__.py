"""Lyrics: where they are and what kind (synced or plain), filling gaps from LRCLIB, orphaned .lrc files,
and synced lyrics for the player.

    files.py    find, classify and parse a song's lyrics (.lrc / .txt next to it, or embedded); orphans
    lrclib.py   LRCLIB client (synced lyrics database, no account needed)
    service.py  status refresh, LRCLIB lookups and saving, orphan fixes (journaled, undoable)
"""
