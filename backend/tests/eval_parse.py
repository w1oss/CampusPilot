import argparse
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.parser import parse_task

CASES = Path(__file__).resolve().parent / "parse_cases.json"


def load_cases():
    data = json.loads(CASES.read_text(encoding="utf-8"))
    return data["cases"]


def check_expect(expect, result):
    errors = []
    draft = result["draft"]
    missing = set(result["missing_fields"])

    for item in expect.get("missing", []):
        if item.endswith(":"):
            if not any(value.startswith(item) for value in missing):
                errors.append(f"missing 应包含 {item}*，实际 {sorted(missing)}")
        elif item not in missing:
            errors.append(f"missing 应包含 {item}，实际 {sorted(missing)}")

    if "missing_any" in expect:
        values = expect["missing_any"]
        satisfied = any(
            (value.endswith(":") and any(item.startswith(value) for item in missing)) or value in missing
            for value in values
        )
        if not satisfied:
            errors.append(f"missing 应包含 {values} 中至少一项，实际 {sorted(missing)}")

    if "pickup" in expect:
        if draft["pickup_location_id"] != expect["pickup"]:
            errors.append(f"取货点期望 {expect['pickup'] or '(空)'}，实际 {draft['pickup_location_id'] or '(空)'}")

    if "stops" in expect:
        actual = {stop["location_id"]: stop["priority"] for stop in draft["stops"]}
        expected = {stop["location"]: stop["priority"] for stop in expect["stops"]}
        if actual != expected:
            errors.append(f"站点期望 {expected}，实际 {actual}")

    if "deadline" in expect:
        if draft["deadline"] != expect["deadline"]:
            errors.append(f"截止时间期望 {expect['deadline']}，实际 {draft['deadline']}")

    if "items_contain" in expect:
        joined = " ".join(item for stop in draft["stops"] for item in stop["items"])
        for token in expect["items_contain"]:
            if token not in joined:
                errors.append(f"物品应包含“{token}”，实际 {joined or '(空)'}")

    return errors


def check_case(case, result):
    expect = case["expect"]
    if "either" in expect:
        attempts = [check_expect(branch, result) for branch in expect["either"]]
        if any(not errors for errors in attempts):
            return []
        return ["多个可接受结果都未满足：" + "；".join(" / ".join(errors) for errors in attempts)]
    return check_expect(expect, result)


def main() -> int:
    parser = argparse.ArgumentParser(description="CampusPilot 解析样例评测")
    parser.add_argument("--parser", choices=("rule", "auto"), default="rule", help="rule=仅规则解析；auto=配置了模型则优先模型")
    parser.add_argument("--require", type=int, default=0, help="低于该通过数时以非零状态退出")
    args = parser.parse_args()

    cases = load_cases()
    prefer_llm = args.parser == "auto"
    passed = 0
    failures = []
    parsers = set()
    parser_counts: dict[str, int] = {}

    for case in cases:
        result = parse_task(case["text"], prefer_llm=prefer_llm)
        parsers.add(result["parser"])
        parser_counts[result["parser"]] = parser_counts.get(result["parser"], 0) + 1
        errors = check_case(case, result)
        if errors:
            failures.append((case, errors, result))
        else:
            passed += 1

    total = len(cases)
    label = "规则解析" if not any(name.startswith("llm:") for name in parsers) else "模型解析"
    print(f"{label}：{passed}/{total} 通过", flush=True)
    print(f"实际解析器：{', '.join(sorted(parsers))}", flush=True)
    print(f"解析器分布：{'，'.join(f'{name} {count} 条' for name, count in parser_counts.items())}", flush=True)

    if failures:
        print("", flush=True)
        for case, errors, result in failures:
            print(f"✗ {case['id']} {case['note']}", flush=True)
            print(f"   输入：{case['text']}", flush=True)
            for error in errors:
                print(f"   - {error}", flush=True)
            print(f"   实际草稿：{json.dumps(result['draft'], ensure_ascii=False)}", flush=True)

    if args.require and passed < args.require:
        print(f"\n未达到验收线：{passed}/{total} < {args.require}/{total}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())