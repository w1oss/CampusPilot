"""Real service/API tests for asynchronous messages and mid-edge execution."""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import db, main, scene_sim, semantic_bridge as bridge, service
from app.mapdata import MapData
from app.schemas import SemanticEventRequest
from app.simulator import advance


@pytest.fixture(autouse=True)
def isolated_world(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "semantic.db")
    nodes = [
        {"id": "pickup", "name": "取货点", "kind": "location", "x": 0, "y": 0},
        {"id": "junction", "name": "路口", "kind": "junction", "x": 100, "y": 0},
        {"id": "library", "name": "图书馆", "kind": "location", "x": 100, "y": 100},
        {"id": "detour", "name": "绕行路口", "kind": "junction", "x": 0, "y": 100},
    ]
    edges = []
    by_id = {n["id"]: n for n in nodes}
    for edge_id, source, target, distance in [
        ("a", "pickup", "junction", 100), ("b", "junction", "library", 100),
        ("c", "pickup", "detour", 120), ("d", "detour", "library", 120),
    ]:
        edges.append({"id": edge_id, "name": edge_id, "from": source, "to": target,
                      "distance_m": distance, "bidirectional": True, "base_status": "open",
                      "polyline": [[by_id[source]["x"], by_id[source]["y"]], [by_id[target]["x"], by_id[target]["y"]]]})
    mapdata = MapData({"map_id": "test", "map_version": "test-v1", "coordinate_system": {"width": 100, "height": 100},
                       "nodes": nodes, "edges": edges})
    for module in (service, bridge, main, scene_sim):
        monkeypatch.setattr(module, "load_map", lambda: mapdata)
    monkeypatch.setattr(bridge, "SEMANTIC_BRIDGE_ENABLED", True)
    db.init_db()
    return mapdata


def action(task_id, name):
    return service.perform_action(task_id, {"action": name, "request_id": f"req_{uuid.uuid4().hex}", "plan_version": 0})


def driving_task(mode="sim"):
    task_id = service.create_task({"mode": mode, "pickup_location_id": "pickup", "stops": [
        {"location_id": "library", "items": ["文件"], "priority": "normal"}
    ]})["task"]["id"]
    service.generate_plan(task_id)
    action(task_id, "start")
    action(task_id, "confirm_pickup")
    if mode == "sim":
        service.sim_tick(task_id, 4, 10)
    return task_id


def event(task_id, **changes):
    return SemanticEventRequest(**{
        "event_id": f"event_{uuid.uuid4().hex}", "task_id": task_id,
        "obstacle_id": "barrier", "revision": 1, "map_version": "test-v1",
        "edge_id": "a", "edge_fraction": 0.7, "category": "construction",
        "passability": "blocked", "summary": "前方存在施工围挡", **changes,
    }).model_dump(mode="json")


def test_mid_edge_replan_preserves_pose_and_executes_real_detour(isolated_world):
    task_id = driving_task()
    before = service.get_bundle(task_id)
    assert before["state"]["position"] == {"x": 40, "y": 0}
    result = bridge.receive(event(task_id))
    bundle = result["bundle"]
    assert result["decision"]["action"] == "replan"
    assert bundle["state"]["position"] == before["state"]["position"]
    assert bundle["plan"]["polyline"][0] == [40, 0]
    assert bundle["plan"]["route_edge_ids"] == ["a", "c", "d"]
    assert bundle["state"]["last_node_id"] == "junction"  # reverse, no teleport
    assert bundle["state"]["edge_progress"] == pytest.approx(0.6)
    assert bundle["task"]["status"] == "in_progress"
    after = service.sim_tick(task_id, 1, 10)
    assert after["state"]["position"] == {"x": 30, "y": 0}
    final = service.sim_tick(task_id, 100, 10)
    assert final["sim"]["waiting"] == "awaiting_delivery"
    assert final["state"]["position"] == {"x": 100, "y": 100}
    assert "__current_position" not in isolated_world.nodes
    assert all(not key.startswith("__pose") for key in isolated_world.edges)
    roles = [trace["agent"] for trace in bundle["agent_traces"]]
    assert "task_planner" in roles and "road_observer" in roles and "dispatcher" in roles


