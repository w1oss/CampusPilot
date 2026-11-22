"""Simulated scene world: ground truth, ongoing observations and en-route rechecks.

Only this module reads ``obstacle_truth``. Agents, planning and route tools read
``road_known`` and can never see that a barrier has already been removed until a
vehicle observes it from within viewing range.
"""
import math
import uuid
from datetime import datetime, timezone
from typing import Any

from .db import dumps, get_conn, loads, now_iso
from .mapdata import load_map
from .schemas import SemanticEventRequest

VIRTUAL_TIME_SCALE = 60.0
OBSERVE_RADIUS_M = 20.0
METER_PER_PIXEL = 0.28
GENERATE_COOLDOWN_VIRTUAL = 60.0
ACTIVE_LIMIT = 6
TRUTH_DURATION_SECONDS = {
    "pedestrian": 120.0,
    "crowd": 600.0,
    "construction": 3600.0,
    "box": 600.0,
    "barrier": 3600.0,
}
SUMMARIES = {
    "pedestrian": "上游自动发现前方行人横穿",
    "construction": "上游自动发现前方施工围挡",
    "crowd": "上游自动发现前方人群拥挤",
    "box": "上游自动发现前方纸箱/小障碍",
    "barrier": "上游自动发现前方维修栏",
}


def ensure_scene(conn, scene_id: str) -> float:
    stamp = now_iso()
    conn.execute(
        "INSERT OR IGNORE INTO scenes (id, kind, virtual_seconds, created_at, updated_at) VALUES (?, 'sim', 0, ?, ?)",
        (scene_id, stamp, stamp),
    )
    row = conn.execute("SELECT virtual_seconds FROM scenes WHERE id = ?", (scene_id,)).fetchone()
    return float(row["virtual_seconds"]) if row else 0.0


def advance_clock(conn, real_seconds: float) -> None:
    """Advance every scene clock; the scripted world may end while known state stays."""
    stamp = now_iso()
    for row in conn.execute("SELECT id, virtual_seconds FROM scenes").fetchall():
        virtual = float(row["virtual_seconds"]) + real_seconds * VIRTUAL_TIME_SCALE
        conn.execute("UPDATE scenes SET virtual_seconds = ?, updated_at = ? WHERE id = ?", (virtual, stamp, row["id"]))
        conn.execute(
            "UPDATE obstacle_truth SET present = 0, updated_at = ?"
            " WHERE scene_id = ? AND present = 1 AND clear_at_virtual IS NOT NULL AND clear_at_virtual <= ?",
            (stamp, row["id"], virtual),
        )


def _point_at(points: list[list[float]], fraction: float) -> tuple[float, float]:
    if len(points) == 1:
        return points[0][0], points[0][1]
    total = 0.0
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        total += math.hypot(x2 - x1, y2 - y1)
    if total <= 0:
        return points[0][0], points[0][1]
    target = max(0.0, min(1.0, fraction)) * total
    walked = 0.0
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        segment = math.hypot(x2 - x1, y2 - y1)
        if walked + segment >= target:
            local = (target - walked) / segment if segment else 0.0
            return x1 + (x2 - x1) * local, y1 + (y2 - y1) * local
        walked += segment
    return points[-1][0], points[-1][1]


