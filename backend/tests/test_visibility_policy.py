"""Visibility uses graph junctions, while small obstacles use local distance."""
import json
from pathlib import Path

import pytest

from app.mapdata import MapData
from app.scene_sim import observable, visible_road_directions
from app.simulator import advance, local_box_position


def crossroads(roads=None):
    nodes = [{"id": "centre", "x": 1500, "y": 1500, "kind": "waypoint"}]
    edges = []
    for name, x, y in (roads or [("n", 1500, 500), ("s", 1500, 2500), ("w", 500, 1500), ("e", 2500, 1500)]):
        nodes.append({"id": name, "x": x, "y": y})
        edges.append({"id": name, "from": "centre", "to": name, "distance_m": 280,
                      "polyline": [[1500, 1500], [x, y]], "base_status": "open"})
    # Geometrically close, but not joined to the same junction.
    nodes += [{"id": "p", "x": 1400, "y": 1510}, {"id": "q", "x": 1600, "y": 1510}]
    edges.append({"id": "parallel", "from": "p", "to": "q", "distance_m": 56,
                  "polyline": [[1400, 1510], [1600, 1510]]})
    return MapData({"map_id": "cross", "map_version": "1", "coordinate_system": {"width": 3000, "height": 3000},
                    "nodes": nodes, "edges": edges})


@pytest.mark.parametrize("category", ["construction", "barrier"])
def test_junction_sees_all_four_whole_incident_roads(category):
    data = crossroads()
    state = {"position_x": 1490, "position_y": 1500, "current_edge_id": "w", "last_node_id": "w"}
    for edge_id in ("n", "s", "w", "e"):
        assert observable(data, state, edge_id, 0.95, category)
    assert not observable(data, state, "parallel", 0.5, category)
    state["position_x"] = 1000  # Far from junction: no remote road inspection.
    assert not observable(data, state, "e", 0.95, category)
    state.update(position_x=1500, current_edge_id=None, last_node_id="centre")
    assert observable(data, state, "e", 0.95, category)


def test_box_has_ten_metre_radius_even_at_junction():
    data = crossroads()
    state = {"position_x": 1500, "position_y": 1500, "current_edge_id": "w", "last_node_id": "w"}
    assert observable(data, state, "e", 9.9 / 280, "box")
    assert not observable(data, state, "e", 10.1 / 280, "box")
    assert not observable(data, state, "e", 0.95, "box")


@pytest.mark.parametrize("roads", [
    [("n", 1500, 500), ("w", 500, 1500), ("e", 2500, 1500)],
    [("n", 1500, 500), ("sw", 650, 2350), ("se", 2350, 2350)],
    [("n", 1500, 500), ("e", 2500, 1500)],
], ids=["T-junction", "Y-junction", "bend"])
@pytest.mark.parametrize("category", ["construction", "barrier"])
def test_all_connected_directions_are_visible_for_any_connection_shape(roads, category):
    data = crossroads(roads)
    # A waypoint label is sufficient; no special junction tag is required.
    state = {"position_x": 1500, "position_y": 1490, "current_edge_id": "n", "last_node_id": "n"}
    expected = {name for name, _, _ in roads}
    # One-way/closed roads are still observable, allowing recovery checks.
    data.edges[roads[-1][0]].update(base_status="blocked", bidirectional=False)
    assert visible_road_directions(data, state) == expected
    for edge_id in expected:
        assert observable(data, state, edge_id, 0.95, category)
        assert not observable(data, state, edge_id, 0.95, "box")
    assert not observable(data, state, "parallel", 0.5, category)
    state.update(current_edge_id=None, last_node_id="centre", position_y=1500)
    assert visible_road_directions(data, state) == expected


def test_visibility_does_not_extend_through_the_next_connection():
    data = crossroads()
    data.nodes["beyond"] = {"id": "beyond", "x": 2500, "y": 2500}
    data.edges["second_connection"] = {"id": "second_connection", "from": "e", "to": "beyond",
                                       "distance_m": 280, "polyline": [[2500, 1500], [2500, 2500]]}
    state = {"position_x": 1490, "position_y": 1500, "current_edge_id": "w", "last_node_id": "w"}
    assert "second_connection" not in visible_road_directions(data, state)
    assert not observable(data, state, "second_connection", 0.1, "construction")


def test_campus_e0026_can_be_rechecked_from_adjacent_road():
    data = MapData(json.loads((Path(__file__).parent / "fixtures" / "campus_map_v4.json").read_text(encoding="utf-8")))
    edge = data.edges["e0026"]
    junction = edge["from"]
    adjacent = next(e for e in data.edges.values() if e["id"] != "e0026" and junction in (e["from"], e["to"]))
    node = data.nodes[junction]
    state = {"position_x": node["x"], "position_y": node["y"], "current_edge_id": adjacent["id"], "last_node_id": junction}
    assert not observable(data, state, "e0026", 0.28, "other")  # Previous 20 m rule cannot reach it.
    assert observable(data, state, "e0026", 0.28, "barrier")
    assert observable(data, state, "e0026", 0.95, "construction")


@pytest.mark.parametrize("reverse", [False, True])
def test_box_arc_moves_in_both_directions_and_rejoins_original_road(reverse):
    data = crossroads()
    edge = data.edges["e"]
    box = {"category": "box", "edge_id": "e", "edge_fraction": 0.5, "effective_status": "blocked"}
    start = "e" if reverse else "centre"
    task = {"pickup_location_id": start, "pickup_status": "collected"}
    state = {"current_edge_id": "e", "last_node_id": start, "edge_progress": 0.49,
             "position_x": 2000, "position_y": 1500, "_remaining_edges": ["e"]}
    result = advance(data, task, [], state, 0.28, 10, {}, [box], {})
    assert result["waiting"] is None
    assert result["edge_progress"] == pytest.approx(0.5)
    assert result["position"]["x"] == 2000
    assert result["position"]["y"] > 1500
    assert result["remaining_edges"] == ["e"]
    end = advance(data, task, [], state, 100, 10, {}, [box], {})
    expected = data.nodes["centre" if reverse else "e"]
    assert end["position"] == {"x": expected["x"], "y": expected["y"]}
    assert local_box_position(edge["polyline"], 0.4, [box]) == (1900, 1500)
    assert local_box_position(edge["polyline"], 0.6, [box]) == (2100, 1500)