def test_short_obstacle_wait_clear_and_manual_pause_ownership():
    task_id = driving_task()
    payload = event(task_id, category="pedestrian", expected_duration_s=5)
    waiting = bridge.receive(payload)["bundle"]
    assert waiting["task"]["status"] == "paused"
    assert waiting["runtime"]["auto_resume"] is True
    cleared = event(task_id, revision=2, lifecycle="cleared", passability="open")
    resumed = bridge.receive(cleared)["bundle"]
    assert resumed["task"]["status"] == "in_progress"
    assert resumed["state"]["position"] == {"x": 40, "y": 0}

    bridge.receive(event(task_id, revision=3, category="pedestrian", expected_duration_s=5))
    action(task_id, "pause")  # take ownership while already automatically paused
    held = bridge.receive(event(task_id, revision=4, lifecycle="cleared", passability="open"))["bundle"]
    assert held["task"]["status"] == "paused"
    assert held["runtime"]["auto_resume"] is False


def test_multiple_obstacles_do_not_clear_each_other():
    task_id = driving_task()
    bridge.receive(event(task_id, category="pedestrian", expected_duration_s=5))
    bridge.receive(event(task_id, obstacle_id="second", category="pedestrian", expected_duration_s=5, edge_fraction=0.8))
    result = bridge.receive(event(task_id, revision=2, lifecycle="cleared", passability="open"))["bundle"]
    assert result["task"]["status"] == "paused"
    assert result["road_status"]["a"] == "blocked"
    assert len(result["semantic_obstacles"]) == 1
    result = bridge.receive(event(task_id, obstacle_id="second", revision=2, lifecycle="cleared", passability="open"))["bundle"]
    assert result["task"]["status"] == "in_progress"
    assert "a" not in result["road_status"]