def _segment_distance(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq <= 0:
        return math.hypot(px - ax, py - ay)
    ratio = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    return math.hypot(px - (ax + dx * ratio), py - (ay + dy * ratio))


def _edge_distance(mapdata, edge: dict[str, Any], x: float, y: float, fraction: float | None) -> float:
    points = [[float(p[0]), float(p[1])] for p in edge["polyline"]]
    if fraction is not None:
        px, py = _point_at(points, fraction)
        return math.hypot(px - x, py - y)
    return min(
        _segment_distance(x, y, a[0], a[1], b[0], b[1]) for a, b in zip(points, points[1:])
    )


def observable(mapdata, state, edge_id: str, fraction: float | None, category: str = "other") -> bool:
    """Simple demo visibility, not a camera/occlusion model.

    Within 20 m of a junction, construction/barriers on its incident edges are
    visible over the entire segment, regardless of length. Never recurse beyond
    that junction. Boxes need their original position inside a 10 m radius.
    """
    if not state or state["position_x"] is None:
        return False
    current_id = state["current_edge_id"]
    edge = mapdata.edges.get(edge_id)
    if edge is None:
        return False
    x, y = float(state["position_x"]), float(state["position_y"])
    current = mapdata.edges.get(current_id) if current_id else None
    if category in ("construction", "barrier"):
        candidates = ({current["from"], current["to"]} if current else {state["last_node_id"]}) & {edge["from"], edge["to"]}
        if any(math.hypot(mapdata.nodes[node]["x"] - x, mapdata.nodes[node]["y"] - y)
               * METER_PER_PIXEL <= OBSERVE_RADIUS_M for node in candidates):
            return True
    if current_id and current_id != edge_id:
        if current is None or not ({current["from"], current["to"]} & {edge["from"], edge["to"]}):
            return False
    distance_px = _edge_distance(mapdata, edge, x, y, fraction)
    return distance_px * METER_PER_PIXEL <= (10.0 if category == "box" else OBSERVE_RADIUS_M)


def _state(conn, task_id: str):
    return conn.execute(
        "SELECT current_edge_id, edge_progress, last_node_id, position_x, position_y"
        " FROM execution_state WHERE task_id = ?",
        (task_id,),
    ).fetchone()


def generation_messages(conn) -> list[dict[str, Any]]:
    """Ongoing observation source: one new obstacle per newly entered road segment."""
    mapdata = load_map()
    messages: list[dict[str, Any]] = []
    rows = conn.execute(
        "SELECT u.task_id, u.category, u.last_edge_id, u.next_generate_at, t.scene_id"
        " FROM upstream_simulations u JOIN tasks t ON t.id = u.task_id"
        " WHERE u.enabled = 1 AND t.mode = 'sim' AND t.status = 'in_progress'"
    ).fetchall()
    for row in rows:
        task_id = row["task_id"]
        scene_id = row["scene_id"]
        state = _state(conn, task_id)
        if state is None or not state["current_edge_id"]:
            continue
        edge_id = state["current_edge_id"]
        if row["last_edge_id"] == edge_id:
            continue
        virtual = ensure_scene(conn, scene_id)
        if virtual < float(row["next_generate_at"] or 0):
            continue
        active = conn.execute("SELECT COUNT(*) FROM road_known WHERE scene_id = ? AND active = 1", (scene_id,)).fetchone()[0]
        if int(active) >= ACTIVE_LIMIT:
            continue
        edge = mapdata.edges[edge_id]
        progress = float(state["edge_progress"] or 0)
        category = row["category"]
        if progress >= 0.995:
            continue
        # Boxes are first seen within 10 geometric metres, just before the arc.
        geometry_m = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(edge["polyline"], edge["polyline"][1:])) * METER_PER_PIXEL
        offset = min(0.9, 9.0 / max(0.1, geometry_m)) if category == "box" else min(0.9, 10.0 / max(1.0, float(edge["distance_m"])))
        fraction = min(0.995, progress + offset)
        if state["last_node_id"] == edge["to"]:
            fraction = 1.0 - fraction
        duration = TRUTH_DURATION_SECONDS.get(category, 600.0)
        obstacle_id = f"auto_{uuid.uuid4().hex[:12]}"
        stamp = now_iso()
        conn.execute(
            "INSERT INTO obstacle_truth (scene_id, obstacle_id, edge_id, edge_fraction, category, present,"
            " clear_at_virtual, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)",
            (scene_id, obstacle_id, edge_id, fraction, category, virtual + duration, stamp, stamp),
        )
        conn.execute(
            "UPDATE upstream_simulations SET last_edge_id = ?, next_generate_at = ?,"
            " generated_count = generated_count + 1 WHERE task_id = ?",
            (edge_id, virtual + GENERATE_COOLDOWN_VIRTUAL, task_id),
        )
        messages.append(
            SemanticEventRequest(
                event_id=f"auto_{uuid.uuid4().hex}",
                task_id=task_id,
                obstacle_id=obstacle_id,
                revision=1,
                map_version=mapdata.map_version,
                edge_id=edge_id,
                edge_fraction=fraction,
                category=category,
                passability="congested" if category == "crowd" else "blocked",
                confidence=1,
                expected_duration_s=duration,
                summary=SUMMARIES[category],
                observed_at=datetime.now(timezone.utc),
            ).model_dump(mode="json")
        )
    return messages


