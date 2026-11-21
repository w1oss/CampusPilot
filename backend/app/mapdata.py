import json
from functools import lru_cache
from typing import Any

from .config import MAP_PATH


class MapDataError(ValueError):
    pass


class MapData:
    def __init__(self, raw: dict[str, Any]):
        self.raw = raw
        self.map_id: str = raw["map_id"]
        self.map_version: str = raw["map_version"]
        self.width: int = int(raw["coordinate_system"]["width"])
        self.height: int = int(raw["coordinate_system"]["height"])
        self.data_notice: str = raw.get("data_notice", "")
        self.nodes: dict[str, dict[str, Any]] = {n["id"]: n for n in raw["nodes"]}
        self.edges: dict[str, dict[str, Any]] = {e["id"]: e for e in raw["edges"]}
        self._validate()

    def _validate(self) -> None:
        if len(self.nodes) != len(self.raw["nodes"]):
            raise MapDataError("node id duplicated")
        if len(self.edges) != len(self.raw["edges"]):
            raise MapDataError("edge id duplicated")
        for edge in self.edges.values():
            if edge["from"] not in self.nodes or edge["to"] not in self.nodes:
                raise MapDataError(f"edge {edge['id']} references unknown node")
            if edge["from"] == edge["to"]:
                raise MapDataError(f"edge {edge['id']} self loop")
            if len(edge.get("polyline", [])) < 2:
                raise MapDataError(f"edge {edge['id']} polyline too short")
            for x, y in edge["polyline"]:
                if not (0 <= x <= self.width and 0 <= y <= self.height):
                    raise MapDataError(f"edge {edge['id']} polyline out of bounds")

    def location_nodes(self) -> list[dict[str, Any]]:
        return [n for n in self.nodes.values() if n.get("kind") == "location"]

    def is_location(self, node_id: str) -> bool:
        node = self.nodes.get(node_id)
        return bool(node and node.get("kind") == "location")

    def alias_index(self) -> dict[str, str]:
        index: dict[str, str] = {}
        for node in self.nodes.values():
            names = [node["name"], *node.get("aliases", [])]
            for name in names:
                if name:
                    index.setdefault(name, node["id"])
        return dict(sorted(index.items(), key=lambda item: len(item[0]), reverse=True))

    def to_public_dict(self) -> dict[str, Any]:
        payload = {
            "map_id": self.map_id,
            "map_version": self.map_version,
            "data_notice": self.data_notice,
            "coordinate_system": self.raw["coordinate_system"],
            "nodes": self.raw["nodes"],
            "edges": self.raw["edges"],
        }
        if self.raw.get("background_image"):
            payload["background_image"] = self.raw["background_image"]
        if self.raw.get("background"):
            payload["background"] = self.raw["background"]
        return payload


@lru_cache(maxsize=4)
def load_map(path: str | None = None) -> MapData:
    target = path or str(MAP_PATH)
    with open(target, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    return MapData(raw)


def node_name(mapdata: MapData, node_id: str) -> str:
    node = mapdata.nodes.get(node_id)
    return node["name"] if node else node_id