def test_duplicates_stale_revisions_and_event_collision():
    task_id = driving_task()
    payload = event(task_id)
    first = bridge.receive(payload)
    replay = bridge.receive(payload)
    assert replay["duplicate"] is True
    assert replay["bundle"]["task"]["plan_version"] == first["bundle"]["task"]["plan_version"]
    stale = bridge.receive(event(task_id, revision=1, lifecycle="cleared", passability="open"))
    assert stale["accepted"] is False
    with pytest.raises(service.ApiError, match="同一事件编号"):
        bridge.receive({**payload, "summary": "changed"})
    expired = bridge.receive(event(task_id, revision=2, observed_at=(datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()))
    assert expired["accepted"] is False


def test_off_route_and_behind_messages_do_not_replan():
    task_id = driving_task()
    unrelated = bridge.receive(event(task_id, edge_id="c"))
    assert unrelated["bundle"]["task"]["plan_version"] == 1
    behind = bridge.receive(event(task_id, obstacle_id="behind", edge_fraction=0.2))
    assert behind["bundle"]["task"]["plan_version"] == 1
    assert service.sim_tick(task_id, 1, 10)["state"]["position"] == {"x": 50, "y": 0}


def test_unknown_geometry_and_one_way_keep_pose_and_report_unreachable(isolated_world):
    task_id = driving_task()
    result = bridge.receive(event(task_id, edge_fraction=None))["bundle"]
    assert result["task"]["status"] == "blocked"
    assert result["plan"]["feasible"] is False
    assert result["state"]["position"] == {"x": 40, "y": 0}
    assert result["plan"]["unreachable_stops"]
    clear = bridge.receive(event(task_id, revision=2, lifecycle="cleared", passability="open"))["bundle"]
    assert clear["task"]["status"] == "in_progress"
    assert clear["state"]["position"] == {"x": 40, "y": 0}
    isolated_world.edges["a"]["bidirectional"] = False
    result = bridge.receive(event(task_id, revision=3))["bundle"]
    assert result["task"]["status"] == "blocked"
    assert result["state"]["position"] == {"x": 40, "y": 0}


def test_scene_sharing_and_real_mode_isolation():
    first, second, real = driving_task(), driving_task(), driving_task("real")
    bridge.receive(event(first))
    assert service.get_bundle(second)["road_status"]["a"] == "blocked"
    assert "a" not in service.get_bundle(real)["road_status"]
    with pytest.raises(service.ApiError, match="仅作用于指定模拟任务"):
        bridge.receive(event(real))


def test_untrusted_clear_cannot_remove_confirmed_obstacle():
    task_id = driving_task()
    bridge.receive(event(task_id, category="pedestrian", expected_duration_s=5))
    result = bridge.receive(event(task_id, revision=2, lifecycle="cleared", passability="open", confidence=0.2))
    assert result["accepted"] is False
    assert result["bundle"]["road_status"]["a"] == "blocked"
    assert result["bundle"]["task"]["status"] == "paused"


def test_stop_is_committed_before_decision_and_survives_planner_failure(monkeypatch):
    task_id = driving_task()
    payload = event(task_id)
    original = service.replan
    seen = []

    def fail(*args, **kwargs):
        seen.append(service.get_bundle(task_id)["runtime"]["emergency_hold"])
        raise RuntimeError("planner unavailable")

    monkeypatch.setattr(service, "replan", fail)
    with pytest.raises(RuntimeError):
        bridge.receive(payload)
    assert seen == [True]
    stopped = service.get_bundle(task_id)
    assert stopped["task"]["status"] == "paused"
    assert stopped["runtime"]["emergency_hold"] is True
    assert stopped["runtime"]["auto_resume"] is True
    assert stopped["state"]["position"] == {"x": 40, "y": 0}
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM semantic_events").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM semantic_obstacles").fetchone()[0] == 1
    assert stopped["task"]["plan_version"] == 1
    monkeypatch.setattr(service, "replan", original)
    recovered = bridge.receive(payload)
    assert recovered["duplicate"] is True
    assert recovered["accepted"] is True
    assert recovered["bundle"]["task"]["status"] == "in_progress"
    assert recovered["bundle"]["runtime"]["emergency_hold"] is False


def test_backend_moves_without_browser_and_receives_mid_motion_messages():
    async def run():
        async with main.app.router.lifespan_context(main.app):
            task_id = driving_task()
            bridge.set_runtime(task_id, True, 10)
            await asyncio.sleep(0.3)
            moving = service.get_bundle(task_id)
            assert moving["state"]["position"]["x"] > 40
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as http:
                blocked_tick = await http.post(f"/api/tasks/{task_id}/sim/tick", json={"seconds": 1})
                assert blocked_tick.status_code == 409
                response = await http.post("/api/semantic-events", json=event(task_id, category="pedestrian", expected_duration_s=5))
                assert response.status_code == 200, response.text
                stopped = response.json()["bundle"]
                assert stopped["task"]["status"] == "paused"
                await asyncio.sleep(0.2)
                assert service.get_bundle(task_id)["state"]["position"] == stopped["state"]["position"]
                await http.post("/api/semantic-events", json=event(task_id, revision=2, lifecycle="cleared", passability="open"))
                await asyncio.sleep(0.2)
                assert service.get_bundle(task_id)["state"]["position"]["x"] > stopped["state"]["position"]["x"]
            bridge.set_runtime(task_id, False, 10)
    asyncio.run(run())


def test_manual_replan_is_idempotent_and_preserves_mid_edge_position():
    task_id = driving_task()
    first = service.replan(task_id, "人工重规划", "request_replan_once", 1)
    again = service.replan(task_id, "人工重规划", "request_replan_once", 1)
    assert first["task"]["plan_version"] == again["task"]["plan_version"] == 2
    assert first["state"]["position"] == {"x": 40, "y": 0}
    with pytest.raises(service.ApiError, match="计划版本"):
        service.replan(task_id, "stale", "another_request", 1)


