import uuid
from functools import wraps
from datetime import datetime, timezone
from time import perf_counter
from typing import Any

from . import agents, scene_sim
from .config import SEMANTIC_BRIDGE_ENABLED, SIM_SPEED_MPS
from .db import dumps, get_conn, loads, now_iso, operation_lock
from .mapdata import load_map, node_name
from .planner import build_plan, plan_from_position
from .report import build_report
from .simulator import advance, obstacle_ahead, is_local_box, local_avoidance


def serialized(function):
    """Serialize state changes and make nested service operations atomic."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        with operation_lock, get_conn() as conn:
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            return function(*args, **kwargs)
    return wrapped


def consistent_snapshot(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with operation_lock, get_conn() as conn:
            if not conn.in_transaction:
                conn.execute("BEGIN")
            return function(*args, **kwargs)
    return wrapped


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _task_row(conn, task_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise ApiError(404, "task_not_found", f"任务 {task_id} 不存在")
    return dict(row)


def _stop_rows(conn, task_id: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM stops WHERE task_id = ? ORDER BY seq", (task_id,)).fetchall()
    return [dict(row) for row in rows]


def _plan_rows(conn, task_id: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM plans WHERE task_id = ? ORDER BY plan_version DESC", (task_id,)).fetchall()
    return [dict(row) for row in rows]


def _state_row(conn, task_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM execution_state WHERE task_id = ?", (task_id,)).fetchone()
    return dict(row) if row else None


def _timeline(conn, task_id: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM task_events WHERE task_id = ? ORDER BY seq", (task_id,)).fetchall()
    return [
        {
            "seq": row["seq"],
            "type": row["type"],
            "payload": loads(row["payload"], {}),
            "plan_version": row["plan_version"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def add_event(conn, task_id: str, event_type: str, payload: dict[str, Any] | None = None, plan_version: int | None = None) -> None:
    conn.execute(
        "INSERT INTO task_events (task_id, type, payload, plan_version, created_at) VALUES (?, ?, ?, ?, ?)",
        (task_id, event_type, dumps(payload or {}), plan_version, now_iso()),
    )


SCENE_SIM_DEFAULT = "sim_default"
SCENE_REAL_DEFAULT = "real_default"
STATUS_RANK = {"open": 0, "congested": 1, "blocked": 2}


def task_scene(conn, task_id: str) -> str:
    row = conn.execute("SELECT scene_id, mode FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        return SCENE_SIM_DEFAULT
    return row["scene_id"] or (SCENE_REAL_DEFAULT if row["mode"] == "real" else SCENE_SIM_DEFAULT)


def task_mode(conn, task_id: str) -> str:
    row = conn.execute("SELECT mode FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return row["mode"] if row else "sim"


def scene_public(conn, scene_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM scenes WHERE id = ?", (scene_id,)).fetchone()
    if not row:
        return {"id": scene_id, "kind": "sim", "virtual_seconds": 0.0}
    return {"id": row["id"], "kind": row["kind"], "virtual_seconds": round(float(row["virtual_seconds"]), 1)}


def known_obstacles(conn, scene_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM road_known WHERE scene_id = ? AND active = 1 ORDER BY observed_at, rowid",
        (scene_id,),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        payload = loads(row["payload"], {})
        payload.update(
            {
                "obstacle_id": row["obstacle_id"],
                "edge_id": row["edge_id"],
                "edge_fraction": row["edge_fraction"],
                "category": row["category"],
                "effective_status": row["status"],
                "active": bool(row["active"]),
                "source": row["source"],
                "authority": row["authority"],
                "revision": row["revision"],
                "observed_at": row["observed_at"],
                "position_known": row["edge_fraction"] is not None,
                "clear_evidence": row["clear_evidence"],
            }
        )
        payload["handling"] = "local_avoidance" if is_local_box(payload) else "road_status"
        result.append(payload)
    return result


def upsert_known(conn, scene_id: str, observation: dict[str, Any], *, source: str, authority: str = "normal") -> None:
    status = "blocked" if observation.get("effective_status") == "unknown" else observation.get("effective_status", "unknown")
    active = 1 if observation.get("lifecycle") != "cleared" and status != "open" else 0
    stamp = observation.get("observed_at") or now_iso()
    payload = dumps({**observation, "effective_status": status, "active": bool(active)})
    conn.execute(
        "INSERT INTO road_known (scene_id, obstacle_id, edge_id, edge_fraction, category, status, active, source,"
        " authority, revision, observed_at, payload, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(scene_id, obstacle_id) DO UPDATE SET edge_id = excluded.edge_id,"
        " edge_fraction = excluded.edge_fraction, category = excluded.category, status = excluded.status,"
        " active = excluded.active, source = excluded.source, authority = excluded.authority,"
        " revision = excluded.revision, observed_at = excluded.observed_at, payload = excluded.payload,"
        " updated_at = excluded.updated_at",
        (
            scene_id,
            observation["obstacle_id"],
            observation["edge_id"],
            observation.get("edge_fraction"),
            observation.get("category", "other"),
            status,
            active,
            source,
            authority,
            int(observation.get("revision", 1)),
            stamp,
            payload,
            now_iso(),
        ),
    )


def clear_admin_known(conn, scene_id: str, edge_id: str) -> None:
    conn.execute(
        "UPDATE road_known SET active = 0, status = 'open', revision = revision + 1, updated_at = ?"
        " WHERE scene_id = ? AND edge_id = ? AND source = 'admin' AND authority = 'normal' AND active = 1",
        (now_iso(), scene_id, edge_id),
    )


def active_obstacles(conn, task_id: str) -> list[dict[str, Any]]:
    if not SEMANTIC_BRIDGE_ENABLED:
        return []
    return known_obstacles(conn, task_scene(conn, task_id))


def runtime_public(conn, task_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM sim_runtime WHERE task_id = ?", (task_id,)).fetchone()
    if not row:
        return {"enabled": False, "speed_mps": SIM_SPEED_MPS, "hold_reason": "", "auto_resume": False,
                "emergency_hold": False, "waiting": None}
    return {**dict(row), "enabled": bool(row["enabled"]) and SEMANTIC_BRIDGE_ENABLED,
            "auto_resume": bool(row["auto_resume"]), "emergency_hold": bool(row["emergency_hold"])}


def upstream_public(conn, task_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM upstream_simulations WHERE task_id = ?", (task_id,)).fetchone()
    if not row:
        return {"enabled": False, "configured": False, "category": "pedestrian", "phase": 0, "elapsed": 0,
                "last_edge_id": None, "next_generate_at": 0.0, "generated_count": 0}
    return {"enabled": bool(row["enabled"]) and SEMANTIC_BRIDGE_ENABLED, "configured": True,
            "category": row["category"], "phase": row["phase"], "elapsed": row["elapsed"],
            "last_edge_id": row["last_edge_id"], "next_generate_at": row["next_generate_at"],
            "generated_count": row["generated_count"]}


def remaining_route_edges(conn, task_id: str) -> tuple[dict[str, Any], list[str]] | None:
    """Read-only view of the route the vehicle still has to drive."""
    state = _state_row(conn, task_id)
    if not state:
        return None
    remaining = loads(state["remaining_edge_ids"], [])
    current = state.get("current_edge_id")
    if current and (not remaining or remaining[0] != current):
        remaining.insert(0, current)
    return state, remaining


def hazard_affects_route(conn, task_id: str, obstacle: dict[str, Any]) -> bool:
    """Whether an observed hazard can still affect forward motion."""
    context = remaining_route_edges(conn, task_id)
    if context is None:
        return False
    state, remaining = context
    edge_id = obstacle["edge_id"]
    if edge_id not in remaining:
        return False
    if edge_id == state.get("current_edge_id") and edge_id not in remaining[1:]:
        return obstacle_ahead(obstacle, load_map().edges[edge_id], state["last_node_id"], state["edge_progress"])
    return True


def baseline_road_status(conn, task_id: str) -> dict[str, str]:
    """Whole-road closures only: hard admin blocks for sim, all admin events for real."""
    mode = task_mode(conn, task_id)
    scene_id = task_scene(conn, task_id)
    rows = conn.execute(
        "SELECT edge_id, status, authority, expires_at FROM road_events WHERE scene_id = ? ORDER BY created_at, rowid",
        (scene_id,),
    ).fetchall()
    now = now_iso()
    baseline: dict[str, str] = {}
    for row in rows:
        if row["expires_at"] and row["expires_at"] < now:
            continue
        if mode == "sim" and row["authority"] != "hard":
            continue
        if STATUS_RANK.get(row["status"], 0) >= STATUS_RANK.get(baseline.get(row["edge_id"], "open"), 0):
            baseline[row["edge_id"]] = row["status"]
    return baseline


def route_hazards(conn, task_id: str) -> list[dict[str, Any]]:
    context = remaining_route_edges(conn, task_id)
    if context is None:
        return []
    _state, remaining = context
    mapdata = load_map()
    result = [obstacle for obstacle in active_obstacles(conn, task_id) if hazard_affects_route(conn, task_id, obstacle)]
    baseline = baseline_road_status(conn, task_id)
    for edge_id in set(remaining):
        status = baseline.get(edge_id, mapdata.edges[edge_id].get("base_status", "open"))
        if status != "open":
            result.append({"obstacle_id": f"manual:{edge_id}", "edge_id": edge_id, "edge_fraction": None,
                           "effective_status": status, "category": "other", "expected_duration_s": None})
    return result


def get_road_status(conn, task_id: str | None = None, scene_id: str | None = None) -> dict[str, str]:
    target_scene = scene_id or (task_scene(conn, task_id) if task_id else SCENE_SIM_DEFAULT)
    mode = task_mode(conn, task_id) if task_id else "sim"
    rows = conn.execute(
        "SELECT edge_id, status, authority, expires_at FROM road_events WHERE scene_id = ? ORDER BY created_at, rowid",
        (target_scene,),
    ).fetchall()
    now = now_iso()
    status: dict[str, str] = {}
    for row in rows:
        if row["expires_at"] and row["expires_at"] < now:
            continue
        if mode == "sim" and row["authority"] != "hard":
            continue
        if STATUS_RANK.get(row["status"], 0) >= STATUS_RANK.get(status.get(row["edge_id"], "open"), 0):
            status[row["edge_id"]] = row["status"]
    if mode != "real":
        for obstacle in known_obstacles(conn, target_scene):
            if is_local_box(obstacle):
                continue
            value = obstacle["effective_status"]
            edge_id = obstacle["edge_id"]
            if STATUS_RANK.get(value, 0) > STATUS_RANK.get(status.get(edge_id, "open"), 0):
                status[edge_id] = value
    return status


def road_events_public(conn) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM road_events ORDER BY created_at").fetchall()
    return [dict(row) for row in rows]


def road_marks(conn, task_id: str) -> dict[str, dict[str, Any]]:
    """Map view: status decides colour, sources decide the small label."""
    scene_id = task_scene(conn, task_id)
    marks: dict[str, dict[str, Any]] = {}
    for obstacle in known_obstacles(conn, scene_id):
        if is_local_box(obstacle):
            continue
        edge_id = obstacle["edge_id"]
        item = marks.setdefault(
            edge_id,
            {"status": "open", "sources": [], "authority": "normal", "position_known": False},
        )
        source = "machine" if obstacle.get("source") in ("mock_vision_llm", "vision_llm") else "admin"
        if source not in item["sources"]:
            item["sources"].append(source)
        value = obstacle["effective_status"]
        if STATUS_RANK.get(value, 0) > STATUS_RANK.get(item["status"], 0):
            item["status"] = value
        if obstacle.get("position_known") or obstacle.get("edge_fraction") is not None:
            item["position_known"] = True
    rows = conn.execute(
        "SELECT edge_id, edge_fraction, status, expires_at FROM road_events"
        " WHERE scene_id = ? AND authority = 'hard' ORDER BY created_at, rowid",
        (scene_id,),
    ).fetchall()
    now = now_iso()
    latest: dict[str, Any] = {}
    for row in rows:
        if row["expires_at"] and row["expires_at"] < now:
            continue
        latest[row["edge_id"]] = row
    for edge_id, row in latest.items():
        if row["status"] == "open":
            continue
        item = marks.setdefault(
            edge_id,
            {"status": "open", "sources": [], "authority": "normal", "position_known": False},
        )
        item["authority"] = "hard"
        if "admin" not in item["sources"]:
            item["sources"].append("admin")
        if row["edge_fraction"] is not None:
            item["position_known"] = True
        if STATUS_RANK.get(row["status"], 0) > STATUS_RANK.get(item["status"], 0):
            item["status"] = row["status"]
    return marks


def _refresh_road_snapshot(conn) -> dict[str, str]:
    return get_road_status(conn)


@serialized
def create_road_event(payload: dict[str, Any]) -> dict[str, Any]:
    mapdata = load_map()
    if payload["edge_id"] not in mapdata.edges:
        raise ApiError(400, "unknown_edge", f"道路 {payload['edge_id']} 不在路网中")
    scene_id = payload.get("scene_id") or SCENE_SIM_DEFAULT
    authority = payload.get("authority", "normal")
    if authority not in ("normal", "hard"):
        raise ApiError(400, "invalid_authority", "authority 必须是 normal 或 hard")
    event_id = _new_id("road")
    stamp = now_iso()
    with get_conn() as conn:
        if payload["status"] == "open":
            conn.execute("DELETE FROM road_events WHERE edge_id = ? AND status != 'open'", (payload["edge_id"],))
        else:
            conn.execute("DELETE FROM road_events WHERE edge_id = ? AND status = 'open'", (payload["edge_id"],))
        conn.execute(
            "INSERT INTO road_events (id, scene_id, edge_id, edge_fraction, status, source, authority, confirmed_by, note, created_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                scene_id,
                payload["edge_id"],
                payload.get("edge_fraction"),
                payload["status"],
                payload.get("source", "user_text"),
                authority,
                payload.get("confirmed_by", "user"),
                payload.get("note", ""),
                stamp,
                payload.get("expires_at"),
            ),
        )
        if authority == "normal":
            obstacle_id = f"admin:{payload['edge_id']}"
            if payload["status"] == "open":
                clear_admin_known(conn, scene_id, payload["edge_id"])
                conn.execute(
                    "UPDATE obstacle_truth SET present = 0, updated_at = ? WHERE scene_id = ? AND obstacle_id = ?",
                    (stamp, scene_id, obstacle_id),
                )
            else:
                previous = conn.execute(
                    "SELECT revision FROM road_known WHERE scene_id = ? AND obstacle_id = ?",
                    (scene_id, obstacle_id),
                ).fetchone()
                upsert_known(
                    conn,
                    scene_id,
                    {
                        "obstacle_id": obstacle_id,
                        "edge_id": payload["edge_id"],
                        "edge_fraction": payload.get("edge_fraction"),
                        "category": "other",
                        "effective_status": payload["status"],
                        "lifecycle": "observed",
                        "source": "admin",
                        "observed_at": stamp,
                        "revision": int(previous["revision"]) + 1 if previous else 1,
                        "summary": payload.get("note") or (
                            "管理员上报道路异常（位置未知，整段受影响）"
                            if payload.get("edge_fraction") is None
                            else "管理员上报道路异常（已选位置）"
                        ),
                        "position_known": payload.get("edge_fraction") is not None,
                        "active": True,
                    },
                    source="admin",
                    authority="normal",
                )
                scene_sim.set_admin_truth(conn, scene_id, obstacle_id, payload["edge_id"], payload.get("edge_fraction"), stamp)
                conn.execute(
                    "UPDATE road_known SET clear_evidence = 0 WHERE scene_id = ? AND obstacle_id = ?",
                    (scene_id, obstacle_id),
                )
        affected = [
            dict(row)
            for row in conn.execute(
                "SELECT id, status FROM tasks WHERE scene_id = ? AND status IN ('ready','in_progress','paused','blocked')",
                (scene_id,),
            ).fetchall()
        ]
        for task in affected:
            event_type = "road_restored" if payload["status"] == "open" else "road_event"
            add_event(
                conn,
                task["id"],
event_type,
                {"edge_id": payload["edge_id"], "status": payload["status"], "event_id": event_id,
                 "authority": authority},
            )
        event = dict(conn.execute("SELECT * FROM road_events WHERE id = ?", (event_id,)).fetchone())
    with get_conn() as conn:
        event["road_status"] = get_road_status(conn, scene_id=scene_id)
    return event


def mark_known_cleared_by_machine(conn, scene_id: str, obstacle_id: str, observation: dict[str, Any]) -> None:
    """Keep the admin source, but record that a machine recheck cleared it."""
    row = conn.execute(
        "SELECT * FROM road_known WHERE scene_id = ? AND obstacle_id = ?", (scene_id, obstacle_id)
    ).fetchone()
    if not row:
        return
    payload = loads(row["payload"], {})
    payload.update(
        {
            "active": False,
            "effective_status": "open",
            "lifecycle": "cleared",
            "cleared_by": "machine",
            "cleared_at": observation.get("observed_at") or now_iso(),
        }
    )
    conn.execute(
        "UPDATE road_known SET active = 0, status = 'open', clear_evidence = 2, revision = ?,"
        " observed_at = ?, payload = ?, updated_at = ? WHERE scene_id = ? AND obstacle_id = ?",
        (
            max(int(row["revision"]) + 1, int(observation.get("revision", 1))),
            observation.get("observed_at") or now_iso(),
            dumps(payload),
            now_iso(),
            scene_id,
            obstacle_id,
        ),
    )


@serialized
def script_clear_truth(scene_id: str, obstacle_id: str) -> dict[str, Any]:
    """Demo script: change only the scene's actual state, never the known road status."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM obstacle_truth WHERE scene_id = ? AND obstacle_id = ?", (scene_id, obstacle_id)
        ).fetchone()
        if not row:
            raise ApiError(404, "unknown_obstacle", "模拟现场没有这个障碍")
        conn.execute(
            "UPDATE obstacle_truth SET present = 0, updated_at = ? WHERE scene_id = ? AND obstacle_id = ?",
            (now_iso(), scene_id, obstacle_id),
        )
        known = conn.execute(
            "SELECT status, active FROM road_known WHERE scene_id = ? AND obstacle_id = ?",
            (scene_id, obstacle_id),
        ).fetchone()
        return {
            "scene_id": scene_id,
            "obstacle_id": obstacle_id,
            "present": 0,
            "known_status": known["status"] if known else None,
            "known_active": bool(known["active"]) if known else False,
            "notice": "现场已恢复，等待车辆经过并在观察范围内连续两次确认",
        }


