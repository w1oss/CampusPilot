import uuid
from typing import Any

from .db import dumps, loads, now_iso
from .simulator import is_local_box

AGENTS: dict[str, dict[str, Any]] = {
    "task_planner": {
        "name": "任务规划Agent",
        "tools": ["create_task", "validate_task", "plan_stops"],
    },
    "road_observer": {
        "name": "道路观察Agent",
        "tools": ["analyze_image", "classify_hazard"],
    },
    "dispatcher": {
        "name": "调度复盘Agent",
        "tools": ["replan_task", "update_status", "export_report"],
    },
}


def observe_road_semantics(event: dict[str, Any]) -> dict[str, Any]:
    """Road observer: ground structured upstream evidence, without inventing facts.

    This deterministic adapter is explicitly labelled; it does not pretend to call
    another LLM after the upstream vision/LLM has already produced semantics.
    """
    trusted = event["confidence"] >= 0.65
    status = event["passability"] if trusted else "unknown"
    return {
        **event,
        "effective_status": status,
        "active": event["lifecycle"] != "cleared" and status != "open",
        "risk": "high" if status == "blocked" else "medium" if status == "congested" else "unknown",
        "analyzer": "structured_semantic_adapter",
    }


def decide_dispatch(snapshot: dict[str, Any], hazards: list[dict[str, Any]]) -> dict[str, str]:
    """Dispatcher chooses actions; graph search and state transitions stay in tools."""
    if snapshot["task"]["status"] in ("ready", "completed", "cancelled"):
        return {"action": "observe", "reason": "已记录环境信息，任务尚未执行或已经结束"}
    if any(h["effective_status"] == "unknown" for h in hazards):
        return {"action": "wait", "reason": "当前路线存在尚未确认的障碍，等待上游复查"}
    blocked = [h for h in hazards if h["effective_status"] == "blocked" and not is_local_box(h)]
    if any(h["category"] == "pedestrian" and (h.get("expected_duration_s") or 0) <= 180 and h.get("age_seconds", 0) < 10 for h in blocked):
        return {"action": "wait", "reason": "前方行人横穿，暂停并等待解除消息"}
    if blocked:
        return {"action": "replan", "reason": "剩余路线受阻，调用规划工具从当前位置绕行"}
    if any(h["effective_status"] == "congested" for h in hazards):
        return {"action": "replan", "reason": "剩余路线拥堵，重新比较配送路线和时间成本"}
    if any(is_local_box(h) for h in hazards):
        return {"action": "local_avoid", "reason": "前方小纸箱，在原路段内用小弧线避让，保持配送路线"}
    return {"action": "continue", "reason": "当前剩余路线没有通行障碍"}


def summarize(text: str, limit: int = 120) -> str:
    value = " ".join(str(text).split())
    if not value:
        return ""
    return value if len(value) <= limit else value[: limit - 1] + "…"


def record(
    conn,
    agent_key: str,
    action: str,
    *,
    task_id: str | None = None,
    input_summary: str = "",
    output_summary: str = "",
    tools: list[str] | None = None,
    status: str = "success",
    duration_ms: int = 0,
) -> str:
    agent = AGENTS[agent_key]
    trace_id = f"trace_{uuid.uuid4().hex[:12]}"
    conn.execute(
        "INSERT INTO agent_traces (id, task_id, agent, agent_name, action, input_summary, output_summary, tools,"
        " status, duration_ms, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            trace_id,
            task_id,
            agent_key,
            agent["name"],
            action,
            summarize(input_summary),
            summarize(output_summary, 160),
            dumps(tools if tools is not None else agent["tools"]),
            status,
            int(duration_ms),
            now_iso(),
        ),
    )
    return trace_id


def _public(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "task_id": row["task_id"],
        "agent": row["agent"],
        "agent_name": row["agent_name"],
        "action": row["action"],
        "input_summary": row["input_summary"],
        "output_summary": row["output_summary"],
        "tools": loads(row["tools"], []),
        "status": row["status"],
        "duration_ms": int(row["duration_ms"]),
        "created_at": row["created_at"],
    }


def list_for_task(conn, task_id: str, limit: int = 80) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM agent_traces WHERE task_id = ? ORDER BY rowid ASC LIMIT ?",
        (task_id, limit),
    ).fetchall()
    return [_public(row) for row in rows]


def summary(conn, task_id: str) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT agent_name, status, COUNT(*) AS calls, COALESCE(SUM(duration_ms), 0) AS elapsed"
        " FROM agent_traces WHERE task_id = ? GROUP BY agent_name, status",
        (task_id,),
    ).fetchall()
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = grouped.setdefault(
            row["agent_name"],
            {"agent_name": row["agent_name"], "calls": 0, "failed": 0, "duration_ms": 0},
        )
        calls = int(row["calls"])
        item["calls"] += calls
        item["duration_ms"] += int(row["elapsed"])
        if row["status"] != "success":
            item["failed"] += calls
    agents = list(grouped.values())
    return {
        "total_calls": sum(item["calls"] for item in agents),
        "failed_calls": sum(item["failed"] for item in agents),
        "agents": agents,
    }
