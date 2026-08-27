#!/usr/bin/env python3
"""Build the final delivery authorization recommendation from confirmed stages 1-3."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from bid_confirm_ui import server


EXCEL_COLUMNS = [
    "图号", "一级章节", "精确放置标题", "具体放置说明", "图片名称", "图片类型",
    "用途/核心表达", "核心节点", "构图建议", "画面方向", "是否章首总览图",
    "统一视觉要求", "应避免的风格", "生图补充说明", "AI生图提示词",
]


def clean_name(value: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', "-", value).strip(" .-")
    return cleaned or "技术服务标书"


def recommended_batch_count(planned_pages: int, chapter_count: int) -> int:
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
    return max(1, min(5, chapter_count, count))


def partition_chapters(chapters: list[dict[str, Any]], count: int) -> list[list[dict[str, Any]]]:
    remaining = list(chapters)
    groups: list[list[dict[str, Any]]] = []
    pages_left = sum(int(chapter.get("pages", 0) or 0) for chapter in remaining)
    for group_index in range(count):
        groups_left = count - group_index
        if groups_left == 1:
            groups.append(remaining)
            break
        target = pages_left / groups_left
        group: list[dict[str, Any]] = []
        group_pages = 0
        while remaining and len(remaining) > groups_left - 1:
            candidate = remaining[0]
            candidate_pages = int(candidate.get("pages", 0) or 0)
            if group and abs(group_pages - target) <= abs(group_pages + candidate_pages - target):
                break
            group.append(remaining.pop(0))
            group_pages += candidate_pages
        if not group:
            group.append(remaining.pop(0))
            group_pages = int(group[0].get("pages", 0) or 0)
        groups.append(group)
        pages_left -= group_pages
    return groups


def build_delivery(project_name: str, chapters: list[dict[str, Any]], image_count: int, additional_notes: str = "") -> dict[str, Any]:
    planned_pages = sum(int(chapter.get("pages", 0) or 0) for chapter in chapters)
    count = recommended_batch_count(planned_pages, len(chapters))
    groups = partition_chapters(chapters, count)
    stem = clean_name(project_name)
    batches = []
    for index, group in enumerate(groups, 1):
        first_number = str(group[0].get("number", ""))
        last_number = str(group[-1].get("number", ""))
        scope = first_number if first_number == last_number else f"{first_number}-{last_number}"
        batches.append({
            "id": f"word-batch-{index}",
            "order": index,
            "chapter_ids": [str(chapter["id"]) for chapter in group],
            "chapter_numbers": [str(chapter["number"]) for chapter in group],
            "chapter_titles": [str(chapter["title"]) for chapter in group],
            "planned_pages": sum(int(chapter.get("pages", 0) or 0) for chapter in group),
            "output_filename": f"{stem}-第{index}批-第{scope}章.docx",
        })
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
            "image_count": image_count,
        },
        "skill_boundary": {
            "generate_word_documents": True,
            "generate_image_plan_excel": True,
            "generate_images": False,
            "insert_images": False,
        },
        "delivery_output_dir": "",
        "additional_notes": additional_notes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
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
    recommendation = {
        "schema_version": 1,
        "stage": "stage4",
        "project_id": stage3_receipt["project_id"],
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "generation_status": "complete",
        "stage3_confirmation_sha256": stage3_receipt["confirmation_sha256"],
        "summary": {
            "project_name": project_name,
            "client": str(project.get("client") or project.get("customer") or ""),
            "project_overview": str(project.get("overview") or project.get("summary") or ""),
            "chapter_count": len(chapters),
            "planned_pages": planned_pages,
            "image_count": len(images),
        },
        "delivery": build_delivery(project_name, chapters, len(images), stage1_notes),
    }
    server.validate_stage4(recommendation, stage3_receipt, chapters)
    output = args.output.expanduser().resolve() if args.output else data_dir / server.STAGE4_INPUT
    server.atomic_write_json(output, recommendation)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
