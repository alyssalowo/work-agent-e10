"""Convert a complete ordinary DingTalk sheet CSV capture to the local snapshot contract."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from io import StringIO
from pathlib import Path
from urllib.parse import urlparse


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def column_number(label: str) -> int:
    value = 0
    for char in label:
        require("A" <= char <= "Z", f"非法列标: {label}")
        value = value * 26 + ord(char) - ord("A") + 1
    return value


def column_label(value: int) -> str:
    result = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(ord("A") + remainder) + result
    return result


def parse_range(value: str) -> tuple[int, int, int, int]:
    match = re.fullmatch(r"([A-Z]+)(\d+):([A-Z]+)(\d+)", value)
    require(match is not None, f"范围不是完整矩形 A1 范围: {value!r}")
    assert match is not None
    c1, r1, c2, r2 = match.groups()
    return column_number(c1), int(r1), column_number(c2), int(r2)


def to_tsv(rows: list[list[str]]) -> str:
    stream = StringIO(newline="")
    writer = csv.writer(
        stream,
        delimiter="\t",
        quotechar='"',
        doublequote=True,
        lineterminator="\n",
        quoting=csv.QUOTE_MINIMAL,
    )
    writer.writerows(rows)
    return stream.getvalue()


def convert(raw: dict, expected_sheets: tuple[str, ...] | None = None,
            expected_node_id: str | None = None) -> dict:
    require(isinstance(raw, dict), "原始文件根节点须为对象")
    require(not raw.get('recovery_blocked') and not raw.get('collection_failures'), '采集失败或恢复待处理，不能生成完整快照')
    target_url = raw.get("target_url")
    require(isinstance(target_url, str) and target_url, "缺少 target_url")
    parsed_url = urlparse(target_url)
    require("/nodes/" in parsed_url.path, "target_url 不是钉钉节点文档路径")
    node_id = parsed_url.path.rstrip("/").split("/")[-1]
    require(bool(node_id), "target_url 缺少 nodeId")
    if expected_node_id is not None:
        require(node_id == expected_node_id, "target_url 中的 nodeId 与指定文档不符")
    if "node_id" in raw:
        require(raw["node_id"] == node_id, "原始 node_id 与 target_url 不一致")

    source_sheets = raw.get("sheets")
    require(isinstance(source_sheets, list) and len(source_sheets) > 0, "原始快照须至少包含一张表")
    names = [sheet.get("name") if isinstance(sheet, dict) else None for sheet in source_sheets]
    ids = [sheet.get("sheet_id") if isinstance(sheet, dict) else None for sheet in source_sheets]
    require(all(isinstance(value, str) and value for value in names), "工作表名称缺失")
    require(len(set(names)) == len(names), "工作表名称重复")
    if expected_sheets is not None:
        require(tuple(names) == expected_sheets, "原始工作表名称/顺序与 --sheets 不一致")
    require(all(isinstance(value, str) and value for value in ids), "工作表缺少真实 sheet_id")
    require(len(set(ids)) == len(ids), "工作表 sheet_id 重复")

    converted = []
    for sheet in source_sheets:
        name = sheet["name"]
        sheet_id = sheet["sheet_id"]
        metadata = sheet.get("metadata")
        response = sheet.get("response")
        require(isinstance(metadata, dict) and isinstance(response, dict), f"{name}: 缺少 metadata/response")
        require(metadata.get("success") is True and response.get("success") is True, f"{name}: MCP 读取未成功")
        require(response.get("hasMore") is False, f"{name}: 返回可能截断")
        require(response.get("truncationReasons") == [], f"{name}: 返回含截断原因")
        require(metadata.get("id") == sheet_id and metadata.get("name") == name, f"{name}: metadata 身份不符")
        nonempty = metadata.get("nonEmptyRange")
        require(isinstance(nonempty, dict), f"{name}: 缺少 metadata.nonEmptyRange")
        returned_range = response.get("returnedRange")
        require(isinstance(returned_range, str) and nonempty.get("range") == returned_range,
                f"{name}: metadata 与返回范围不一致")
        c1, r1, c2, r2 = parse_range(returned_range)
        require(nonempty.get("lastRow") == r2 and nonempty.get("lastColumn") == column_label(c2),
                f"{name}: metadata 非空末行/列与范围不一致")
        require(nonempty.get("lastCell") == f"{column_label(c2)}{r2}", f"{name}: metadata.lastCell 与范围不一致")

        col_indices = response.get("colIndices")
        row_indices = response.get("rowIndices")
        require(isinstance(col_indices, list) and isinstance(row_indices, list), f"{name}: 缺少真实行列索引")
        expected_cols = [column_label(col) for col in range(c1, c2 + 1)]
        expected_rows = list(range(r1, r2 + 1))
        require(col_indices == expected_cols, f"{name}: colIndices 不连续或与范围不符")
        require(row_indices == expected_rows, f"{name}: rowIndices 不连续或与范围不符")
        require(len(col_indices) == c2 - c1 + 1, f"{name}: 列索引与真实范围宽度不符")

        csv_text = response.get("csv")
        require(isinstance(csv_text, str), f"{name}: 缺少 CSV 文本")
        try:
            matrix = list(csv.reader(StringIO(csv_text, newline=""), strict=True))
        except csv.Error as error:
            raise ValueError(f"{name}: RFC4180 CSV 无法解析: {error}") from error
        require(len(matrix) == len(row_indices), f"{name}: CSV 行数与真实 rowIndices 不符")
        require(all(len(row) == len(col_indices) for row in matrix), f"{name}: CSV 矩阵列数不完整")
        headers = matrix[0]
        require(len(set(headers)) == len(headers), f"{name}: 表头字段名重复")

        converted.append({
            "name": name,
            "sheet_id": sheet_id,
            "range": returned_range,
            "scope_complete": True,
            "tsv": to_tsv(matrix),
            "sheet_mcp": {
                "node_id": node_id,
                "sheet_id": sheet_id,
                "row_indices": row_indices,
                "col_indices": col_indices,
                "has_more": False,
            },
        })

    result = {
        "schema_version": 1,
        "transport": "dingtalk_sheet",
        "phase": "collect",
        "ok": True,
        "complete": True,
        "node_id": node_id,
        "target_url": target_url,
        "sheets": converted,
        "snapshot_basis": "mcp_capture",
    }
    if "captured_at" in raw:
        require(isinstance(raw["captured_at"], str) and bool(raw["captured_at"]), "captured_at 须为实际采集时间文本")
        result["captured_at"] = raw["captured_at"]
    return result


def document_index(raw: dict) -> dict:
    """Validate mixed full/column captures without promoting column data to a snapshot."""
    require(not raw.get('recovery_blocked') and raw.get('collection_failures') == [], '采集失败或恢复待处理')
    sheets=raw.get('sheets', [])
    require(sheets and len({s['name'] for s in sheets})==len(sheets) and len({s['sheet_id'] for s in sheets})==len(sheets), '采集目录缺失或重复')
    result=[]
    for sheet in sheets:
        scope=sheet.get('capture_scope')
        require(scope in ('full','doc_no_column'), '未知或补查范围不能用于完整去重')
        if scope=='full':
            convert({**raw,'sheets':[sheet]})
            rows=list(csv.reader(StringIO(sheet['response']['csv']),strict=True))
            require(rows[0].count('单号')==1,'单号表头缺失或重复')
            col=rows[0].index('单号')
        else:
            metadata,response=sheet['metadata'],sheet['response']
            bounds=metadata['nonEmptyRange']; c1,r1,c2,r2=parse_range(response['returnedRange'])
            require(c1==c2 and r1==1 and r2==bounds['lastRow'], '单号列范围不完整')
            require(bounds['range']==f"A1:{bounds['lastColumn']}{bounds['lastRow']}" and bounds['lastCell']==f"{bounds['lastColumn']}{bounds['lastRow']}" and c1<=column_number(bounds['lastColumn']), '原始整表元数据无效')
            # Validate identity/indices/CSV with a local range metadata view. The
            # returned index is explicitly column scope, never a business snapshot.
            scoped={**sheet,'metadata':{**metadata,'nonEmptyRange':{'range':response['returnedRange'],'lastColumn':column_label(c2),'lastRow':r2,'lastCell':f'{column_label(c2)}{r2}'}}}
            convert({**raw,'sheets':[scoped]})
            rows=list(csv.reader(StringIO(response['csv']),strict=True));col=0
            require(rows[0]==['单号'],'单号列表头不符')
        result.append({'name':sheet['name'],'sheet_id':sheet['sheet_id'],'doc_nos':[r[col] for r in rows[1:] if r[col]],'capture_scope':'doc_no_column','dedup_complete':True})
    return {'schema_version':1,'node_id':raw['node_id'],'target_url':raw['target_url'],'round_id':raw.get('round_id'),'scope':'document_numbers_only','sheets':result}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", required=True, type=Path, help="原始普通表格 CSV/metadata JSON")
    parser.add_argument("--out", required=True, type=Path, help="输出快照 JSON")
    parser.add_argument("--sheets", help="可选的授权工作表名称，按原始顺序逗号分隔")
    parser.add_argument("--node-id", help="可选的预期文档 nodeId")
    parser.add_argument('--doc-index',action='store_true',help='输出经过校验的全表单号目录，不生成业务快照')
    parser.add_argument('--full-sheets',help='混合采集中需要完整快照的表名，逗号分隔')
    args = parser.parse_args()
    raw_path = args.raw.resolve()
    out_path = args.out.resolve()
    require(raw_path != out_path, "输出路径不能覆盖 raw")
    require(raw_path.is_file(), f"raw 文件不存在: {raw_path}")
    require(not out_path.exists(), f"拒绝覆盖已存在输出文件: {out_path}")
    try:
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        expected_sheets = tuple(part.strip() for part in args.sheets.split(",")) if args.sheets else None
        if expected_sheets is not None:
            require(all(expected_sheets), "--sheets 含空名称")
            require(not args.doc_index, '--doc-index 不同时使用 --sheets')
        require(not (args.doc_index and args.full_sheets), '单号目录与完整表选择不能同时使用')
        if args.doc_index or args.full_sheets:
            index=document_index(raw)
            if args.node_id:require(index['node_id']==args.node_id,'文档身份不符')
            if args.doc_index:snapshot=index
            else:
                require(not args.sheets,'--full-sheets 不同时使用 --sheets')
                names=tuple(args.full_sheets.split(',')); require(all(names) and len(names)==len(set(names)), '完整表名缺失或重复')
                selected=[next((s for s in raw['sheets'] if s['name']==n and s['capture_scope']=='full'),None) for n in names]
                require(all(selected), '所选表没有完整采集证据')
                snapshot=convert({**raw,'sheets':selected},names,args.node_id)
        else:snapshot = convert(raw, expected_sheets, args.node_id)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open('x',encoding='utf-8') as f:f.write(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n")
    except (OSError, json.JSONDecodeError, ValueError) as error:
        print(f"sheet_snapshot: {error}", file=sys.stderr)
        return 2
    print(json.dumps({
        "ok": True,
        "out": str(out_path),
        "sheets": [s['name'] for s in snapshot['sheets']],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
