import math
from typing import Any

from .config import CONGESTED_FACTOR, SIM_SPEED_MPS
from .mapdata import MapData


def polyline_length(points: list[list[float]]) -> float:
    total = 0.0
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        total += ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
    return total


def _point_at(points: list[list[float]], ratio: float) -> tuple[float, float]:
    if len(points) == 1:
        return points[0][0], points[0][1]
    total = polyline_length(points)
    if total <= 0:
        return points[0][0], points[0][1]
    target = max(0.0, min(1.0, ratio)) * total
    walked = 0.0
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        segment = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
        if walked + segment >= target:
            local = (target - walked) / segment if segment else 0.0
            return x1 + (x2 - x1) * local, y1 + (y2 - y1) * local
        walked += segment
    return points[-1][0], points[-1][1]


def edge_points(mapdata: MapData, edge_id: str, from_node: str | None) -> list[list[float]]:
    edge = mapdata.edges[edge_id]
    points = [list(map(float, p)) for p in edge["polyline"]]
    if from_node is not None and edge["from"] != from_node:
        points.reverse()
    return points


def split_polyline(points: list[list[float]], ratio: float) -> tuple[list[list[float]], list[list[float]]]:
    """Split geometry at an actual position; both halves include that position."""
    target = polyline_length(points) * ratio
    walked = 0.0
    for index, (a, b) in enumerate(zip(points, points[1:])):
        length = math.hypot(b[0] - a[0], b[1] - a[1])
        if walked + length >= target:
            point = list(_point_at(points, ratio))
            return points[:index + 1] + [point], [point] + points[index + 1:]
        walked += length
    return points, [points[-1], points[-1]]


def obstacle_ahead(obstacle: dict[str, Any], edge: dict[str, Any], from_node: str, progress: float) -> bool:
    fraction = obstacle.get("edge_fraction")
    if fraction is None:
        return True
    along_motion = fraction if from_node == edge["from"] else 1.0 - fraction
    return along_motion >= progress - 1e-6


def advance(
    mapdata: MapData,
    task: dict[str, Any],
    stops: list[dict[str, Any]],
    state: dict[str, Any],
    seconds: float,
    speed_mps: float | None = None,
    road_status: dict[str, str] | None = None,
    obstacles: list[dict[str, Any]] | None = None,
    baseline_status: dict[str, str] | None = None,
) -> dict[str, Any]:
    speed = speed_mps or SIM_SPEED_MPS
    remaining_edges: list[str] = list(state.get("_remaining_edges", []))
    if state.get("current_edge_id") and (not remaining_edges or remaining_edges[0] != state["current_edge_id"]):
        remaining_edges.insert(0, state["current_edge_id"])
    progress = float(state.get("edge_progress") or 0.0)
    last_node = state.get("last_node_id") or task["pickup_location_id"]
    position = (state.get("position_x"), state.get("position_y"))
    if position[0] is None:
        node = mapdata.nodes[task["pickup_location_id"]]
        position = (node["x"], node["y"])

    stop_by_location = {s["location_id"]: s for s in stops}
    arrived_now: list[dict[str, Any]] = []
    waiting: str | None = None

    def blocking_here() -> str | None:
        if last_node == task["pickup_location_id"] and task["pickup_status"] == "pending":
            return "awaiting_pickup"
        stop = stop_by_location.get(last_node)
        if stop and stop["status"] in ("pending", "arrived"):
            return "awaiting_delivery"
        return None

    waiting = blocking_here()

    remaining = seconds
    guard = 0
    while waiting is None and remaining > 0 and remaining_edges and guard < 500:
        guard += 1
        edge_id = remaining_edges[0]
        edge = mapdata.edges.get(edge_id)
        if edge is None:
            break
        status = (road_status or {}).get(edge_id, edge.get("base_status", "open"))
        hazards = [o for o in (obstacles or []) if o["edge_id"] == edge_id and o["effective_status"] in ("blocked", "unknown")]
        whole_block = (baseline_status or {}).get(edge_id, edge.get("base_status", "open")) == "blocked"
        if whole_block or (status == "blocked" and not hazards) or any(obstacle_ahead(o, edge, last_node, progress) for o in hazards):
            waiting = "road_blocked"
            break
        edge_speed = speed / (CONGESTED_FACTOR if status == "congested" else 1.0)
        points = edge_points(mapdata, edge_id, last_node)
        length = polyline_length(points)
        if length <= 0:
            remaining_edges.pop(0)
            continue
        remaining_distance = length * (1.0 - progress)
        step_px = remaining * edge_speed / edge["distance_m"] * length if edge["distance_m"] else remaining_distance
        if step_px < remaining_distance:
            progress += step_px / length
            position = _point_at(points, progress)
            remaining = 0.0
            break
        remaining -= remaining_distance / edge_speed * (edge["distance_m"] / length if length else 1.0)
        remaining = max(0.0, remaining)
        next_node = edge["to"] if last_node == edge["from"] else edge["from"]
        last_node = next_node
        progress = 0.0
        position = (mapdata.nodes[last_node]["x"], mapdata.nodes[last_node]["y"])
        remaining_edges.pop(0)
        stop = stop_by_location.get(last_node)
        if stop and stop["status"] == "pending":
            arrived_now.append(stop)
            waiting = "awaiting_delivery"
            break
        waiting = blocking_here()
        if waiting:
            break

    return {
        "current_edge_id": remaining_edges[0] if remaining_edges and waiting not in ("awaiting_pickup", "awaiting_delivery") else None,
        "remaining_edges": remaining_edges,
        "edge_progress": round(progress, 4),
        "last_node_id": last_node,
        "position": {"x": round(position[0], 1), "y": round(position[1], 1)},
        "arrived_stop_ids": [s["id"] for s in arrived_now],
        "waiting": waiting,
    }
