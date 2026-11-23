"""FastAPI application: routing, error envelopes and demo seeding."""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from typing import Annotated, AsyncIterator

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import __version__, service
from .db import connect, init_db, read_tx
from .demo import seed_demo
from .errors import ApiError
from .models import (
    CorrectionSheetOut,
    DeliveryListResponse,
    DeliveryOut,
    DeliveryRequest,
    DemoSeedResponse,
    ErrorEnvelope,
    HealthOut,
    ProjectCreateRequest,
    ProjectListResponse,
    ProjectOut,
    ProjectRowListResponse,
    SourceRowListResponse,
    SourceRowsUpsertRequest,
    SourceRowsUpsertResponse,
    SplitListResponse,
    SplitOut,
    SplitRequest,
)

log = logging.getLogger("supply_correction")

DEFAULT_DB_PATH = os.environ.get("APP_DB_PATH", "./data/app.db")
MAX_PAGE_LIMIT = 1000


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_db(app.state.db_path)
    log.info("database ready at %s", app.state.db_path)
    yield


app = FastAPI(
    title="培训供数与更正 API",
    version=__version__,
    description=(
        "培训报名表与测评表按课题分批交付，使用课题专用匿名行号和学员号；"
        "支持误并学员的拆分，并按成功登记的交付记录出具可重复获取的更正单。\n\n"
        "演示：先 `POST /demo/seed` 生成一套包含跨表关联、交错交付与拆分的"
        "数据，再用 `GET /projects/{project_id}/rows` 和 "
        "`GET /projects/{project_id}/corrections` 查看编号与更正单。"
    ),
    lifespan=lifespan,
    openapi_tags=[
        {"name": "system", "description": "健康检查"},
        {"name": "source", "description": "源明细：表名 + 行号 + 归并学员号"},
        {"name": "projects", "description": "课题组"},
        {"name": "delivery", "description": "按课题交付与课题内匿名编号"},
        {"name": "split", "description": "误并学员拆分"},
        {"name": "corrections", "description": "更正单"},
        {"name": "demo", "description": "演示数据"},
    ],
)
app.state.db_path = DEFAULT_DB_PATH


# ---------------------------------------------------------------------------
# Dependencies and error handling
# ---------------------------------------------------------------------------

def get_conn(request: Request):
    conn = connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


@app.exception_handler(ApiError)
async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.payload())


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    _: Request, exc: RequestValidationError
) -> JSONResponse:
    # Pydantic puts the raised ``ValueError`` instance into ``ctx``; render
    # every context value as text so the envelope stays JSON serialisable.
    errors = []
    for error in exc.errors():
        rendered = {k: v for k, v in error.items() if k != "ctx"}
        if "ctx" in error:
            rendered["ctx"] = {k: str(v) for k, v in error["ctx"].items()}
        rendered["loc"] = [str(part) for part in rendered.get("loc", ())]
        errors.append(rendered)
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "validation_error",
                "message": "请求体或参数未通过校验",
                "details": {"errors": errors},
            }
        },
    )


