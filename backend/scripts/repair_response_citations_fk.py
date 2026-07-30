"""Repair DBs whose ``response_citations`` FK dangles to a dropped ``responses_legacy`` table.

Root cause (FR-706a prod incident): the ``brand_focus``-nullable rebuild in
``app/models/database.py`` ran ``ALTER TABLE responses RENAME TO responses_legacy`` WITHOUT
``PRAGMA legacy_alter_table=ON``. Modern SQLite therefore rewrote ``response_citations``'s
FOREIGN KEY to point at ``responses_legacy``; the migration then dropped ``responses_legacy``,
leaving the FK dangling. With ``PRAGMA foreign_keys=ON`` every citation INSERT then fails with
``no such table: main.responses_legacy`` — so Source Authority classification (run-time AND the
backfill sweep) silently inserts nothing.

This rebuilds ONLY ``response_citations`` with its FK pointing back at ``responses``, preserving
every existing row and index. It is idempotent (a healthy schema is left untouched) and
transactional (any error rolls the rebuild back). Dry-run by default; pass ``--apply`` to write.

    python -m scripts.repair_response_citations_fk               # dry-run (report only)
    python -m scripts.repair_response_citations_fk --apply       # perform the repair
"""
from __future__ import annotations

import argparse
import json
import sqlite3

TABLE = "response_citations"


def _table_sql(cur: sqlite3.Cursor, name: str) -> str | None:
    row = cur.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row[0] if row else None


def repair(db_path: str, *, apply: bool) -> dict:
    con = sqlite3.connect(db_path)
    con.isolation_level = None  # explicit BEGIN/COMMIT control
    cur = con.cursor()
    try:
        sql = _table_sql(cur, TABLE)
        if sql is None:
            return {"status": "skipped", "reason": f"no {TABLE} table in {db_path}"}
        if "legacy" not in sql.lower():
            return {"status": "healthy", "reason": "FK already references responses; nothing to do"}

        rows_before = cur.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
        old_fk = [ln.strip() for ln in sql.splitlines() if "REFERENCES" in ln.upper()]
        # Correct the FK target and stage the fixed table under a temp name. Only the dangling
        # reference is rewritten; every column/constraint is otherwise preserved verbatim.
        fixed_sql = sql.replace('"responses_legacy"', "responses").replace(
            "responses_legacy", "responses"
        )
        fixed_sql = fixed_sql.replace(
            f"CREATE TABLE {TABLE}", f"CREATE TABLE {TABLE}__fixed", 1
        )
        # Explicit (named) indexes to recreate after the swap. sqlite_autoindex_* rows (backing
        # PK/UNIQUE, sql IS NULL) are recreated automatically with the table.
        idx_sqls = [
            r[0]
            for r in cur.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? "
                "AND sql IS NOT NULL",
                (TABLE,),
            )
        ]

        result = {
            "status": "would_repair" if not apply else "repaired",
            "db": db_path,
            "rows": rows_before,
            "indexes_to_restore": len(idx_sqls),
            "old_fk": old_fk,
        }
        if not apply:
            return result

        # foreign_keys must be toggled OUTSIDE a transaction; legacy_alter_table stops the
        # final RENAME from rewriting references again.
        cur.execute("PRAGMA foreign_keys=OFF")
        cur.execute("PRAGMA legacy_alter_table=ON")
        cur.execute("BEGIN")
        cur.execute(fixed_sql)
        cur.execute(f"INSERT INTO {TABLE}__fixed SELECT * FROM {TABLE}")
        cur.execute(f"DROP TABLE {TABLE}")
        cur.execute(f"ALTER TABLE {TABLE}__fixed RENAME TO {TABLE}")
        for isql in idx_sqls:
            cur.execute(isql)
        cur.execute("COMMIT")
        cur.execute("PRAGMA legacy_alter_table=OFF")
        cur.execute("PRAGMA foreign_keys=ON")

        new_sql = _table_sql(cur, TABLE) or ""
        violations = cur.execute("PRAGMA foreign_key_check").fetchall()
        result.update(
            rows_after=cur.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0],
            fk_repaired="legacy" not in new_sql.lower(),
            new_fk=[ln.strip() for ln in new_sql.splitlines() if "REFERENCES" in ln.upper()],
            fk_violations=len(violations),
        )
        return result
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="/app/data/evidence_monitoring.db", help="SQLite file path")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    args = ap.parse_args()
    print(json.dumps(repair(args.db, apply=args.apply), indent=2))


if __name__ == "__main__":
    main()