def test_repeated_edges_in_execution_route_are_not_removed(isolated_world):
    state = {"current_edge_id": "a", "edge_progress": 0, "last_node_id": "pickup", "position_x": 0,
             "position_y": 0, "_remaining_edges": ["a", "b", "b", "a"]}
    outcome = advance(isolated_world, {"pickup_location_id": "pickup", "pickup_status": "picked_up"}, [], state, 1, 10)
    assert outcome["remaining_edges"] == ["a", "b", "b", "a"]


def test_low_confidence_update_cannot_move_a_confirmed_barrier_off_route():
    task_id = driving_task()
    bridge.receive(event(task_id, category="pedestrian", expected_duration_s=5))
    uncertain = bridge.receive(event(task_id, revision=2, edge_id="c", confidence=0.2))
    assert uncertain["accepted"] is False
    assert uncertain["bundle"]["task"]["status"] == "paused"
    assert uncertain["bundle"]["semantic_obstacles"][0]["edge_id"] == "a"


def test_wait_timeout_rechecks_route_instead_of_assuming_clear():
    task_id = driving_task()
    result = bridge.receive(event(task_id, category="pedestrian", expected_duration_s=5,
        observed_at=(datetime.now(timezone.utc) - timedelta(seconds=11)).isoformat()))
    assert result["decision"]["action"] == "replan"
    assert result["bundle"]["road_status"]["a"] == "blocked"
    assert result["bundle"]["state"]["position"] == {"x": 40, "y": 0}


def test_disabled_adapter_retains_manual_simulation(monkeypatch):
    task_id = driving_task()
    bridge.set_runtime(task_id, True, 10)
    monkeypatch.setattr(bridge, "SEMANTIC_BRIDGE_ENABLED", False)
    monkeypatch.setattr(service, "SEMANTIC_BRIDGE_ENABLED", False)
    with pytest.raises(service.ApiError, match="未启用"):
        bridge.receive(event(task_id))
    assert service.sim_tick(task_id, 1, 10)["state"]["position"] == {"x": 50, "y": 0}


def test_repeated_observations_do_not_generate_repeated_plans():
    task_id = driving_task()
    bridge.receive(event(task_id, category="crowd", passability="congested"))
    version = service.get_bundle(task_id)["task"]["plan_version"]
    for revision in range(2, 5):
        bridge.receive(event(task_id, revision=revision, category="crowd", passability="congested"))
    assert service.get_bundle(task_id)["task"]["plan_version"] == version


def test_initial_plan_cannot_reset_a_moving_vehicle():
    task_id = driving_task()
    with pytest.raises(service.ApiError, match="保留当前位置"):
        service.generate_plan(task_id)
    assert service.get_bundle(task_id)["state"]["position"] == {"x": 40, "y": 0}


def test_automatic_source_observes_and_clears_without_admin_or_browser():
    task_id = driving_task()
    bridge.set_runtime(task_id, True, 10, True, "pedestrian")
    bridge.step_runtime(3)
    stopped = service.get_bundle(task_id)
    assert stopped["task"]["status"] == "paused"
    assert stopped["upstream_simulation"]["generated_count"] == 1
    assert stopped["semantic_obstacles"][0]["edge_id"] == "a"
    assert stopped["semantic_obstacles"][0]["edge_fraction"] > 0.4
    assert stopped["state"]["position"] == {"x": 40, "y": 0}
    bridge.step_runtime(3)
    cleared = service.get_bundle(task_id)
    assert cleared["task"]["status"] == "in_progress"
    assert cleared["semantic_obstacles"] == []
    assert cleared["state"]["position"]["x"] > 40
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM semantic_events").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM road_events").fetchone()[0] == 0
    bridge.step_runtime(1)
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM semantic_events").fetchone()[0] == 2