@app.exception_handler(sqlite3.IntegrityError)
async def integrity_error_handler(
    _: Request, exc: sqlite3.IntegrityError
) -> JSONResponse:
    log.warning("sqlite integrity error: %s", exc)
    return JSONResponse(
        status_code=409,
        content={
            "error": {
                "code": "storage_conflict",
                "message": "数据约束冲突，操作未生效",
                "details": {"reason": str(exc)},
            }
        },
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health", response_model=HealthOut, tags=["system"], summary="健康检查")
def health(conn: Conn) -> HealthOut:
    conn.execute("SELECT 1").fetchone()
    return HealthOut(status="ok", database="sqlite")


# ---------------------------------------------------------------------------
# Source rows
# ---------------------------------------------------------------------------

@app.post(
    "/source-rows",
    response_model=SourceRowsUpsertResponse,
    tags=["source"],
    summary="登记或更正源明细（表名 + 行号 + 归并学员号）",
    responses={409: {"model": ErrorEnvelope}, 422: {"model": ErrorEnvelope}},
)
def upsert_source_rows(payload: SourceRowsUpsertRequest, conn: Conn):
    return service.upsert_source_rows(conn, [row.model_dump() for row in payload.rows])


@app.get(
    "/source-rows",
    response_model=SourceRowListResponse,
    tags=["source"],
    summary="查看已登记的源明细",
)
def list_source_rows(
    conn: Conn,
    table_name: str | None = Query(default=None, max_length=128),
    limit: int = Query(default=50, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(default=0, ge=0),
):
    with read_tx(conn):
        return service.list_source_rows(conn, limit, offset, table_name)


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

@app.post(
    "/projects",
    response_model=ProjectOut,
    status_code=201,
    tags=["projects"],
    summary="创建课题组",
)
def create_project(payload: ProjectCreateRequest, conn: Conn):
    return service.create_project(conn, payload.name)


@app.get(
    "/projects", response_model=ProjectListResponse, tags=["projects"], summary="列出课题组"
)
def list_projects(conn: Conn):
    with read_tx(conn):
        return service.list_projects(conn)


@app.get(
    "/projects/{project_id}",
    response_model=ProjectOut,
    tags=["projects"],
    summary="查看课题组",
    responses={404: {"model": ErrorEnvelope}},
)
def get_project(project_id: str, conn: Conn):
    with read_tx(conn):
        return service.get_project(conn, project_id)


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

@app.post(
    "/projects/{project_id}/deliveries",
    response_model=DeliveryOut,
    status_code=201,
    tags=["delivery"],
    summary="按课题交付指定行（返回课题专用行号和学员号）",
    responses={404: {"model": ErrorEnvelope}, 409: {"model": ErrorEnvelope}},
)
def create_delivery(project_id: str, payload: DeliveryRequest, conn: Conn):
    return service.deliver(
        conn,
        project_id,
        [item.model_dump() for item in payload.items],
        payload.request_id,
    )


@app.get(
    "/projects/{project_id}/deliveries",
    response_model=DeliveryListResponse,
    tags=["delivery"],
    summary="列出成功登记的交付记录",
)
def list_deliveries(
    project_id: str,
    conn: Conn,
    limit: int = Query(default=50, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(default=0, ge=0),
):
    with read_tx(conn):
        return service.list_deliveries(conn, project_id, limit, offset)


@app.get(
    "/projects/{project_id}/deliveries/{delivery_id}",
    response_model=DeliveryOut,
    tags=["delivery"],
    summary="重取一条交付记录",
    responses={404: {"model": ErrorEnvelope}},
)
def get_delivery(project_id: str, delivery_id: int, conn: Conn):
    with read_tx(conn):
        return service.get_delivery(conn, project_id, delivery_id)


@app.get(
    "/projects/{project_id}/rows",
    response_model=ProjectRowListResponse,
    tags=["delivery"],
    summary="查看课题行号、学员号与拆分状态",
)
def list_project_rows(
    project_id: str,
    conn: Conn,
    table_name: str | None = Query(default=None, max_length=128),
    student_no: int | None = Query(default=None, ge=1),
    limit: int = Query(default=50, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(default=0, ge=0),
):
    with read_tx(conn):
        return service.list_project_rows(
            conn, project_id, limit, offset, table_name, student_no
        )


# ---------------------------------------------------------------------------
# Splits
# ---------------------------------------------------------------------------

@app.post(
    "/projects/{project_id}/splits",
    response_model=SplitOut,
    status_code=201,
    tags=["split"],
    summary="拆分误并学员：指定要拆出的行，统一分配新学员号",
    responses={
        404: {"model": ErrorEnvelope},
        409: {"model": ErrorEnvelope},
    },
)
def create_split(project_id: str, payload: SplitRequest, conn: Conn):
    return service.split_student(
        conn,
        project_id,
        payload.merged_student_no,
        [row.model_dump() for row in payload.rows],
        payload.request_id,
    )


@app.get(
    "/projects/{project_id}/splits",
    response_model=SplitListResponse,
    tags=["split"],
    summary="列出拆分记录",
)
def list_splits(
    project_id: str,
    conn: Conn,
    limit: int = Query(default=50, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(default=0, ge=0),
):
    with read_tx(conn):
        return service.list_splits(conn, project_id, limit, offset)


@app.get(
    "/projects/{project_id}/splits/{split_id}",
    response_model=SplitOut,
    tags=["split"],
    summary="重取一条拆分记录",
    responses={404: {"model": ErrorEnvelope}},
)
def get_split(project_id: str, split_id: int, conn: Conn):
    with read_tx(conn):
        return service.get_split(conn, project_id, split_id)


# ---------------------------------------------------------------------------
# Correction sheets
# ---------------------------------------------------------------------------

@app.get(
    "/projects/{project_id}/corrections",
    response_model=CorrectionSheetOut,
    tags=["corrections"],
    summary="按成功交付记录出具更正单（可原样重取）",
    description=(
        "不传参数时返回该课题全部更正行；传 `split_id` 则返回该次拆分的"
        "更正单（内容固定，可重复获取）；传 `delivery_id` 则只看某次交付"
        "涉及的更正行。"
    ),
    responses={404: {"model": ErrorEnvelope}},
)
def correction_sheet(
    project_id: str,
    conn: Conn,
    split_id: int | None = Query(default=None, ge=1),
    delivery_id: int | None = Query(default=None, ge=1),
):
    with read_tx(conn):
        return service.correction_sheet(conn, project_id, split_id, delivery_id)


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

@app.post(
    "/demo/seed",
    response_model=DemoSeedResponse,
    tags=["demo"],
    summary="生成跨表交付、交错拆分与更正单演示数据",
)
def demo_seed(conn: Conn):
    return seed_demo(conn)
