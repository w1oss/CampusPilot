"""Optional upstream adapter and simulation runner, mounted in the existing app.

The bridge handles transport/lifecycle. Road interpretation and dispatch decisions
belong to the existing agents; all movement and planning use service tools.
Run one ASGI worker: SQLite transactions + operation_lock own simulation writes.
"""
import asyncio
import json
import logging
import time
import uuid
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter

from . import agents, service
from .config import (
    SEMANTIC_BRIDGE_ENABLED,
    SEMANTIC_MAX_AGE_SECONDS,
    SIM_SPEED_MPS,
    SIM_TICK_INTERVAL,
    SIM_UPSTREAM_CLEAR_SECONDS,
    SIM_UPSTREAM_DELAY_SECONDS,
    SIM_UPSTREAM_EXPECTED_SECONDS,
)
from .db import dumps, get_conn, loads, now_iso
from .mapdata import load_map
from .schemas import SemanticEventRequest, SimRuntimeRequest

router = APIRouter()
logger = logging.getLogger(__name__)

STATUS_RANK = {"open": 0, "congested": 1, "blocked": 2}
HOLD_REASON = "收到影响行驶的危险消息，已紧急停车，等待分析与放行"


def require_enabled() -> None:
    if not SEMANTIC_BRIDGE_ENABLED:
        raise service.ApiError(503, "bridge_disabled", "实时语义接入模块未启用")


def normalized_status(observation: dict[str, Any]) -> str:
    return "blocked" if observation.get("effective_status") == "unknown" else observation.get("effective_status", "unknown")


def should_stop(conn, task_id: str, observation: dict[str, Any]) -> bool:
    """Fast safety gate: only the road state matters here, not route planning."""
    if observation.get("lifecycle") == "cleared" or normalized_status(observation) == "open":
        return False
    task = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task or task["status"] != "in_progress":
        return False
    if not service.hazard_affects_route(conn, task_id, observation):
        return False
    mapdata = load_map()
    edge_id = observation["edge_id"]
    base = mapdata.edges[edge_id].get("base_status", "open")
    current = service.get_road_status(conn, task_id).get(edge_id, base)
    return STATUS_RANK.get(normalized_status(observation), 0) > STATUS_RANK.get(current, 0)


def emergency_stop(conn, task_id: str, observation: dict[str, Any]) -> bool:
    """Step 1: persist the stop before any interpretation or planning runs."""
    if not should_stop(conn, task_id, observation):
        return False
    task = conn.execute("SELECT plan_version FROM tasks WHERE id = ?", (task_id,)).fetchone()
    service.apply_dispatch_status(task_id, "paused")
    ensure_runtime(conn, task_id)
    conn.execute(
        "UPDATE sim_runtime SET emergency_hold = 1, auto_resume = 1, hold_reason = ?, waiting = 'road_blocked',"
        " updated_at = ? WHERE task_id = ?",
        (HOLD_REASON, now_iso(), task_id),
    )
    service.add_event(
        conn,
        task_id,
        "safety_stop",
        {
            "obstacle_id": observation.get("obstacle_id"),
            "edge_id": observation.get("edge_id"),
            "status": normalized_status(observation),
            "summary": observation.get("summary", ""),
            "reason": HOLD_REASON,
        },
        int(task["plan_version"]) if task and task["plan_version"] else None,
    )
    return True


def ensure_runtime(conn, task_id: str) -> None:
    conn.execute("INSERT OR IGNORE INTO sim_runtime (task_id, speed_mps, updated_at) VALUES (?, ?, ?)", (task_id, SIM_SPEED_MPS, now_iso()))


def dispatch_key(snapshot: dict[str, Any], hazards: list[dict[str, Any]]) -> str:
    return json.dumps({
        "plan": snapshot["task"]["plan_version"],
        "status": snapshot["task"]["status"],
        "hazards": sorted(((h["obstacle_id"], h["edge_id"], h["effective_status"], h.get("edge_fraction"),
                            h["category"], h["category"] == "pedestrian" and (h.get("expected_duration_s") or 0) <= 10
                            and h.get("age_seconds", 0) < 10) for h in hazards), key=repr),
    }, sort_keys=True)


def current_hazards(conn, task_id: str) -> list[dict[str, Any]]:
    hazards = service.route_hazards(conn, task_id)
    now = datetime.now(timezone.utc)
    for hazard in hazards:
        observed = hazard.get("observed_at")
        hazard["age_seconds"] = (now - datetime.fromisoformat(observed)).total_seconds() if observed else 0
    return hazards


