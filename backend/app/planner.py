from dataclasses import dataclass, field
from copy import deepcopy
from datetime import datetime, time, timedelta
from itertools import permutations
from typing import Any

from .config import DEFAULT_DWELL_SECONDS, SPEED_MPS
from .graph import PathResult, dijkstra
from .mapdata import MapData, node_name
from .simulator import split_polyline, is_local_box


def plan_from_position(
    mapdata: MapData,
    task: dict[str, Any],
    stops: list[dict[str, Any]],
    road_status: dict[str, str],
    start: datetime,
    state: dict[str, Any],
    obstacles: list[dict[str, Any]],
    baseline_status: dict[str, str],
) -> tuple["PlanResult", dict[str, Any] | None]:
    """Plan through a directed virtual start without changing the cached map.

    The first temporary segment is mapped back to a real edge for execution.
    A blocked segment can only be escaped on the side containing the car.
    """
    edge = mapdata.edges.get(state.get("current_edge_id"))
    progress = float(state.get("edge_progress") or 0)
    if edge is None or progress <= 1e-6:
        return build_plan(mapdata, task, stops, road_status, start, state.get("last_node_id")), None
    fraction = progress if state["last_node_id"] == edge["from"] else 1 - progress
    local_map = deepcopy(mapdata)
    anchor = "__current_position"
    local_map.nodes[anchor] = {"id": anchor, "name": "当前位置", "kind": "waypoint", "x": state["position_x"], "y": state["position_y"]}
    before, after = split_polyline([list(p) for p in edge["polyline"]], fraction)
    before[-1] = [state["position_x"], state["position_y"]]
    after[0] = before[-1]
    blocks = [o for o in obstacles if o["edge_id"] == edge["id"] and o["effective_status"] in ("blocked", "unknown") and not is_local_box(o)]
    whole_block = baseline_status.get(edge["id"], edge.get("base_status", "open")) == "blocked"
    seeds: dict[str, dict[str, Any]] = {}
    statuses = dict(road_status)
    for backwards, target, geometry, distance_fraction in (
        (True, edge["from"], list(reversed(before)), fraction),
        (False, edge["to"], after, 1 - fraction),
    ):
        if backwards and not edge.get("bidirectional", True):
            continue
        target_fraction = 0 if backwards else 1
        if whole_block or any(
            o.get("edge_fraction") is None
            or min(fraction, target_fraction) - 1e-6 <= o["edge_fraction"] <= max(fraction, target_fraction) + 1e-6
            for o in blocks
        ):
            continue
        connector = "__pose_back" if backwards else "__pose_forward"
        local_map.edges[connector] = {
            "id": connector, "from": anchor, "to": target, "bidirectional": False,
            "distance_m": edge["distance_m"] * distance_fraction,
            "polyline": geometry, "base_status": "open",
        }
        statuses[connector] = "congested" if road_status.get(edge["id"]) == "congested" else "open"
        seeds[connector] = {
            "last_node_id": edge["to"] if backwards else edge["from"],
            "edge_progress": 1 - fraction if backwards else fraction,
            "current_edge_id": edge["id"],
        }
    result = build_plan(local_map, task, stops, statuses, start, anchor)
    first = result.route_edge_ids[0] if result.route_edge_ids else None
    seed = seeds.get(first)
    if seed:
        result.route_edge_ids[0] = edge["id"]
    result.assumptions.append("本次从车辆实时位置规划，当前位置不回退到上一个路口。")
    return result, seed


@dataclass
class PlanResult:
    ordered_stops: list[dict[str, Any]]
    route_node_ids: list[str]
    route_edge_ids: list[str]
    polyline: list[list[float]]
    estimated_duration_seconds: float
    assumptions: list[str]
    unreachable_stops: list[dict[str, Any]]
    warnings: list[str] = field(default_factory=list)
    feasible: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "ordered_stops": self.ordered_stops,
            "route_node_ids": self.route_node_ids,
            "route_edge_ids": self.route_edge_ids,
            "polyline": self.polyline,
            "estimated_duration_seconds": round(self.estimated_duration_seconds, 1),
            "assumptions": self.assumptions,
            "unreachable_stops": self.unreachable_stops,
            "warnings": self.warnings,
            "feasible": self.feasible,
        }


def _deadline_datetime(deadline: str | None, deadline_kind: str, base: datetime) -> datetime | None:
    if not deadline or deadline_kind != "clock":
        return None
    try:
        hour, minute = [int(part) for part in deadline.split(":")]
    except (ValueError, AttributeError):
        return None
    candidate = datetime.combine(base.date(), time(hour, minute), tzinfo=base.tzinfo)
    if candidate < base:
        candidate += timedelta(days=1)
    return candidate


def _fmt_time(moment: datetime) -> str:
    return moment.strftime("%H:%M")


