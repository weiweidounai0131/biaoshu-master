#!/usr/bin/env python3
"""Build a Stage 1 recommendation skeleton bound to an intake receipt.

The agent should enrich the generated JSON with source-backed project facts
before allowing the confirmation UI to advance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any


def canonical_json(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_data(data: Any) -> str:
    return hashlib.sha256(canonical_json(data)).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temp, path)


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_dir", type=Path)
    parser.add_argument("--project-name", default="待AI分析后填写")
    parser.add_argument("--customer", default="")
    parser.add_argument("--service-object", default="")
    parser.add_argument("--summary", default="")
    args = parser.parse_args()

    project_dir = args.project_dir.expanduser().resolve()
    data_dir = project_dir / "bid_confirm_ui"
    intake_source = read_json(data_dir / "intake-recommendations.json")
    intake_receipt = read_json(data_dir / "intake-confirmation.json")
    if intake_receipt.get("status") != "confirmed":
        parser.error("intake confirmation is not valid")
    if intake_receipt.get("source_sha256") != sha256_data(intake_source):
        parser.error("intake confirmation does not match the current intake recommendation")

    background_paths = intake_receipt.get("background_paths")
    if not isinstance(background_paths, list):
        background_paths = intake_receipt.get("source_paths", [])
    reference_paths = intake_receipt.get("reference_paths", [])
    if not isinstance(reference_paths, list):
        reference_paths = []
    all_paths = list(background_paths) + list(reference_paths)
    tender_position = intake_receipt.get("tender_position", "main")
    if tender_position not in {"main", "companion"}:
        parser.error("intake tender_position must be main or companion")
    target_pages = 100 if tender_position == "companion" else 700
    default_notes = ["评分表的商务部分不用写"]
    if tender_position == "companion":
        default_notes.append("内容不要精准，选择2~3个不重要的评分点不写")
    recommendation = {
        "schema_version": 1,
        "stage": "stage1",
        "project_id": intake_receipt["project_id"],
        "run_id": intake_receipt.get("run_id") or intake_source.get("run_id"),
        "generated_at": now(),
        "intake_confirmation_sha256": intake_receipt["confirmation_sha256"],
        "tender_position": tender_position,
        "generation_status": "generating",
        "source_summary": {
            "file_count": len(all_paths),
            "background_file_count": len(background_paths),
            "reference_file_count": len(reference_paths),
            "description": "背景资料是项目事实、需求和评分响应的权威底层；参考资料仅供择优借鉴，采用前必须核对适用性。AI应在读取资料后改写本说明，并确保所有项目口径均有依据。",
        },
        "materials": {
            "background_paths": background_paths,
            "reference_paths": reference_paths,
            "background_rule": "需求书、招标文件、评分表、澄清文件和客户正式资料决定项目事实、范围、数字、时限、评分响应和承诺边界。",
            "reference_rule": "历史项目、成熟策略和公司经验只提供可选方法与表达参考；不得替代背景资料，不得直接带入其中的客户、人员、业绩、数字或承诺。",
        },
        "project": {
            "project_name": args.project_name,
            "customer": args.customer,
            "service_object": args.service_object,
            "summary": args.summary,
            "scope": "",
            "region": "",
            "service_period": "",
            "objectives": "",
            "key_quantities": "",
            "terminology": "",
            "bidder_name": "我司",
        },
        "scoring": {
            "bid_scope": "技术部分和服务部分；评分表的商务部分不写；其余以本项目正式文件为准。",
            "summary": "",
            "evidence_boundary": "仅在招标文件或评分表明确要求时设置证明材料位置。",
        },
        "formatting": {
            "font_family": "仿宋_GB2312",
            "font_size": "四号（14磅）",
            "target_pages": target_pages,
            "max_heading_level": 6,
            "heading_style": "全部标题加粗、文字颜色为黑色，无首行缩进，段前间距10磅；标题层级按内容自然拆分，最多六级。",
            "body_style": "正文文字颜色为黑色，首行缩进2字符，1.25倍多倍行距；以文字为主体，列表、表格和图片辅助表达。",
            "list_style": "所有列表统一使用“·”，文字颜色为黑色。",
            "table_style": "表格无背景颜色，黑色单边框；表内文字全部居中、不设置首行缩进，文字颜色为黑色。",
        },
        "boundaries": {
            "confirmed_facts": "",
            "pending_items": "",
            "forbidden_content": "不得虚构公司、人员、证书、业绩、系统、数据或客户授权；不得写入内部资料来源和制作痕迹。",
        },
        "additional_notes": "\n".join(default_notes),
    }
    target = data_dir / "stage1-recommendations.json"
    write_json(target, recommendation)
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