def coordinate(conn, task_id: str) -> dict[str, Any]:
    """Pass observer state to dispatcher, then execute its decision via services."""
    ensure_runtime(conn, task_id)
    snapshot = service.get_bundle(task_id)
    runtime = service.runtime_public(conn, task_id)
    hazards = current_hazards(conn, task_id)
    key = dispatch_key(snapshot, hazards)
    stored = conn.execute("SELECT decision_key FROM sim_runtime WHERE task_id = ?", (task_id,)).fetchone()
    if stored["decision_key"] == key:
        return {"action": "unchanged", "reason": "通行影响未变化，沿用当前决策"}

    decision = agents.decide_dispatch(snapshot, hazards)
    if snapshot["task"]["status"] == "blocked" and runtime["auto_resume"] and not hazards:
        decision = {"action": "replan", "reason": "阻断信息已变化，重新验证剩余任务可达性"}
    if all(stop["status"] == "delivered" for stop in snapshot["stops"]):
        decision = {"action": "observe", "reason": "站点已全部交付，等待用户确认完成"}
    was_running = snapshot["task"]["status"] == "in_progress"
    may_resume = was_running or runtime["auto_resume"]
    action = decision["action"]
    if action == "wait":
        if was_running:
            service.apply_dispatch_status(task_id, "paused")
        conn.execute(
            "UPDATE sim_runtime SET hold_reason = ?, auto_resume = ?, emergency_hold = 1, waiting = 'road_blocked'"
            " WHERE task_id = ?",
            (decision["reason"], int(may_resume), task_id),
        )
    elif action == "replan":
        result = service.replan(task_id, decision["reason"], plan_version=snapshot["task"]["plan_version"])
        if result["plan"]["feasible"]:
            if may_resume:
                service.apply_dispatch_status(task_id, "in_progress")
            conn.execute(
                "UPDATE sim_runtime SET hold_reason = '', auto_resume = 0, emergency_hold = 0, waiting = NULL"
                " WHERE task_id = ?",
                (task_id,),
            )
        else:
            conn.execute(
                "UPDATE sim_runtime SET hold_reason = ?, auto_resume = ?, emergency_hold = 1, waiting = 'road_blocked'"
                " WHERE task_id = ?",
                ("剩余任务不可达，等待障碍解除或人工处理", int(may_resume), task_id),
            )
    elif action == "continue" and runtime["hold_reason"]:
        if runtime["auto_resume"] and snapshot["task"]["status"] == "paused":
            service.apply_dispatch_status(task_id, "in_progress")
        conn.execute(
            "UPDATE sim_runtime SET hold_reason = '', auto_resume = 0, emergency_hold = 0, waiting = NULL"
            " WHERE task_id = ?",
            (task_id,),
        )

    fresh = service.get_bundle(task_id)
    conn.execute("UPDATE sim_runtime SET decision_key = ?, updated_at = ? WHERE task_id = ?",
                 (dispatch_key(fresh, current_hazards(conn, task_id)), now_iso(), task_id))
    agents.record(conn, "dispatcher", "decide_dispatch", task_id=task_id,
                  input_summary=f"位置={snapshot['state']['position']}；计划v{snapshot['task']['plan_version']}；相关障碍={len(hazards)}",
                  output_summary=f"{action}：{decision['reason']}", tools=["replan_task"] if action == "replan" else ["update_status"])
    service.add_event(conn, task_id, "semantic_decision", decision, fresh["task"]["plan_version"])
    return {**decision, "position": snapshot["state"]["position"], "plan_version": fresh["task"]["plan_version"]}


