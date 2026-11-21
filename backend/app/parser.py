import json
import re
import urllib.error
import urllib.request
from typing import Any

from .config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_TIMEOUT_SECONDS
from .mapdata import MapData, load_map

PICKUP_RE = re.compile(r"(?:从|在|去)([^，,。；;\s]{1,16}?)(?:取|拿|领|取件|取货|接货)")
DELIVERY_VERBS = ["分别送到", "分别送往", "送到", "送往", "送至", "配送到", "送达", "交给", "分发到"]
DELIVERY_TAIL_RE = re.compile(r"(?:送到|送往|送至|配送到|送达|交给|分发到)([^。；;\n]*)")
PRIORITY_SUFFIX_RE = re.compile(r"(?:优先|加急|尽快|先送|最急|急送)+$")
CLOCK_RE = re.compile(r"(\d{1,2})[:：](\d{2})")
HOUR_RE = re.compile(r"(上午|下午|晚上|中午)?\s*(\d{1,2})\s*[点时](半|(\d{1,2})分?)?")
URGENT_WORDS = ["优先", "加急", "尽快", "先送", "最急", "急送"]
SPLIT_RE = re.compile(r"[、,，和及跟]")


def _find_locations(text: str, mapdata: MapData) -> list[tuple[int, int, str, str]]:
    matches: list[tuple[int, int, str, str]] = []
    occupied: list[tuple[int, int]] = []
    for alias, node_id in mapdata.alias_index().items():
        start = 0
        while True:
            idx = text.find(alias, start)
            if idx < 0:
                break
            end = idx + len(alias)
            if not any(idx < occ_end and end > occ_start for occ_start, occ_end in occupied):
                matches.append((idx, end, alias, node_id))
                occupied.append((idx, end))
            start = end
    matches.sort(key=lambda item: item[0])
    return matches


def _extract_items(text: str, pickup_end: int) -> list[str]:
    tail = text[pickup_end:]
    cut = len(tail)
    for verb in sorted(DELIVERY_VERBS, key=len, reverse=True):
        pos = tail.find(verb)
        if 0 <= pos < cut:
            cut = pos
    segment = tail[:cut].strip(" ，,。；;：:")
    segment = re.sub(r"^(?:取|拿|领|取件|取货|接货)", "", segment).strip()
    if not segment:
        return []
    parts = [p.strip(" 。；;：:") for p in SPLIT_RE.split(segment)]
    return [p for p in parts if p]


def _extract_deadline(text: str) -> tuple[str | None, str]:
    match = CLOCK_RE.search(text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}", "clock"
    match = HOUR_RE.search(text)
    if match:
        period, hour = match.group(1), int(match.group(2))
        minute_token, minute_digits = match.group(3), match.group(4)
        minute = 30 if minute_token == "半" else int(minute_digits or 0)
        if period in ("下午", "晚上") and hour < 12:
            hour += 12
        if period == "中午" and hour < 12:
            hour += 12
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}", "clock"
    return None, "none"


def _mark_priority(text: str, locations: list[tuple[int, int, str, str]], pickup_id: str | None) -> set[str]:
    urgent: set[str] = set()
    delivery_locations = [item for item in locations if item[3] != pickup_id]
    for idx, end, _alias, node_id in delivery_locations:
        next_start = len(text)
        for other in delivery_locations:
            if other[0] > idx:
                next_start = min(next_start, other[0])
        tail = text[end:next_start]
        tail = re.split(r"[，,。；;：:\n]", tail, maxsplit=1)[0][:8]
        head = text[max(0, idx - 4):idx]
        if (any(word in tail for word in URGENT_WORDS) or any(word in head for word in URGENT_WORDS)) and node_id not in urgent:
            urgent.add(node_id)
    if not urgent and re.search(r"优先|加急|尽快|先送", text):
        for item in delivery_locations:
            urgent.add(item[3])
            break
    return urgent


