from datetime import datetime
from typing import Any

from .db import loads
from .mapdata import MapData, node_name


def build_report(
    mapdata: MapData,
    task: dict[str, Any],
    stops: list[dict[str, Any]],
    plans: list[dict[str, Any]],
    road_events: list[dict[str, Any]],
    timeline: list[dict[str, Any]],
) -> dict[str, Any]:
    latest_plan = plans[0] if plans else None
    delivered = [s for s in stops if s["status"] == "delivered"]
    pending = [s for s in stops if s["status"] != "delivered"]
    delays: list[str] = []
    if last_plan_warnings := (loads(latest_plan["warnings"], []) if latest_plan else []):
        delays.extend(last_plan_warnings)


    started = next((e for e in timeline if e["type"] == "action:start"), None)
    completed = next((e for e in timeline if e["type"] == "action:complete"), None)
    actual_seconds = None
    if started and completed:
        actual_seconds = round(
            (datetime.fromisoformat(completed["created_at"]) - datetime.fromisoformat(started["created_at"])).total_seconds(), 1
        )

    replan_count = sum(1 for p in plans if int(p["plan_version"]) > 1)
    return {
        "task": {
            "id": task["id"],
            "mode": task["mode"],
            "status": task["status"],
            "pickup_location": node_name(mapdata, task["pickup_location_id"]),
            "deadline": task["deadline"],
            "deadline_kind": task["deadline_kind"],
            "parser": task["parser"],
            "raw_input": task["raw_input"],
            "created_at": task["created_at"],
            "plan_version": task["plan_version"],
            "pickup_status": task["pickup_status"],
        },
        "summary": {
            "total_stops": len(stops),
            "delivered_stops": len(delivered),
            "pending_stops": len(pending),
            "completion_rate": round(len(delivered) / len(stops), 3) if stops else 0.0,
            "plan_versions": len(plans),
            "replan_count": max(0, len(plans) - 1),
            "actual_duration_seconds": actual_seconds,
            "estimated_duration_seconds": latest_plan["estimated_duration_seconds"] if latest_plan else None,
        },
        "stops": [
            {
                "stop_id": s["id"],
                "location_id": s["location_id"],
                "location_name": node_name(mapdata, s["location_id"]),
                "items": loads(s["items"], []),
                "priority": s["priority"],
                "status": s["status"],
                "arrived_at": s["arrived_at"],
                "delivered_at": s["delivered_at"],
            }
            for s in stops
        ],
        "latest_plan": loads(latest_plan["ordered_stops"], []) if latest_plan else [],
        "plans": [
            {
                "plan_version": p["plan_version"],
                "created_at": p["created_at"],
                "ordered_stops": loads(p["ordered_stops"], []),
                "estimated_duration_seconds": p["estimated_duration_seconds"],
                "assumptions": loads(p["assumptions"], []),
                "unreachable_stops": loads(p["unreachable_stops"], []),
                "warnings": loads(p["warnings"], []),
            }
            for p in plans
        ],
        "road_events": road_events,
        "timeline": timeline,
        "delays": delays,
        "data_notice": "报告数据来自数据库中的任务事件与人工确认操作；道路距离和时间为示例估算值。",
    }