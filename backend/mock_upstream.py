"""Independent timed upstream sender. It never changes tasks or plans directly.

python mock_upstream.py --demo --category pedestrian --delay 3 --clear-after 3
python mock_upstream.py --task-id task_xxx --edge-id e0001 --category construction
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone


def main() -> None:
    parser = argparse.ArgumentParser(description="行驶途中模拟视觉与大模型汇总后的语义消息")
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--task-id")
    parser.add_argument("--demo", action="store_true", help="创建一个独立模拟任务并开启后台移动")
    parser.add_argument("--category", choices=["construction", "pedestrian", "crowd"], default="pedestrian")
    parser.add_argument("--edge-id", help="不填时，发送时读取车辆所在道路")
    parser.add_argument("--fraction", type=float, help="障碍位置，沿 edge.from 到 edge.to 的 0..1 比例；不填时取车前方")
    parser.add_argument("--delay", type=float, default=3, help="等待多少秒发送，与路口位置无关")
    parser.add_argument("--clear-after", type=float, default=3, help="发送后多少秒再发送解除消息")
    args = parser.parse_args()
    if not args.demo and not args.task_id:
        parser.error("请指定 --task-id 或 --demo")
    if min(args.delay, args.clear_after) < 0 or (args.fraction is not None and not 0 <= args.fraction <= 1):
        parser.error("延时不得为负，位置比例必须介于 0 和 1")

    def request(path, payload=None):
        req = urllib.request.Request(args.url.rstrip("/") + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as response:
            return json.load(response)

    mapdata = request("/api/map")
    task_id = args.task_id
    if args.demo:
        created = request("/api/tasks", {"mode": "sim", "pickup_location_id": "cs_building",
            "stops": [{"location_id": "library", "items": ["模拟文件"], "priority": "normal"}], "confirmed": True})
        task_id = created["task"]["id"]
        request(f"/api/tasks/{task_id}/plan", {})
        for action in ("start", "confirm_pickup"):
            request(f"/api/tasks/{task_id}/actions", {"action": action, "request_id": uuid.uuid4().hex})
        request(f"/api/tasks/{task_id}/sim/runtime", {"enabled": True, "speed_mps": 5})
    print(f"task_id={task_id}; {args.delay} 秒后发送，不等待标记点", flush=True)
    time.sleep(args.delay)
    snapshot = request(f"/api/tasks/{task_id}")
    state = snapshot["state"]
    edge_id = args.edge_id or state["current_edge_id"] or (state["remaining_edge_ids"] or [None])[0]
    if not edge_id:
        raise ValueError("当前没有待行驶道路，请先生成计划并开始配送，或指定 --edge-id")
    edge = next(e for e in mapdata["edges"] if e["id"] == edge_id)
    fraction = args.fraction
    if fraction is None:
        progress = state["edge_progress"] if state["current_edge_id"] == edge_id else 0
        fraction = progress + (1 - progress) * 0.7
        if state["current_edge_id"] == edge_id and state["last_node_id"] == edge["to"]:
            fraction = 1 - fraction
    descriptions = {"construction": "前方施工围挡，车辆无法通过", "pedestrian": "前方行人横穿，预计短暂占道", "crowd": "前方行人密集，通行缓慢"}
    payload = {"event_id": uuid.uuid4().hex, "task_id": task_id, "obstacle_id": f"demo_{uuid.uuid4().hex[:8]}",
        "revision": 1, "map_version": mapdata["map_version"], "observed_at": datetime.now(timezone.utc).isoformat(),
        "source": "mock_vision_llm", "lifecycle": "observed", "edge_id": edge_id, "edge_fraction": fraction,
        "category": args.category, "passability": "congested" if args.category == "crowd" else "blocked",
        "confidence": 1, "expected_duration_s": 5 if args.category == "pedestrian" else 1800,
        "summary": descriptions[args.category]}
    for lifecycle in ("observed", "cleared"):
        if lifecycle == "cleared":
            time.sleep(args.clear_after)
            payload.update(event_id=uuid.uuid4().hex, revision=2, lifecycle="cleared", passability="open",
                           observed_at=datetime.now(timezone.utc).isoformat(), summary="上游复查确认障碍已解除")
        result = request("/api/semantic-events", payload)
        print(json.dumps({"lifecycle": lifecycle, "accepted": result["accepted"], "decision": result["decision"],
                          "position": result["bundle"]["state"]["position"]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        main()
    except urllib.error.HTTPError as exc:
        print(exc.read().decode("utf-8"), file=sys.stderr)
        sys.exit(1)
    except (urllib.error.URLError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