def list_tasks(mode: str | None = None) -> list[dict[str, Any]]:
    mapdata = load_map()
    with get_conn() as conn:
        if mode:
            rows = conn.execute("SELECT * FROM tasks WHERE mode = ? ORDER BY created_at DESC", (mode,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM tasks ORDER BY created_at DESC").fetchall()
        result = []
        for row in rows:
            stops = _stop_rows(conn, row["id"])
            assignment = conn.execute(
                "SELECT assignee, accepted_at FROM task_assignments WHERE task_id = ?", (row["id"],)
            ).fetchone()
            result.append(
                {
                    "id": row["id"],
                    "mode": row["mode"],
                    "status": row["status"],
                    "pickup_location_name": node_name(mapdata, row["pickup_location_id"]),
                    "deadline": row["deadline"],
                    "plan_version": row["plan_version"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "total_stops": len(stops),
                    "delivered_stops": sum(1 for s in stops if s["status"] == "delivered"),
                    "assignee": assignment["assignee"] if assignment else None,
                    "accepted_at": assignment["accepted_at"] if assignment else None,
                }
            )
        return result


@serialized
def accept_task(task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    assignee = str(payload.get("assignee") or "").strip()
    if not assignee:
        raise ApiError(400, "missing_assignee", "请填写接单人")
    note = str(payload.get("note") or "").strip()
    with get_conn() as conn:
        task = _task_row(conn, task_id)
        if task["status"] in ("completed", "cancelled"):
            raise ApiError(409, "task_closed", "已结束的任务不能接单")
        stamp = now_iso()
        conn.execute(
            "INSERT INTO task_assignments (task_id, assignee, note, accepted_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(task_id) DO UPDATE SET assignee = excluded.assignee, note = excluded.note,"
            " accepted_at = excluded.accepted_at",
            (task_id, assignee, note, stamp),
        )
        add_event(conn, task_id, "admin_accepted", {"assignee": assignee}, task["plan_version"])
        agents.record(
            conn,
            "dispatcher",
            "accept_task",
            task_id=task_id,
            input_summary=f"接单人={assignee}" + (f"；备注={note}" if note else ""),
            output_summary="任务已分配给管理员处理",
            tools=["update_status"],
        )
    return get_bundle(task_id)


@serialized
def add_task_note(task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    content = str(payload.get("content") or "").strip()
    if not content:
        raise ApiError(400, "empty_note", "备注内容不能为空")
    author = str(payload.get("author") or "管理员").strip() or "管理员"
    with get_conn() as conn:
        task = _task_row(conn, task_id)
        conn.execute(
            "INSERT INTO task_notes (id, task_id, author, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (_new_id("note"), task_id, author, content, now_iso()),
        )
        add_event(conn, task_id, "admin_note", {"author": author}, task["plan_version"])
        agents.record(
            conn,
            "dispatcher",
            "note_update",
            task_id=task_id,
            input_summary=f"作者={author}",
            output_summary=content,
            tools=["update_status"],
        )
    return get_bundle(task_id)


@serialized
def create_task(payload: dict[str, Any]) -> dict[str, Any]:
    started = perf_counter()
    mapdata = load_map()
    if not payload.get("confirmed", True):
        raise ApiError(400, "not_confirmed", "用户未确认，不能创建任务")
    pickup = payload["pickup_location_id"]
    if not mapdata.is_location(pickup):
        raise ApiError(400, "invalid_pickup", "取货点必须是白名单内的校园地点")
    stops = payload.get("stops") or []
    if not stops:
        raise ApiError(400, "no_stops", "至少需要一个配送站点")
    if len(stops) > 5:
        raise ApiError(400, "too_many_stops", "第一版最多支持 5 个配送站点")
    seen: set[str] = set()
    for stop in stops:
        location_id = stop["location_id"]
        if not mapdata.is_location(location_id):
            raise ApiError(400, "invalid_stop", f"地点 {location_id} 不在白名单中")
        if location_id == pickup:
            raise ApiError(400, "duplicate_location", "配送站点不能与取货点相同")
        if location_id in seen:
            raise ApiError(400, "duplicate_stop", "同一地点不能重复添加")
        if not stop.get("items"):
            raise ApiError(400, "empty_items", f"站点 {node_name(mapdata, location_id)} 未分配物品")
        seen.add(location_id)
    mode = payload.get("mode", "sim")
    if mode not in ("real", "sim"):
        raise ApiError(400, "invalid_mode", "模式必须是 real 或 sim")
    scene_id = payload.get("scene_id") or (SCENE_REAL_DEFAULT if mode == "real" else SCENE_SIM_DEFAULT)
    deadline = payload.get("deadline")
    deadline_kind = payload.get("deadline_kind", "none")
    if deadline and deadline_kind == "clock" and not _valid_clock(deadline):
        raise ApiError(400, "invalid_deadline", "截止时间格式应为 HH:MM")
    task_id = _new_id("task")
    stamp = now_iso()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO tasks (id, mode, scene_id, raw_input, parser, pickup_location_id, deadline, deadline_kind, status,"
            " plan_version, pickup_status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                mode,
                scene_id,
                payload.get("raw_input", ""),
                payload.get("parser", "manual"),
                pickup,
                deadline,
                deadline_kind,
                "ready",
                0,
                "pending",
                stamp,
                stamp,
            ),
        )
        for seq, stop in enumerate(stops, start=1):
            conn.execute(
                "INSERT INTO stops (id, task_id, seq, location_id, items, priority, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    _new_id("stop"),
                    task_id,
                    seq,
                    stop["location_id"],
                    dumps(stop["items"]),
                    stop.get("priority", "normal"),
                    "pending",
                ),
            )
        node = mapdata.nodes[pickup]
        conn.execute(
            "INSERT INTO execution_state (task_id, mode, plan_version, current_edge_id, edge_progress, last_node_id,"
            " position_x, position_y, remaining_node_ids, remaining_edge_ids, dwell_remaining, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, mode, 0, None, 0.0, pickup, node["x"], node["y"], "[]", "[]", 0.0, stamp),
        )
        add_event(conn, task_id, "task_created", {"mode": mode, "pickup": pickup, "stops": len(stops)})
        agents.record(
            conn,
            "task_planner",
            "create_task",
            task_id=task_id,
            input_summary=payload.get("raw_input") or "手工创建任务",
            output_summary=(
                f"取货={node_name(mapdata, pickup)}；配送站点={len(stops)}；"
                f"截止={deadline or '无'}；解析={payload.get('parser', 'manual')}"
            ),
            tools=["create_task", "validate_task"],
            duration_ms=int((perf_counter() - started) * 1000),
        )
    return get_bundle(task_id)


def _valid_clock(value: str) -> bool:
    try:
        hour, minute = [int(part) for part in value.split(":")]
    except (ValueError, AttributeError):
        return False
    return 0 <= hour <= 23 and 0 <= minute <= 59


@consistent_snapshot
def get_bundle(task_id: str) -> dict[str, Any]:
    mapdata = load_map()
    with get_conn() as conn:
        task = _task_row(conn, task_id)
        stops = _stop_rows(conn, task_id)
        plans = _plan_rows(conn, task_id)
        state = _state_row(conn, task_id)
        road_status = get_road_status(conn, task_id)
        assignment = conn.execute("SELECT * FROM task_assignments WHERE task_id = ?", (task_id,)).fetchone()
        notes = conn.execute("SELECT * FROM task_notes WHERE task_id = ? ORDER BY rowid", (task_id,)).fetchall()
        bundle = {
            "task": _task_public(task, mapdata),
            "stops": [_stop_public(stop, mapdata) for stop in stops],
            "plan": _plan_public(plans[0]) if plans else None,
            "plan_history": [_plan_public(p) for p in plans],
            "state": _state_public(state),
            "road_status": road_status,
            "road_events": road_events_public(conn),
            "timeline": _timeline(conn, task_id),
            "agent_traces": agents.list_for_task(conn, task_id),
            "assignment": dict(assignment) if assignment else None,
            "notes": [dict(row) for row in notes],
            "map_version": mapdata.map_version,
            "runtime": runtime_public(conn, task_id),
            "semantic_obstacles": active_obstacles(conn, task_id),
            "road_marks": road_marks(conn, task_id),
            "upstream_simulation": upstream_public(conn, task_id),
            "scene": scene_public(conn, task_scene(conn, task_id)),
        }
        bundle["local_avoidance"] = local_avoidance(mapdata, state, bundle["semantic_obstacles"]) if task["mode"] == "sim" else None
        return bundle


def _task_public(task: dict[str, Any], mapdata) -> dict[str, Any]:
    return {
        "id": task["id"],
        "mode": task["mode"],
        "scene_id": task["scene_id"],
        "raw_input": task["raw_input"],
        "parser": task["parser"],
        "pickup_location_id": task["pickup_location_id"],
        "pickup_location_name": node_name(mapdata, task["pickup_location_id"]),
        "deadline": task["deadline"],
        "deadline_kind": task["deadline_kind"],
        "status": task["status"],
        "plan_version": task["plan_version"],
        "pickup_status": task["pickup_status"],
        "created_at": task["created_at"],
        "updated_at": task["updated_at"],
    }


def _stop_public(stop: dict[str, Any], mapdata) -> dict[str, Any]:
    return {
        "id": stop["id"],
        "seq": stop["seq"],
        "location_id": stop["location_id"],
        "location_name": node_name(mapdata, stop["location_id"]),
        "items": loads(stop["items"], []),
        "priority": stop["priority"],
        "status": stop["status"],
        "arrived_at": stop["arrived_at"],
        "delivered_at": stop["delivered_at"],
    }


def _plan_public(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "plan_version": plan["plan_version"],
        "created_at": plan["created_at"],
        "ordered_stops": loads(plan["ordered_stops"], []),
        "route_node_ids": loads(plan["route_node_ids"], []),
        "route_edge_ids": loads(plan["route_edge_ids"], []),
        "polyline": loads(plan["polyline"], []),
        "estimated_duration_seconds": plan["estimated_duration_seconds"],
        "assumptions": loads(plan["assumptions"], []),
        "unreachable_stops": loads(plan["unreachable_stops"], []),
        "warnings": loads(plan["warnings"], []),
        "feasible": bool(loads(plan["ordered_stops"], [])) and not loads(plan["unreachable_stops"], []),
    }


def _state_public(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if state is None:
        return None
    return {
        "mode": state["mode"],
        "plan_version": state["plan_version"],
        "current_edge_id": state["current_edge_id"],
        "edge_progress": state["edge_progress"],
        "last_node_id": state["last_node_id"],
        "position": {"x": state["position_x"], "y": state["position_y"]},
        "updated_at": state["updated_at"],
        "remaining_edge_ids": loads(state["remaining_edge_ids"], []),
    }


def _start_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.now().astimezone()
    if _valid_clock(value):
        hour, minute = [int(part) for part in value.split(":")]
        now = datetime.now().astimezone()
        moment = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if moment < now:
            moment = moment.replace(day=1)
        return moment
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


@serialized
def generate_plan(task_id: str, start_time: str | None = None) -> dict[str, Any]:
    started = perf_counter()
    mapdata = load_map()
    with get_conn() as conn:
        task = _task_row(conn, task_id)
        if task["status"] in ("completed", "cancelled"):
            raise ApiError(409, "task_closed", "已结束的任务不能重新生成计划")
        state = _state_row(conn, task_id)
        if state and float(state["edge_progress"]) > 1e-6:
            raise ApiError(409, "use_replan", "车辆已在路段中行驶，请使用剩余任务重规划以保留当前位置")
        stops = _stop_rows(conn, task_id)
        road_status = get_road_status(conn, task_id)
        active = [s for s in stops if s["status"] != "delivered"]
        result = build_plan(mapdata, task, active, road_status, _start_datetime(start_time), task["pickup_location_id"])
        version = int(task["plan_version"]) + 1
        conn.execute(
            "INSERT INTO plans (id, task_id, plan_version, ordered_stops, route_node_ids, route_edge_ids, polyline,"
            " estimated_duration_seconds, assumptions, unreachable_stops, warnings, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _new_id("plan"),
                task_id,
                version,
                dumps(result.ordered_stops),
                dumps(result.route_node_ids),
                dumps(result.route_edge_ids),
                dumps(result.polyline),
                result.estimated_duration_seconds,
                dumps(result.assumptions),
                dumps(result.unreachable_stops),
                dumps(result.warnings),
                now_iso(),
            ),
        )
        conn.execute("UPDATE tasks SET plan_version = ?, status = ?, updated_at = ? WHERE id = ?", (version, "ready", now_iso(), task_id))
        _reset_execution(conn, task_id, task["pickup_location_id"], result.route_edge_ids)
        add_event(conn, task_id, "plan_generated", {"stops": len(result.ordered_stops), "feasible": result.feasible}, version)
        order = "→".join(node_name(mapdata, item["location_id"]) for item in result.ordered_stops)
        agents.record(
            conn,
            "task_planner",
            "plan_stops",
            task_id=task_id,
            input_summary=f"待配送 {len(active)} 站；异常道路 {sum(1 for value in road_status.values() if value != 'open')} 条",
            output_summary=(
                f"计划 v{version}：{order or '无可行站点'}；约 {round(result.estimated_duration_seconds / 60)} 分钟；"
                f"{'可行' if result.feasible else '存在不可达站点'}"
            ),
            tools=["plan_stops"],
            duration_ms=int((perf_counter() - started) * 1000),
        )
    return get_bundle(task_id)


def _reset_execution(conn, task_id: str, start_node: str, edge_ids: list[str]) -> None:
    mapdata = load_map()
    node = mapdata.nodes[start_node]
    conn.execute(
        "UPDATE execution_state SET plan_version = (SELECT plan_version FROM tasks WHERE id = ?), current_edge_id = ?,"
        " edge_progress = 0, last_node_id = ?, position_x = ?, position_y = ?, remaining_node_ids = '[]',"
        " remaining_edge_ids = ?, dwell_remaining = 0, updated_at = ? WHERE task_id = ?",
        (
            task_id,
            edge_ids[0] if edge_ids else None,
            start_node,
            node["x"],
            node["y"],
            dumps(edge_ids),
            now_iso(),
            task_id,
        ),
    )


@serialized
def perform_action(task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    started = perf_counter()
    action = payload["action"]
    request_id = payload["request_id"]
    with get_conn() as conn:
        logged = conn.execute("SELECT response FROM action_log WHERE request_id = ?", (request_id,)).fetchone()
        if logged:
            response = loads(logged["response"], {})
            if response.get("task", {}).get("id") != task_id:
                raise ApiError(409, "request_id_conflict", "该请求编号属于其他任务")
            return response
        task = _task_row(conn, task_id)
        _check_plan_version(task, payload.get("plan_version", 0))
        stops = _stop_rows(conn, task_id)

        if action in ("pause", "resume", "start", "cancel", "complete"):
            conn.execute(
                "UPDATE sim_runtime SET auto_resume = 0, emergency_hold = 0, hold_reason = '' WHERE task_id = ?",
                (task_id,),
            )

        if action == "start":
            if task["status"] not in ("ready", "paused"):
                raise ApiError(409, "invalid_transition", f"当前状态 {task['status']} 不能开始配送")
            if int(task["plan_version"]) < 1:
                raise ApiError(409, "no_plan", "请先生成配送计划")
            if not _plan_public(_plan_rows(conn, task_id)[0])["feasible"]:
                raise ApiError(409, "no_feasible_plan", "当前没有完整可执行路线，请先处理不可达站点")
            _set_status(conn, task_id, "in_progress")
            add_event(conn, task_id, "action:start", {}, task["plan_version"])
        elif action == "pause":
            if task["status"] not in ("in_progress", "paused"):
                raise ApiError(409, "invalid_transition", "只有执行中的任务可以暂停")
            _set_status(conn, task_id, "paused")
            add_event(conn, task_id, "action:pause", {}, task["plan_version"])
        elif action == "resume":
            if task["status"] not in ("paused", "blocked"):
                raise ApiError(409, "invalid_transition", "只有暂停或阻塞状态可以继续")
            plans = _plan_rows(conn, task_id)
            if not plans or not _plan_public(plans[0])["feasible"]:
                raise ApiError(409, "no_feasible_plan", "当前没有完整可执行路线，请先重规划")
            _set_status(conn, task_id, "in_progress")
            add_event(conn, task_id, "action:resume", {}, task["plan_version"])
        elif action == "arrive":
            if task["status"] not in ("in_progress", "paused"):
                raise ApiError(409, "invalid_transition", "任务尚未开始，不能标记到达")
            stop_id = payload.get("stop_id")
            stop = next((s for s in stops if s["id"] == stop_id), None)
            if stop is None:
                raise ApiError(404, "stop_not_found", "站点不存在")
            if stop["status"] == "delivered":
                raise ApiError(409, "already_delivered", "该站点已交付")
            if stop["status"] == "pending":
                conn.execute("UPDATE stops SET status = 'arrived', arrived_at = ? WHERE id = ?", (now_iso(), stop_id))
                add_event(conn, task_id, "stop_arrived", {"stop_id": stop_id}, task["plan_version"])
        elif action == "confirm_pickup":
            if task["status"] not in ("in_progress", "paused"):
                raise ApiError(409, "invalid_transition", "任务尚未开始，不能确认取货")
            if task["pickup_status"] != "picked_up":
                conn.execute("UPDATE tasks SET pickup_status = 'picked_up', updated_at = ? WHERE id = ?", (now_iso(), task_id))
                add_event(conn, task_id, "pickup_confirmed", {}, task["plan_version"])
        elif action == "confirm_delivery":
            if task["pickup_status"] != "picked_up":
                raise ApiError(409, "pickup_required", "请先确认取货，才能记录交付")
            stop_id = payload.get("stop_id")
            stop = next((s for s in stops if s["id"] == stop_id), None)
            if stop is None:
                raise ApiError(404, "stop_not_found", "站点不存在")
            if stop["status"] == "delivered":
                pass
            else:
                if stop["status"] == "pending":
                    conn.execute("UPDATE stops SET status = 'arrived', arrived_at = ? WHERE id = ?", (now_iso(), stop_id))
                conn.execute("UPDATE stops SET status = 'delivered', delivered_at = ? WHERE id = ?", (now_iso(), stop_id))
                add_event(conn, task_id, "stop_delivered", {"stop_id": stop_id}, task["plan_version"])
        elif action == "complete":
            remaining = [s for s in stops if s["status"] != "delivered"]
            if remaining or task["pickup_status"] != "picked_up":
                raise ApiError(409, "stops_remaining", "仍有未交付站点，不能完成任务")
            if task["status"] == "completed":
                pass
            else:
                _set_status(conn, task_id, "completed")
                add_event(conn, task_id, "action:complete", {}, task["plan_version"])
        elif action == "cancel":
            if task["status"] == "completed":
                raise ApiError(409, "invalid_transition", "已完成的任务不能取消")
            _set_status(conn, task_id, "cancelled")
            add_event(conn, task_id, "action:cancel", {"note": payload.get("note", "")}, task["plan_version"])
        else:
            raise ApiError(400, "unknown_action", f"未知动作 {action}")

        current = conn.execute("SELECT status, pickup_status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        agents.record(
            conn,
            "dispatcher",
            "update_status",
            task_id=task_id,
            input_summary=f"动作={action}" + (f"；站点={payload.get('stop_id')}" if payload.get("stop_id") else ""),
            output_summary=f"任务状态={current['status']}；取货={current['pickup_status']}",
            tools=["update_status"],
            duration_ms=int((perf_counter() - started) * 1000),
        )

    bundle = get_bundle(task_id)
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO action_log (request_id, task_id, action, response, created_at) VALUES (?, ?, ?, ?, ?)",
            (request_id, task_id, action, dumps(bundle), now_iso()),
        )
    return bundle


def _check_plan_version(task: dict[str, Any], plan_version: int, allow_stale_for: tuple[str, ...] = ()) -> None:
    if plan_version and int(plan_version) != int(task["plan_version"]):
        raise ApiError(409, "stale_plan_version", "计划版本已更新，请刷新页面后重试")


def _set_status(conn, task_id: str, status: str) -> None:
    conn.execute("UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?", (status, now_iso(), task_id))


@serialized
def apply_dispatch_status(task_id: str, status: str) -> None:
    """Limited execution tool for dispatch; never confirms pickup or delivery."""
    with get_conn() as conn:
        task = _task_row(conn, task_id)
        if status not in ("paused", "in_progress") or task["status"] not in ("in_progress", "paused", "blocked"):
            raise ApiError(409, "invalid_dispatch_transition", "调度不能执行该任务状态变更")
        plans = _plan_rows(conn, task_id)
        if status == "in_progress" and (not plans or not _plan_public(plans[0])["feasible"]):
            raise ApiError(409, "no_feasible_plan", "尚无可执行路线，不能自动继续")
        _set_status(conn, task_id, status)


def _edges_near_node(mapdata, node_id: str) -> set[str]:
    if not node_id:
        return set()
    return {eid for eid, edge in mapdata.edges.items() if edge["from"] == node_id or edge["to"] == node_id}


def _observe_and_maybe_restore(conn, mapdata, task_id: str, plan_version: int, observed_edge_ids: set[str]) -> list[str]:
    if not observed_edge_ids:
        return []
    restored: list[str] = []
    for edge_id in observed_edge_ids:
        row = conn.execute(
            "SELECT id, edge_id, status, observe_count FROM road_events WHERE edge_id = ? AND status != 'open' ORDER BY created_at DESC LIMIT 1",
            (edge_id,),
        ).fetchone()
        if row is None:
            continue
        new_count = int(row["observe_count"] or 0) + 1
        if new_count >= 2:
            conn.execute("DELETE FROM road_events WHERE id = ?", (row["id"],))
            edge = mapdata.edges.get(edge_id, {})
            add_event(
                conn,
                task_id,
                "road_restored",
                {"edge_id": edge_id, "edge_name": edge.get("name", edge_id), "reason": "sim_observe_twice"},
                plan_version,
            )
            restored.append(edge_id)
        else:
            conn.execute(
                "UPDATE road_events SET observe_count = ?, last_observed_at = ? WHERE id = ?",
                (new_count, now_iso(), row["id"]),
            )
    return restored


@serialized
def replan(task_id: str, reason: str = "", request_id: str | None = None, plan_version: int = 0) -> dict[str, Any]:
    started = perf_counter()
    mapdata = load_map()
    with get_conn() as conn:
        if request_id:
            logged = conn.execute("SELECT task_id, response FROM action_log WHERE request_id = ?", (request_id,)).fetchone()
            if logged:
                if logged["task_id"] != task_id:
                    raise ApiError(409, "request_id_conflict", "该请求编号属于其他任务")
                return loads(logged["response"], {})
        task = _task_row(conn, task_id)
        _check_plan_version(task, plan_version)
        if task["status"] in ("completed", "cancelled"):
            raise ApiError(409, "task_closed", "已结束的任务不能重规划")
        stops = _stop_rows(conn, task_id)
        state = _state_row(conn, task_id)
        start_node = (state or {}).get("last_node_id") or task["pickup_location_id"]
        active = [s for s in stops if s["status"] != "delivered"]
        if not active:
            raise ApiError(409, "nothing_to_replan", "没有剩余站点需要重规划")
        road_status = get_road_status(conn, task_id)
        result, motion_seed = plan_from_position(
            mapdata, task, active, road_status, _start_datetime(None), state or {},
            route_hazards(conn, task_id), baseline_road_status(conn, task_id),
        )
        version = int(task["plan_version"]) + 1
        conn.execute(
            "INSERT INTO plans (id, task_id, plan_version, ordered_stops, route_node_ids, route_edge_ids, polyline,"
            " estimated_duration_seconds, assumptions, unreachable_stops, warnings, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _new_id("plan"),
                task_id,
                version,
                dumps(result.ordered_stops),
                dumps(result.route_node_ids),
                dumps(result.route_edge_ids),
                dumps(result.polyline),
                result.estimated_duration_seconds,
                dumps(result.assumptions),
                dumps(result.unreachable_stops),
                dumps(result.warnings),
                now_iso(),
            ),
        )
        new_status = ("paused" if task["status"] == "in_progress" else task["status"]) if result.feasible else "blocked"
        conn.execute("UPDATE tasks SET plan_version = ?, status = ?, updated_at = ? WHERE id = ?", (version, new_status, now_iso(), task_id))
        if state and float(state["edge_progress"]) > 1e-6 and state["current_edge_id"]:
            seed = motion_seed or state
            conn.execute(
                "UPDATE execution_state SET plan_version = ?, current_edge_id = ?, edge_progress = ?, last_node_id = ?,"
                " remaining_edge_ids = ?, updated_at = ? WHERE task_id = ?",
                (version, seed["current_edge_id"], seed["edge_progress"], seed["last_node_id"], dumps(result.route_edge_ids), now_iso(), task_id),
            )
        else:
            _reset_execution(conn, task_id, start_node, result.route_edge_ids)
        add_event(
            conn,
            task_id,
            "replan",
            {
                "reason": reason,
                "from_node": start_node,
                "from_position": {"x": state["position_x"], "y": state["position_y"]} if state else None,
                "kept_delivered": [s["id"] for s in stops if s["status"] == "delivered"],
                "unreachable": result.unreachable_stops,
            },
            version,
        )
        order = "→".join(node_name(mapdata, item["location_id"]) for item in result.ordered_stops)
        agents.record(
            conn,
            "dispatcher",
            "replan_task",
            task_id=task_id,
            input_summary=f"原因={reason or '未说明'}；剩余 {len(active)} 站",
            output_summary=(
                f"新计划 v{version}：{order or '无可行站点'}；保留已完成 "
                f"{len([s for s in stops if s['status'] == 'delivered'])} 站；不可达 {len(result.unreachable_stops)}"
            ),
            tools=["replan_task"],
            duration_ms=int((perf_counter() - started) * 1000),
        )
    bundle = get_bundle(task_id)
    bundle["replan_diff"] = _replan_diff(bundle)
    if request_id:
        with get_conn() as conn:
            conn.execute("INSERT INTO action_log VALUES (?, ?, ?, ?, ?)", (request_id, task_id, "replan", dumps(bundle), now_iso()))
    return bundle


def _replan_diff(bundle: dict[str, Any]) -> dict[str, Any]:
    history = bundle.get("plan_history") or []
    if len(history) < 2:
        return {"changed": False}
    current, previous = history[0], history[1]
    before = {item["stop_id"]: item["seq"] for item in previous["ordered_stops"]}
    after = {item["stop_id"]: item["seq"] for item in current["ordered_stops"]}
    return {
        "changed": before != after,
        "moved": [
            {"stop_id": key, "from_seq": before.get(key), "to_seq": after.get(key)}
            for key in after
            if before.get(key) != after.get(key)
        ],
        "new_unreachable": current["unreachable_stops"],
        "warnings": current["warnings"],
    }


@serialized
def sim_tick(task_id: str, seconds: float, speed_mps: float | None = None, *, runtime: bool = False) -> dict[str, Any]:
    mapdata = load_map()
    with get_conn() as conn:
        task = _task_row(conn, task_id)
        if runtime_public(conn, task_id)["enabled"] and not runtime:
            raise ApiError(409, "runtime_owns_motion", "后台正在推进该车辆，请勿重复调用手动推进")
        if task["mode"] != "sim":
            raise ApiError(409, "real_mode", "真实模式不使用模拟位置执行器")
        if task["status"] != "in_progress":
            raise ApiError(409, "not_in_progress", "任务未在执行中，模拟器不推进")
        stops = _stop_rows(conn, task_id)
        state = _state_row(conn, task_id)
        if state is None:
            raise ApiError(500, "state_missing", "执行状态缺失")
        plan = _plan_rows(conn, task_id)[0]
        if int(plan["plan_version"]) != int(task["plan_version"]):
            raise ApiError(409, "stale_plan_version", "执行状态与计划版本不一致，请重新规划")
        old_node_id = state["last_node_id"]
        old_edge_id = state["current_edge_id"]
        working = {
            "current_edge_id": state["current_edge_id"],
            "edge_progress": state["edge_progress"],
            "last_node_id": state["last_node_id"],
            "position_x": state["position_x"],
            "position_y": state["position_y"],
            "_remaining_edges": loads(state["remaining_edge_ids"], []),
        }
        outcome = advance(
            mapdata, task, stops, working, seconds, speed_mps or SIM_SPEED_MPS,
            get_road_status(conn, task_id), active_obstacles(conn, task_id), baseline_road_status(conn, task_id),
        )
        observed: set[str] = set()
        observed.add(old_edge_id)
        observed.add(outcome["current_edge_id"])
        observed.update(_edges_near_node(mapdata, old_node_id))
        if outcome["last_node_id"] != old_node_id:
            observed.update(_edges_near_node(mapdata, outcome["last_node_id"]))
        observed.discard(None)
        restored = _observe_and_maybe_restore(conn, mapdata, task_id, task["plan_version"], observed)
        for stop_id in outcome["arrived_stop_ids"]:
            conn.execute("UPDATE stops SET status = 'arrived', arrived_at = ? WHERE id = ? AND status = 'pending'", (now_iso(), stop_id))
            add_event(conn, task_id, "stop_arrived", {"stop_id": stop_id, "source": "simulator"}, task["plan_version"])
        conn.execute(
            "UPDATE execution_state SET current_edge_id = ?, edge_progress = ?, last_node_id = ?, position_x = ?,"
            " position_y = ?, remaining_edge_ids = ?, updated_at = ? WHERE task_id = ?",
            (
                outcome["current_edge_id"],
                outcome["edge_progress"],
                outcome["last_node_id"],
                outcome["position"]["x"],
                outcome["position"]["y"],
                dumps(outcome["remaining_edges"]),
                now_iso(),
                task_id,
            ),
        )
        conn.execute("UPDATE sim_runtime SET waiting = ?, updated_at = ? WHERE task_id = ?", (outcome["waiting"], now_iso(), task_id))
    bundle = get_bundle(task_id)
    bundle["sim"] = {
        "waiting": outcome["waiting"],
        "arrived_stop_ids": outcome["arrived_stop_ids"],
        "restored_edges": restored,
    }
    return bundle


@serialized
def get_report(task_id: str) -> dict[str, Any]:
    mapdata = load_map()
    with get_conn() as conn:
        task = _task_row(conn, task_id)
        stops = _stop_rows(conn, task_id)
        plans = _plan_rows(conn, task_id)
        events = _timeline(conn, task_id)
        road_edges = [dict(row) for row in conn.execute("SELECT * FROM road_events ORDER BY created_at").fetchall()]
        report = build_report(mapdata, task, stops, plans, road_edges, events)
        report["agent_summary"] = agents.summary(conn, task_id)
        report["agent_traces"] = agents.list_for_task(conn, task_id)
        report["semantic_events"] = [loads(row["response"], {}) for row in conn.execute(
            "SELECT response FROM semantic_events WHERE task_id = ? ORDER BY rowid", (task_id,)
        ).fetchall()]
        return report
