from contextlib import asynccontextmanager
import asyncio
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import agents, service
from .config import BASE_DIR, LLM_BASE_URL, LLM_MODEL, SEMANTIC_BRIDGE_ENABLED
from .db import get_conn, init_db, loads
from .mapdata import load_map
from .parser import parse_task
from .semantic_bridge import router as semantic_router, run_runtime, stop_runtime
from .schemas import (
    AcceptTaskRequest,
    ActionRequest,
    CreateTaskRequest,
    ParseRequest,
    PlanRequest,
    ReplanRequest,
    RoadEventRequest,
    TaskNoteRequest,
    SimTickRequest,
)

SEED_TEXTS = [
    "从计算机学院取3份实验材料，分别送到科研楼、实验室和学一楼，实验室材料优先，16:00前完成。",
    "从后勤服务中心取2箱饮用水送到图书馆和学一楼，图书馆优先，无截止时间。",
    "从计算机学院取5份会议资料送到科研楼和后勤服务中心。",
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    runtime = asyncio.create_task(run_runtime()) if SEMANTIC_BRIDGE_ENABLED else None
    try:
        yield
    finally:
        if runtime:
            await stop_runtime(runtime)


app = FastAPI(title="CampusPilot API", version="1.0.0", lifespan=lifespan)
app.include_router(semantic_router)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:4173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


FRONTEND_DIR = BASE_DIR.parent / "frontend"

if (FRONTEND_DIR / "static").is_dir():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR / "static")), name="static")
if (FRONTEND_DIR / "vendor").is_dir():
    app.mount("/vendor", StaticFiles(directory=str(FRONTEND_DIR / "vendor")), name="vendor")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


@app.exception_handler(service.ApiError)
async def api_error_handler(request: Request, exc: service.ApiError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"code": exc.code, "message": exc.message})


@app.get("/api/health")
def health() -> dict[str, Any]:
    mapdata = load_map()
    return {
        "status": "ok",
        "map_version": mapdata.map_version,
        "llm_configured": bool(LLM_BASE_URL and LLM_MODEL),
        "llm_model": LLM_MODEL or None,
        "data_notice": mapdata.data_notice,
        "semantic_bridge_enabled": SEMANTIC_BRIDGE_ENABLED,
    }


@app.get("/api/map")
def get_map() -> dict[str, Any]:
    mapdata = load_map()
    payload = mapdata.to_public_dict()
    with get_conn() as conn:
        payload["road_status"] = service.get_road_status(conn)
        payload["road_events"] = service.road_events_public(conn)
    return payload


@app.post("/api/tasks/parse")
def parse(payload: ParseRequest) -> dict[str, Any]:
    result = parse_task(payload.text, payload.clarification)
    with get_conn() as conn:
        trace_id = agents.record(
            conn,
            "task_planner",
            "parse_task",
            input_summary=payload.text,
            output_summary=(
                f"取货={result['draft']['pickup_location_id'] or '未确定'}；"
                f"配送站点={len(result['draft']['stops'])}；待补={len(result['missing_fields'])}；"
                f"解析={result['parser']}"
            ),
            tools=["parse_task"],
        )
    result["agent_trace"] = {
        "id": trace_id,
        "agent": "任务规划Agent",
        "action": "parse_task",
        "summary": f"识别 {len(result['draft']['stops'])} 个站点，待补 {len(result['missing_fields'])} 项",
    }
    return result


@app.post("/api/tasks")
def create_task(payload: CreateTaskRequest) -> dict[str, Any]:
    return service.create_task(payload.model_dump())


@app.post("/api/tasks/{task_id}/accept")
def accept_task(task_id: str, payload: AcceptTaskRequest) -> dict[str, Any]:
    return service.accept_task(task_id, payload.model_dump())


@app.post("/api/tasks/{task_id}/notes")
def add_task_note(task_id: str, payload: TaskNoteRequest) -> dict[str, Any]:
    return service.add_task_note(task_id, payload.model_dump())


@app.get("/api/tasks")
def list_tasks(mode: str | None = None) -> dict[str, Any]:
    return {"tasks": service.list_tasks(mode)}


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str) -> dict[str, Any]:
    return service.get_bundle(task_id)