@service.serialized
def ingest(payload: dict[str, Any]) -> dict[str, Any]:
    """Step 1 transaction: validate, stop the vehicle, then persist the hazard.

    The wire payload is retained separately from normalized timestamps, allowing
    exact retries even when observed_at was omitted by the sender.
    """
    require_enabled()
    wire = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    task_id, event_id = payload["task_id"], payload["event_id"]
    with get_conn() as conn:
        previous = conn.execute("SELECT payload, response FROM semantic_events WHERE event_id = ?", (event_id,)).fetchone()
        if previous:
            if previous["payload"] != wire:
                raise service.ApiError(409, "event_id_conflict", "同一事件编号不能用于不同内容")
            stored = loads(previous["response"], {})
            return {"task_id": task_id, "event_id": event_id, "accepted": bool(stored.get("accepted")),
                    "duplicate": True, "response": stored, "stopped": False}
        task = service.get_bundle(task_id)["task"]
        if task["mode"] != "sim":
            raise service.ApiError(409, "simulation_only", "此接入模块仅作用于指定模拟任务，不修改真实配送")
        if task["status"] in ("completed", "cancelled"):
            raise service.ApiError(409, "task_closed", "已结束的任务不再接收新的障碍事件")
        mapdata = load_map()
        if payload["map_version"] != mapdata.map_version:
            raise service.ApiError(409, "map_version_mismatch", "上游地图版本不匹配")
        if payload["edge_id"] not in mapdata.edges:
            raise service.ApiError(400, "unknown_edge", "上游引用的道路不存在")
        now = datetime.now(timezone.utc)
        observed = datetime.fromisoformat(payload["observed_at"]).astimezone(timezone.utc) if payload.get("observed_at") else now
        age = (now - observed).total_seconds()
        if age < -5:
            raise service.ApiError(400, "future_observation", "观测时间晚于接收端时钟，请校准时间")
        old = conn.execute("SELECT * FROM semantic_obstacles WHERE task_id = ? AND obstacle_id = ?",
                           (task_id, payload["obstacle_id"])).fetchone()
        stale = age > SEMANTIC_MAX_AGE_SECONDS or (old and (
            payload["revision"] <= old["revision"] or observed.isoformat() < old["observed_at"]
        ))
        accepted = not stale
        observation = agents.observe_road_semantics({**payload, "observed_at": observed.astimezone(timezone.utc).isoformat()})
        weak_clear = (payload["lifecycle"] == "cleared" or payload["passability"] == "open") and payload["confidence"] < 0.65
        weak_update = old and old["active"] and payload["confidence"] < 0.65
        if weak_clear or weak_update:
            accepted = False
        stopped = False
        if accepted:
            stopped = emergency_stop(conn, task_id, observation)
            conn.execute(
                "INSERT INTO semantic_obstacles VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(task_id, obstacle_id) DO UPDATE SET"
                " revision = excluded.revision, observed_at = excluded.observed_at, active = excluded.active, payload = excluded.payload",
                (task_id, payload["obstacle_id"], payload["revision"], observation["observed_at"], int(observation["active"]), dumps(observation)),
            )
        agents.record(conn, "road_observer", "observe_road_semantics", task_id=task_id,
                      input_summary=f"{event_id}：{payload['summary']}",
                      output_summary=f"道路={payload['edge_id']}；通行={observation['effective_status']}；{'已接纳' if accepted else '忽略旧消息或等待复核'}；结构化适配；{'已紧急停车' if stopped else '无需停车'}",
                      tools=["classify_hazard"])
        response = {"event_id": event_id, "accepted": bool(accepted), "duplicate": False, "stopped": stopped}
        conn.execute("INSERT INTO semantic_events VALUES (?, ?, ?, ?, ?)", (event_id, task_id, wire, dumps(response), now_iso()))
        service.add_event(conn, task_id, "semantic_event", {"event_id": event_id, "accepted": bool(accepted),
                                                           "stopped": stopped, "source": payload["source"]})
        return {"task_id": task_id, "event_id": event_id, "accepted": bool(accepted),
                "duplicate": False, "response": response, "stopped": stopped}


@service.serialized
def resolve(task_id: str) -> dict[str, Any]:
    """Step 2 transaction: analyze the hazard, plan, and release only if feasible."""
    with get_conn() as conn:
        return coordinate(conn, task_id)


def receive(payload: dict[str, Any]) -> dict[str, Any]:
    """Two explicit steps: stop first (committed), decide and release afterwards.

    ingest() commits the stop on its own transaction, so a slow or failed decision
    can never roll the vehicle back into motion. resolve() then analyzes and plans.
    """
    result = ingest(payload)
    task_id = result["task_id"]
    if result["accepted"]:
        decision = resolve(task_id)
    else:
        decision = {"action": "ignored", "reason": "过期/乱序消息或解除证据不足，保留当前环境状态"}
    return {**result["response"], "duplicate": bool(result.get("duplicate")), "decision": decision,
            "bundle": service.get_bundle(task_id)}


