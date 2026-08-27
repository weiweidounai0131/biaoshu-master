"""Build a deterministic per-image prompt from a confirmed image plan.

The prompt is a hand-off instruction for a local/native image model. It only
uses confirmed planning fields and never invents project facts, people,
metrics, logos, or customer material.
"""

from __future__ import annotations

from typing import Any


ORIENTATION_LABELS = {
    "landscape": "横向",
    "portrait": "纵向",
    "square": "方形",
    "auto": "自适应",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _visual_value(visual: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = visual.get(key)
        if isinstance(value, (str, int, float)) and _text(value):
            return _text(value)
    return ""


def visual_direction_text(visual: dict[str, Any]) -> str:
    values = (
        _visual_value(visual, "palette", "primary_colors", "main_colors", "color_palette", "配色", "主色"),
        _visual_value(visual, "style", "visual_style", "风格", "视觉风格"),
        _visual_value(visual, "background", "background_style", "背景", "背景风格"),
        _visual_value(visual, "density", "information_density", "info_density", "信息密度"),
    )
    return "；".join(value for value in values if value)


def visual_avoid_text(visual: dict[str, Any]) -> str:
    for key in ("avoid", "avoid_styles", "avoid_style", "negative_prompt", "避免的风格", "应避免", "避免"):
        value = visual.get(key)
        if isinstance(value, list):
            result = "、".join(_text(item) for item in value if _text(item))
            if result:
                return result
        elif _text(value):
            return _text(value)
    return ""


def compose_ai_image_prompt(image: dict[str, Any], visual: dict[str, Any]) -> str:
    position = image.get("position") if isinstance(image.get("position"), dict) else {}
    nodes = image.get("core_nodes") if isinstance(image.get("core_nodes"), list) else []
    node_text = "、".join(_text(item) for item in nodes if _text(item)) or "按已确认的核心表达组织信息"
    orientation = ORIENTATION_LABELS.get(_text(image.get("orientation")), _text(image.get("orientation"))) or "自适应"
    image_type = _text(image.get("type")) or "信息图"
    figure_no = _text(image.get("figure_no"))
    name = _text(image.get("name"))
    chapter = f"{_text(image.get('chapter_number'))} {_text(image.get('chapter_title'))}".strip()
    outline = f"{_text(position.get('outline_number'))} {_text(position.get('outline_title'))}".strip()
    placement = _text(position.get("placement_note"))
    purpose = _text(image.get("purpose"))
    composition = _text(image.get("composition"))
    direction = visual_direction_text(visual)
    avoid = visual_avoid_text(visual)

    parts = [
        f"生成一张中文{image_type}，图号为“{figure_no}”，图片名称为“{name}”。",
        f"它用于{chapter}，放置在“{outline}”位置；具体放置说明：{placement}。",
        f"核心表达：{purpose}。画面只围绕以下已确认核心节点组织：{node_text}。",
        f"构图要求：{composition}。画面方向为{orientation}，层级、连线和阅读顺序应清晰，重点信息优先可读。",
    ]
    if direction:
        parts.append(f"统一视觉方向：{direction}。")
    if avoid:
        parts.append(f"应避免的风格或限制：{avoid}。")
    parts.append(
        "图中文字只使用规划中已经出现或由规划字段明确要求的中文，不添加未经确认的客户名称、人员姓名、证书、业绩、数据、日期、Logo、品牌标识或承诺；"
        "不出现英文、乱码、水印、虚构事实、无关装饰和与正文无关的内容。"
    )
    return "".join(parts)


def prompt_for_image(image: dict[str, Any], visual: dict[str, Any]) -> str:
    existing = _text(image.get("ai_prompt"))
    return existing or compose_ai_image_prompt(image, visual)
