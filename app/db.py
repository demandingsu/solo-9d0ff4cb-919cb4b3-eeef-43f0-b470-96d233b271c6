"""SQLite connection helpers and schema definition.

All state lives in SQLite.  Every request gets its own connection.  Write
operations use ``BEGIN IMMEDIATE`` so number allocation, delivery registration
and splitting are serialised even when the API is started with multiple
workers or receives concurrent requests.

Key modelling decisions
-----------------------
* ``project_rows`` gives each ``(project, source table, source row)`` one
  anonymous project row number that never changes.
* ``project_student_numbers`` gives each merged student exactly one anonymous
  project student number, shared by every table of the project.  A split group
  receives its own row in the same table with ``kind = 'split'``.
* ``delivery_items`` records every successfully registered delivery line: one
  row per delivered ``(delivery, source row)`` with the student number the
  project saw at that moment.  This is the "successful delivery record" the
  correction sheet is derived from; it is never rewritten.
* ``correction_items`` links a split to each delivery item that used the old
  student number for a moved row.  Lines are written in the same transaction as
  the split, keyed by the *original anonymous row number* and the new student
  number, so a correction sheet is immutable and can be re-fetched verbatim.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS projects (
    project_id   TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_counters (
    project_id      TEXT PRIMARY KEY
                        REFERENCES projects(project_id) ON DELETE CASCADE,
    next_row_no     INTEGER NOT NULL DEFAULT 1,
    next_student_no INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS source_rows (
    table_name        TEXT NOT NULL,
    row_no            INTEGER NOT NULL,
    merged_student_no TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (table_name, row_no)
);

CREATE TABLE IF NOT EXISTS project_student_numbers (
    project_id        TEXT NOT NULL REFERENCES projects(project_id)
                          ON DELETE CASCADE,
    student_no        INTEGER NOT NULL,
    kind              TEXT NOT NULL CHECK (kind IN ('merged', 'split')),
    merged_student_no TEXT,
    origin_merged_student_no TEXT NOT NULL,
    split_id          INTEGER,
    created_at        TEXT NOT NULL,
    PRIMARY KEY (project_id, student_no),
    CHECK (
        (kind = 'merged' AND merged_student_no IS NOT NULL AND split_id IS NULL)
        OR (kind = 'split' AND merged_student_no IS NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_project_student_numbers_merged
    ON project_student_numbers(project_id, merged_student_no)
    WHERE merged_student_no IS NOT NULL;

CREATE TABLE IF NOT EXISTS project_rows (
    project_id          TEXT NOT NULL REFERENCES projects(project_id)
                            ON DELETE CASCADE,
    table_name          TEXT NOT NULL,
    row_no              INTEGER NOT NULL,
    project_row_no      INTEGER NOT NULL,
    current_student_no  INTEGER NOT NULL,
    original_student_no INTEGER NOT NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    PRIMARY KEY (project_id, table_name, row_no),
    UNIQUE (project_id, project_row_no),
    FOREIGN KEY (table_name, row_no)
        REFERENCES source_rows(table_name, row_no),
    FOREIGN KEY (project_id, current_student_no)
        REFERENCES project_student_numbers(project_id, student_no),
    FOREIGN KEY (project_id, original_student_no)
        REFERENCES project_student_numbers(project_id, student_no)
);

CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id            TEXT NOT NULL REFERENCES projects(project_id)
                              ON DELETE CASCADE,
    request_id            TEXT,
    request_fingerprint   TEXT NOT NULL,
    requested_count       INTEGER NOT NULL,
    new_project_row_count INTEGER NOT NULL DEFAULT 0,
    created_at            TEXT NOT NULL,
    UNIQUE (project_id, request_id)
);

CREATE TABLE IF NOT EXISTS delivery_items (
    delivery_item_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id       INTEGER NOT NULL REFERENCES deliveries(delivery_id)
                          ON DELETE CASCADE,
    project_id        TEXT NOT NULL,
    ordinal           INTEGER NOT NULL,
    table_name        TEXT NOT NULL,
    row_no            INTEGER NOT NULL,
    project_row_no    INTEGER NOT NULL,
    student_no        INTEGER NOT NULL,
    source_merged_student_no TEXT NOT NULL,
    first_registration INTEGER NOT NULL CHECK (first_registration IN (0, 1)),
    created_at        TEXT NOT NULL,
    UNIQUE (delivery_id, ordinal),
    FOREIGN KEY (project_id, table_name, row_no)
        REFERENCES project_rows(project_id, table_name, row_no),
    FOREIGN KEY (project_id, student_no)
        REFERENCES project_student_numbers(project_id, student_no)
);

CREATE TABLE IF NOT EXISTS splits (
    split_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id         TEXT NOT NULL REFERENCES projects(project_id)
                           ON DELETE CASCADE,
    request_id         TEXT,
    request_fingerprint TEXT NOT NULL,
    old_student_no     INTEGER NOT NULL,
    new_student_no     INTEGER NOT NULL,
    row_count          INTEGER NOT NULL,
    correction_count   INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL,
    UNIQUE (project_id, request_id),
    UNIQUE (project_id, new_student_no),
    FOREIGN KEY (project_id, old_student_no)
        REFERENCES project_student_numbers(project_id, student_no)
);

CREATE TABLE IF NOT EXISTS split_rows (
    split_id       INTEGER NOT NULL REFERENCES splits(split_id)
                       ON DELETE CASCADE,
    project_id     TEXT NOT NULL,
    table_name     TEXT NOT NULL,
    row_no         INTEGER NOT NULL,
    project_row_no INTEGER NOT NULL,
    old_student_no INTEGER NOT NULL,
    new_student_no INTEGER NOT NULL,
    was_in_project INTEGER NOT NULL CHECK (was_in_project IN (0, 1)),
    PRIMARY KEY (split_id, table_name, row_no),
    FOREIGN KEY (project_id, table_name, row_no)
        REFERENCES project_rows(project_id, table_name, row_no)
);

CREATE TABLE IF NOT EXISTS correction_items (
    correction_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id       TEXT NOT NULL REFERENCES projects(project_id)
                         ON DELETE CASCADE,
    split_id         INTEGER NOT NULL REFERENCES splits(split_id)
                         ON DELETE CASCADE,
    delivery_item_id INTEGER NOT NULL
                         REFERENCES delivery_items(delivery_item_id),
    delivery_id      INTEGER NOT NULL REFERENCES deliveries(delivery_id),
    table_name       TEXT NOT NULL,
    row_no           INTEGER NOT NULL,
    original_row_no  INTEGER NOT NULL,
    old_student_no   INTEGER NOT NULL,
    new_student_no   INTEGER NOT NULL,
    recorded_at      TEXT NOT NULL,
    UNIQUE (split_id, delivery_item_id)
);

CREATE INDEX IF NOT EXISTS ix_project_rows_student
    ON project_rows(project_id, current_student_no);
CREATE INDEX IF NOT EXISTS ix_delivery_items_project
    ON delivery_items(project_id, table_name, row_no);
CREATE INDEX IF NOT EXISTS ix_delivery_items_project_row_student
    ON delivery_items(project_id, table_name, row_no, student_no);
CREATE INDEX IF NOT EXISTS ix_correction_items_project_row
    ON correction_items(project_id, original_row_no);
CREATE INDEX IF NOT EXISTS ix_splits_project
    ON splits(project_id, split_id);
"""


def connect(db_path: str) -> sqlite3.Connection:
    """Open a connection configured for this application."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(path),
        timeout=30.0,
        isolation_level=None,  # explicit transactions only
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(db_path: str) -> None:
    """Create the schema and switch the database to WAL mode."""
    conn = connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(SCHEMA)
        conn.execute("PRAGMA user_version = 1")
    finally:
        conn.close()


@contextmanager
def write_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """An immediate write transaction.

    ``BEGIN IMMEDIATE`` acquires SQLite's write lock before any read that is
    used for allocation.  This keeps project row/student numbers unique when
    delivery and split requests arrive concurrently.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@contextmanager
def read_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """A read transaction giving a consistent snapshot for multi-query reads."""
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        conn.execute("COMMIT")
