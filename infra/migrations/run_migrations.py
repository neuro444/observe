"""
Applies every .sql file in this directory, in filename order, that hasn't
already been recorded in schema_migrations. Safe to re-run: already-applied
files are skipped, and each file runs inside its own transaction so a
mid-file failure never leaves a half-applied migration recorded as done.

Used by CI (against a fresh Postgres service container) and available for
local/dev use against the docker-compose Postgres.

Run: python3 infra/migrations/run_migrations.py [DATABASE_URL]
(DATABASE_URL defaults to the same local default main.py uses.)
"""
from __future__ import annotations

import sys
from pathlib import Path

import psycopg2

DEFAULT_DSN = "postgresql://neuroheart:dev_only_change_in_real_deployment@127.0.0.1:5433/cost_ledger"
MIGRATIONS_DIR = Path(__file__).resolve().parent


def run(dsn: str) -> None:
    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    filename    TEXT PRIMARY KEY,
                    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            conn.commit()

        applied = 0
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM schema_migrations WHERE filename = %s", (path.name,))
                if cur.fetchone():
                    continue
                print(f"applying {path.name}...")
                cur.execute(path.read_text(encoding="utf-8"))
                cur.execute("INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,))
            conn.commit()
            applied += 1

        print(f"done: {applied} migration(s) applied")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DSN)
