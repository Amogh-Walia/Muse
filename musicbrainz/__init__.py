"""MusicBrainz service: look up releases, write their details into files and the catalog, flag incomplete albums.

    client.py   rate-limited MusicBrainz web-service client
    match.py    choose a release for a local album and pair tracks with files
    service.py  fetch proposals / apply them (journaled, undoable with the cleanup's Undo)
"""
