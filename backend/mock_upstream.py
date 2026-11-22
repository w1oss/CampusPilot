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


def choose_demo_route(mapdata):
    """Choose reachable locations from the live map without changing road state."""
    locations = [n["id"] for n in mapdata["nodes"] if n.get("kind") == "location"]
    adjacency = {n["id"]: [] for n in mapdata["nodes"]}
    for edge in mapdata["edges"]:
        if mapdata.get("road_status", {}).get(edge["id"], edge.get("base_status", "open")) == "blocked":
            continue
        adjacency[edge["from"]].append(edge["to"])
        if edge.get("bidirectional", True):
            adjacency[edge["to"]].append(edge["from"])
    candidates = [("cs_building", "library")]
    candidates += [(source, "library") for source in locations if source != "library"]
    candidates += [(source, target) for source in locations for target in locations if source != target]
    for source, target in candidates:
        if source not in locations or target not in locations:
            continue
        visited, pending = {source}, [source]
        while pending:
            for neighbor in adjacency[pending.pop()]:
                if neighbor not in visited:
                    visited.add(neighbor)
                    pending.append(neighbor)
        if target in visited:
            return source, target
    raise ValueError("当前路况下没有两个可连通的演示地点，请先在页面检查封闭道路；未创建演示任务，也未修改路况。")


def create_demo_task(request, mapdata):
    source, target = choose_demo_route(mapdata)
    names = {node["id"]: node["name"] for node in mapdata["nodes"]}
    print(f"按当前路况选择演示路线：{names[source]} → {names[target]}", flush=True)
    created = request("/api/tasks", {"mode": "sim", "pickup_location_id": source,
        "stops": [{"location_id": target, "items": ["模拟文件"], "priority": "normal"}], "confirmed": True})
    task_id = created["task"]["id"]
    planned = request(f"/api/tasks/{task_id}/plan", {})
    if not planned.get("plan", {}).get("feasible"):
        raise ValueError(f"规划时路况发生变化，演示任务 {task_id} 暂未启动。请在页面查看不可达原因后重试；未修改路况。")
    for action in ("start", "confirm_pickup"):
        request(f"/api/tasks/{task_id}/actions", {"action": action, "request_id": uuid.uuid4().hex})
    request(f"/api/tasks/{task_id}/sim/runtime", {"enabled": True, "speed_mps": 5})
    return task_id


def main() -> None:
    parser = argparse.ArgumentParser(description="行驶途中模拟视觉与大模型汇总后的语义消息")
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--task-id")
    parser.add_argument("--demo", action="store_true", help="创建一个独立模拟任务并开启后台移动")
    parser.add_argument("--category", choices=["construction", "pedestrian", "crowd"], default="pedestrian")
    parser.add_argument("--edge-id", help="不填时，发送时读取车辆所在道路")
    parser.add_argument("--fraction", type=float, help="障碍位置，沿 edge.from 到 edge.to 的 0..1 比例；不填时取车前方")
    parser.add_argument("--delay", type=float, default=0, help="等待多少秒发送；0 表示立即发送并先停车")
    parser.add_argument("--clear-after", type=float, default=None, help="发送后多少秒再发送解除消息；0 表示保持到人工解除")
    args = parser.parse_args()
    if args.clear_after is None:
        args.clear_after = {"pedestrian": 3.0, "crowd": 30.0, "construction": 0.0}[args.category]
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
        task_id = create_demo_task(request, mapdata)
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
            if args.clear_after <= 0:
                print("长时间障碍保持中；需要解除时再次运行并指定 --clear-after 为正数", flush=True)
                break
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