@service.serialized
def set_runtime(task_id: str, enabled: bool, speed_mps: float, auto_upstream: bool = False, upstream_category: str = "pedestrian") -> dict[str, Any]:
    require_enabled()
    with get_conn() as conn:
        task = service.get_bundle(task_id)["task"]
        if task["mode"] != "sim" or task["status"] in ("completed", "cancelled") or not task["plan_version"]:
            raise service.ApiError(409, "invalid_runtime_task", "只能控制已有计划且尚未结束的模拟任务")
        ensure_runtime(conn, task_id)
        conn.execute("UPDATE sim_runtime SET enabled = ?, speed_mps = ?, updated_at = ? WHERE task_id = ?",
                     (int(enabled), speed_mps, now_iso(), task_id))
        if not enabled:
            conn.execute("UPDATE sim_runtime SET auto_resume = 0 WHERE task_id = ?", (task_id,))
        previous = service.upstream_public(conn, task_id)
        # Changing speed or reopening the page must not restart an existing cycle.
        if auto_upstream and (not previous["enabled"] or previous["category"] != upstream_category):
            if previous["enabled"] and previous["phase"] == 1:
                raise service.ApiError(409, "upstream_cycle_active", "当前模拟障碍尚未解除，请等待本轮结束后切换场景")
            pending = conn.execute("SELECT payload, phase FROM upstream_simulations WHERE task_id = ?", (task_id,)).fetchone()
            if pending and pending["phase"] == 1:
                # Resume a stopped source without losing its outstanding obstacle.
                conn.execute("UPDATE upstream_simulations SET enabled = 1 WHERE task_id = ?", (task_id,))
            else:
                conn.execute("INSERT INTO upstream_simulations (task_id, enabled, category) VALUES (?, 1, ?)"
                             " ON CONFLICT(task_id) DO UPDATE SET enabled = 1, category = excluded.category, phase = 0, elapsed = 0, payload = '{}'",
                             (task_id, upstream_category))
        elif not auto_upstream:
            if enabled or previous["configured"]:
                conn.execute("INSERT OR IGNORE INTO upstream_simulations (task_id, category) VALUES (?, ?)", (task_id, upstream_category))
            conn.execute("UPDATE upstream_simulations SET enabled = 0 WHERE task_id = ?", (task_id,))
        return service.get_bundle(task_id)


def advance_upstream(conn, task_id: str, seconds: float) -> dict[str, Any] | None:
    """Independent timed source; returns the message to deliver after commit.

    The first hazard is published as soon as the source is enabled (delay 0), so
    the vehicle stops before any route planning runs. Clearing is category-aware:
    pedestrians clear quickly, crowds wait longer, construction stays until a
    human confirms removal. Replacing this source with a real publisher leaves
    receive() unchanged.
    """
    row = conn.execute("SELECT * FROM upstream_simulations WHERE task_id = ? AND enabled = 1", (task_id,)).fetchone()
    if not row or row["phase"] >= 2:
        return None
    snapshot = service.get_bundle(task_id)
    if snapshot["task"]["status"] in ("completed", "cancelled"):
        conn.execute("UPDATE upstream_simulations SET enabled = 0 WHERE task_id = ?", (task_id,))
        return None
    state = snapshot["state"]
    if row["phase"] == 0 and (snapshot["task"]["status"] != "in_progress" or not state["current_edge_id"]):
        return None
    elapsed = row["elapsed"] + seconds
    if row["phase"] == 0:
        if elapsed < SIM_UPSTREAM_DELAY_SECONDS:
            conn.execute("UPDATE upstream_simulations SET elapsed = ? WHERE task_id = ?", (elapsed, task_id))
            return None
        edge = load_map().edges[state["current_edge_id"]]
        progress = float(state["edge_progress"] or 0)
        fraction = progress + (1 - progress) * 0.7
        if state["last_node_id"] == edge["to"]:
            fraction = 1 - fraction
        category = row["category"]
        summaries = {"pedestrian": "上游自动发现前方行人横穿", "construction": "上游自动发现前方施工围挡", "crowd": "上游自动发现前方人群拥挤"}
        payload = SemanticEventRequest(
            event_id=f"auto_{uuid.uuid4().hex}", task_id=task_id, obstacle_id=f"auto_{uuid.uuid4().hex}", revision=1,
            map_version=snapshot["map_version"], edge_id=edge["id"], edge_fraction=fraction,
            category=category, passability="congested" if category == "crowd" else "blocked",
            confidence=1, expected_duration_s=SIM_UPSTREAM_EXPECTED_SECONDS.get(category, 1800.0),
            summary=summaries[category], observed_at=datetime.now(timezone.utc),
        ).model_dump(mode="json")
    else:
        clear_after = SIM_UPSTREAM_CLEAR_SECONDS.get(row["category"], 0.0)
        if clear_after <= 0 or elapsed < clear_after:
            conn.execute("UPDATE upstream_simulations SET elapsed = ? WHERE task_id = ?", (elapsed, task_id))
            return None
        payload = loads(row["payload"], {})
        payload.update(event_id=f"auto_{uuid.uuid4().hex}", revision=int(payload.get("revision", 1)) + 1,
                       lifecycle="cleared", passability="open", observed_at=now_iso(),
                       summary="模拟上游复查确认障碍已解除")
    conn.execute("UPDATE upstream_simulations SET phase = ?, elapsed = 0, payload = ? WHERE task_id = ?",
                 (row["phase"] + 1, dumps(payload), task_id))
    return payload


