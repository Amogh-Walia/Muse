"""Album artwork for songs without an embedded cover.

    images.py   image checks, the artwork cache (cache/), resizing for embedding (ffmpeg)
    sources.py  candidates: album-folder images, sibling songs, Cover Art Archive, iTunes
    embed.py    embed/snapshot/restore pictures in FLAC, MP3, M4A and Ogg
    service.py  find candidates, take uploads, apply (journaled, undone by the cleanup's Undo)
"""
