"""Checks of the derived-data cache's idea of "the database changed":
python3 tests/test_cache.py

Reading must leave the cache alone (SQLite touches the WAL's mtime when a
connection closes), and every write must clear it, before and after the WAL
is checkpointed and reused.
"""
import os
import sqlite3
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "dota_stats.db")

import db  # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool) -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")
    if not ok:
        failures.append(label)


def main() -> int:
    conn = sqlite3.connect(db.DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    conn.close()

    builds = []
    get = lambda: db._derived(("test",), lambda: builds.append(1) or [{"n": len(builds)}])
    get()
    for _ in range(5):
        time.sleep(0.01)
        with db._conn() as c:
            c.execute("SELECT COUNT(*) FROM t").fetchone()
        get()
    check("reading leaves the cache alone", len(builds) == 1)

    def write():
        time.sleep(0.01)
        c = sqlite3.connect(db.DB_PATH)
        c.execute("INSERT INTO t VALUES (1)")
        c.commit()
        return c

    keep = write()                      # a connection held open: the WAL keeps its frames
    get()
    check("a write clears it", len(builds) == 2)
    keep.execute("PRAGMA wal_checkpoint(PASSIVE)")
    write().close()                     # the WAL is reused from the start after a checkpoint
    get()
    check("so does a write after a checkpoint", len(builds) == 3)
    keep.close()
    write().close()                     # the last connection closing checkpoints and empties it
    get()
    check("and one checkpointed into the database file", len(builds) == 4)

    print()
    if failures:
        print(f"{len(failures)} FAILED: " + ", ".join(failures))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
