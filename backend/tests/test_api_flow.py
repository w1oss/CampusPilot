import asyncio
import os
import sys
from pathlib import Path

TEST_DB = Path(__file__).resolve().parent.parent / "var" / "test_api.db"
os.environ["CAMPUSPILOT_DB"] = str(TEST_DB)
os.environ["CAMPUSPILOT_LLM_BASE_URL"] = ""
os.environ["CAMPUSPILOT_LLM_API_KEY"] = ""
os.environ["CAMPUSPILOT_LLM_MODEL"] = ""

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import httpx

from app.db import get_conn, init_db
from app.main import app

TEXT = "从计算机学院取3份实验材料，分别送到科研楼、实验室和学一楼，实验室材料优先，16:00前完成。"


def reset() -> None:
    init_db()
    with get_conn() as conn:
        for table in ("action_log", "task_events", "execution_state", "road_events", "plans", "stops", "tasks"):
            conn.execute(f"DELETE FROM {table}")


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def create_task(http: httpx.AsyncClient, text: str = TEXT) -> str:
    parsed = (await http.post("/api/tasks/parse", json={"text": text})).json()
    draft = parsed["draft"]
    response = await http.post(
        "/api/tasks",
        json={
            "mode": "sim",
            "pickup_location_id": draft["pickup_location_id"],
            "stops": draft["stops"],
            "deadline": draft["deadline"],
            "deadline_kind": draft["deadline_kind"],
            "raw_input": text,
            "parser": parsed["parser"],
            "confirmed": True,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["task"]["id"]


def test_static_and_health() -> None:
    reset()

    async def run() -> None:
        async with client() as http:
            health = await http.get("/api/health")
            assert health.status_code == 200
            assert health.json()["status"] == "ok"

            index = await http.get("/")
            assert index.status_code == 200
            assert "CampusPilot" in index.text
            assert "/static/main.js" in index.text

            assert (await http.get("/static/main.js")).status_code == 200
            assert (await http.get("/static/App.js")).status_code == 200
            assert (await http.get("/vendor/react.production.min.js")).status_code == 200
            assert (await http.get("/vendor/react-dom.production.min.js")).status_code == 200

            map_payload = (await http.get("/api/map")).json()
            assert map_payload["map_version"]
            assert len(map_payload["nodes"]) >= 14

    asyncio.run(run())


def test_full_flow() -> None:
    reset()

    async def run() -> None:
        async with client() as http:
            parsed = (await http.post("/api/tasks/parse", json={"text": TEXT})).json()
            assert parsed["missing_fields"] == []
            assert parsed["draft"]["pickup_location_id"] == "cs_building"
            assert len(parsed["draft"]["stops"]) == 3
            assert parsed["draft"]["deadline"] == "16:00"
            assert any(stop["priority"] == "urgent" for stop in parsed["draft"]["stops"])

            task_id = await create_task(http)
            planned = (await http.post(f"/api/tasks/{task_id}/plan", json={})).json()
            plan = planned["plan"]
            assert plan["plan_version"] == 1
            assert plan["ordered_stops"][0]["priority"] == "urgent"
            assert plan["route_edge_ids"]
            assert plan["feasible"] is True

            version = plan["plan_version"]
            started = (
                await http.post(
                    f"/api/tasks/{task_id}/actions",
                    json={"action": "start", "request_id": "req_start_0001", "plan_version": version},
                )
            ).json()
            assert started["task"]["status"] == "in_progress"

            picked = (
                await http.post(
                    f"/api/tasks/{task_id}/actions",
                    json={"action": "confirm_pickup", "request_id": "req_pickup_00001", "plan_version": version},
                )
            ).json()
            assert picked["task"]["pickup_status"] == "picked_up"

            first_stop = picked["plan"]["ordered_stops"][0]["stop_id"]
            bundle = picked
            for _ in range(400):
                bundle = (await http.post(f"/api/tasks/{task_id}/sim/tick", json={"seconds": 0.5})).json()
                if bundle["sim"]["waiting"] == "awaiting_delivery":
                    break
            assert bundle["sim"]["waiting"] == "awaiting_delivery"

            delivered = (
                await http.post(
                    f"/api/tasks/{task_id}/actions",
                    json={
                        "action": "confirm_delivery",
                        "request_id": "req_deliver_001",
                        "plan_version": version,
                        "stop_id": first_stop,
                    },
                )
            ).json()
            assert any(stop["status"] == "delivered" for stop in delivered["stops"])

            repeat = await http.post(
                f"/api/tasks/{task_id}/actions",
                json={
                    "action": "confirm_delivery",
                    "request_id": "req_deliver_001",
                    "plan_version": version,
                    "stop_id": first_stop,
                },
            )
            assert repeat.status_code == 200
            assert repeat.json()["task"]["plan_version"] == version

            blocked_edge = next(edge_id for edge_id in plan["route_edge_ids"] if not edge_id.startswith("s"))
            road = await http.post(
                "/api/road-events",
                json={"edge_id": blocked_edge, "status": "blocked", "source": "user_text", "confirmed_by": "tester", "note": "路段施工"},
            )
            assert road.status_code == 200

            replanned = (
                await http.post(f"/api/tasks/{task_id}/replan", json={"request_id": "req_replan_0001", "reason": "道路施工"})
            ).json()
            assert replanned["task"]["plan_version"] == 2
            assert replanned["plan"]["route_edge_ids"]
            assert blocked_edge not in replanned["plan"]["route_edge_ids"]
            assert any(stop["status"] == "delivered" for stop in replanned["stops"])
            assert replanned["replan_diff"]["changed"] is True

            report = (await http.get(f"/api/tasks/{task_id}/report")).json()
            assert report["summary"]["delivered_stops"] >= 1
            assert report["summary"]["replan_count"] >= 1
            assert report["data_notice"]

    asyncio.run(run())


def test_validation_and_unreachable() -> None:
    reset()

    async def run() -> None:
        async with client() as http:
            unknown = (await http.post("/api/tasks/parse", json={"text": "从火星基地取文件送到图书馆"})).json()
            assert any(field.startswith("unknown_location:") for field in unknown["missing_fields"])

            invalid = await http.post(
                "/api/tasks",
                json={
                    "mode": "sim",
                    "pickup_location_id": "mars_base",
                    "stops": [{"location_id": "library", "items": ["文件"], "priority": "normal"}],
                    "deadline_kind": "none",
                    "confirmed": True,
                },
            )
            assert invalid.status_code == 400
            assert invalid.json()["code"] == "invalid_pickup"

            unconfirmed = await http.post(
                "/api/tasks",
                json={
                    "mode": "sim",
                    "pickup_location_id": "cs_building",
                    "stops": [{"location_id": "library", "items": ["文件"], "priority": "normal"}],
                    "deadline_kind": "none",
                    "confirmed": False,
                },
            )
            assert unconfirmed.status_code == 400
            assert unconfirmed.json()["code"] == "not_confirmed"

            map_payload = (await http.get("/api/map")).json()
            library_edge = next(
                edge["id"] for edge in map_payload["edges"] if edge["from"] == "library" or edge["to"] == "library"
            )
            await http.post(
                "/api/road-events",
                json={"edge_id": library_edge, "status": "blocked", "source": "user_text", "confirmed_by": "tester", "note": "图书馆入口封闭"},
            )
            task_id = await create_task(http, "从计算机学院取1份文件送到图书馆，无截止时间。")
            planned = (await http.post(f"/api/tasks/{task_id}/plan", json={})).json()
            assert planned["plan"]["feasible"] is False
            assert planned["plan"]["unreachable_stops"]
            assert planned["plan"]["unreachable_stops"][0]["location_id"] == "library"

    asyncio.run(run())