"""Ordinary reports share the simulated observation/recovery lifecycle."""
import json
from pathlib import Path

from app import db, main, scene_sim, semantic_bridge as bridge, service
from app.mapdata import MapData
from test_semantic_bridge import isolated_world, driving_task, action, admin_report


def place_car(task_id, near=True):
    action(task_id, "pause")
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE execution_state SET current_edge_id = 'b', last_node_id = 'junction',"
            " position_x = 100, position_y = ?, edge_progress = ? WHERE task_id = ?",
            (5 if near else 100, 0.05 if near else 1, task_id),
        )


def test_scene_recovery_alone_does_not_clear_unobserved_whole_road(isolated_world):
    task_id = driving_task()
    place_car(task_id, near=False)
    admin_report(edge_fraction=None)
    with db.get_conn() as conn:
        scene_sim.advance_clock(conn, 60)
        assert conn.execute("SELECT present FROM obstacle_truth WHERE obstacle_id = 'admin:a'").fetchone()[0] == 0
        assert scene_sim.recheck_messages(conn) == []
        assert scene_sim.recheck_messages(conn) == []
        known = conn.execute("SELECT active,clear_evidence FROM road_known WHERE obstacle_id = 'admin:a'").fetchone()
        assert tuple(known) == (1, 0)
        conn.execute("UPDATE execution_state SET position_y = 5 WHERE task_id = ?", (task_id,))
        assert scene_sim.recheck_messages(conn) == []  # First explicit clear observation.
        messages = scene_sim.recheck_messages(conn)
    assert len(messages) == 1
    bridge.receive(messages[0])
    bundle = service.get_bundle(task_id)
    assert bundle["semantic_obstacles"] == []
    assert bundle["task"]["status"] == "paused"  # Preserve manual pause.
    assert "a" not in bundle["road_status"]
    with db.get_conn() as conn:
        known = conn.execute("SELECT source,active,clear_evidence FROM road_known WHERE obstacle_id = 'admin:a'").fetchone()
        assert tuple(known) == ("admin", 0, 2)
        assert conn.execute("SELECT COUNT(*) FROM road_events WHERE edge_id='a'").fetchone()[0] == 1


def test_old_indefinite_reports_get_one_finite_scene_deadline(isolated_world):
    admin_report(edge_fraction=None)
    with db.get_conn() as conn:
        conn.execute("UPDATE scenes SET virtual_seconds = 10000 WHERE id = 'sim_default'")
        conn.execute("UPDATE obstacle_truth SET clear_at_virtual = NULL")
        scene_sim.advance_clock(conn, 1)
        scene_sim.advance_clock(conn, 1)
        truth = conn.execute("SELECT present,clear_at_virtual FROM obstacle_truth").fetchone()
        assert tuple(truth) == (1, 13600)  # Does not slide on each tick.
        scene_sim.advance_clock(conn, 58)
        assert conn.execute("SELECT present FROM obstacle_truth").fetchone()[0] == 0
        assert conn.execute("SELECT active FROM road_known").fetchone()[0] == 1


def test_scene_reset_preserves_remaining_admin_lifetime(isolated_world):
    with db.get_conn() as conn:
        conn.execute("UPDATE scenes SET virtual_seconds = 10000 WHERE id = 'sim_default'")
    admin_report(edge_fraction=None)
    with db.get_conn() as conn:
        scene_sim.advance_clock(conn, 10)
        scene_sim.reset_scene(conn, "sim_default")
        assert conn.execute("SELECT clear_at_virtual FROM obstacle_truth").fetchone()[0] == 3000
        scene_sim.advance_clock(conn, 49)
        assert conn.execute("SELECT present FROM obstacle_truth").fetchone()[0] == 1
        scene_sim.advance_clock(conn, 1)
        assert conn.execute("SELECT present FROM obstacle_truth").fetchone()[0] == 0
        assert conn.execute("SELECT active FROM road_known").fetchone()[0] == 1


def test_real_reports_do_not_receive_simulated_expiry(isolated_world):
    task_id = driving_task(mode="real")
    admin_report(edge_fraction=None, scene_id="real_default")
    with db.get_conn() as conn:
        scene_sim.advance_clock(conn, 1000)
        truth = conn.execute("SELECT present,clear_at_virtual FROM obstacle_truth WHERE scene_id = 'real_default'").fetchone()
        assert tuple(truth) == (1, None)
        assert scene_sim.recheck_messages(conn) == []
    assert service.get_bundle(task_id)["road_status"]["a"] == "blocked"


def test_machine_recovery_cannot_remove_forced_prohibition(isolated_world):
    task_id = driving_task()
    place_car(task_id)
    admin_report(edge_fraction=None)
    admin_report(edge_fraction=None, authority="hard")
    with db.get_conn() as conn:
        scene_sim.advance_clock(conn, 60)
        assert scene_sim.recheck_messages(conn) == []
        messages = scene_sim.recheck_messages(conn)
    bridge.receive(messages[0])
    bundle = service.get_bundle(task_id)
    assert bundle["semantic_obstacles"] == []
    assert bundle["road_status"]["a"] == "blocked"
    assert bundle["road_marks"]["a"]["authority"] == "hard"


def test_report_again_restarts_evidence_and_can_be_cleared_again(isolated_world):
    task_id = driving_task()
    place_car(task_id)
    for _ in range(2):
        admin_report(edge_fraction=None)
        with db.get_conn() as conn:
            known = conn.execute("SELECT active,clear_evidence FROM road_known").fetchone()
            assert tuple(known) == (1, 0)
            assert conn.execute("SELECT present FROM obstacle_truth").fetchone()[0] == 1
            scene_sim.advance_clock(conn, 60)
            assert scene_sim.recheck_messages(conn) == []
            messages = scene_sim.recheck_messages(conn)
        response = bridge.receive(messages[0])
        assert response["accepted"]
        assert service.get_bundle(task_id)["semantic_obstacles"] == []


def test_e0074_whole_road_can_be_cleared_from_g42_without_entering_it(isolated_world, monkeypatch):
    data = MapData(json.loads((Path(__file__).parent / "fixtures" / "campus_map_v4.json").read_text(encoding="utf-8")))
    for module in (service, bridge, main, scene_sim):
        monkeypatch.setattr(module, "load_map", lambda: data)
    task_id = service.create_task({"mode": "sim", "pickup_location_id": "cs_building", "stops": [
        {"location_id": "library", "items": ["文件"], "priority": "normal"}
    ]})["task"]["id"]
    service.generate_plan(task_id)
    for name in ("start", "confirm_pickup", "pause"):
        action(task_id, name)
    node = data.nodes["g42"]
    with db.get_conn() as conn:
        conn.execute("UPDATE execution_state SET current_edge_id='e0041',last_node_id='g41',"
                     " position_x=?,position_y=?,edge_progress=1 WHERE task_id=?", (node["x"], node["y"], task_id))
    admin_report(edge_fraction=None, edge_id="e0074")
    with db.get_conn() as conn:
        assert scene_sim.recheck_messages(conn) == []  # Closure still present.
        scene_sim.advance_clock(conn, 60)
        assert scene_sim.recheck_messages(conn) == []
        assert conn.execute("SELECT clear_evidence FROM road_known").fetchone()[0] == 1
        messages = scene_sim.recheck_messages(conn)
    bridge.receive(messages[0])
    bundle = service.get_bundle(task_id)
    assert bundle["semantic_obstacles"] == []
    assert "e0074" not in bundle["road_status"]
    assert bundle["state"]["current_edge_id"] == "e0041"
