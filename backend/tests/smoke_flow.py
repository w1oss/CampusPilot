import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import service
from app.db import get_conn, init_db
from app.parser import parse_task

TEXT = "从计算机学院取3份实验材料，分别送到科研楼、实验室和学一楼，实验室材料优先，16:00前完成。"


def reset() -> None:
    with get_conn() as conn:
        for table in ("action_log", "task_events", "execution_state", "road_events", "plans", "stops", "tasks"):
            conn.execute(f"DELETE FROM {table}")


def main() -> None:
    init_db()
    reset()

    parsed = parse_task(TEXT, prefer_llm=False)
    print("parse.missing_fields =", parsed["missing_fields"])
    print("parse.draft =", parsed["draft"])
    assert parsed["draft"]["pickup_location_id"] == "cs_building"
    assert len(parsed["draft"]["stops"]) == 3
    assert parsed["draft"]["deadline"] == "16:00"
    urgent = [s for s in parsed["draft"]["stops"] if s["priority"] == "urgent"]
    assert urgent and urgent[0]["location_id"] == "lab_building"

    draft = parsed["draft"]
    bundle = service.create_task(
        {
            "mode": "sim",
            "pickup_location_id": draft["pickup_location_id"],
            "stops": draft["stops"],
            "deadline": draft["deadline"],
            "deadline_kind": draft["deadline_kind"],
            "raw_input": TEXT,
            "parser": parsed["parser"],
            "confirmed": True,
        }
    )
    task_id = bundle["task"]["id"]
    print("task =", task_id, bundle["task"]["status"])

    bundle = service.generate_plan(task_id)
    plan = bundle["plan"]
    print("plan_version =", plan["plan_version"])
    print("ordered =", [(s["location_name"], s["eta"]) for s in plan["ordered_stops"]])
    print("warnings =", plan["warnings"])
    assert plan["plan_version"] == 1
    assert len(plan["ordered_stops"]) == 3
    assert plan["ordered_stops"][0]["priority"] == "urgent", "优先站点必须排在最前"
    assert plan["route_edge_ids"]

    bundle = service.perform_action(task_id, {"action": "start", "request_id": "req_start_0001", "plan_version": 1})
    assert bundle["task"]["status"] == "in_progress"
    bundle = service.perform_action(task_id, {"action": "confirm_pickup", "request_id": "req_pick_00001", "plan_version": 1})
    assert bundle["task"]["pickup_status"] == "picked_up"

    first_stop = bundle["plan"]["ordered_stops"][0]["stop_id"]
    for _ in range(400):
        bundle = service.sim_tick(task_id, 0.5)
        if bundle["sim"]["waiting"] == "awaiting_delivery":
            break
    print("waiting =", bundle["sim"]["waiting"], "stops =", [(s["location_name"], s["status"]) for s in bundle["stops"]])
    assert bundle["sim"]["waiting"] == "awaiting_delivery"
    assert any(s["status"] == "arrived" for s in bundle["stops"])

    bundle = service.perform_action(
        task_id, {"action": "confirm_delivery", "request_id": "req_deliver_001", "plan_version": 1, "stop_id": first_stop}
    )
    assert any(s["status"] == "delivered" for s in bundle["stops"])
    repeat = service.perform_action(
        task_id, {"action": "confirm_delivery", "request_id": "req_deliver_001", "plan_version": 1, "stop_id": first_stop}
    )
    assert repeat["task"]["plan_version"] == 1, "重复 request_id 应幂等返回"

    blocked_edge = next(edge for edge in bundle["plan"]["route_edge_ids"] if not edge.startswith("s"))
    service.create_road_event(
        {"edge_id": blocked_edge, "status": "blocked", "source": "user_text", "confirmed_by": "tester", "note": "路段施工"}
    )
    bundle = service.replan(task_id, reason="中央大道施工封闭")
    print("replan version =", bundle["task"]["plan_version"], "diff =", bundle["replan_diff"])
    assert bundle["task"]["plan_version"] == 2
    assert any(s["status"] == "delivered" for s in bundle["stops"])
    assert blocked_edge not in (bundle["plan"]["route_edge_ids"] or [])

    report = service.get_report(task_id)
    print("report summary =", report["summary"])
    assert report["summary"]["delivered_stops"] >= 1
    print("SMOKE OK")


if __name__ == "__main__":
    main()