@service.serialized
def mark_upstream_cleared(task_id: str) -> dict[str, Any]:
    """Human-confirmed clear for long-lived hazards such as construction."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM upstream_simulations WHERE task_id = ?", (task_id,)).fetchone()
        if not row or row["phase"] != 1:
            raise service.ApiError(409, "no_upstream_obstacle", "当前没有等待解除的模拟障碍")
        payload = loads(row["payload"], {})
        payload.update(
            event_id=f"auto_{uuid.uuid4().hex}",
            revision=int(payload.get("revision", 1)) + 1,
            lifecycle="cleared",
            passability="open",
            observed_at=now_iso(),
            summary="人工确认模拟障碍已解除",
        )
        conn.execute("UPDATE upstream_simulations SET phase = 2, elapsed = 0, payload = ? WHERE task_id = ?",
                     (dumps(payload), task_id))
    return payload


def clear_upstream(task_id: str) -> dict[str, Any]:
    """Commit the clear message first, then run the regular two-step decision."""
    payload = mark_upstream_cleared(task_id)
    return receive(payload)


@service.serialized
def trigger_upstream(seconds: float) -> list[dict[str, Any]]:
    """Commit the source timing first, then deliver messages in their own transactions."""
    pending: list[dict[str, Any]] = []
    with get_conn() as conn:
        rows = conn.execute("SELECT task_id FROM sim_runtime WHERE enabled = 1 UNION "
                            "SELECT task_id FROM upstream_simulations WHERE enabled = 1 AND phase < 2").fetchall()
        for row in rows:
            task_id = row["task_id"]
            task = service.get_bundle(task_id)["task"]
            if task["status"] in ("completed", "cancelled"):
                conn.execute("UPDATE sim_runtime SET enabled = 0 WHERE task_id = ?", (task_id,))
                conn.execute("UPDATE upstream_simulations SET enabled = 0 WHERE task_id = ?", (task_id,))
                continue
            payload = advance_upstream(conn, task_id, seconds)
            if payload:
                pending.append(payload)
    return pending


@service.serialized
def move_runtime(seconds: float) -> None:
    with get_conn() as conn:
        rows = conn.execute("SELECT task_id FROM sim_runtime WHERE enabled = 1 UNION "
                            "SELECT task_id FROM upstream_simulations WHERE enabled = 1 AND phase < 2").fetchall()
        for row in rows:
            task_id = row["task_id"]
            task = service.get_bundle(task_id)["task"]
            if task["status"] in ("completed", "cancelled"):
                conn.execute("UPDATE sim_runtime SET enabled = 0 WHERE task_id = ?", (task_id,))
                conn.execute("UPDATE upstream_simulations SET enabled = 0 WHERE task_id = ?", (task_id,))
                continue
            coordinate(conn, task_id)
            if service.get_bundle(task_id)["task"]["status"] != "in_progress":
                continue
            runtime = service.runtime_public(conn, task_id)
            if runtime["enabled"]:
                service.sim_tick(task_id, seconds, runtime["speed_mps"], runtime=True)


def step_runtime(seconds: float) -> None:
    """One bounded real-time step: stop first, decide next, move only if released."""
    if not SEMANTIC_BRIDGE_ENABLED:
        return
    for payload in trigger_upstream(seconds):
        try:
            receive(payload)
        except Exception:
            logger.exception("Vehicle remains stopped: upstream decision failed")
    move_runtime(seconds)


async def run_runtime() -> None:
    last = time.monotonic()
    while True:
        await asyncio.sleep(SIM_TICK_INTERVAL)
        now = time.monotonic()
        elapsed, last = min(now - last, SIM_TICK_INTERVAL * 3), now
        try:
            await asyncio.to_thread(step_runtime, elapsed)
        except Exception:
            logger.exception("Background simulation step failed; no movement was committed")


async def stop_runtime(task: asyncio.Task) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


@router.post("/api/semantic-events")
def semantic_event(payload: SemanticEventRequest) -> dict[str, Any]:
    return receive(payload.model_dump(mode="json"))


@router.post("/api/tasks/{task_id}/sim/runtime")
def runtime_control(task_id: str, payload: SimRuntimeRequest) -> dict[str, Any]:
    return set_runtime(task_id, payload.enabled, payload.speed_mps, payload.auto_upstream, payload.upstream_category)

@router.post("/api/tasks/{task_id}/sim/upstream/clear")
def upstream_clear(task_id: str) -> dict[str, Any]:
    return clear_upstream(task_id)