def _unknown_delivery_locations(text: str, mapdata: MapData) -> list[str]:
    aliases = mapdata.alias_index()
    unknown: list[str] = []
    for match in DELIVERY_TAIL_RE.finditer(text):
        clause = re.split(r"[，,。；;：:]", match.group(1), maxsplit=1)[0]
        for part in re.split(r"[、和及跟]", clause):
            part = PRIORITY_SUFFIX_RE.sub("", part.strip(" 。；;：:"))
            if not part or len(part) > 12:
                continue
            if re.fullmatch(r"[\d:：.\s]+", part):
                continue
            if any(alias in part for alias in aliases):
                continue
            if part not in unknown:
                unknown.append(part)
    return unknown


def rule_parse(text: str, mapdata: MapData) -> dict[str, Any]:
    locations = _find_locations(text, mapdata)
    notes: list[str] = []
    missing: list[str] = []

    pickup_id: str | None = None
    pickup_match = PICKUP_RE.search(text)
    if pickup_match:
        phrase = pickup_match.group(1)
        for _idx, _end, alias, node_id in locations:
            if alias in phrase or phrase in alias:
                pickup_id = node_id
                break
        if pickup_id is None:
            missing.append(f"unknown_location:{phrase}")
            notes.append(f"取货地点“{phrase}”不在校园地点白名单内，请从列表中选择。")

    if pickup_match is None and pickup_id is None:
        notes.append("未识别到取货点，请补充“从哪里取”。")
    remaining = [item for item in locations if item[3] != pickup_id]
    dedup: list[tuple[int, int, str, str]] = []
    seen: set[str] = set()
    for item in remaining:
        if item[3] in seen:
            continue
        seen.add(item[3])
        dedup.append(item)
    dedup.sort(key=lambda item: item[0])

    if pickup_id is None:
        missing.append("pickup_location")
    if not dedup:
        missing.append("delivery_stops")

    items = _extract_items(text, pickup_match.end() if pickup_match else 0)
    if not items:
        missing.append("items")
        notes.append("未识别到取货物品，请补充物品名称和数量。")

    urgent_ids = _mark_priority(text, locations, pickup_id)
    deadline, deadline_kind = _extract_deadline(text)

    for name in _unknown_delivery_locations(text, mapdata):
        field = f"unknown_location:{name}"
        if field not in missing:
            missing.append(field)
            notes.append(f"配送地点“{name}”不在校园地点白名单内，请从列表中选择。")

    stops = []
    for _idx, _end, _alias, node_id in dedup:
        stop_items = list(items)
        stops.append(
            {
                "location_id": node_id,
                "items": stop_items,
                "priority": "urgent" if node_id in urgent_ids else "normal",
            }
        )
    if len(stops) > 1 and items:
        notes.append("已按“同一批物品送到每个站点”预填每站物品，请逐站核对后再确认。")
    if stops and any(stop["priority"] == "urgent" for stop in stops):
        urgent_names = [mapdata.nodes[s["location_id"]]["name"] for s in stops if s["priority"] == "urgent"]
        notes.append(f"已识别优先站点：{'、'.join(urgent_names)}（优先将排在其他普通站点之前）。")
    if deadline_kind == "none":
        notes.append("未提供截止时间，将按无截止时间生成计划。")
    notes.append("联系人、重量等非必需信息未提供时不会阻塞计划，可在备注中补充。")

    if pickup_id is None:
        pickup_id = ""

    return {
        "draft": {
            "mode": "sim",
            "pickup_location_id": pickup_id,
            "stops": stops,
            "deadline": deadline,
            "deadline_kind": deadline_kind,
        },
        "missing_fields": missing,
        "notes": notes,
        "parser": "rule_based_mock",
        "raw_text": text,
    }


