"""End-to-end tests for the supply/ correction API."""

from __future__ import annotations


def deliver(client, project_id, items, request_id=None):
    body = {"items": items}
    if request_id is not None:
        body["request_id"] = request_id
    response = client.post(f"/projects/{project_id}/deliveries", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def split(client, project_id, merged_student_no, rows, request_id=None):
    body = {"merged_student_no": merged_student_no, "rows": rows}
    if request_id is not None:
        body["request_id"] = request_id
    response = client.post(f"/projects/{project_id}/splits", json=body)
    return response


def rows_by_source(client, project_id):
    response = client.get(f"/projects/{project_id}/rows", params={"limit": 1000})
    assert response.status_code == 200, response.text
    return {
        (row["table_name"], row["source_row_no"]): row
        for row in response.json()["items"]
    }


def test_project_numbers_are_stable_and_shared_across_tables(client, project, seeded):
    first = deliver(
        client,
        project,
        [
            {"table_name": "registration", "row_no": 1},
            {"table_name": "assessment", "row_no": 10},
            {"table_name": "registration", "row_no": 2},
        ],
    )
    second = deliver(
        client,
        project,
        [
            {"table_name": "assessment", "row_no": 11},
            {"table_name": "registration", "row_no": 1},
        ],
    )

    items = {item["table_name"] + ":" + str(item["project_row_no"]): item for item in first["items"]}
    assert len(items) == 3
    # S001's two rows share one student number, each table gets its own row number.
    s001 = [i for i in first["items"] if i["table_name"] in ("registration",)]
    assert s001[0]["project_row_no"] == 1
    assert s001[0]["student_no"] == 1
    assert first["items"][1]["project_row_no"] == 2
    assert first["items"][1]["student_no"] == 1
    assert first["items"][2]["student_no"] == 2

    # Re-delivery keeps row numbers and student numbers.
    assert second["items"][1]["project_row_no"] == 1
    assert second["items"][1]["student_no"] == 1
    assert second["items"][0]["student_no"] == 2
    assert second["new_project_row_count"] == 1  # only assessment:11 is new

    rows = rows_by_source(client, project)
    assert rows[("registration", 1)]["project_row_no"] == 1
    assert rows[("assessment", 10)]["project_row_no"] == 2
    assert rows[("registration", 2)]["project_row_no"] == 3
    assert rows[("assessment", 11)]["project_row_no"] == 4


def test_delivery_isolation_between_projects(client, seeded):
    a = client.post("/projects", json={"name": "A"}).json()["project_id"]
    b = client.post("/projects", json={"name": "B"}).json()["project_id"]
    first_a = deliver(client, a, [{"table_name": "registration", "row_no": 1}])
    first_b = deliver(client, b, [{"table_name": "assessment", "row_no": 10}])
    assert first_a["items"][0]["project_row_no"] == 1
    assert first_b["items"][0]["project_row_no"] == 1
    assert first_a["items"][0]["student_no"] == 1
    assert first_b["items"][0]["student_no"] == 1


def test_unknown_and_duplicate_rows_rejected_without_side_effects(client, project, seeded):
    missing = client.post(
        f"/projects/{project}/deliveries",
        json={"items": [{"table_name": "registration", "row_no": 1}, {"table_name": "x", "row_no": 9}]},
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "source_row_not_found"

    duplicate = client.post(
        f"/projects/{project}/deliveries",
        json={
            "items": [
                {"table_name": "registration", "row_no": 1},
                {"table_name": "registration", "row_no": 1},
            ]
        },
    )
    assert duplicate.status_code == 422

    response = client.get(f"/projects/{project}/rows")
    assert response.json()["total"] == 0


def test_split_moves_rows_to_new_number_and_keeps_others(client, project, seeded):
    deliver(
        client,
        project,
        [
            {"table_name": "registration", "row_no": 2},
            {"table_name": "assessment", "row_no": 11},
            {"table_name": "assessment", "row_no": 12},
        ],
    )
    response = split(
        client,
        project,
        "S002",
        [
            {"table_name": "assessment", "row_no": 11},
            {"table_name": "assessment", "row_no": 12},
        ],
    )
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["row_count"] == 2
    assert result["correction_count"] == 2

    rows = rows_by_source(client, project)
    assert rows[("registration", 2)]["current_student_no"] == result["old_student_no"]
    assert rows[("assessment", 11)]["current_student_no"] == result["new_student_no"]
    assert rows[("assessment", 12)]["current_student_no"] == result["new_student_no"]
    # Row numbers never change.
    assert rows[("assessment", 11)]["project_row_no"] == 2
    assert rows[("assessment", 12)]["project_row_no"] == 3


def test_interleaved_delivery_and_split_corrections(client, project, seeded):
    # 1) deliver under the merged number
    d1 = deliver(
        client,
        project,
        [
            {"table_name": "registration", "row_no": 2},
            {"table_name": "assessment", "row_no": 11},
        ],
        request_id="d1",
    )
    # 2) another delivery of the row that will move
    d2 = deliver(client, project, [{"table_name": "assessment", "row_no": 12}], request_id="d2")
    # 3) split two rows out of S002; row 2 stays
    s1 = split(
        client,
        project,
        "S002",
        [
            {"table_name": "assessment", "row_no": 11},
            {"table_name": "assessment", "row_no": 12},
        ],
        request_id="s1",
    )
    assert s1.status_code == 201, s1.text
    new_no = s1.json()["new_student_no"]
    # 4) deliver after the split
    d3 = deliver(
        client,
        project,
        [
            {"table_name": "assessment", "row_no": 11},
            {"table_name": "registration", "row_no": 2},
        ],
        request_id="d3",
    )
    assert d3["items"][0]["student_no"] == new_no
    assert d3["items"][1]["student_no"] != new_no

    sheet = client.get(
        f"/projects/{project}/corrections", params={"split_id": s1.json()["split_id"]}
    ).json()
    assert sheet["line_count"] == 2
    correction_rows = {line["original_row_no"] for line in sheet["lines"]}
    # Both rows were delivered before the split, and the later delivery does
    # not create additional correction lines.
    assert correction_rows == {d1["items"][1]["project_row_no"], d2["items"][0]["project_row_no"]}
    for line in sheet["lines"]:
        assert line["old_student_no"] == s1.json()["old_student_no"]
        assert line["new_student_no"] == new_no

    # Fetching the same correction sheet again is byte-identical.
    again = client.get(
        f"/projects/{project}/corrections", params={"split_id": s1.json()["split_id"]}
    ).json()
    assert again == sheet


def test_correction_sheets_are_per_project(client, seeded):
    a = client.post("/projects", json={"name": "A"}).json()["project_id"]
    b = client.post("/projects", json={"name": "B"}).json()["project_id"]
    for pid in (a, b):
        deliver(client, pid, [{"table_name": "registration", "row_no": 2}])
    sa = split(client, a, "S002", [{"table_name": "registration", "row_no": 2}])
    assert sa.status_code == 201

    sheet_a = client.get(f"/projects/{a}/corrections").json()
    sheet_b = client.get(f"/projects/{b}/corrections").json()
    assert sheet_a["line_count"] == 1
    assert sheet_b["line_count"] == 0


def test_split_without_prior_delivery_has_empty_correction_sheet(client, project, seeded):
    deliver(client, project, [{"table_name": "registration", "row_no": 1}])
    response = split(client, project, "S002", [{"table_name": "assessment", "row_no": 11}])
    assert response.status_code == 201, response.text
    assert response.json()["correction_count"] == 0
    sheet = client.get(f"/projects/{project}/corrections").json()
    assert sheet["line_count"] == 0

    # The split row is known to the project and can be delivered later with
    # its new number.
    delivery = deliver(client, project, [{"table_name": "assessment", "row_no": 11}])
    assert delivery["items"][0]["student_no"] == response.json()["new_student_no"]


def test_failed_split_keeps_original_assignment_and_sheet(client, project, seeded):
    deliver(
        client,
        project,
        [
            {"table_name": "registration", "row_no": 2},
            {"table_name": "assessment", "row_no": 11},
        ],
    )
    first = split(client, project, "S002", [{"table_name": "assessment", "row_no": 11}])
    assert first.status_code == 201
    sheet_before = client.get(f"/projects/{project}/corrections").json()
    rows_before = rows_by_source(client, project)

    # assessment:11 no longer belongs to S002; this split must fail and must
    # not touch the existing correction sheet.
    failed = split(client, project, "S002", [{"table_name": "assessment", "row_no": 11}])
    assert failed.status_code == 409
    assert failed.json()["error"]["code"] == "row_not_in_student"

    assert rows_by_source(client, project) == rows_before
    assert client.get(f"/projects/{project}/corrections").json() == sheet_before

    # An invalid batch mixing a valid row with an invalid one must also roll
    # back completely: no new project rows, no new student numbers.
    failed_batch = split(
        client,
        project,
        "S002",
        [
            {"table_name": "registration", "row_no": 2},
            {"table_name": "assessment", "row_no": 11},
        ],
    )
    assert failed_batch.status_code == 409
    assert rows_by_source(client, project) == rows_before

    deliveries = client.get(f"/projects/{project}/deliveries").json()
    assert deliveries["total"] == 1


def test_split_addressing_by_anonymous_row_no(client, project, seeded):
    deliver(
        client,
        project,
        [
            {"table_name": "registration", "row_no": 2},
            {"table_name": "assessment", "row_no": 11},
        ],
    )
    rows = rows_by_source(client, project)
    anonymous = rows[("assessment", 11)]["project_row_no"]
    response = split(client, project, "S002", [{"original_row_no": anonymous}])
    assert response.status_code == 201, response.text
    assert response.json()["rows"][0]["project_row_no"] == anonymous
    assert rows_by_source(client, project)[("assessment", 11)]["current_student_no"] == response.json()["new_student_no"]

    unknown = split(client, project, "S002", [{"original_row_no": 99999}])
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "anonymous_row_not_found"


def test_request_id_makes_delivery_and_split_idempotent(client, project, seeded):
    payload = {"request_id": "fixed", "items": [{"table_name": "registration", "row_no": 1}]}
    first = client.post(f"/projects/{project}/deliveries", json=payload)
    second = client.post(f"/projects/{project}/deliveries", json=payload)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json() == second.json()

    conflict = client.post(
        f"/projects/{project}/deliveries",
        json={"request_id": "fixed", "items": [{"table_name": "assessment", "row_no": 10}]},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "request_id_conflict"

    split_payload = {
        "request_id": "split-fixed",
        "merged_student_no": "S002",
        "rows": [{"table_name": "assessment", "row_no": 11}],
    }
    split_first = client.post(f"/projects/{project}/splits", json=split_payload)
    split_second = client.post(f"/projects/{project}/splits", json=split_payload)
    assert split_first.status_code == 201
    assert split_second.status_code == 201
    assert split_first.json() == split_second.json()


def test_rows_span_multiple_students_rejected(client, project, seeded):
    response = split(
        client,
        project,
        None,
        [
            {"table_name": "registration", "row_no": 2},
            {"table_name": "registration", "row_no": 3},
        ],
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "rows_span_multiple_students"

    response = split(
        client,
        project,
        "S002",
        [
            {"table_name": "registration", "row_no": 2},
            {"table_name": "registration", "row_no": 3},
        ],
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "merged_student_mismatch"


def test_source_row_cannot_change_after_project_use(client, project, seeded):
    deliver(client, project, [{"table_name": "registration", "row_no": 1}])
    response = client.post(
        "/source-rows",
        json={"rows": [{"table_name": "registration", "row_no": 1, "merged_student_no": "S999"}]},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "source_row_locked"


def test_demo_seed_is_idempotent_and_consistent(client):
    first = client.post("/demo/seed")
    assert first.status_code == 200, first.text
    assert first.json()["seeded"] is True
    project_id = first.json()["project_id"]

    second = client.post("/demo/seed")
    assert second.json()["seeded"] is False
    assert second.json()["project_id"] == project_id

    rows = client.get(f"/projects/{project_id}/rows").json()
    anonymous = {row["project_row_no"] for row in rows["items"]}
    assert len(anonymous) == len(rows["items"])

    sheet = client.get(f"/projects/{project_id}/corrections").json()
    assert sheet["line_count"] == 2
    for line in sheet["lines"]:
        assert line["original_row_no"] in anonymous
        assert line["new_student_no"] != line["old_student_no"]


def test_validation_errors_use_the_standard_envelope(client, project):
    mixed_addressing = client.post(
        f"/projects/{project}/splits",
        json={"rows": [{"table_name": "x", "row_no": 1, "original_row_no": 2}]},
    )
    assert mixed_addressing.status_code == 422
    body = mixed_addressing.json()
    assert body["error"]["code"] == "validation_error"
    assert isinstance(body["error"]["details"]["errors"], list)

    unknown_field = client.post(
        f"/projects/{project}/deliveries",
        json={"items": [{"table_name": "x", "row_no": 1, "extra": True}]},
    )
    assert unknown_field.status_code == 422
    assert unknown_field.json()["error"]["code"] == "validation_error"


def test_correction_sheet_filtered_by_delivery(client, project, seeded):
    d1 = deliver(client, project, [{"table_name": "registration", "row_no": 2}])
    d2 = deliver(client, project, [{"table_name": "assessment", "row_no": 11}])
    result = split(
        client,
        project,
        "S002",
        [
            {"table_name": "registration", "row_no": 2},
            {"table_name": "assessment", "row_no": 11},
        ],
    )
    assert result.status_code == 201

    all_lines = client.get(f"/projects/{project}/corrections").json()
    assert all_lines["line_count"] == 2

    only_d1 = client.get(
        f"/projects/{project}/corrections", params={"delivery_id": d1["delivery_id"]}
    ).json()
    assert only_d1["scope"] == "delivery"
    assert [line["delivery_id"] for line in only_d1["lines"]] == [d1["delivery_id"]]

    only_d2 = client.get(
        f"/projects/{project}/corrections", params={"delivery_id": d2["delivery_id"]}
    ).json()
    assert [line["original_row_no"] for line in only_d2["lines"]] == [
        d2["items"][0]["project_row_no"]
    ]


def test_repeated_delivery_of_a_moved_row_yields_one_correction_per_record(
    client, project, seeded
):
    # The same row is delivered twice before the split.  Both records used the
    # old number, so both must appear on the correction sheet.
    first = deliver(client, project, [{"table_name": "assessment", "row_no": 11}])
    second = deliver(client, project, [{"table_name": "assessment", "row_no": 11}])
    result = split(
        client, project, "S002", [{"table_name": "assessment", "row_no": 11}]
    )
    assert result.status_code == 201, result.text
    sheet = client.get(f"/projects/{project}/corrections").json()
    assert sheet["line_count"] == 2
    assert {line["delivery_id"] for line in sheet["lines"]} == {
        first["delivery_id"],
        second["delivery_id"],
    }
    assert {line["original_row_no"] for line in sheet["lines"]} == {
        first["items"][0]["project_row_no"]
    }
    # Both lines point the same original row number at the same new number.
    assert {line["new_student_no"] for line in sheet["lines"]} == {
        result.json()["new_student_no"]
    }


def test_correction_sheet_is_stable_after_later_deliveries(client, project, seeded):
    deliver(client, project, [{"table_name": "registration", "row_no": 2}])
    result = split(
        client, project, "S002", [{"table_name": "registration", "row_no": 2}]
    )
    before = client.get(
        f"/projects/{project}/corrections",
        params={"split_id": result.json()["split_id"]},
    ).json()
    # Deliver the moved row again and split a different student; the earlier
    # correction sheet must not change.
    deliver(client, project, [{"table_name": "registration", "row_no": 2}])
    split(client, project, "S003", [{"table_name": "registration", "row_no": 3}])
    after = client.get(
        f"/projects/{project}/corrections",
        params={"split_id": result.json()["split_id"]},
    ).json()
    assert after == before
