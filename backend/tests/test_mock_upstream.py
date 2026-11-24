from copy import deepcopy

import pytest

from mock_upstream import choose_demo_route, create_demo_task


def map_with_closed_pickup():
    return {
        "nodes": [{"id": n, "name": n, "kind": "location"} for n in ("cs_building", "library", "research")],
        "edges": [
            {"id": "entry", "from": "cs_building", "to": "library", "bidirectional": True},
            {"id": "other", "from": "research", "to": "library", "bidirectional": True},
        ],
        "road_status": {"entry": "blocked"},
    }


def test_demo_uses_reachable_pickup_and_preserves_live_map():
    live_map = map_with_closed_pickup()
    original = deepcopy(live_map)
    assert choose_demo_route(live_map) == ("research", "library")
    assert live_map == original
    live_map["road_status"] = {}
    assert choose_demo_route(live_map) == ("cs_building", "library")


def test_no_connected_locations_does_not_create_task():
    live_map = map_with_closed_pickup()
    live_map["road_status"]["other"] = "blocked"
    calls = []
    with pytest.raises(ValueError, match="未创建演示任务"):
        create_demo_task(lambda *args: calls.append(args), live_map)
    assert calls == []


def test_road_change_during_planning_does_not_start_unreachable_task():
    calls = []
    def request(path, payload):
        calls.append(path)
        return {"task": {"id": "demo"}} if path == "/api/tasks" else {"plan": {"feasible": False}}
    with pytest.raises(ValueError, match="暂未启动"):
        create_demo_task(request, map_with_closed_pickup())
    assert calls == ["/api/tasks", "/api/tasks/demo/plan"]


def test_demo_respects_one_way_roads():
    live_map = map_with_closed_pickup()
    live_map["edges"][1].update({"from": "library", "to": "research", "bidirectional": False})
    assert choose_demo_route(live_map) == ("library", "research")
