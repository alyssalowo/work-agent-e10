"""Project a complete, successful ordinary-sheet receipt into its source snapshot.

This is a local evidence transformation, never a claim of a new DingTalk read.
"""
from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import re
import sys
from io import StringIO
from pathlib import Path
from urllib.parse import urlparse


def require(ok: bool, message: str) -> None:
    if not ok:
        raise ValueError(message)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def col_num(label: str) -> int:
    value = 0
    for ch in label:
        require("A" <= ch <= "Z", f"invalid A1 column: {label}")
        value = value * 26 + ord(ch) - 64
    return value


def column_label(value: int) -> str:
    result = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(65 + remainder) + result
    return result


def parse_range(value: str) -> tuple[int, int, int, int]:
    match = re.fullmatch(r"([A-Z]+)([1-9]\d*):([A-Z]+)([1-9]\d*)", value or "")
    require(match is not None, f"invalid full A1 range: {value!r}")
    assert match
    c1, r1, c2, r2 = match.groups()
    return col_num(c1), int(r1), col_num(c2), int(r2)


def parse_cell(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"([A-Z]+)([1-9]\d*)", value or "")
    require(match is not None, f"invalid A1 cell: {value!r}")
    assert match
    return col_num(match.group(1)), int(match.group(2))


def snapshot_sheets(snapshot: dict) -> dict[str, tuple[dict, list[list[str]], tuple[int, int, int, int]]]:
    require(snapshot.get("schema_version") == 1 and snapshot.get("transport") == "dingtalk_sheet"
            and snapshot.get("phase") == "collect" and snapshot.get("ok") is True
            and snapshot.get("complete") is True, "source must be a complete ordinary-sheet snapshot")
    parsed = urlparse(snapshot.get("target_url", ""))
    node_id = parsed.path.rstrip("/").split("/")[-1]
    require(node_id and snapshot.get("node_id") == node_id, "snapshot node_id/target_url mismatch")
    result = {}
    for sheet in snapshot.get("sheets", []):
        name = sheet.get("name")
        require(name and name not in result and sheet.get("scope_complete") is True, "incomplete/duplicate snapshot sheet")
        bounds = parse_range(sheet.get("range", ""))
        c1, r1, c2, r2 = bounds
        mcp = sheet.get("sheet_mcp") or {}
        require(mcp.get("node_id") == node_id and mcp.get("sheet_id") == sheet.get("sheet_id")
                and mcp.get("has_more") is False, f"{name}: snapshot identity/index evidence incomplete")
        require(mcp.get("row_indices") == list(range(r1, r2 + 1)), f"{name}: row coverage mismatch")
        require(mcp.get("col_indices") == [column_label(i) for i in range(c1, c2 + 1)], f"{name}: column coverage mismatch")
        matrix = list(csv.reader(StringIO(sheet.get("tsv", ""), newline=""), delimiter="\t", strict=True))
        require(len(matrix) == r2 - r1 + 1 and all(len(row) == c2 - c1 + 1 for row in matrix),
                f"{name}: snapshot TSV is not the complete rectangular range")
        result[name] = (sheet, matrix, bounds)
    require(bool(result), "snapshot has no sheets")
    return result


def canonical_target(value: str) -> str:
    parsed = urlparse(value or "")
    return value if parsed.scheme and parsed.netloc else value.rstrip("/")


def check_optional_evidence(receipt: dict, paths: dict[str, Path]) -> None:
    evidence = receipt.get("evidence")
    if evidence is None:
        return  # Historical receipt: independently compare its full plan/spec/coverage.
    require(isinstance(evidence, dict), "receipt evidence must be an object")
    for path_key, hash_key in (("snapshot_path", "snapshot_sha256"), ("batch_path", "batch_sha256"),
                               ("spec_path", "spec_sha256"), ("check_path", "check_sha256")):
        if path_key not in evidence and hash_key not in evidence:
            continue
        require(path_key in evidence and hash_key in evidence, f"receipt evidence lacks {path_key}/{hash_key}")
        p = paths.get(path_key)
        if p is None:
            p = Path(evidence[path_key])
        require(p.is_file() and sha256(p) == evidence[hash_key], f"receipt evidence hash mismatch: {path_key}")
        if path_key in paths:
            require(Path(evidence[path_key]).resolve() == paths[path_key].resolve(), f"receipt evidence path mismatch: {path_key}")


