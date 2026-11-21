import heapq
from dataclasses import dataclass, field
from typing import Any

from .config import CONGESTED_FACTOR
from .mapdata import MapData


@dataclass
class PathResult:
    node_ids: list[str]
    edge_ids: list[str]
    distance_m: float
    cost: float
    polyline: list[list[float]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_ids": self.node_ids,
            "edge_ids": self.edge_ids,
            "distance_m": round(self.distance_m, 1),
            "polyline": self.polyline,
        }


def effective_status(edge: dict[str, Any], road_events: dict[str, str] | None) -> str:
    if road_events and edge["id"] in road_events:
        return road_events[edge["id"]]
    return edge.get("base_status", "open")


def build_adjacency(mapdata: MapData, road_events: dict[str, str] | None = None) -> dict[str, list[dict[str, Any]]]:
    adjacency: dict[str, list[dict[str, Any]]] = {node_id: [] for node_id in mapdata.nodes}
    for edge in mapdata.edges.values():
        status = effective_status(edge, road_events)
        if status == "blocked":
            continue
        factor = CONGESTED_FACTOR if status == "congested" else 1.0
        weight = edge["distance_m"] * factor
        forward = {"edge_id": edge["id"], "to": edge["to"], "weight": weight, "status": status}
        adjacency[edge["from"]].append(forward)
        if edge.get("bidirectional", True):
            backward = {"edge_id": edge["id"], "to": edge["from"], "weight": weight, "status": status}
            adjacency[edge["to"]].append(backward)
    return adjacency


def dijkstra(
    mapdata: MapData,
    start: str,
    goal: str,
    road_events: dict[str, str] | None = None,
) -> PathResult | None:
    if start == goal:
        return PathResult([start], [], 0.0, 0.0, [[mapdata.nodes[start]["x"], mapdata.nodes[start]["y"]]])
    if start not in mapdata.nodes or goal not in mapdata.nodes:
        return None
    adjacency = build_adjacency(mapdata, road_events)
    dist: dict[str, float] = {start: 0.0}
    prev: dict[str, tuple[str, str]] = {}
    heap: list[tuple[float, str]] = [(0.0, start)]
    visited: set[str] = set()
    while heap:
        cost, node = heapq.heappop(heap)
        if node in visited:
            continue
        visited.add(node)
        if node == goal:
            break
        for link in adjacency[node]:
            nxt = link["to"]
            new_cost = cost + link["weight"]
            if new_cost < dist.get(nxt, float("inf")):
                dist[nxt] = new_cost
                prev[nxt] = (node, link["edge_id"])
                heapq.heappush(heap, (new_cost, nxt))
    if goal not in visited:
        return None
    node_ids = [goal]
    edge_ids: list[str] = []
    cursor = goal
    while cursor != start:
        parent, edge_id = prev[cursor]
        edge_ids.append(edge_id)
        node_ids.append(parent)
        cursor = parent
    node_ids.reverse()
    edge_ids.reverse()
    polyline: list[list[float]] = []
    for index, edge_id in enumerate(edge_ids):
        edge = mapdata.edges[edge_id]
        points = [list(map(float, p)) for p in edge["polyline"]]
        if edge["from"] != node_ids[index]:
            points.reverse()
        if polyline and _close(polyline[-1], points[0]):
            points = points[1:]
        polyline.extend(points)
    return PathResult(node_ids, edge_ids, dist[goal] / _average_factor(edge_ids, mapdata, road_events), dist[goal], polyline)


def _close(a: list[float], b: list[float]) -> bool:
    return abs(a[0] - b[0]) < 1e-6 and abs(a[1] - b[1]) < 1e-6


def _average_factor(edge_ids: list[str], mapdata: MapData, road_events: dict[str, str] | None) -> float:
    total = sum(mapdata.edges[e]["distance_m"] for e in edge_ids)
    if total <= 0:
        return 1.0
    weighted = sum(
        mapdata.edges[e]["distance_m"] * (CONGESTED_FACTOR if effective_status(mapdata.edges[e], road_events) == "congested" else 1.0)
        for e in edge_ids
    )
    return weighted / total


def travel_seconds(distance_m: float, speed_mps: float) -> float:
    if speed_mps <= 0:
        raise ValueError("speed must be positive")
    return distance_m / speed_mps