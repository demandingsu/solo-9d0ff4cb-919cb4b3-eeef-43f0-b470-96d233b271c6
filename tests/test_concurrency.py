"""Concurrency and storage-level rollback tests.

These bypass the HTTP layer and drive the service with independent SQLite
connections, which is the situation the ``BEGIN IMMEDIATE`` transactions are
designed for.
"""

from __future__ import annotations

import threading

from app import service
from app.db import connect, init_db, write_tx


def _seed(db_path):
    init_db(db_path)
    conn = connect(db_path)
    try:
        service.upsert_source_rows(
            conn,
            [
                {"table_name": "registration", "row_no": 1, "merged_student_no": "S1"},
                {"table_name": "assessment", "row_no": 10, "merged_student_no": "S1"},
                {"table_name": "registration", "row_no": 2, "merged_student_no": "S2"},
                {"table_name": "assessment", "row_no": 11, "merged_student_no": "S2"},
            ],
        )
        return service.create_project(conn, "并发课题组")["project_id"]
    finally:
        conn.close()


def test_parallel_deliveries_get_unique_row_and_student_numbers(tmp_path):
    db_path = str(tmp_path / "concurrent.db")
    project_id = _seed(db_path)

    results: list[dict] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker(index: int) -> None:
        conn = connect(db_path)
        try:
            barrier.wait(timeout=10)
            rows = [
                {"table_name": "registration", "row_no": 1},
                {"table_name": "assessment", "row_no": 10},
                {"table_name": "registration", "row_no": 2},
                {"table_name": "assessment", "row_no": 11},
            ]
            result = service.deliver(conn, project_id, rows, request_id=f"r{index}")
            results.append(result)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, errors
    assert len(results) == 8

    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT project_row_no, current_student_no FROM project_rows WHERE project_id = ?",
            (project_id,),
        ).fetchall()
        assert len(rows) == 4
        assert len({r["project_row_no"] for r in rows}) == 4
        # S1 and S2 each own exactly one student number, shared across tables.
        student_numbers = {r["current_student_no"] for r in rows}
        assert student_numbers == {1, 2}
    finally:
        conn.close()

    for result in results:
        assert [(i["project_row_no"], i["student_no"]) for i in result["items"]] == [
            (1, 1),
            (2, 1),
            (3, 2),
            (4, 2),
        ]


def test_parallel_splits_never_reuse_a_student_number(tmp_path):
    db_path = str(tmp_path / "split-race.db")
    project_id = _seed(db_path)

    setup = connect(db_path)
    try:
        service.deliver(
            setup,
            project_id,
            [{"table_name": "assessment", "row_no": 10}],
            request_id="setup",
        )
    finally:
        setup.close()

    results: list[dict] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(6)

    def worker(index: int) -> None:
        conn = connect(db_path)
        try:
            barrier.wait(timeout=10)
            # Every worker splits the same (not yet split) row; only one may
            # succeed, and every failure must leave state untouched.
            result = service.split_student(
                conn,
                project_id,
                "S1",
                [{"table_name": "assessment", "row_no": 10}],
                request_id=f"split-{index}",
            )
            results.append(result)
        except Exception as exc:
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(results) == 1, errors
    assert len(errors) == 5
    conn = connect(db_path)
    try:
        splits = conn.execute(
            "SELECT new_student_no FROM splits WHERE project_id = ?", (project_id,)
        ).fetchall()
        assert len(splits) == 1
        notices = conn.execute(
            "SELECT COUNT(*) AS n FROM correction_items WHERE project_id = ?",
            (project_id,),
        ).fetchone()["n"]
        assert notices == 1
        row = conn.execute(
            """
            SELECT current_student_no FROM project_rows
            WHERE project_id = ? AND table_name = 'assessment' AND row_no = 10
            """,
            (project_id,),
        ).fetchone()
        assert row["current_student_no"] == splits[0]["new_student_no"]
    finally:
        conn.close()


def test_failed_split_rolls_back_counters_and_student_numbers(tmp_path):
    db_path = str(tmp_path / "rollback.db")
    project_id = _seed(db_path)
    conn = connect(db_path)
    try:
        service.deliver(
            conn,
            project_id,
            [{"table_name": "registration", "row_no": 2}],
            request_id="d1",
        )
        before = conn.execute(
            "SELECT next_row_no, next_student_no FROM project_counters WHERE project_id = ?",
            (project_id,),
        ).fetchone()

        # Force a failure deep inside the split transaction: the source row is
        # valid, but the second row does not belong to S2.
        try:
            with write_tx(conn):
                conn.execute(
                    "UPDATE source_rows SET merged_student_no = 'S1' WHERE table_name = 'assessment' AND row_no = 11"
                )
                raise RuntimeError("injected failure")
        except RuntimeError:
            pass
        # The update above was rolled back; verify the transaction helper.
        merged = conn.execute(
            "SELECT merged_student_no FROM source_rows WHERE table_name = 'assessment' AND row_no = 11"
        ).fetchone()["merged_student_no"]
        assert merged == "S2"

        # A normal failing split (row already split) must not move counters.
        first = service.split_student(
            conn, project_id, "S2", [{"table_name": "registration", "row_no": 2}], None
        )
        after_first = conn.execute(
            "SELECT next_row_no, next_student_no FROM project_counters WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        try:
            service.split_student(
                conn, project_id, "S2", [{"table_name": "registration", "row_no": 2}], None
            )
        except Exception:
            pass
        after_failure = conn.execute(
            "SELECT next_row_no, next_student_no FROM project_counters WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        assert after_failure["next_row_no"] == after_first["next_row_no"]
        assert after_failure["next_student_no"] == after_first["next_student_no"]
        assert after_first["next_student_no"] > before["next_student_no"]
        assert first["correction_count"] == 1

        sheet = service.correction_sheet(conn, project_id, None, None)
        assert sheet["line_count"] == 1
    finally:
        conn.close()