def build_plan(
    mapdata: MapData,
    task: dict[str, Any],
    active_stops: list[dict[str, Any]],
    road_events: dict[str, str],
    start: datetime,
    start_node_id: str | None = None,
) -> PlanResult:
    pickup_id = start_node_id or task["pickup_location_id"]
    assumptions = [
        f"道路距离为示例估算值，平均速度按 {SPEED_MPS:.1f} m/s 计算。",
        f"每站停留 {DEFAULT_DWELL_SECONDS:.0f} 秒用于交接。",
        "拥堵道路按 2 倍时间成本计算，封闭道路不可通行。",
    ]
    warnings: list[str] = []
    unreachable: list[dict[str, Any]] = []

    reachable_stops: list[dict[str, Any]] = []
    paths: dict[tuple[str, str], PathResult | None] = {}

    def get_path(src: str, dst: str) -> PathResult | None:
        key = (src, dst)
        if key not in paths:
            paths[key] = dijkstra(mapdata, src, dst, road_events)
        return paths[key]

    for stop in active_stops:
        path = get_path(pickup_id, stop["location_id"])
        if path is None:
            unreachable.append(
                {
                    "stop_id": stop["id"],
                    "location_id": stop["location_id"],
                    "location_name": node_name(mapdata, stop["location_id"]),
                    "reason": "当前路况下从取货点无法到达该站点",
                }
            )
        else:
            reachable_stops.append(stop)

    if unreachable:
        warnings.append("存在不可达站点，未纳入路线；请开放道路或取消该站点后重新规划。")

    if not reachable_stops:
        return PlanResult(
            ordered_stops=[],
            route_node_ids=[pickup_id],
            route_edge_ids=[],
            polyline=[[mapdata.nodes[pickup_id]["x"], mapdata.nodes[pickup_id]["y"]]],
            estimated_duration_seconds=0.0,
            assumptions=assumptions,
            unreachable_stops=unreachable,
            warnings=warnings + ["所有配送站点均不可达，未生成可行路线。"],
            feasible=False,
        )

    deadline_at = _deadline_datetime(task.get("deadline"), task.get("deadline_kind", "none"), start)
    urgent_ids = {stop["id"] for stop in reachable_stops if stop.get("priority") == "urgent"}

    best: dict[str, Any] | None = None
    for order in permutations(reachable_stops):
        urgent_positions = [index for index, stop in enumerate(order) if stop["id"] in urgent_ids]
        normal_positions = [index for index, stop in enumerate(order) if stop["id"] not in urgent_ids]
        if urgent_positions and normal_positions and max(urgent_positions) > min(normal_positions):
            continue

        cursor = pickup_id
        moment = start
        total_cost = 0.0
        leg_nodes: list[str] = []
        leg_edges: list[str] = []
        legs: list[dict[str, Any]] = []
        blocked = False
        for stop in order:
            path = get_path(cursor, stop["location_id"])
            if path is None:
                blocked = True
                break
            moment = moment + timedelta(seconds=path.cost / SPEED_MPS)
            total_cost += path.cost
            eta = moment
            moment = moment + timedelta(seconds=DEFAULT_DWELL_SECONDS)
            total_cost += DEFAULT_DWELL_SECONDS * SPEED_MPS
            leg_edges.extend(path.edge_ids)
            legs.append({"stop": stop, "path": path, "eta": eta})
            cursor = stop["location_id"]
        if blocked:
            continue

        last_eta = legs[-1]["eta"]
        lateness = (last_eta - deadline_at).total_seconds() if deadline_at else 0.0
        score = (max(0.0, lateness), total_cost)
        if best is None or score < best["score"]:
            best = {"order": order, "legs": legs, "last_eta": last_eta, "lateness": lateness, "score": score, "leg_edges": leg_edges}

    if best is None:
        return PlanResult(
            ordered_stops=[],
            route_node_ids=[pickup_id],
            route_edge_ids=[],
            polyline=[[mapdata.nodes[pickup_id]["x"], mapdata.nodes[pickup_id]["y"]]],
            estimated_duration_seconds=0.0,
            assumptions=assumptions,
            unreachable_stops=unreachable,
            warnings=warnings + ["在优先级约束下无法生成完整路线。"],
            feasible=False,
        )

    ordered_stops = []
    route_nodes: list[str] = [pickup_id]
    route_edges: list[str] = []
    polyline: list[list[float]] = [[mapdata.nodes[pickup_id]["x"], mapdata.nodes[pickup_id]["y"]]]
    elapsed = 0.0
    for seq, leg in enumerate(best["legs"], start=1):
        stop = leg["stop"]
        path: PathResult = leg["path"]
        elapsed += path.cost / SPEED_MPS + DEFAULT_DWELL_SECONDS
        route_edges.extend(path.edge_ids)
        for node_id in path.node_ids[1:]:
            if node_id not in route_nodes or node_id == stop["location_id"]:
                if not route_nodes or route_nodes[-1] != node_id:
                    route_nodes.append(node_id)
        for point in path.polyline:
            if not polyline or polyline[-1] != point:
                polyline.append(point)
        ordered_stops.append(
            {
                "stop_id": stop["id"],
                "location_id": stop["location_id"],
                "location_name": node_name(mapdata, stop["location_id"]),
                "items": stop.get("items", []),
                "priority": stop.get("priority", "normal"),
                "seq": seq,
                "eta": _fmt_time(leg["eta"]),
                "status": stop.get("status", "pending"),
            }
        )

    if deadline_at and best["lateness"] > 0:
        warnings.append(
            f"按当前路况预计最后一站 {_fmt_time(best['last_eta'])} 到达，晚于截止时间 {_fmt_time(deadline_at)}，"
            "无法保证按时完成，请调整截止时间或减少站点。"
        )
        assumptions.append("该计划不满足截止时间要求，已如实标注，未生成虚假可行结论。")
    elif deadline_at:
        assumptions.append(f"预计最后一站 {_fmt_time(best['last_eta'])} 到达，满足 {_fmt_time(deadline_at)} 截止要求。")

    return PlanResult(
        ordered_stops=ordered_stops,
        route_node_ids=route_nodes,
        route_edge_ids=route_edges,
        polyline=polyline,
        estimated_duration_seconds=elapsed,
        assumptions=assumptions,
        unreachable_stops=unreachable,
        warnings=warnings,
        feasible=not unreachable,
    )
