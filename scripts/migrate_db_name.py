#!/usr/bin/env python3
"""Safely align the SQLite database filename with the Misantropic branding.

Phase 0 rule: never lose data. This script therefore NEVER moves, renames,
truncates or deletes the legacy database. It produces a *consistent* copy of
the legacy database using ``VACUUM INTO`` (which correctly folds a hot
``-wal`` file into the output) and then verifies the copy table-by-table.

Usage:
    python scripts/migrate_db_name.py --check          # report only, no writes
    python scripts/migrate_db_name.py                  # create the copy
    python scripts/migrate_db_name.py --overwrite      # rebuild the copy

Exit codes: 0 = ok, 1 = failure, 2 = nothing to do.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

LEGACY = DATA_DIR / "odysseus.db"
CANONICAL = DATA_DIR / "misantropic.db"


def read_only_uri(path: Path) -> str:
    # as_uri() yields file:///C:/... on Windows; sqlite wants file:/C:/...
    return "file:" + path.as_posix().lstrip("/") if path.drive else "file:" + path.as_posix()


def open_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)


def describe(path: Path) -> dict:
    """Return integrity + row counts without modifying anything."""
    con = open_ro(path)
    try:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        tables = [
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        counts = {}
        for t in tables:
            counts[t] = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        return {"integrity": integrity, "tables": tables, "counts": counts}
    finally:
        con.close()


def report() -> int:
    print(f"data dir : {DATA_DIR}")
    for label, path in (("legacy", LEGACY), ("canonical", CANONICAL)):
        if path.exists():
            print(f"\n[{label}] {path.name}  ({path.stat().st_size:,} bytes)")
            if not path.suffix == ".db":
                continue
            info = describe(path)
            print(f"  integrity : {info['integrity']}")
            print(f"  tables    : {len(info['tables'])}")
            for t, n in info["counts"].items():
                if n:
                    print(f"    {t}: {n}")
        else:
            print(f"\n[{label}] {path.name}  -- not present")
    # WAL sidecars matter: a copy of only the .db can silently drop data.
    for sidecar in ("odysseus.db-wal", "odysseus.db-shm"):
        p = DATA_DIR / sidecar
        if p.exists():
            print(f"  note: {sidecar} present ({p.stat().st_size:,} bytes) "
                  "-- VACUUM INTO folds this in")
    return 0


def migrate(overwrite: bool) -> int:
    if not LEGACY.exists():
        print(f"nothing to do: {LEGACY} not found")
        return 2

    if CANONICAL.exists() and not overwrite:
        src = describe(LEGACY)
        dst = describe(CANONICAL)
        if src["counts"] == dst["counts"] and src["tables"] == dst["tables"]:
            print(f"already migrated and verified: {CANONICAL.name}")
            return 0
        print(
            f"ERROR: {CANONICAL.name} exists but does not match {LEGACY.name}.\n"
            "       Re-run with --overwrite, or inspect manually first.",
            file=sys.stderr,
        )
        return 1

    if CANONICAL.exists() and overwrite:
        CANONICAL.unlink()

    src_info = describe(LEGACY)
    if src_info["integrity"] != "ok":
        print(f"ERROR: source integrity_check = {src_info['integrity']}", file=sys.stderr)
        return 1

    con = open_ro(LEGACY)
    try:
        con.execute("VACUUM INTO ?", (str(CANONICAL),))
    finally:
        con.close()

    dst_info = describe(CANONICAL)
    if dst_info["integrity"] != "ok":
        print(f"ERROR: copy integrity_check = {dst_info['integrity']}", file=sys.stderr)
        return 1
    if dst_info["tables"] != src_info["tables"] or dst_info["counts"] != src_info["counts"]:
        print("ERROR: copy does not match source", file=sys.stderr)
        return 1

    print(f"ok: wrote {CANONICAL.name} from {LEGACY.name} and verified it")
    print(f"    tables: {len(dst_info['tables'])}, "
          f"rows: {sum(dst_info['counts'].values())}")
    print(f"    legacy {LEGACY.name} and its -wal/-shm sidecars are untouched")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="report only, make no changes")
    ap.add_argument("--overwrite", action="store_true", help="rebuild the canonical copy")
    args = ap.parse_args()
    return report() if args.check else migrate(args.overwrite)


if __name__ == "__main__":
    raise SystemExit(main())
