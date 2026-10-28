"""Ingestion status: which catalogued songs already sit where the cleanup wants them (processed)."""
import crud

from . import plan as planner


def refresh(conn, root):
    """Recompute every song's clean location and processed flag."""
    plan = planner.build(root)
    clean = {t["path"]: t["new_path"] for album in plan["albums"] for t in album["tracks"]}
    crud.tracks.set_clean_paths(conn, clean)
    conn.commit()
    from lyrics import service as lyrics_service  # imported here: lyrics uses the cleanup package
    lyrics_service.refresh_status(conn, root)
    return plan
