"""Deterministic demo data for ``POST /demo/seed``.

The scenario is designed so that it exercises every rule in one run:

* two source tables (``registration`` and ``assessment``) share merged student
  numbers, so the project student number is shared across tables;
* merged student ``S002`` is an accidental merge of two real people.  It is
  delivered before the split, split, and delivered again afterwards, so its
  correction sheet contains the interspersed deliveries while the later
  delivery uses the new student number;
* a second split (``S003``) only touches a row that was never delivered, so it
  must produce an empty correction sheet.

Seeding is idempotent: it returns the project it created on the first call.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import service

DEMO_PROJECT_NAME = "演示课题组：培训报名与测评"

SOURCE_ROWS: list[dict[str, Any]] = [
    # S001: one learner, one row in each table.
    {"table_name": "registration", "row_no": 1, "merged_student_no": "S001"},
    {"table_name": "assessment", "row_no": 10, "merged_student_no": "S001"},
    # S002: two people accidentally merged, three rows in two tables.
    {"table_name": "registration", "row_no": 2, "merged_student_no": "S002"},
    {"table_name": "assessment", "row_no": 11, "merged_student_no": "S002"},
    {"table_name": "assessment", "row_no": 12, "merged_student_no": "S002"},
    # S003: never delivered before its split.
    {"table_name": "registration", "row_no": 3, "merged_student_no": "S003"},
    {"table_name": "assessment", "row_no": 13, "merged_student_no": "S003"},
    # S004: delivered but never split.
    {"table_name": "registration", "row_no": 4, "merged_student_no": "S004"},
    {"table_name": "assessment", "row_no": 14, "merged_student_no": "S004"},
]


def seed_demo(conn: sqlite3.Connection) -> dict[str, Any]:
    existing = service.find_project_by_name(conn, DEMO_PROJECT_NAME)
    if existing is not None:
        return {
            "project_id": existing,
            "seeded": False,
            "message": "演示数据已存在，未重复写入",
            "summary": _summary(conn, existing),
        }

    from .db import write_tx

    with write_tx(conn):
        service.upsert_source_rows_tx(conn, SOURCE_ROWS)
        project = service.create_project_tx(conn, DEMO_PROJECT_NAME)
        project_id = project["project_id"]

        # 1. First delivery: every row of S002 and S004, plus S001's two rows.
        first = service.deliver_tx(
            conn,
            project_id,
            [
                {"table_name": "registration", "row_no": 1},
                {"table_name": "assessment", "row_no": 10},
                {"table_name": "registration", "row_no": 2},
                {"table_name": "assessment", "row_no": 11},
                {"table_name": "registration", "row_no": 4},
                {"table_name": "assessment", "row_no": 14},
            ],
            request_id="demo-delivery-1",
        )
        # 2. A second delivery interleaved after the first: S002's third row.
        service.deliver_tx(
            conn,
            project_id,
            [{"table_name": "assessment", "row_no": 12}],
            request_id="demo-delivery-2",
        )
        # 3. Split the accidental merge of S002: two of its three rows become
        #    a new student, one row stays with S002.  Both moved rows were
        #    already delivered under the old number.
        split = service.split_student_tx(
            conn,
            project_id,
            "S002",
            [
                {"table_name": "assessment", "row_no": 11},
                {"table_name": "assessment", "row_no": 12},
            ],
            request_id="demo-split-1",
        )
        # 4. Delivery after the split: the moved row comes out with the new
        #    number and must not generate an extra correction line.
        service.deliver_tx(
            conn,
            project_id,
            [
                {"table_name": "assessment", "row_no": 11},
                {"table_name": "registration", "row_no": 2},
            ],
            request_id="demo-delivery-3",
        )
        # 5. Split rows that were never delivered: empty correction sheet.
        service.split_student_tx(
            conn,
            project_id,
            "S003",
            [
                {"table_name": "registration", "row_no": 3},
                {"table_name": "assessment", "row_no": 13},
            ],
            request_id="demo-split-2",
        )

        summary = _summary(conn, project_id)
        summary["first_delivery_id"] = first["delivery_id"]
        summary["first_split_id"] = split["split_id"]
    return {
        "project_id": project_id,
        "seeded": True,
        "message": "演示数据已生成",
        "summary": summary,
    }


def _summary(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    counts = {}
    for key, sql in {
        "source_rows": "SELECT COUNT(*) AS n FROM source_rows",
        "project_rows": "SELECT COUNT(*) AS n FROM project_rows WHERE project_id = ?",
        "deliveries": "SELECT COUNT(*) AS n FROM deliveries WHERE project_id = ?",
        "delivery_items": "SELECT COUNT(*) AS n FROM delivery_items WHERE project_id = ?",
        "splits": "SELECT COUNT(*) AS n FROM splits WHERE project_id = ?",
        "correction_lines": "SELECT COUNT(*) AS n FROM correction_items WHERE project_id = ?",
    }.items():
        params = () if key == "source_rows" else (project_id,)
        counts[key] = int(conn.execute(sql, params).fetchone()["n"])
    return counts
