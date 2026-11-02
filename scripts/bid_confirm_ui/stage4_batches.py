"""Deterministic, outline-aware Stage4 Word batching."""

from __future__ import annotations

from math import inf
import re
from typing import Any


MAX_BATCH_COUNT = 5
MAX_SEGMENT_LEVEL = 3


def _children(node: dict[str, Any]) -> list[dict[str, Any]]:
    value = node.get("children", [])
    return [child for child in value if isinstance(child, dict)] if isinstance(value, list) else []


def _pages(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _node_id(node: dict[str, Any]) -> str:
    return str(node.get("id", "")).strip()


def _complexity(node: dict[str, Any]) -> int:
    children = _children(node)
    return sum(_complexity(child) for child in children) if children else 1


def _allocate(total: int, children: list[dict[str, Any]]) -> list[int]:
    if not children:
        return []
    weights = [float(_pages(child.get("pages"))) or float(_complexity(child)) for child in children]
    weight_total = sum(weights) or float(len(children))
    exact = [total * weight / weight_total for weight in weights]
    allocated = [int(value) for value in exact]
    remainder = total - sum(allocated)
    priority = sorted(range(len(children)), key=lambda i: (-(exact[i] - allocated[i]), i))
    for index in priority[:remainder]:
        allocated[index] += 1
    return allocated


def _page_map(chapters: list[dict[str, Any]]) -> dict[str, int]:
    result: dict[str, int] = {}

    def visit(node: dict[str, Any], inherited_pages: int | None = None) -> None:
        node_id = _node_id(node)
        if not node_id or node_id in result:
            raise ValueError("Stage2目录节点ID缺失或重复，无法安全分批")
        total = _pages(node.get("pages")) if inherited_pages is None else inherited_pages
        result[node_id] = total
        children = _children(node)
        for child, child_pages in zip(children, _allocate(total, children)):
            visit(child, child_pages)

    for chapter in chapters:
        visit(chapter)
    return result


def _node_paths(chapters: list[dict[str, Any]]) -> tuple[dict[str, list[str]], dict[str, dict[str, Any]], dict[str, int]]:
    paths: dict[str, list[str]] = {}
    nodes: dict[str, dict[str, Any]] = {}
    order: dict[str, int] = {}
    cursor = 0

    def visit(node: dict[str, Any], prefix: list[str]) -> None:
        nonlocal cursor
        node_id = _node_id(node)
        path = [*prefix, node_id]
        paths[node_id] = path
        nodes[node_id] = node
        order[node_id] = cursor
        cursor += 1
        for child in _children(node):
            visit(child, path)

    for chapter in chapters:
        visit(chapter, [])
    return paths, nodes, order


def _number(node: dict[str, Any], path: list[str], nodes: dict[str, dict[str, Any]]) -> str:
    explicit = str(node.get("number", "")).strip()
    if explicit:
        return explicit
    index = 1
    parent = nodes.get(path[-2]) if len(path) > 1 else None
    siblings = _children(parent) if parent else []
    for pos, sibling in enumerate(siblings, 1):
        if _node_id(sibling) == path[-1]:
            index = pos
            break
    if len(path) == 1:
        return str(index)
    parent_number = str(parent.get("number", "")).strip() if parent else ""
    return f"{parent_number}.{index}" if parent_number else f"{len(path)}.{index}"


def _segment(node_id: str, path: list[str], nodes: dict[str, dict[str, Any]], pages: dict[str, int]) -> dict[str, Any]:
    node = nodes[node_id]
    return {
        "chapter_id": path[0],
        "path_ids": list(path),
        "level": len(path),
        "number": _number(node, path, nodes),
        "title": str(node.get("title", "")).strip(),
        "planned_pages": pages[node_id],
    }


def _split_segment(segment: dict[str, Any], nodes: dict[str, dict[str, Any]], pages: dict[str, int]) -> list[dict[str, Any]]:
    path = segment["path_ids"]
    if len(path) >= MAX_SEGMENT_LEVEL:
        return []
    parent = nodes[path[-1]]
    return [
        _segment(_node_id(child), [*path, _node_id(child)], nodes, pages)
        for child in _children(parent)
    ]


def _all_leaf_ids(node: dict[str, Any]) -> set[str]:
    children = _children(node)
    if not children:
        return {_node_id(node)}
    result: set[str] = set()
    for child in children:
        result.update(_all_leaf_ids(child))
    return result


def max_batch_count(chapters: list[dict[str, Any]]) -> int:
    def segment_capacity(node: dict[str, Any], level: int = 1) -> int:
        children = _children(node)
        if not children or level >= MAX_SEGMENT_LEVEL:
            return 1
        return sum(segment_capacity(child, level + 1) for child in children)

    capacity = sum(segment_capacity(chapter) for chapter in chapters)
    return max(1, min(MAX_BATCH_COUNT, capacity))


def _partition(units: list[dict[str, Any]], count: int) -> list[list[dict[str, Any]]]:
    total = sum(int(unit["planned_pages"]) for unit in units)
    target = total / count if count else 0
    prefix = [0]
    for unit in units:
        prefix.append(prefix[-1] + int(unit["planned_pages"]))

    # Dynamic programming finds the contiguous grouping with the least page
    # imbalance while preserving the original chapter and section order.
    costs = [[inf] * (len(units) + 1) for _ in range(count + 1)]
    cuts = [[-1] * (len(units) + 1) for _ in range(count + 1)]
    costs[0][0] = 0.0
    for groups in range(1, count + 1):
        for end in range(groups, len(units) + 1):
            for start in range(groups - 1, end):
                previous = costs[groups - 1][start]
                if previous == inf:
                    continue
                pages = prefix[end] - prefix[start]
                candidate = previous + (pages - target) ** 2
                if candidate < costs[groups][end]:
                    costs[groups][end] = candidate
                    cuts[groups][end] = start

    result: list[list[dict[str, Any]]] = []
    end = len(units)
    for groups in range(count, 0, -1):
        start = cuts[groups][end]
        if start < 0:
            raise ValueError("无法将可分章节连续分配到所选批次数")
        result.append(units[start:end])
        end = start
    result.reverse()
    return result


def _chapter_metadata(segments: list[dict[str, Any]], chapter_map: dict[str, dict[str, Any]]) -> tuple[list[str], list[str], list[str]]:
    ids: list[str] = []
    for segment in segments:
        if segment["chapter_id"] not in ids:
            ids.append(segment["chapter_id"])
    return (
        ids,
        [str(chapter_map[item].get("number", "")).strip() for item in ids],
        [str(chapter_map[item].get("title", "")).strip() for item in ids],
    )


def _scope(segments: list[dict[str, Any]], chapter_map: dict[str, dict[str, Any]]) -> str:
    if all(segment["level"] == 1 for segment in segments):
        numbers = [str(chapter_map[segment["chapter_id"]].get("number", "")).strip() for segment in segments]
        return f"第{numbers[0]}章" if len(numbers) == 1 else f"第{numbers[0]}-{numbers[-1]}章"
    values = []
    for segment in segments:
        chapter = chapter_map[segment["chapter_id"]]
        chapter_number = str(chapter.get("number", "")).strip()
        values.append(f"{chapter_number}章{segment['number']}节")
    if len(values) == 1:
        return values[0]
    if all(segment["chapter_id"] == segments[0]["chapter_id"] for segment in segments):
        return f"{values[0].split('章', 1)[0]}章{segments[0]['number']}-{segments[-1]['number']}节"
    return f"{values[0]}-{values[-1]}"


def _is_descendant(nodes: dict[str, dict[str, Any]], ancestor_id: str, target_id: str) -> bool:
    if ancestor_id == target_id:
        return True
    return any(_node_id(node) == target_id for node in _walk(_children(nodes[ancestor_id])))


def _walk(nodes: list[dict[str, Any]]):
    for node in nodes:
        yield node
        yield from _walk(_children(node))


def assign_image_ids(batches: list[dict[str, Any]], chapters: list[dict[str, Any]], images: list[dict[str, Any]]) -> list[dict[str, Any]]:
    _, nodes, _ = _node_paths(chapters)
    result = [{**batch, "image_ids": []} for batch in batches]
    for image in images:
        chapter_id = str(image.get("chapter_id", "")).strip()
        position = image.get("position") if isinstance(image.get("position"), dict) else {}
        target_id = str(position.get("outline_node_id", "")).strip()
        candidates: list[tuple[int, int]] = []
        for batch_index, batch in enumerate(result):
            for segment_index, segment in enumerate(batch["segments"]):
                if segment["chapter_id"] != chapter_id:
                    continue
                selected_id = segment["path_ids"][-1]
                if target_id and target_id not in nodes:
                    raise ValueError(f"图片{image.get('figure_no', '')}引用了不存在的目录节点")
                # A figure anchored to a chapter or section overview belongs
                # to the first output segment under that anchor. Figures
                # anchored below a selected segment stay with that segment.
                if not target_id or _is_descendant(nodes, selected_id, target_id) or _is_descendant(nodes, target_id, selected_id):
                    candidates.append((batch_index, segment_index))
        if not candidates:
            raise ValueError(f"图片{image.get('figure_no', '')}无法匹配到Stage4批次范围")
        batch_index, _segment_index = candidates[0]
        image_id = str(image.get("id", "")).strip()
        if not image_id:
            raise ValueError("Stage3图片缺少稳定ID，无法分配到Word批次")
        result[batch_index]["image_ids"].append(image_id)
    return result


def build_word_batches(chapters: list[dict[str, Any]], count: int, project_name: str, images: list[dict[str, Any]] | None = None) -> tuple[list[dict[str, Any]], int]:
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_BATCH_COUNT:
        raise ValueError("Word生成批次必须是1至5之间的整数")
    capacity = max_batch_count(chapters)
    if count > capacity:
        raise ValueError(f"当前确认目录最多可分成{capacity}个非空Word批次；请先细化Stage2框架")

    pages = _page_map(chapters)
    paths, nodes, _order = _node_paths(chapters)
    units = [_segment(_node_id(chapter), [_node_id(chapter)], nodes, pages) for chapter in chapters]
    total = sum(int(unit["planned_pages"]) for unit in units)
    target = total / count if count else 0
    while True:
        splittable = [(index, unit) for index, unit in enumerate(units) if _split_segment(unit, nodes, pages)]
        if not splittable:
            break
        if len(units) < count:
            index, unit = max(splittable, key=lambda item: (item[1]["planned_pages"], -item[0]))
        else:
            oversized = [item for item in splittable if item[1]["planned_pages"] > max(1, target * 1.35)]
            if not oversized:
                break
            index, unit = max(oversized, key=lambda item: (item[1]["planned_pages"], -item[0]))
        units[index:index + 1] = _split_segment(unit, nodes, pages)

    if len(units) < count:
        raise ValueError(f"当前确认目录最多可分成{len(units)}个非空Word批次；请先细化Stage2框架")

    groups = _partition(units, count)
    chapter_map = { _node_id(chapter): chapter for chapter in chapters }
    stem = re.sub(r'[\\/:*?"<>|]+', "-", str(project_name)).strip(" .-") or "技术服务标书"
    stem = stem[:120]
    batches: list[dict[str, Any]] = []
    for index, group in enumerate(groups, 1):
        ids, numbers, titles = _chapter_metadata(group, chapter_map)
        scope = _scope(group, chapter_map)[:70]
        batches.append({
            "id": f"word-batch-{index}",
            "order": index,
            "chapter_ids": ids,
            "chapter_numbers": numbers,
            "chapter_titles": titles,
            "segments": group,
            "image_ids": [],
            "planned_pages": sum(int(segment["planned_pages"]) for segment in group),
            "output_filename": f"{stem}-第{index}批-{scope}.docx",
        })
    if images:
        batches = assign_image_ids(batches, chapters, images)
    return batches, capacity


def validate_batch_plan(batches: list[dict[str, Any]], chapters: list[dict[str, Any]], image_ids: list[str] | None = None) -> None:
    pages = _page_map(chapters)
    paths, nodes, order = _node_paths(chapters)
    chapter_map = {_node_id(chapter): chapter for chapter in chapters}
    expected_atomic: set[str] = set()
    for chapter in chapters:
        expected_atomic.update(_all_leaf_ids(chapter))
    seen_atomic: set[str] = set()
    seen_files: set[str] = set()
    assigned_images: list[str] = []
    previous_order = -1

    for batch_index, batch in enumerate(batches, 1):
        if not isinstance(batch, dict):
            raise ValueError("Word批次明细必须是对象")
        required = {"id", "order", "chapter_ids", "chapter_numbers", "chapter_titles", "segments", "image_ids", "planned_pages", "output_filename"}
        if set(batch) != required or batch.get("id") != f"word-batch-{batch_index}" or batch.get("order") != batch_index:
            raise ValueError("Word批次字段或顺序无效")
        segments = batch.get("segments")
        if not isinstance(segments, list) or not segments:
            raise ValueError("每个Word批次必须包含至少一个目录分段")
        batch_pages = 0
        covered_roots: list[str] = []
        for segment in segments:
            fields = {"chapter_id", "path_ids", "level", "number", "title", "planned_pages"}
            if not isinstance(segment, dict) or set(segment) != fields:
                raise ValueError("Word批次目录分段字段无效")
            path = segment["path_ids"]
            if not isinstance(path, list) or not path or path[0] != segment["chapter_id"] or path[-1] not in nodes:
                raise ValueError("Word批次目录分段路径无效")
            if any(paths.get(node_id) != path[:index + 1] for index, node_id in enumerate(path)):
                raise ValueError("Word批次目录分段不是连续的父子路径")
            node_id = path[-1]
            if segment["level"] != len(path) or len(path) > MAX_SEGMENT_LEVEL:
                raise ValueError("Word批次分段层级必须为一级至三级")
            expected_number = _number(nodes[node_id], path, nodes)
            if str(segment["number"]).strip() != expected_number or str(segment["title"]).strip() != str(nodes[node_id].get("title", "")).strip():
                raise ValueError("Word批次分段标题或编号与已确认框架不一致")
            if segment["planned_pages"] != pages[node_id]:
                raise ValueError("Word批次分段页数与已确认页数预算不一致")
            coverage = _all_leaf_ids(nodes[node_id])
            if seen_atomic.intersection(coverage):
                raise ValueError("Word批次分段重复覆盖已确认目录内容")
            seen_atomic.update(coverage)
            batch_pages += pages[node_id]
            covered_roots.append(path[0])
            node_order = order[node_id]
            if node_order <= previous_order:
                raise ValueError("Word批次分段必须按原目录顺序连续排列")
            previous_order = node_order
        unique_roots = list(dict.fromkeys(covered_roots))
        if batch["chapter_ids"] != unique_roots:
            raise ValueError("Word批次一级章节范围与目录分段不一致")
        if batch["chapter_numbers"] != [str(chapter_map[item].get("number", "")).strip() for item in unique_roots]:
            raise ValueError("Word批次一级章节编号已过期")
        if batch["chapter_titles"] != [str(chapter_map[item].get("title", "")).strip() for item in unique_roots]:
            raise ValueError("Word批次一级章节标题已过期")
        if batch["planned_pages"] != batch_pages:
            raise ValueError("Word批次预计页数与所含目录分段不一致")
        filename = str(batch.get("output_filename", "")).strip()
        if not filename.lower().endswith(".docx") or filename in seen_files:
            raise ValueError("Word批次文件名必须唯一且以.docx结尾")
        seen_files.add(filename)
        image_batch_ids = batch.get("image_ids")
        if not isinstance(image_batch_ids, list) or any(not isinstance(item, str) or not item.strip() for item in image_batch_ids) or len(image_batch_ids) != len(set(image_batch_ids)):
            raise ValueError("Word批次图片编号无效或重复")
        assigned_images.extend(image_batch_ids)

    if seen_atomic != expected_atomic:
        raise ValueError("Word批次必须按原顺序完整覆盖全部已确认目录内容，且不得遗漏")
    if len(assigned_images) != len(set(assigned_images)):
        raise ValueError("同一张规划图片不能重复分配到多个Word批次")
    if image_ids is not None and sorted(assigned_images) != sorted(image_ids):
        raise ValueError("Word批次图片占位分配必须覆盖每张图片且不得重复")
