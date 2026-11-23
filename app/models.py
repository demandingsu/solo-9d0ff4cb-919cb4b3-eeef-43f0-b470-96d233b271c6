"""Pydantic request and response models.

Request models are strict (``extra="forbid"``) so a typo in a client payload is
reported instead of silently ignored.  Identifiers are stripped and blank
values are rejected.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _strip_required(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("must not be blank")
    return stripped


def _strip_optional(value: str | None) -> str | None:
    if value is None:
        return None
    return _strip_required(value)


# ---------------------------------------------------------------------------
# Source data
# ---------------------------------------------------------------------------

class SourceRowInput(StrictModel):
    table_name: str = Field(min_length=1, max_length=128)
    row_no: int = Field(ge=1, le=2_000_000_000)
    merged_student_no: str = Field(min_length=1, max_length=128)

    _strip = field_validator("table_name", "merged_student_no")(_strip_required)


class SourceRowsUpsertRequest(StrictModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "rows": [
                        {
                            "table_name": "registration",
                            "row_no": 1,
                            "merged_student_no": "S001",
                        },
                        {
                            "table_name": "assessment",
                            "row_no": 10,
                            "merged_student_no": "S001",
                        },
                    ]
                }
            ]
        },
    )

    rows: list[SourceRowInput] = Field(min_length=1, max_length=5000)


class SourceRowOut(BaseModel):
    table_name: str
    row_no: int
    merged_student_no: str
    created_at: str
    updated_at: str


class SourceRowsUpsertResponse(BaseModel):
    received: int
    created: int
    updated: int
    unchanged: int
    rows: list[SourceRowOut]


class SourceRowListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[SourceRowOut]


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

class ProjectCreateRequest(StrictModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"name": "2026 春季培训课题组 A"}]},
    )

    name: str = Field(min_length=1, max_length=200)

    _strip = field_validator("name")(_strip_required)


class ProjectOut(BaseModel):
    project_id: str
    name: str
    created_at: str


class ProjectListResponse(BaseModel):
    total: int
    items: list[ProjectOut]


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

class DeliveryItemInput(StrictModel):
    table_name: str = Field(min_length=1, max_length=128)
    row_no: int = Field(ge=1, le=2_000_000_000)
    merged_student_no: str | None = Field(default=None, max_length=128)

    _strip_table = field_validator("table_name")(_strip_required)
    _strip_merged = field_validator("merged_student_no")(_strip_optional)


class DeliveryRequest(StrictModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "request_id": "demo-delivery-1",
                    "items": [
                        {"table_name": "registration", "row_no": 1},
                        {
                            "table_name": "assessment",
                            "row_no": 10,
                            "merged_student_no": "S001",
                        },
                    ],
                }
            ]
        },
    )

    request_id: str | None = Field(default=None, min_length=1, max_length=128)
    items: list[DeliveryItemInput] = Field(min_length=1, max_length=1000)

    _strip_request = field_validator("request_id")(_strip_optional)


class DeliveryItemOut(BaseModel):
    """A delivered row described only with project-specific numbers."""

    ordinal: int
    table_name: str
    project_row_no: int
    student_no: int
    first_registration: bool


class DeliveryOut(BaseModel):
    delivery_id: int
    project_id: str
    request_id: str | None
    created_at: str
    item_count: int
    new_project_row_count: int
    items: list[DeliveryItemOut]


class DeliveryListResponse(BaseModel):
    total: int
    items: list[DeliveryOut]


# ---------------------------------------------------------------------------
# Splits
# ---------------------------------------------------------------------------

class SplitRowInput(StrictModel):
    """A row to split out.

    A row is addressed either by source coordinates (``table_name`` and
    ``row_no``) or by the anonymous project row number that appears on a
    correction sheet (``original_row_no``).  Exactly one addressing mode must
    be used.
    """

    table_name: str | None = Field(default=None, max_length=128)
    row_no: int | None = Field(default=None, ge=1, le=2_000_000_000)
    original_row_no: int | None = Field(default=None, ge=1)

    _strip_table = field_validator("table_name")(_strip_optional)

    @model_validator(mode="after")
    def _exactly_one_address(self) -> "SplitRowInput":
        by_source = self.table_name is not None or self.row_no is not None
        by_anonymous = self.original_row_no is not None
        if by_source and by_anonymous:
            raise ValueError(
                "use either table_name+row_no or original_row_no, not both"
            )
        if not by_source and not by_anonymous:
            raise ValueError("provide table_name+row_no or original_row_no")
        if by_source and (self.table_name is None or self.row_no is None):
            raise ValueError("table_name and row_no must be provided together")
        return self


class SplitRequest(StrictModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "request_id": "demo-split-1",
                    "merged_student_no": "S001",
                    "rows": [
                        {"table_name": "assessment", "row_no": 10},
                        {"original_row_no": 2},
                    ],
                }
            ]
        },
    )

    request_id: str | None = Field(default=None, min_length=1, max_length=128)
    merged_student_no: str | None = Field(default=None, max_length=128)
    rows: list[SplitRowInput] = Field(min_length=1, max_length=1000)

    _strip_request = field_validator("request_id")(_strip_optional)
    _strip_merged = field_validator("merged_student_no")(_strip_optional)


class SplitRowOut(BaseModel):
    table_name: str
    source_row_no: int
    project_row_no: int
    old_student_no: int
    new_student_no: int
    was_delivered_under_old_no: bool
    correction_registered: bool


class SplitOut(BaseModel):
    split_id: int
    project_id: str
    request_id: str | None
    merged_student_no: str
    old_student_no: int
    new_student_no: int
    row_count: int
    correction_count: int
    created_at: str
    rows: list[SplitRowOut]


class SplitListResponse(BaseModel):
    total: int
    items: list[SplitOut]


# ---------------------------------------------------------------------------
# Correction sheets
# ---------------------------------------------------------------------------

CorrectionScope = Literal["project", "split", "delivery"]


class CorrectionLineOut(BaseModel):
    correction_id: int
    split_id: int
    table_name: str
    original_row_no: int
    old_student_no: int
    new_student_no: int
    delivery_id: int
    recorded_at: str


class CorrectionSheetOut(BaseModel):
    project_id: str
    scope: CorrectionScope
    split_id: int | None
    delivery_id: int | None
    line_count: int
    digest_sha256: str
    lines: list[CorrectionLineOut]


# ---------------------------------------------------------------------------
# Project rows and misc
# ---------------------------------------------------------------------------

class ProjectRowOut(BaseModel):
    project_row_no: int
    table_name: str
    source_row_no: int
    merged_student_no: str
    current_student_no: int
    original_student_no: int
    is_split_out: bool
    is_delivered: bool
    created_at: str
    updated_at: str


class ProjectRowListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[ProjectRowOut]


class HealthOut(BaseModel):
    status: str
    database: str


class DemoSeedResponse(BaseModel):
    project_id: str
    seeded: bool
    message: str
    summary: dict[str, Any]


class ErrorBody(BaseModel):
    code: str
    message: str
    details: Any | None = None


class ErrorEnvelope(BaseModel):
    error: ErrorBody