def project(snapshot: dict, batch: dict, spec: dict, receipt: dict,
            hashes: dict[str, str] | None = None, paths: dict[str, str] | None = None) -> dict:
    require(batch.get("schema_version") == 1 and batch.get("transport") == "dingtalk_sheet"
            and batch.get("kind") in ("workorder", "picking"), "unsupported ordinary-sheet batch")
    target = canonical_target(snapshot.get("target_url", ""))
    node_id = snapshot.get("node_id")
    require(target == canonical_target(batch.get("target_url", "")) == canonical_target(spec.get("target_url", ""))
            == canonical_target(receipt.get("target_url", "")), "document target mismatch")
    require(batch.get("node_id") == node_id and spec.get("node_id") == node_id
            and receipt.get("node_id", node_id) == node_id, "document node_id mismatch")
    require(spec.get("schema_version") == 1 and spec.get("transport") == "dingtalk_sheet"
            and spec.get("phase") == "submit" and spec.get("batch_id") == batch.get("batch_id")
            and spec.get("kind") == batch.get("kind"), "submit spec does not match batch")
    require(receipt.get("schema_version") == 1 and receipt.get("transport") == "dingtalk_sheet"
            and receipt.get("phase") == "submit" and receipt.get("ok") is True
            and receipt.get("failure") in (None, {}) and receipt.get("batch_id") == batch.get("batch_id")
            and receipt.get("kind") == batch.get("kind"), "receipt is not a complete successful submission")

    ss = snapshot_sheets(snapshot)
    batch_sheets, spec_sheets = batch.get("sheets", []), spec.get("sheets", [])
    require([x.get("name") for x in batch_sheets] == list(ss)
            == [x.get("name") for x in spec_sheets], "sheet collection/order mismatch")
    known: dict[tuple[str, str], dict] = {}
    for bsheet, sspec in zip(batch_sheets, spec_sheets):
        name = bsheet["name"]
        snap_sheet = ss[name][0]
        snap_bounds = ss[name][2]
        c1, r1, c2, r2 = snap_bounds
        expected_rows = list(range(r1, r2 + 1))
        expected_cols = [column_label(i) for i in range(c1, c2 + 1)]
        for obj, label in ((bsheet, "batch"), (sspec, "spec")):
            require(obj.get("sheet_id") == snap_sheet.get("sheet_id")
                    and obj.get("range") == snap_sheet.get("range"), f"{name}: {label} sheet/range mismatch")
            if obj.get("node_id") is not None:
                require(obj.get("node_id") == node_id, f"{name}: {label} node mismatch")
        batch_mcp = bsheet.get("sheet_mcp") or {}
        require(batch_mcp.get("node_id") == node_id and batch_mcp.get("sheet_id") == snap_sheet.get("sheet_id")
                and batch_mcp.get("row_indices") == expected_rows and batch_mcp.get("col_indices") == expected_cols
                and batch_mcp.get("has_more") is False, f"{name}: batch scope incomplete")
        mcp = sspec.get("sheet_mcp") or {}
        require(mcp.get("node_id") == node_id and mcp.get("sheet_id") == snap_sheet.get("sheet_id")
                and mcp.get("row_indices") == expected_rows and mcp.get("col_indices") == expected_cols
                and mcp.get("has_more") is False, f"{name}: spec scope incomplete")
        for change in bsheet.get("changes", []):
            cell = change.get("cell")
            key = (name, cell)
            require(key not in known and cell, "duplicate or invalid batch change")
            known[key] = change
        spec_changes = {x.get("cell"): x for x in sspec.get("changes", [])}
        require(len(spec_changes) == len(sspec.get("changes", [])), f"{name}: duplicate spec changes")
        batch_changes = {x.get("cell"): x for x in bsheet.get("changes", [])}
        require(set(spec_changes) == set(batch_changes), f"{name}: batch/spec change coverage mismatch")
        for cell, change in batch_changes.items():
            s_change = spec_changes[cell]
            require((change.get("before"), change.get("after")) == (s_change.get("before"), s_change.get("after")),
                    f"{name}!{cell}: batch/spec before-after mismatch")

    submitted = []
    for entry in receipt.get("submitted", []):
        require(isinstance(entry.get("cells"), list), "invalid submitted receipt row")
        submitted.extend((entry.get("sheet"), cell, "submitted") for cell in entry["cells"])
    already = [(entry.get("sheet"), entry.get("cell"), "already_present") for entry in receipt.get("already_present", [])]
    skipped = [(entry.get("sheet"), entry.get("cell"), "skipped_history") for entry in receipt.get("skipped_history", [])]
    reported = submitted + already + skipped
    require(len({(s, c) for s, c, _ in reported}) == len(reported), "duplicate receipt cell")
    require({(s, c) for s, c, _ in reported} == set(known), "successful receipt does not cover exact planned changes")

    result = copy.deepcopy(snapshot)
    by_name = {s["name"]: s for s in result["sheets"]}
    matrices = {name: ss[name][1] for name in ss}
    excluded = set()
    for row in (batch.get("exclusions") or {}).get("rows", []):
        excluded.add((row.get("sheet"), row.get("row")))
        name, physical_row = row.get("sheet"), row.get("row")
        require(name in ss and isinstance(physical_row, int), "invalid excluded-row evidence")
        sheet, matrix, (c1, r1, c2, r2) = ss[name]
        require(r1 <= physical_row <= r2, f"{name} row {physical_row}: excluded row outside snapshot")
        headers = matrix[0]
        row_values = matrix[physical_row - r1]
        for header, expected in (row.get("identity") or {}).items():
            require(header in headers and row_values[headers.index(header)] == str(expected),
                    f"{name} row {physical_row}: excluded identity changed ({header})")

    projected = []
    for name, cell, status in reported:
        change = known[(name, cell)]
        col, row = parse_cell(cell)
        sheet, matrix, (c1, r1, c2, r2) = ss[name]
        require(c1 <= col <= c2 and r1 <= row <= r2, f"{name}!{cell}: outside snapshot range")
        require((name, row) not in excluded, f"{name}!{cell}: receipt includes an explicitly excluded row")
        ri, ci = row - r1, col - c1
        current = matrix[ri][ci]
        before, after = str(change.get("before", "")), str(change.get("after", ""))
        require(current in (before, after), f"{name}!{cell}: snapshot value matches neither before nor after")
        if status in ("submitted", "already_present"):
            matrix_value = after
            projected.append({"sheet": name, "cell": cell, "status": status, "before": before, "after": after})
        else:
            matrix_value = current
        # already_present is backed by a successful prewrite check; if the supplied source was older,
        # the receipt provides the locally auditable final value without claiming another read.
        matrices[name][ri][ci] = matrix_value
    for name, matrix in matrices.items():
        output = StringIO(newline="")
        csv.writer(output, delimiter="\t", quotechar='"', doublequote=True,
                   lineterminator="\n", quoting=csv.QUOTE_MINIMAL).writerows(matrix)
        by_name[name]["tsv"] = output.getvalue()

    chain = snapshot.get("provenance")
    base_snapshot_hash = (chain or {}).get("base_snapshot_sha256") if isinstance(chain, dict) else None
    base_snapshot_hash = base_snapshot_hash or (hashes or {}).get("snapshot")
    provenance = {
        "kind": "local_receipt_projection",
        "online_read_performed": False,
        "source_snapshot_sha256": (hashes or {}).get("snapshot"),
        "base_snapshot_sha256": base_snapshot_hash,
        "batch_sha256": (hashes or {}).get("batch"),
        "spec_sha256": (hashes or {}).get("spec"),
        "receipt_sha256": (hashes or {}).get("receipt"),
        "input_paths": paths or {},
        "prior_projection": chain if isinstance(chain, dict) else None,
        "applied_cells": projected,
    }
    result["snapshot_basis"] = "local_receipt_projection"
    result["provenance"] = provenance
    # captured_at is intentionally carried only when it existed on the source.
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--batch", required=True, type=Path)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    try:
        inputs = {"snapshot": args.snapshot.resolve(), "batch": args.batch.resolve(),
                  "spec": args.spec.resolve(), "receipt": args.receipt.resolve()}
        require(args.out.resolve() not in inputs.values(), "output cannot overwrite an input")
        hashes = {name: sha256(path) for name, path in inputs.items()}
        snapshot, batch, spec, receipt = (read_json(inputs[k]) for k in ("snapshot", "batch", "spec", "receipt"))
        source_snapshot_path = batch.get("snapshot_file")
        require(source_snapshot_path and Path(source_snapshot_path).is_file(), "batch snapshot_file is missing")
        batch_snapshot_hash = sha256(Path(source_snapshot_path))
        require(batch_snapshot_hash == hashes["snapshot"],
                "batch snapshot_file hash does not exactly match the supplied snapshot")
        source_path = batch.get("source")
        require(source_path and Path(source_path).is_file() and sha256(Path(source_path)) == batch.get("source_sha256"),
                "batch source file hash mismatch")
        check_optional_evidence(receipt, {
            "snapshot_path": inputs["snapshot"], "batch_path": inputs["batch"], "spec_path": inputs["spec"]
        })
        projected = project(snapshot, batch, spec, receipt, hashes,
                            {name: str(path) for name, path in inputs.items()})
        args.out.resolve().parent.mkdir(parents=True, exist_ok=True)
        require(not args.out.resolve().exists(), "refusing to overwrite projection output")
        args.out.resolve().write_text(json.dumps(projected, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"ok": True, "out": str(args.out.resolve()), "projected_cells": len(projected["provenance"]["applied_cells"]),
                          "snapshot_basis": projected["snapshot_basis"], "source_snapshot_sha256": hashes["snapshot"],
                          "receipt_sha256": hashes["receipt"]}, ensure_ascii=False))
        return 0
    except (OSError, json.JSONDecodeError, ValueError, KeyError, TypeError, csv.Error) as error:
        print(f"sheet_snapshot_project: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