LLM_SYSTEM_PROMPT = """你是校园配送任务解析器。只输出 JSON，不要输出解释或 Markdown。
JSON 结构：
{"pickup_location":"地点名称","stops":[{"location":"地点名称","items":["物品"],"priority":"normal|urgent"}],"deadline":"HH:MM 或 null"}
规则：
1. 只能使用用户原文中出现的地点，不得编造新地点。
2. 每个配送站点的物品必须明确列出。
3. 用户说“优先/加急/先送”的站点标记为 urgent。
4. 未提供截止时间时 deadline 为 null。
5. 无法确定取货点或配送点时对应字段留空字符串或空数组。"""


def _call_llm(text: str, clarification: dict | None) -> dict[str, Any] | None:
    if not (LLM_BASE_URL and LLM_API_KEY and LLM_MODEL):
        return None
    payload = {
        "model": LLM_MODEL,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": LLM_SYSTEM_PROMPT},
            {"role": "user", "content": text if not clarification else f"{text}\n补充信息：{json.dumps(clarification, ensure_ascii=False)}"},
        ],
    }
    request = urllib.request.Request(
        f"{LLM_BASE_URL.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {LLM_API_KEY}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=LLM_TIMEOUT_SECONDS) as response:
            body = json.loads(response.read().decode("utf-8"))
        content = body["choices"][0]["message"]["content"]
        return json.loads(content)
    except (urllib.error.URLError, KeyError, IndexError, json.JSONDecodeError, TimeoutError):
        return None


def _resolve_name(name: str, mapdata: MapData) -> str | None:
    if not name:
        return None
    if name in mapdata.nodes and mapdata.nodes[name].get("kind") == "location":
        return name
    index = mapdata.alias_index()
    if name in index:
        return index[name]
    for alias, node_id in index.items():
        if alias in name or name in alias:
            return node_id
    return None


def llm_parse(text: str, clarification: dict | None, mapdata: MapData) -> dict[str, Any] | None:
    raw = _call_llm(text, clarification)
    if not isinstance(raw, dict):
        return None
    pickup_id = _resolve_name(str(raw.get("pickup_location") or ""), mapdata)
    stops = []
    for item in raw.get("stops") or []:
        if not isinstance(item, dict):
            continue
        node_id = _resolve_name(str(item.get("location") or ""), mapdata)
        if not node_id or node_id == pickup_id:
            continue
        stops.append(
            {
                "location_id": node_id,
                "items": [str(i) for i in (item.get("items") or []) if str(i).strip()],
                "priority": "urgent" if item.get("priority") == "urgent" else "normal",
            }
        )
    if not pickup_id:
        return None
    missing: list[str] = []
    if not stops:
        missing.append("delivery_stops")
    if stops and any(not stop["items"] for stop in stops):
        missing.append("item_allocation")
    for name in _unknown_delivery_locations(text, mapdata):
        field = f"unknown_location:{name}"
        if field not in missing:
            missing.append(field)
    deadline = raw.get("deadline")
    deadline_kind = "clock" if deadline else "none"
    if deadline and not re.fullmatch(r"\d{2}:\d{2}", str(deadline)):
        deadline, deadline_kind = None, "none"
    return {
        "draft": {
            "mode": "sim",
            "pickup_location_id": pickup_id,
            "stops": stops,
            "deadline": deadline,
            "deadline_kind": deadline_kind,
        },
        "missing_fields": missing,
        "notes": (["由真实模型解析，地点已通过白名单校验。"] + (
            ["原文中有地点不在校园地点白名单内，已提示用户确认。"]
            if any(field.startswith("unknown_location:") for field in missing)
            else []
        )),
        "parser": f"llm:{LLM_MODEL}",
        "raw_text": text,
    }


def parse_task(text: str, clarification: dict | None = None, prefer_llm: bool = True) -> dict[str, Any]:
    mapdata = load_map()
    if prefer_llm:
        result = llm_parse(text, clarification, mapdata)
        if result is not None:
            return result
    result = rule_parse(text, mapdata)
    if prefer_llm and LLM_BASE_URL:
        result["notes"].append("模型调用失败，已回落到规则解析（结果已明确标注为模拟解析）。")
    return result