@app.post("/api/tasks/{task_id}/plan")
def plan(task_id: str, payload: PlanRequest | None = None) -> dict[str, Any]:
    return service.generate_plan(task_id, payload.start_time if payload else None)


@app.post("/api/tasks/{task_id}/actions")
def actions(task_id: str, payload: ActionRequest) -> dict[str, Any]:
    return service.perform_action(task_id, payload.model_dump())


@app.post("/api/tasks/{task_id}/replan")
def replan(task_id: str, payload: ReplanRequest) -> dict[str, Any]:
    return service.replan(task_id, payload.reason, payload.request_id, payload.plan_version)


@app.post("/api/tasks/{task_id}/sim/tick")
def sim_tick(task_id: str, payload: SimTickRequest) -> dict[str, Any]:
    return service.sim_tick(task_id, payload.seconds, payload.speed_mps)


@app.get("/api/tasks/{task_id}/report")
def report(task_id: str) -> dict[str, Any]:
    return service.get_report(task_id)


@app.get("/api/road-events")
def list_road_events() -> dict[str, Any]:
    with get_conn() as conn:
        return {"road_events": service.road_events_public(conn), "road_status": service.get_road_status(conn)}


@app.post("/api/road-events")
def create_road_event(payload: RoadEventRequest) -> dict[str, Any]:
    return service.create_road_event(payload.model_dump())


@app.post("/api/road-events/analyze")
def analyze_road_event(payload: dict[str, Any]) -> dict[str, Any]:
    text = str(payload.get("text", ""))
    edge_id = str(payload.get("edge_id", ""))
    mapdata = load_map()
    if edge_id not in mapdata.edges:
        raise service.ApiError(400, "unknown_edge", "请先在路网中选择道路")
    lowered = text.lower()
    if any(word in lowered for word in ["封", "阻断", "不能走", "禁止", "封闭"]):
        status = "blocked"
        risk = "high"
    elif any(word in lowered for word in ["堵", "拥挤", "慢", "排队", "人多"]):
        status = "congested"
        risk = "medium"
    else:
        status = None
        risk = "unknown"
    with get_conn() as conn:
        agents.record(
            conn,
            "road_observer",
            "classify_hazard",
            task_id=str(payload.get("task_id") or "") or None,
            input_summary=f"{mapdata.edges[edge_id]['name']}：{text}",
            output_summary=f"候选={status or '未识别'}；风险={risk}；需人工确认",
            tools=["classify_hazard"],
        )
    return {
        "edge_id": edge_id,
        "edge_name": mapdata.edges[edge_id]["name"],
        "candidate_status": status,
        "risk": risk,
        "requires_confirmation": True,
        "analyzer": "rule_candidate_mock",
        "notice": "当前为文字规则候选判断，需人工确认后才会修改路况；图片识别需接入视觉模型。",
    }


@app.post("/api/seed")
def seed(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    reset = bool((payload or {}).get("reset", False))
    if reset:
        with get_conn() as conn:
            for table in ("action_log", "task_events", "execution_state", "road_events", "plans", "stops", "tasks"):
                conn.execute(f"DELETE FROM {table}")
    created = []
    for text in SEED_TEXTS:
        parsed = parse_task(text, prefer_llm=False)
        draft = parsed["draft"]
        if parsed["missing_fields"] or not draft["stops"]:
            continue
        task = service.create_task(
            {
                "mode": "sim",
                "pickup_location_id": draft["pickup_location_id"],
                "stops": draft["stops"],
                "deadline": draft["deadline"],
                "deadline_kind": draft["deadline_kind"],
                "raw_input": text,
                "parser": parsed["parser"],
                "confirmed": True,
            }
        )
        task = service.generate_plan(task["task"]["id"])
        created.append({"id": task["task"]["id"], "text": text})
    return {"created": created}


@app.post("/api/tasks/{task_id}/history/reset-demo")
def reset_demo(task_id: str) -> dict[str, Any]:
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        raise service.ApiError(404, "task_not_found", "任务不存在")
    return service.get_bundle(task_id)
