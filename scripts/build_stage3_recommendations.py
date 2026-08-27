#!/usr/bin/env python3
"""Build Stage 3 image recommendations from a confirmed Stage 2 outline.

The external inventory stays easy to author: every image names an
``anchor_heading_number``. This script resolves that number against the
confirmed outline and emits the node IDs and titles required by the Stage 3
confirmation UI.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from bid_delivery_ui.image_prompt import compose_ai_image_prompt


IMAGE_TYPES = {"章首总览图", "流程图", "泳道图", "矩阵图", "时间轴", "生命周期图", "对比图", "其他"}

COMPANION_VISUAL_STYLES = [
    {"key": "blueprint", "palette": "雾蓝、石墨灰和少量柠檬黄", "style": "工程蓝图式线框信息图", "background": "浅灰蓝底，使用细线和局部网格", "density": "中等偏疏，突出流程节点", "avoid": ["高饱和渐变", "大面积照片", "复杂装饰纹理"]},
    {"key": "paper-cut", "palette": "朱砂红、米白和墨黑", "style": "现代剪纸分层信息图", "background": "暖白底，使用平面色块形成层次", "density": "中等，重点内容分区明确", "avoid": ["拟物质感", "密集小字", "通用商务蓝模板"]},
    {"key": "terminal", "palette": "深青、薄荷绿和白色", "style": "轻量终端监控式图表", "background": "深色底，局部使用等宽标注", "density": "偏高，适合展示指标和状态", "avoid": ["霓虹发光", "紫色渐变", "过度科技化装饰"]},
    {"key": "museum", "palette": "橄榄绿、砖红和浅灰", "style": "档案展板式模块化信息图", "background": "浅灰底，使用不规则留白和编号块", "density": "偏疏，保留章节呼吸感", "avoid": ["卡通插画", "统一圆角卡片墙", "营销海报感"]},
]


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
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, path)


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def validate_receipt_hash(receipt: dict[str, Any]) -> None:
    expected = str(receipt.get("confirmation_sha256", "")).strip()
    unsigned = dict(receipt)
    unsigned.pop("confirmation_sha256", None)
    if not expected or sha256_data(unsigned) != expected:
        raise ValueError("stage2 confirmation_sha256 is invalid")


def validate_stage2(
    data_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    source = read_json(data_dir / "stage2-recommendations.json")
    receipt = read_json(data_dir / "stage2-confirmation.json")
    stage1_receipt = read_json(data_dir / "stage1-confirmation.json")

    if source.get("schema_version") != 1 or source.get("stage") != "stage2":
        raise ValueError("unsupported stage2 recommendation schema")
    if receipt.get("schema_version") != 1 or receipt.get("stage") != "stage2":
        raise ValueError("unsupported stage2 confirmation schema")
    if receipt.get("status") != "confirmed":
        raise ValueError("stage2 is not confirmed")
    validate_receipt_hash(receipt)

    project_id = source.get("project_id")
    if not project_id or receipt.get("project_id") != project_id:
        raise ValueError("stage2 project_id does not match its recommendation")
    if stage1_receipt.get("project_id") != project_id:
        raise ValueError("stage2 project_id does not match stage1")
    if receipt.get("source_sha256") != sha256_data(source):
        raise ValueError("stage2 confirmation does not match the current recommendation")
    stage1_hash = stage1_receipt.get("confirmation_sha256")
    if not stage1_hash or source.get("stage1_confirmation_sha256") != stage1_hash:
        raise ValueError("stage2 recommendation is stale because stage1 changed")
    if receipt.get("stage1_confirmation_sha256") != stage1_hash:
        raise ValueError("stage2 confirmation is stale because stage1 changed")

    confirmed_data = receipt.get("data")
    if not isinstance(confirmed_data, dict):
        raise ValueError("stage2 confirmation data must be an object")
    chapters = confirmed_data.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        raise ValueError("stage2 confirmation must contain a non-empty chapter outline")
    return source, receipt, confirmed_data


def outline_indexes(chapters: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    by_number: dict[str, dict[str, Any]] = {}
    by_chapter: dict[str, dict[str, Any]] = {}

    def walk(nodes: Any, expected_level: int, chapter: dict[str, Any] | None = None) -> None:
        if not isinstance(nodes, list):
            raise ValueError("outline children must be arrays")
        for node in nodes:
            if not isinstance(node, dict):
                raise ValueError("outline nodes must be objects")
            number = str(node.get("number", "")).strip()
            node_id = str(node.get("id", "")).strip()
            title = str(node.get("title", "")).strip()
            level = int(node.get("level", 0))
            if not number or not node_id or not title or level != expected_level:
                raise ValueError(f"invalid outline node: {number or '<unnumbered>'}")
            if number in by_number:
                raise ValueError(f"duplicate outline number: {number}")
            by_number[number] = node
            active_chapter = node if level == 1 else chapter
            if level == 1:
                by_chapter[number] = node
            walk(node.get("children", []), expected_level + 1, active_chapter)

    walk(chapters, 1)
    return by_number, by_chapter


def text_list(value: Any, label: str, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    result = [str(item).strip() for item in value if str(item).strip()]
    if not result and not allow_empty:
        raise ValueError(f"{label} cannot be empty")
    return result


def tender_position_from_stage1(data_dir: Path) -> str:
    receipt = read_json(data_dir / "stage1-confirmation.json")
    value = receipt.get("tender_position") or (receipt.get("data") or {}).get("tender_position")
    return "companion" if value == "companion" else "main"


def companion_visual_direction() -> dict[str, Any]:
    preset = secrets.choice(COMPANION_VISUAL_STYLES)
    return {key: value for key, value in preset.items() if key != "key"} | {
        "selection_mode": "companion-random",
        "random_style_key": preset["key"],
    }


def build_chapter_settings(
    source: Any,
    by_chapter: dict[str, dict[str, Any]],
    overview_counts: dict[str, int],
) -> list[dict[str, Any]]:
    if not isinstance(source, list) or not source:
        raise ValueError("chapter_settings must be a non-empty array")
    seen: set[str] = set()
    result = []
    for setting in source:
        if not isinstance(setting, dict):
            raise ValueError("chapter settings must be objects")
        number = str(setting.get("chapter_number", "")).strip()
        chapter = by_chapter.get(number)
        if chapter is None or number in seen:
            raise ValueError(f"invalid or duplicate chapter setting: {number}")
        seen.add(number)
        policy = str(setting.get("overview_policy", "")).strip()
        reason = str(setting.get("overview_reason", "")).strip()
        if policy not in {"required", "exempt"}:
            raise ValueError(f"chapter {number} overview_policy must be required or exempt")
        if policy == "exempt" and not reason:
            raise ValueError(f"chapter {number} exempt overview policy requires a reason")
        overview_count = overview_counts.get(number, 0)
        if policy == "required" and overview_count != 1:
            raise ValueError(f"chapter {number} requires exactly one overview image")
        if policy == "exempt" and overview_count:
            raise ValueError(f"chapter {number} does not allow an overview image")
        result.append({
            "chapter_id": chapter["id"],
            "chapter_number": number,
            "chapter_title": chapter["title"],
            "overview_policy": policy,
            "overview_reason": reason,
        })
    missing = sorted(set(by_chapter) - seen)
    if missing:
        raise ValueError("missing chapter settings: " + ", ".join(missing))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_dir", type=Path)
    parser.add_argument("inventory", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    project_dir = args.project_dir.expanduser().resolve()
    inventory_path = args.inventory.expanduser().resolve()
    data_dir = project_dir / "bid_confirm_ui"
    _, stage2_receipt, confirmed = validate_stage2(data_dir)
    inventory = read_json(inventory_path)

    if inventory.get("schema_version") != 1 or inventory.get("stage") != "stage3-inventory":
        parser.error("unsupported Stage 3 inventory schema")
    project_id = str(stage2_receipt["project_id"])
    if inventory.get("project_id") != project_id:
        parser.error("inventory project_id does not match Stage 2")
    stage2_hash = str(stage2_receipt["confirmation_sha256"])
    expected_hash = inventory.get("stage2_confirmation_sha256")
    if expected_hash not in (None, "", "bind_at_build") and expected_hash != stage2_hash:
        parser.error("inventory is bound to a different Stage 2 confirmation")

    visual_direction = inventory.get("visual_direction")
    if not isinstance(visual_direction, dict) or not visual_direction:
        parser.error("visual_direction must be a non-empty object")
    for key in ("palette", "style", "background", "density"):
        if not str(visual_direction.get(key, "")).strip():
            parser.error(f"visual_direction.{key} must contain an AI suggestion")
    try:
        text_list(visual_direction.get("avoid"), "visual_direction.avoid")
    except ValueError as exc:
        parser.error(str(exc))
    if tender_position_from_stage1(data_dir) == "companion":
        visual_direction = companion_visual_direction()
    raw_images = inventory.get("images")
    if not isinstance(raw_images, list) or not raw_images:
        parser.error("images must be a non-empty array")

    by_number, by_chapter = outline_indexes(confirmed["chapters"])
    seen_ids: set[str] = set()
    seen_figures: set[str] = set()
    orders_by_chapter: dict[str, set[int]] = {}
    overview_counts: dict[str, int] = {}
    images = []
    for raw in raw_images:
        if not isinstance(raw, dict):
            parser.error("image inventory entries must be objects")
        image_id = str(raw.get("id", "")).strip()
        figure_no = str(raw.get("figure_no", "")).strip()
        order = int(raw.get("order", 0))
        anchor_number = str(raw.get("anchor_heading_number", "")).strip()
        if not image_id or image_id in seen_ids:
            parser.error(f"invalid or duplicate image id: {image_id}")
        if not figure_no or figure_no in seen_figures:
            parser.error(f"invalid or duplicate figure_no: {figure_no}")
        if order <= 0:
            parser.error(f"invalid image order: {order}")
        anchor = by_number.get(anchor_number)
        if anchor is None:
            parser.error(f"{figure_no} anchor heading does not exist: {anchor_number}")
        chapter_number = anchor_number.split(".", 1)[0]
        chapter = by_chapter.get(chapter_number)
        if chapter is None:
            parser.error(f"{figure_no} cannot resolve its chapter")
        chapter_orders = orders_by_chapter.setdefault(chapter_number, set())
        if order in chapter_orders:
            parser.error(f"duplicate image order {order} in chapter {chapter_number}")
        if figure_no != f"图{chapter_number}-{order}":
            parser.error(f"{figure_no} must match chapter {chapter_number} order {order}")
        is_overview = bool(raw.get("is_chapter_overview", False))
        if is_overview and anchor_number != chapter_number:
            parser.error(f"{figure_no} chapter overview must anchor to the level-1 chapter")

        seen_ids.add(image_id)
        seen_figures.add(figure_no)
        chapter_orders.add(order)
        if is_overview:
            overview_counts[chapter_number] = overview_counts.get(chapter_number, 0) + 1
        try:
            core_nodes = text_list(raw.get("core_nodes"), f"{figure_no} core_nodes")
        except ValueError as exc:
            parser.error(str(exc))
        images.append({
            "id": image_id,
            "figure_no": figure_no,
            "order": order,
            "chapter_id": chapter["id"],
            "chapter_number": chapter_number,
            "chapter_title": chapter["title"],
            "position": {
                "outline_node_id": anchor["id"],
                "outline_number": anchor_number,
                "outline_title": anchor["title"],
                "placement_note": str(raw.get("placement_note", "")).strip(),
            },
            "name": str(raw.get("name", "")).strip(),
            "type": str(raw.get("type", "")).strip(),
            "purpose": str(raw.get("purpose", "")).strip(),
            "core_nodes": core_nodes,
            "composition": str(raw.get("composition", "")).strip(),
            "orientation": str(raw.get("orientation", "")).strip(),
            "is_chapter_overview": is_overview,
            "origin": "ai",
        })
        raw_prompt_value = raw.get("ai_prompt")
        raw_prompt = raw_prompt_value.strip() if isinstance(raw_prompt_value, str) else ""
        images[-1]["ai_prompt"] = raw_prompt or compose_ai_image_prompt(images[-1], visual_direction)
        for key in ("name", "type", "purpose", "composition", "orientation"):
            if not images[-1][key]:
                parser.error(f"{figure_no} {key} cannot be blank")
        if not images[-1]["ai_prompt"]:
            parser.error(f"{figure_no} ai_prompt cannot be blank")
        if images[-1]["type"] not in IMAGE_TYPES:
            parser.error(f"{figure_no} uses unsupported image type: {images[-1]['type']}")

    for chapter_number, orders in orders_by_chapter.items():
        if orders != set(range(1, len(orders) + 1)):
            parser.error(f"image orders must be continuous from 1 in chapter {chapter_number}")
    chapter_rank = {number: index for index, number in enumerate(by_chapter)}
    images.sort(key=lambda item: (chapter_rank[item["chapter_number"]], item["order"]))
    try:
        chapter_settings = build_chapter_settings(
            inventory.get("chapter_settings"), by_chapter, overview_counts
        )
    except ValueError as exc:
        parser.error(str(exc))
    cleanup_actions = inventory.get("cleanup_actions")
    if not isinstance(cleanup_actions, list):
        parser.error("cleanup_actions must be an array")
    for action in cleanup_actions:
        if not isinstance(action, dict) or not str(action.get("action", "")).strip():
            parser.error("cleanup_actions entries must be objects with an action")

    recommendation = {
        "schema_version": 1,
        "stage": "stage3",
        "project_id": project_id,
        "generated_at": now(),
        "generation_status": "complete",
        "stage2_confirmation_sha256": stage2_hash,
        "visual_direction": visual_direction,
        "chapter_settings": chapter_settings,
        "images": images,
        "cleanup_actions": cleanup_actions,
    }
    output = args.output.expanduser().resolve() if args.output else data_dir / "stage3-recommendations.json"
    write_json(output, recommendation)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
