from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("APP_DB_PATH", str(db_path))

    import app.main as main_module

    importlib.reload(main_module)
    main_module.app.state.db_path = str(db_path)
    with TestClient(main_module.app) as test_client:
        yield test_client


@pytest.fixture()
def project(client):
    response = client.post("/projects", json={"name": "课题组 测试"})
    assert response.status_code == 201, response.text
    return response.json()["project_id"]


@pytest.fixture()
def seeded(client):
    source_rows = [
        {"table_name": "registration", "row_no": 1, "merged_student_no": "S001"},
        {"table_name": "assessment", "row_no": 10, "merged_student_no": "S001"},
        {"table_name": "registration", "row_no": 2, "merged_student_no": "S002"},
        {"table_name": "assessment", "row_no": 11, "merged_student_no": "S002"},
        {"table_name": "assessment", "row_no": 12, "merged_student_no": "S002"},
        {"table_name": "registration", "row_no": 3, "merged_student_no": "S003"},
        {"table_name": "assessment", "row_no": 13, "merged_student_no": "S003"},
    ]
    response = client.post("/source-rows", json={"rows": source_rows})
    assert response.status_code == 200, response.text
    return source_rows
