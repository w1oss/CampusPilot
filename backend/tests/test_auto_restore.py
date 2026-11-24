import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import service
from app.db import get_conn, init_db
from app.parser import parse_task


def main() -> None:
    init_db()
    with get_conn() as c:
        for t in ("action_log", "task_events", "execution_state", "road_events", "plans", "stops", "tasks"):
            c.execute(f"DELETE FROM {t}")

    TEXT = "从计算机学院取3份实验材料，分别送到科研楼和实验室，实验室优先。"
    parsed = parse_task(TEXT, prefer_llm=False)
    bundle = service.create_task(
        {
            "mode": "sim",
            "pickup_location_id": parsed["draft"]["pickup_location_id"],
            "stops": parsed["draft"]["stops"],
            "raw_input": TEXT,
            "parser": parsed["parser"],
            "confirmed": True,
        }
    )
    tid = bundle["task"]["id"]
    bundle = service.generate_plan(tid)
    plan = bundle["plan"]
    route = plan["route_edge_ids"]
    print(f"计划 edges 前3条: {route[:3]}")

    blocked = route[0]
    print(f"标记 {blocked} 为 blocked")
    service.create_road_event(
        {"edge_id": blocked, "status": "blocked", "source": "user_text", "confirmed_by": "tester", "note": "施工"}
    )

    bundle = service.replan(tid, reason="测试")
    print(f"重规划后路线: {bundle['plan']['route_edge_ids'][:5]}")

    bundle = service.perform_action(
        tid, {"action": "start", "request_id": "r1", "plan_version": bundle["task"]["plan_version"]}
    )
    plan_ver = bundle["task"]["plan_version"]
    bundle = service.perform_action(
        tid, {"action": "confirm_pickup", "request_id": "r2", "plan_version": plan_ver}
    )

    restored_found = False
    for i in range(5000):
        bundle = service.sim_tick(tid, 0.5)
        sim = bundle.get("sim", {})
        if sim.get("restored_edges"):
            print(f"第 {i + 1} 次 tick 后自动恢复了: {sim['restored_edges']}")
            restored_found = True
            break
        if sim.get("waiting") == "awaiting_delivery":
            break

    if not restored_found:
        print("5000 次 tick 内未触发自动恢复")

    with get_conn() as c:
        rows = c.execute("SELECT edge_id, status, observe_count, last_observed_at FROM road_events").fetchall()
        print("当前 road_events 表:")
        for r in rows:
            print(f"  edge={r['edge_id']} status={r['status']} observe_count={r['observe_count']}")

    print("测试完成")


if __name__ == "__main__":
    main()
