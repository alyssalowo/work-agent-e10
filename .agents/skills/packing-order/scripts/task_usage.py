"""Deterministic offline task-usage recording and comparison; no service calls."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any


COUNT_FIELDS = ("subagent_calls", "mcp_reads", "mcp_writes", "rework_count")
REQUIRED_TASK_FIELDS = (
    "task_id", "task_type", "model", "reasoning_effort", "actual_quota",
    *COUNT_FIELDS, "qualified", "gates",
)


class UsageError(ValueError):
    """Raised when an input record is malformed or internally inconsistent."""


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise UsageError(f"{label} must be an object")
    return value


def _nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise UsageError(f"{label} must be a non-empty string")
    return value.strip()


def _count(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise UsageError(f"{label} must be a non-negative integer")
    return value


def _quota(value: Any, label: str) -> dict[str, Any]:
    quota = _object(value, label)
    for key in ("value", "unit", "basis_id", "evidence_ref"):
        if key not in quota:
            raise UsageError(f"{label}.{key} is required")
    amount = quota["value"]
    if amount is not None:
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            raise UsageError(f"{label}.value must be a number or null")
        if not math.isfinite(amount) or amount < 0:
            raise UsageError(f"{label}.value must be finite and non-negative")
        evidence_ref = _nonempty_string(quota["evidence_ref"], f"{label}.evidence_ref")
    else:
        evidence_ref = quota["evidence_ref"]
        if evidence_ref is not None and (not isinstance(evidence_ref, str) or not evidence_ref.strip()):
            raise UsageError(f"{label}.evidence_ref must be a non-empty string or null")
    return {
        "value": amount,
        "unit": _nonempty_string(quota["unit"], f"{label}.unit"),
        "basis_id": _nonempty_string(quota["basis_id"], f"{label}.basis_id"),
        "evidence_ref": evidence_ref,
    }


def _task(value: Any, index: int, *, baseline: bool = False) -> dict[str, Any]:
    label = f"{'baseline' if baseline else 'tasks'}[{index}]"
    record = _object(value, label)
    if baseline:
        required = ("task_type", "actual_quota", "qualified", "gates")
    else:
        required = REQUIRED_TASK_FIELDS
    missing = [key for key in required if key not in record]
    if missing:
        raise UsageError(f"{label} missing required fields: {', '.join(missing)}")

    normalized: dict[str, Any] = {"task_type": _nonempty_string(record["task_type"], f"{label}.task_type")}
    if not baseline:
        normalized.update({
            "task_id": _nonempty_string(record["task_id"], f"{label}.task_id"),
            "model": _nonempty_string(record["model"], f"{label}.model"),
            "reasoning_effort": _nonempty_string(record["reasoning_effort"], f"{label}.reasoning_effort"),
        })
        for field in COUNT_FIELDS:
            normalized[field] = _count(record[field], f"{label}.{field}")
    normalized["actual_quota"] = _quota(record["actual_quota"], f"{label}.actual_quota")
    if not isinstance(record["qualified"], bool):
        raise UsageError(f"{label}.qualified must be a boolean")
    gates = _object(record["gates"], f"{label}.gates")
    if not gates:
        raise UsageError(f"{label}.gates must contain at least one result")
    normalized_gates: dict[str, bool] = {}
    for gate, result in gates.items():
        gate_name = _nonempty_string(gate, f"{label}.gates key")
        if not isinstance(result, bool):
            raise UsageError(f"{label}.gates.{gate_name} must be a boolean")
        normalized_gates[gate_name] = result
    if record["qualified"] and not all(normalized_gates.values()):
        raise UsageError(f"{label}.qualified cannot be true when any gate failed")
    normalized["qualified"] = record["qualified"]
    normalized["gates"] = normalized_gates
    return normalized


def _comparison(task: dict[str, Any], baseline: dict[str, Any] | None) -> dict[str, Any]:
    actual = task["actual_quota"]
    result: dict[str, Any] = {"status": None, "baseline_value": None, "actual_value": actual["value"], "delta": None}
    if not task["qualified"]:
        result["status"] = "task_not_qualified"
        return result
    if actual["value"] is None:
        result["status"] = "actual_quota_unavailable"
        return result
    if baseline is None:
        result["status"] = "baseline_missing"
        return result
    if not baseline["qualified"]:
        result["status"] = "baseline_not_qualified"
        return result
    expected = baseline["actual_quota"]
    if expected["value"] is None:
        result["status"] = "baseline_quota_unavailable"
        return result
    if expected["unit"] != actual["unit"] or expected["basis_id"] != actual["basis_id"]:
        result["status"] = "incomparable_quota_basis"
        return result
    result.update({
        "status": "comparable",
        "baseline_value": expected["value"],
        "delta": expected["value"] - actual["value"],
        "unit": actual["unit"],
        "basis_id": actual["basis_id"],
    })
    return result


def summarize(document: Any) -> dict[str, Any]:
    """Validate the manual record and return a stable, deterministic summary."""
    root = _object(document, "input")
    if "tasks" not in root or "baseline" not in root:
        raise UsageError("input must contain baseline and tasks arrays")
    if not isinstance(root["tasks"], list) or not isinstance(root["baseline"], list):
        raise UsageError("baseline and tasks must be arrays")
    baselines = [_task(item, i, baseline=True) for i, item in enumerate(root["baseline"])]
    tasks = [_task(item, i) for i, item in enumerate(root["tasks"])]
    by_type: dict[str, dict[str, Any]] = {}
    for baseline in baselines:
        task_type = baseline["task_type"]
        if task_type in by_type:
            raise UsageError(f"duplicate baseline task_type: {task_type}")
        by_type[task_type] = baseline

    results = []
    for task in tasks:
        comparison = _comparison(task, by_type.get(task["task_type"]))
        results.append({**task, "comparison": comparison})
    statuses: dict[str, int] = {}
    for item in results:
        status = item["comparison"]["status"]
        statuses[status] = statuses.get(status, 0) + 1
    comparable = statuses.get("comparable", 0)
    return {
        "schema_version": 1,
        "comparison_policy": "qualified tasks only; actual quota values must be available and match unit and basis_id; no token or prompt-length proxy",
        "task_count": len(results),
        "qualified_count": sum(1 for item in results if item["qualified"]),
        "comparison_status_counts": dict(sorted(statuses.items())),
        "comparable_count": comparable,
        "tasks": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("summarize", help="validate and summarize manually recorded JSON")
    command.add_argument("--input", required=True, type=Path)
    command.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        data = json.loads(args.input.read_text(encoding="utf-8"))
        result = summarize(data)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    except (OSError, json.JSONDecodeError, UsageError) as exc:
        print(f"task_usage: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({
        "status": "ok",
        "task_count": result["task_count"],
        "qualified_count": result["qualified_count"],
        "comparable_count": result["comparable_count"],
        "output": str(args.out),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
