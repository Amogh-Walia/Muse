"""Preview, apply or undo the cleanup from the command line.

    .venv/bin/python -m cleanup                 # preview every change
    .venv/bin/python -m cleanup --apply         # fix tags and rename/move files
    .venv/bin/python -m cleanup --apply --no-rename
    .venv/bin/python -m cleanup --undo          # reverse the last apply
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import config  # noqa: E402
import crud  # noqa: E402
import scan  # noqa: E402
from cleanup import apply as applier, plan as planner  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", nargs="?", help="music folder (default: MUSE_LIBRARY)")
    ap.add_argument("--db", default=config.DB_PATH, help="catalog database (default: MUSE_DB)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--undo", action="store_true")
    mode.add_argument("--history", action="store_true", help="show undo history, trash and when they expire")
    mode.add_argument("--prune", action="store_true", help="delete undo history past MUSE_UNDO_DAYS now")
    ap.add_argument("--no-tags", action="store_true", help="with --apply: leave tags alone")
    ap.add_argument("--no-rename", action="store_true", help="with --apply: leave files where they are")
    args = ap.parse_args()

    root = scan.resolve_root(args.db, args.root)
    if not os.path.exists(args.db):
        scan.scan(root, args.db)  # the cleanup keeps the catalog in step, so it needs one to start from

    def progress(done, total, message):
        print(f"\r\033[K[{done}/{total}] {message}"[:120], end="", file=sys.stderr, flush=True)

    def refresh_status():
        from cleanup import status
        conn = crud.connect(args.db)
        status.refresh(conn, root)
        conn.close()

    if args.history or args.prune:
        from cleanup import retention
        if args.prune:
            conn = crud.connect(args.db)
            print("removed:", retention.prune(conn, root))
            conn.close()
        inv = retention.inventory(root)
        mb = lambda n: f"{n / 1e6:.1f} MB"  # noqa: E731
        print(f"Changes stay undoable for {inv['days']:g} days" if inv["days"] else "Undo history never expires (MUSE_UNDO_DAYS=0)")
        for j in inv["journals"]:
            print(f"  {j['created']}  {j['kind']:<12} {j['entries']:>5} entries  {mb(j['bytes']):>9}"
                  f"  {'undone' if j['undone'] else 'expires ' + (j['expires'] or 'never')}")
        for t in inv["trash"]:
            print(f"  trash {t['name']:<24} {t['files']:>5} files    {mb(t['bytes']):>9}  expires {t['expires'] or 'never'}")
        totals = inv["totals"]
        print(f"Total: {mb(totals['journal_bytes'])} of journals, {mb(totals['trash_bytes'])} of discarded duplicates, "
              f"{mb(totals['cache_bytes'])} of cached covers")
        return

    if args.undo:
        result = applier.undo(args.db, progress=progress)
        refresh_status()
        print(file=sys.stderr)
        print(result)
        return

    plan = planner.build(root)
    if args.apply:
        result = applier.apply(plan, args.db, fix_tags=not args.no_tags, rename=not args.no_rename, progress=progress)
        refresh_status()
        print(file=sys.stderr)
        print(result)
        return

    for album in plan["albums"]:
        flag = "  [downloading, skipped]" if album["busy"] else ""
        print(f"\n## {album['folder'] or '(root)'}/  ->  {album['new_folder'] or '(several folders)'}/{flag}")
        for t in album["tracks"]:
            print(f"  {t['path']}")
            for c in t["changes"]:
                print(f"      {c['label']:<13} {c['old']!r} -> {c['new']!r}")
            if t["new_path"] != t["path"]:
                print(f"      {'File':<13} -> {t['new_path']}{'  [CLASH, will not move]' if t['clash'] else ''}")
    for e in plan["errors"]:
        print(f"unreadable: {e['path']}: {e['error']}")
    print("\n", plan["summary"])


if __name__ == "__main__":
    main()