def recheck_messages(conn) -> list[dict[str, Any]]:
    """An actual observation is required; merely timing out never clears knowledge."""
    mapdata = load_map()
    messages: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    counted: set[tuple[str, str]] = set()
    tasks = conn.execute(
        "SELECT id, scene_id FROM tasks WHERE mode = 'sim' AND status IN ('in_progress', 'paused', 'blocked')"
    ).fetchall()
    for task in tasks:
        task_id = task["id"]
        scene_id = task["scene_id"]
        state = _state(conn, task_id)
        if state is None or state["position_x"] is None:
            continue
        known = conn.execute(
            "SELECT * FROM road_known WHERE scene_id = ? AND active = 1 ORDER BY observed_at",
            (scene_id,),
        ).fetchall()
        for row in known:
            key = (scene_id, row["obstacle_id"])
            if key in seen:
                continue
            if row["authority"] == "hard" or row["edge_fraction"] is None:
                # Whole-road report: a clear entrance cannot prove the rest is open.
                continue
            if not observable(mapdata, state, row["edge_id"], row["edge_fraction"], row["category"]):
                continue
            truth = conn.execute(
                "SELECT present FROM obstacle_truth WHERE scene_id = ? AND obstacle_id = ?",
                (scene_id, row["obstacle_id"]),
            ).fetchone()
            if truth is None:
                continue
            if int(truth["present"]) != 0:
                if row["source"] == "admin" and int(row["clear_evidence"] or 0):
                    conn.execute(
                        "UPDATE road_known SET clear_evidence = 0, updated_at = ?"
                        " WHERE scene_id = ? AND obstacle_id = ?",
                        (now_iso(), scene_id, row["obstacle_id"]),
                    )
                continue
            if row["source"] == "admin":
                if key in counted:
                    continue
                counted.add(key)
                evidence = int(row["clear_evidence"] or 0) + 1
                conn.execute(
                    "UPDATE road_known SET clear_evidence = ?, updated_at = ?"
                    " WHERE scene_id = ? AND obstacle_id = ?",
                    (evidence, now_iso(), scene_id, row["obstacle_id"]),
                )
                if evidence < 2:
                    continue
            seen.add(key)
            payload = loads(row["payload"], {})
            prefix = "上游连续两次确认" if row["source"] == "admin" else "途经附近复查"
            payload.update(
                event_id=f"recheck_{uuid.uuid4().hex}",
                task_id=task_id,
                obstacle_id=row["obstacle_id"],
                revision=int(row["revision"]) + 1,
                map_version=mapdata.map_version,
                category=row["category"],
                lifecycle="cleared",
                passability="open",
                effective_status="open",
                observed_at=now_iso(),
                source="mock_vision_llm",
                confidence=1,
                summary=f"{prefix}：{payload.get('summary', '原障碍')}已解除",
            )
            messages.append(payload)
    return messages


def step(conn, real_seconds: float) -> list[dict[str, Any]]:
    """Advance the scene clock, then emit newly observed and rechecked messages."""
    advance_clock(conn, real_seconds)
    return generation_messages(conn) + recheck_messages(conn)


def reset_scene(conn, scene_id: str, include_admin: bool = False) -> None:
    stamp = now_iso()
    if include_admin:
        conn.execute("DELETE FROM obstacle_truth WHERE scene_id = ?", (scene_id,))
        conn.execute("DELETE FROM road_known WHERE scene_id = ?", (scene_id,))
    else:
        conn.execute(
            "DELETE FROM obstacle_truth WHERE scene_id = ? AND obstacle_id NOT LIKE 'admin:%'",
            (scene_id,),
        )
        conn.execute(
            "DELETE FROM road_known WHERE scene_id = ? AND source != 'admin'",
            (scene_id,),
        )
    conn.execute(
        "DELETE FROM semantic_obstacles WHERE task_id IN (SELECT id FROM tasks WHERE scene_id = ?)",
        (scene_id,),
    )
    conn.execute(
        "DELETE FROM upstream_simulations WHERE task_id IN (SELECT id FROM tasks WHERE scene_id = ?)",
        (scene_id,),
    )
    conn.execute("UPDATE scenes SET virtual_seconds = 0, updated_at = ? WHERE id = ?", (stamp, scene_id))
