#!/usr/bin/env python3
"""Build the final delivery authorization recommendation from confirmed stages 1-3."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from bid_confirm_ui import server, stage4_batches


EXCEL_COLUMNS = [
    "图号", "一级章节", "精确放置标题", "具体放置说明", "图片名称", "图片类型",
    "用途/核心表达", "核心节点", "构图建议", "画面方向", "是否章首总览图",
    "统一视觉要求", "应避免的风格", "生图补充说明", "AI生图提示词",
]


def clean_name(value: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', "-", value).strip(" .-")
    return cleaned or "技术服务标书"


def recommended_batch_count(planned_pages: int, batch_capacity: int) -> int:
    if planned_pages <= 250:
        count = 1
    elif planned_pages <= 500:
        count = 2
    elif planned_pages <= 800:
        count = 3
    elif planned_pages <= 1100:
        count = 4
    else:
        count = 5
    return max(1, min(stage4_batches.MAX_BATCH_COUNT, batch_capacity, count))


def build_delivery(project_name: str, chapters: list[dict[str, Any]], images: list[dict[str, Any]], additional_notes: str = "") -> tuple[dict[str, Any], int]:
    planned_pages = sum(int(chapter.get("pages", 0) or 0) for chapter in chapters)
    capacity = stage4_batches.max_batch_count(chapters)
    count = recommended_batch_count(planned_pages, capacity)
    batches, capacity = stage4_batches.build_word_batches(chapters, count, project_name, images)
    stem = clean_name(project_name)
    return {
        "word_batch_count": count,
        "word_batches": batches,
        "image_plan_workbook": {
            "count": 1,
            "format": ".xlsx",
            "filename": f"{stem}-图片规划表.xlsx",
            "purpose": "供用户交给其他AI或最终交付后的本机生图流程逐张使用；G0至G7不生成图片、不保存成图路径，也不把图片插入Word。",
            "worksheet_names": ["图片规划清单"],
            "columns": EXCEL_COLUMNS,
            "image_count": len(images),
        },
        "skill_boundary": {
            "generate_word_documents": True,
            "generate_image_plan_excel": True,
            "generate_images": False,
            "insert_images": False,
        },
        "delivery_output_dir": "",
        "additional_notes": additional_notes,
    }, capacity

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_dir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--subagents-available", action="store_true", help="only set after verifying the current AI host can create and collect isolated subagents")
    parser.add_argument("--subagent-max-parallel", type=int, default=0)
    args = parser.parse_args()
    if args.subagents_available and args.subagent_max_parallel < 1:
        parser.error("--subagents-available requires a positive --subagent-max-parallel")
    if not args.subagents_available and args.subagent_max_parallel != 0:
        parser.error("--subagent-max-parallel requires --subagents-available")
    project_dir = args.project_dir.expanduser().resolve()
    data_dir = project_dir / server.DATA_DIR_NAME
    stage3_bound = server.stage3_confirmation_valid(data_dir)
    stage1_bound = server.stage1_confirmation_valid(data_dir)
    if not stage3_bound or not stage1_bound:
        parser.error("stages 1-3 must be valid and confirmed")
    _, _, stage3_receipt = stage3_bound
    _, stage1_receipt = stage1_bound
    chapters = server.confirmed_stage2_chapters(data_dir)
    project = stage1_receipt.get("data", {}).get("project", {})
    stage1_notes = str(stage1_receipt.get("data", {}).get("additional_notes", "")).strip()
    project_name = str(project.get("project_name", "技术服务标书")).strip() or "技术服务标书"
    planned_pages = sum(int(chapter.get("pages", 0) or 0) for chapter in chapters)
    images = stage3_receipt.get("data", {}).get("images", [])
    delivery, batch_capacity = build_delivery(project_name, chapters, images, stage1_notes)
    capability = {
        "subagents_available": bool(args.subagents_available),
        "max_parallel": int(args.subagent_max_parallel),
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    recommendation = {
        "schema_version": 1,
        "stage": "stage4",
        "project_id": stage3_receipt["project_id"],
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "generation_status": "complete",
        "stage3_confirmation_sha256": stage3_receipt["confirmation_sha256"],
        "production_capabilities": {"word_batch_capacity": batch_capacity, "subagents": capability},
        "summary": {
            "project_name": project_name,
            "client": str(project.get("client") or project.get("customer") or ""),
            "project_overview": str(project.get("overview") or project.get("summary") or ""),
            "chapter_count": len(chapters),
            "planned_pages": planned_pages,
            "image_count": len(images),
        },
        "delivery": delivery,
    }
    server.validate_stage4(recommendation, stage3_receipt, chapters)
    output = args.output.expanduser().resolve() if args.output else data_dir / server.STAGE4_INPUT
    server.atomic_write_json(output, recommendation)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
