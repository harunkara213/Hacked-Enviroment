"""Tiny database layer: Postgres when DATABASE_URL is set (Render), SQLite otherwise (local dev).

app.py writes SQLite-style SQL ("?" placeholders, REAL/BLOB types). This module adapts it
for Postgres and returns rows that work like sqlite3.Row: row["col"], row[0], dict(row).

Why: Render's free web server wipes its disk on every deploy and every sleep, so a SQLite
file there loses all spots, points and photos. Postgres keeps them.
"""
import os
import re
import sqlite3
from pathlib import Path

DATABASE_URL = os.getenv("DATABASE_URL", "")
IS_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))

# "user" is a reserved word in Postgres (SELECT user returns the login name), so always quote it.
_USER_COL = re.compile(r'(?<!["\w])user(?!["\w])')


class Row:
    """Behaves like sqlite3.Row: index by name or position, dict(row), iterate values."""
    __slots__ = ("_keys", "_vals")

    def __init__(self, keys, vals):
        self._keys, self._vals = keys, tuple(vals)

    def __getitem__(self, k):
        if isinstance(k, (int, slice)):
            return self._vals[k]
        return self._vals[self._keys.index(k)]

    def keys(self):
        return list(self._keys)

    def __iter__(self):
        return iter(self._vals)

    def __len__(self):
        return len(self._vals)


class Cursor:
    def __init__(self, cur, rowcount):
        self._cur, self.rowcount = cur, rowcount
        self._keys = [d[0] for d in cur.description] if cur.description else []

    def _wrap(self, r):
        if r is None:
            return None
        return Row(self._keys, [bytes(v) if isinstance(v, memoryview) else v for v in r])

    def fetchone(self):
        return self._wrap(self._cur.fetchone())

    def fetchall(self):
        return [self._wrap(r) for r in self._cur.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


def _adapt(sql: str) -> str:
    sql = _USER_COL.sub('"user"', sql)
    if IS_PG:
        sql = sql.replace("%", "%%").replace("?", "%s")
        sql = re.sub(r"\bREAL\b", "DOUBLE PRECISION", sql)  # Postgres REAL is too imprecise for GPS/timestamps
        sql = re.sub(r"\bBLOB\b", "BYTEA", sql)
    return sql


class Connection:
    """`with closing(db()) as c, c:` commits on success and rolls back on error, like sqlite3."""

    def __init__(self):
        if IS_PG:
            import psycopg
            self._conn = psycopg.connect(DATABASE_URL)
        else:
            path = Path(os.getenv("DB_PATH", Path(__file__).parent / "data.db"))
            self._conn = sqlite3.connect(path)

    def execute(self, sql, params=()):
        cur = self._conn.cursor()
        cur.execute(_adapt(sql), tuple(params))
        return Cursor(cur, cur.rowcount)

    def executescript(self, script):
        for stmt in (s.strip() for s in script.split(";")):
            if stmt:
                self.execute(stmt)

    def columns(self, table) -> set[str]:
        if IS_PG:
            rows = self.execute("SELECT column_name FROM information_schema.columns WHERE table_name=?", (table,))
            return {r[0] for r in rows}
        return {r[1] for r in self.execute(f"PRAGMA table_info({table})")}

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        if exc_type:
            self._conn.rollback()
        else:
            self._conn.commit()
        return False


def connect() -> Connection:
    return Connection()