def test_manual_pause_survives_recheck_and_source_restart():
    task_id = driving_task()
    bridge.set_runtime(task_id, True, 10, True)
    bridge.step_runtime(3)
    action(task_id, "pause")
    bridge.set_runtime(task_id, True, 10, False)
    bridge.step_runtime(3)
    checked = service.get_bundle(task_id)
    assert checked["semantic_obstacles"] == []
    assert checked["task"]["status"] == "paused"
    bridge.set_runtime(task_id, True, 10, True)
    bridge.step_runtime(3)
    assert service.get_bundle(task_id)["task"]["status"] == "paused"


def test_speed_changes_do_not_rearm_automatic_source():
    task_id = driving_task()
    bridge.set_runtime(task_id, True, 10, True)
    bridge.step_runtime(1)
    elapsed = service.get_bundle(task_id)["upstream_simulation"]["elapsed"]
    bridge.set_runtime(task_id, True, 15, True)
    assert service.get_bundle(task_id)["upstream_simulation"]["elapsed"] == elapsed


def test_external_source_does_not_enable_automatic_simulator():
    task_id = driving_task()
    bridge.set_runtime(task_id, True, 10)
    bridge.step_runtime(3)
    assert service.get_bundle(task_id)["upstream_simulation"]["enabled"] is False
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM semantic_events").fetchone()[0] == 0


def test_changing_speed_before_first_start_keeps_default_source_unconfigured():
    task_id = driving_task()
    bridge.set_runtime(task_id, False, 5)
    assert service.get_bundle(task_id)["upstream_simulation"]["configured"] is False
    bridge.set_runtime(task_id, True, 5, True)
    assert service.get_bundle(task_id)["upstream_simulation"]["enabled"] is True

def test_source_stops_the_vehicle_immediately_when_enabled():
    task_id = driving_task()
    bridge.set_runtime(task_id, True, 10, True, "pedestrian")
    bridge.step_runtime(0.1)
    bundle = service.get_bundle(task_id)
    assert bundle["task"]["status"] == "paused"
    assert bundle["runtime"]["emergency_hold"] is True
    assert bundle["semantic_obstacles"]
    assert bundle["state"]["position"] == {"x": 40, "y": 0}


def test_construction_truth_ends_but_known_waits_for_recheck():
    task_id = driving_task()
    bridge.set_runtime(task_id, True, 10, True, "construction")
    bridge.step_runtime(3)
    action(task_id, "pause")
    started = service.get_bundle(task_id)
    assert started["semantic_obstacles"] and started["semantic_obstacles"][0]["active"]
    assert started["upstream_simulation"]["generated_count"] == 1
    with db.get_conn() as conn:
        conn.execute("UPDATE obstacle_truth SET present = 0")
        conn.execute(
            "UPDATE execution_state SET position_x = 100, position_y = 100, current_edge_id = 'b',"
            " last_node_id = 'junction', edge_progress = 0.5 WHERE task_id = ?",
            (task_id,),
        )
    bridge.step_runtime(1)
    held = service.get_bundle(task_id)
    assert held["semantic_obstacles"] and held["semantic_obstacles"][0]["active"]
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE execution_state SET position_x = 40, position_y = 0, current_edge_id = 'a',"
            " last_node_id = 'pickup', edge_progress = 0.4 WHERE task_id = ?",
            (task_id,),
        )
    bridge.step_runtime(1)
    cleared = service.get_bundle(task_id)
    assert cleared["semantic_obstacles"] == []

def test_manual_whole_road_is_not_auto_cleared_by_passing_nearby():
    task_id = driving_task()
    service.create_road_event({
        "edge_id": "a", "status": "blocked", "scene_id": "sim_default",
        "source": "admin", "authority": "normal", "confirmed_by": "operator", "note": "整段施工",
    })
    marks = service.get_bundle(task_id)["road_marks"]
    assert marks["a"]["sources"] == ["admin"]
    assert marks["a"]["position_known"] is False
    bridge.step_runtime(3)
    bridge.step_runtime(3)
    kept = service.get_bundle(task_id)
    assert any(o["obstacle_id"] == "admin:a" for o in kept["semantic_obstacles"])


