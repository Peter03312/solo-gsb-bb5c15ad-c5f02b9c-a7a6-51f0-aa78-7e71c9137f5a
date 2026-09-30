"""FastAPI 入口：场馆疏散通道编排服务。

启动：uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Union

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import database
from .planner import PlanningError
from .schemas import ClockIn, ClosureIn, ConfirmIn, GroupIn, ScenarioIn
from .seed import seed_scenario
from .service import (
    CredentialInvalid,
    CredentialNotFound,
    EvacuationService,
    RequestError,
)

DB_PATH = os.environ.get("EVAC_DB_PATH", "/data/evacuation.db")
MAX_PATHS = int(os.environ.get("EVAC_MAX_PATHS", "10000"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = database.connect(DB_PATH)
    database.init_db(conn)
    service = EvacuationService(conn, max_paths=MAX_PATHS)
    app.state.conn = conn
    app.state.service = service
    # 首次启动（空库）：写入演示场景并完成初始编排
    state = database.load_state(conn)
    if not state.edges:
        service.replace_scenario(seed_scenario())
    yield
    conn.close()


app = FastAPI(
    title="场馆疏散通道编排服务",
    version="1.1.0",
    description="基于有向通道、容量与封闭时段的分组疏散编排（贪心 + 全量原子重排 + 封闭预演/凭据确认）",
    lifespan=lifespan,
)


@app.exception_handler(RequestError)
async def _request_error_handler(_: Request, exc: RequestError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": "bad_request", "detail": str(exc)})


@app.exception_handler(PlanningError)
async def _planning_error_handler(_: Request, exc: PlanningError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"error": "planning_conflict", "detail": str(exc)})


@app.exception_handler(CredentialNotFound)
async def _credential_not_found_handler(_: Request, exc: CredentialNotFound) -> JSONResponse:
    return JSONResponse(status_code=404, content={"error": "credential_not_found", "detail": str(exc)})


@app.exception_handler(CredentialInvalid)
async def _credential_invalid_handler(_: Request, exc: CredentialInvalid) -> JSONResponse:
    return JSONResponse(status_code=409, content={"error": "credential_invalid", "detail": str(exc)})


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/plan", summary="查询当前编排结果")
def get_plan(request: Request) -> dict:
    return request.app.state.service.snapshot()


@app.post("/api/scenario", summary="整体替换场景（清空后重置，时钟归 0）")
def replace_scenario(payload: ScenarioIn, request: Request) -> dict:
    return request.app.state.service.replace_scenario(payload)


@app.post("/api/groups", summary="新增一个或多个人群组，仅重排未出发组")
def add_groups(payload: Union[GroupIn, list[GroupIn]], request: Request) -> dict:
    items = payload if isinstance(payload, list) else [payload]
    return request.app.state.service.add_groups(items)


@app.delete("/api/groups/{group_id}", summary="删除未出发人群组并重排")
def delete_group(group_id: int, request: Request) -> dict:
    return request.app.state.service.delete_group(group_id)


@app.post("/api/closures", summary="新增一个或多个封闭时段；触及已出发组则整次拒绝")
def add_closures(payload: Union[ClosureIn, list[ClosureIn]], request: Request) -> dict:
    items = payload if isinstance(payload, list) else [payload]
    return request.app.state.service.add_closures(items)


@app.post("/api/closures/preview",
          summary="预演一批封闭（不写状态）：返回确认凭据、拟执行后编排与逐组变化")
def preview_closures(payload: Union[ClosureIn, list[ClosureIn]], request: Request) -> dict:
    items = payload if isinstance(payload, list) else [payload]
    return request.app.state.service.preview_closures(items)


@app.post("/api/closures/confirm",
          summary="按预演凭据原子提交封闭：核对内容与状态版本后重算并落库")
def confirm_closures(payload: ConfirmIn, request: Request) -> dict:
    return request.app.state.service.confirm_closures(payload.token)


@app.delete("/api/closures/{closure_id}", summary="删除封闭时段并重排未出发组")
def delete_closure(closure_id: int, request: Request) -> dict:
    return request.app.state.service.delete_closure(closure_id)


@app.post("/api/clock/advance", summary="时钟向前推进 N 分钟（已出发状态随之固定）")
def advance_clock(payload: ClockIn, request: Request) -> dict:
    return request.app.state.service.advance(payload.minutes)


@app.post("/api/reset", summary="恢复内置演示场景")
def reset_demo(request: Request) -> dict:
    return request.app.state.service.reset_seed(seed_scenario())
