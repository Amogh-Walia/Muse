"""Regex-based cleanup of tags and file names.

    rules.py   pure text rules (artists, titles, albums, years, safe names)
    tags.py    read/write tags in place with mutagen
    plan.py    compute every proposed change, touching nothing
    apply.py   apply a plan with a journal, and undo it
"""