def test_road_marks_keep_manual_and_machine_sources_on_same_edge():
    task_id = driving_task()
    service.create_road_event({
        "edge_id": "a", "status": "blocked", "scene_id": "sim_default",
        "source": "admin", "authority": "normal", "confirmed_by": "operator", "note": "人工施工",
    })
    bridge.receive(event(task_id, obstacle_id="machine-a", edge_fraction=0.7))
    marks = service.get_bundle(task_id)["road_marks"]["a"]
    assert set(marks["sources"]) == {"admin", "machine"}
    assert marks["position_known"] is True


def test_hard_block_cannot_be_cleared_by_machine_observation():
    task_id = driving_task()
    service.create_road_event({
        "edge_id": "a", "status": "blocked", "scene_id": "sim_default",
        "source": "admin", "authority": "hard", "confirmed_by": "operator", "note": "强制禁行",
    })
    bridge.receive(event(task_id, obstacle_id="machine-a", lifecycle="cleared", passability="open"))
    marks = service.get_bundle(task_id)["road_marks"]["a"]
    assert marks["authority"] == "hard"
    assert marks["status"] == "blocked"

def test_box_and_barrier_are_observed_and_locally_detoured():
    for category in ("box", "barrier"):
        task_id = driving_task()
        bridge.set_runtime(task_id, True, 10, True, category)
        bridge.step_runtime(3)
        bundle = service.get_bundle(task_id)
        assert bundle["upstream_simulation"]["generated_count"] == 1
        obstacle = bundle["semantic_obstacles"][0]
        assert obstacle["category"] == category
        assert obstacle["effective_status"] == "blocked"
        assert bundle["task"]["status"] in ("in_progress", "paused", "blocked")
        bridge.reset_scene_state("sim_default")

def admin_report(edge_fraction=0.5):
    return service.create_road_event({
        "edge_id": "a",
        "edge_fraction": edge_fraction,
        "status": "blocked",
        "scene_id": "sim_default",
        "source": "admin",
        "authority": "normal",
        "confirmed_by": "operator",
        "note": "管理员上报",
    })


def test_admin_report_needs_two_explicit_recovery_observations():
    task_id = driving_task()
    admin_report()
    other = driving_task()
    assert any(o["obstacle_id"] == "admin:a" for o in service.get_bundle(other)["semantic_obstacles"])
    service.script_clear_truth("sim_default", "admin:a")
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT active, clear_evidence FROM road_known WHERE obstacle_id = 'admin:a'"
        ).fetchone()
        assert row["active"] == 1
        assert row["clear_evidence"] == 0
    bridge.step_runtime(1)
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT active, clear_evidence FROM road_known WHERE obstacle_id = 'admin:a'"
        ).fetchone()
        assert row["active"] == 1
        assert row["clear_evidence"] == 1
    bridge.step_runtime(1)
    assert all(o["obstacle_id"] != "admin:a" for o in service.get_bundle(task_id)["semantic_obstacles"])


def test_admin_whole_road_not_cleared_after_two_observations():
    task_id = driving_task()
    admin_report(edge_fraction=None)
    service.script_clear_truth("sim_default", "admin:a")
    bridge.step_runtime(1)
    bridge.step_runtime(1)
    assert any(o["obstacle_id"] == "admin:a" for o in service.get_bundle(task_id)["semantic_obstacles"])


def test_default_reset_keeps_admin_reports_but_drops_machine_ones():
    task_id = driving_task()
    admin_report()
    bridge.set_runtime(task_id, True, 10, True, "pedestrian")
    bridge.step_runtime(3)
    bridge.reset_scene_state("sim_default")
    bundle = service.get_bundle(task_id)
    assert any(o["obstacle_id"] == "admin:a" for o in bundle["semantic_obstacles"])
    assert all(o["source"] != "mock_vision_llm" for o in bundle["semantic_obstacles"])
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM obstacle_truth WHERE obstacle_id = 'admin:a'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM obstacle_truth WHERE obstacle_id NOT LIKE 'admin:%'").fetchone()[0] == 0
