#!/usr/bin/env python3
"""Coordinate the opt-in, post-delivery local image-generation hand-off.

The script deliberately does not call an image model.  It verifies the final
delivery receipt, writes a small auditable request for the current host's
local/native model, and records model-produced local files.  The skill or host
conversation remains responsible for asking the user for the exact opt-in,
showing the example, and calling the local model.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
from pathlib import Path
from typing import Any

try:
    from . import protocol
    from .image_prompt import prompt_for_image
except ImportError:
    import protocol
    from image_prompt import prompt_for_image


IMAGE_GENERATION_DIR_NAME = "image-generation"
STATE_NAME = "state.json"
REQUESTS_DIR_NAME = "requests"
RESULTS_DIR_NAME = "results"
HISTORY_DIR_NAME = "history"
ALLOWED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
ACTIVE_STATUSES = {"example_pending", "example_ready", "awaiting_batch_count", "generating"}


def _root(project_dir: Path) -> Path:
    return protocol.delivery_dir(project_dir) / IMAGE_GENERATION_DIR_NAME


def _state_path(project_dir: Path) -> Path:
    return _root(project_dir) / STATE_NAME


def _request_path(project_dir: Path, name: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise ValueError("生图请求文件名无效")
    return _root(project_dir) / REQUESTS_DIR_NAME / name


def _result_path(project_dir: Path, name: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise ValueError("生图结果文件名无效")
    return _root(project_dir) / RESULTS_DIR_NAME / name


def _write_json(path: Path, data: dict[str, Any]) -> None:
    protocol.atomic_write_json(path, data)


def _read_state(project_dir: Path) -> dict[str, Any] | None:
    path = _state_path(project_dir)
    if not path.is_file():
        return None
    return protocol.read_json(path)


def _require_state(project_dir: Path) -> dict[str, Any]:
    state = _read_state(project_dir)
    if state is None:
        raise ValueError("尚未明确回复“继续”启动生图流程")
    return state


def _require_final_image_plan(project_dir: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    manifest = protocol.load_manifest(project_dir)
    if manifest.get("status") != "final_confirmed":
        raise ValueError("只有最终交付已确认后，才能启动可选生图流程")
    final_receipt = protocol._require_final_confirmation(project_dir, manifest)
    payload = protocol.image_plan_payload(project_dir)
    images = payload.get("images")
    if not isinstance(images, list) or not images:
        raise ValueError("已确认图片规划中没有可生成的图片")
    return manifest, final_receipt, payload


def _image_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    visual = payload.get("visual_direction") or {}
    rows: list[dict[str, Any]] = []
    for raw in payload["images"]:
        image = copy.deepcopy(raw)
        image["ai_prompt"] = prompt_for_image(image, visual)
        rows.append(image)
    return rows


def _base_context(manifest: dict[str, Any], final_receipt: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    workbook_path = str(final_receipt["image_plan_workbook"]["delivery_output_path"])
    return {
        "schema_version": 1,
        "kind": "bid_delivery_local_image_generation_request",
        "project_id": manifest["project_id"],
        "final_confirmation_sha256": final_receipt["confirmation_sha256"],
        "image_plan_workbook_path": workbook_path,
        "image_plan_workbook_sha256": final_receipt["image_plan_workbook"]["delivery_output_sha256"],
        "visual_direction": copy.deepcopy(payload["visual_direction"]),
        "created_at": protocol.utc_now(),
    }


def _archive_previous_state(project_dir: Path, state: dict[str, Any]) -> None:
    digest = str(state.get("final_confirmation_sha256", ""))[:16] or "previous"
    archive = _root(project_dir) / HISTORY_DIR_NAME / f"state-{digest}.json"
    if not archive.exists():
        _write_json(archive, state)


def start_example(project_dir: Path) -> dict[str, Any]:
    manifest, final_receipt, payload = _require_final_image_plan(project_dir)
    rows = _image_rows(payload)
    final_hash = final_receipt["confirmation_sha256"]
    previous = _read_state(project_dir)
    if previous and previous.get("final_confirmation_sha256") == final_hash:
        if previous.get("status") in ACTIVE_STATUSES | {"complete"}:
            return {"status": "already_started", "state": previous}
        _archive_previous_state(project_dir, previous)
    elif previous:
        _archive_previous_state(project_dir, previous)

    example = rows[0]
    context = _base_context(manifest, final_receipt, payload)
    request = {
        **context,
        "kind": "bid_delivery_local_image_generation_example_request",
        "request_type": "example",
        "example_image_id": example["id"],
        "image": example,
        "instruction": "只生成这一张示例图，用于确认整体视觉风格；不得生成其他图片，不得插入Word。",
    }
    request_path = _request_path(project_dir, "example.json")
    _write_json(request_path, request)
    state = {
        "schema_version": 1,
        "kind": "bid_delivery_local_image_generation_state",
        "status": "example_pending",
        "project_id": manifest["project_id"],
        "final_confirmation_sha256": final_hash,
        "example_image_id": example["id"],
        "example_request_path": str(request_path),
        "example_result_path": None,
        "example_revision": 0,
        "requested_batch_count": None,
        "pending_batches": [],
        "updated_at": protocol.utc_now(),
    }
    _write_json(_state_path(project_dir), state)
    return {
        "status": state["status"],
        "request_path": str(request_path),
        "workbook_path": context["image_plan_workbook_path"],
        "image": example,
        "state": state,
    }


def _require_local_image(path_value: Any) -> Path:
    path = Path(str(path_value or "").strip()).expanduser()
    if not path.is_absolute() or not path.is_file():
        raise ValueError("生图结果必须是存在的本机绝对路径")
    if path.suffix.lower() not in ALLOWED_IMAGE_SUFFIXES:
        raise ValueError("生图结果仅支持 PNG、JPG、JPEG 或 WEBP")
    return path.resolve()


def record_example(project_dir: Path, image_path: str) -> dict[str, Any]:
    state = _require_state(project_dir)
    if state.get("status") != "example_pending":
        raise ValueError("当前不在等待示例图结果的状态")
    path = _require_local_image(image_path)
    result_path = _result_path(project_dir, "example.json")
    result = {
        "schema_version": 1,
        "kind": "bid_delivery_local_image_generation_example_result",
        "project_id": state["project_id"],
        "final_confirmation_sha256": state["final_confirmation_sha256"],
        "example_image_id": state["example_image_id"],
        "image_path": str(path),
        "image_sha256": protocol.sha256_file(path),
        "recorded_at": protocol.utc_now(),
    }
    _write_json(result_path, result)
    state["status"] = "example_ready"
    state["example_result_path"] = str(result_path)
    state["example_image_path"] = str(path)
    state["example_image_sha256"] = result["image_sha256"]
    state["updated_at"] = protocol.utc_now()
    _write_json(_state_path(project_dir), state)
    return {"status": state["status"], "result_path": str(result_path), "state": state}


def revise_example(project_dir: Path, instruction: str) -> dict[str, Any]:
    state = _require_state(project_dir)
    if state.get("status") not in {"example_ready", "awaiting_batch_count"}:
        raise ValueError("示例图只有在生成完成后、确认批量生成前才能修改")
    text = str(instruction or "").strip()
    if not text:
        raise ValueError("请填写示例图修改要求")
    revision = int(state.get("example_revision", 0)) + 1
    name = f"example-revision-{revision:02d}.json"
    current_request = _request_path(project_dir, "example.json")
    request = protocol.read_json(current_request)
    revised = {
        **request,
        "kind": "bid_delivery_local_image_generation_example_revision_request",
        "request_type": "example-revision",
        "revision": revision,
        "instruction": text,
        "previous_result_path": state.get("example_result_path"),
        "created_at": protocol.utc_now(),
    }
    request_path = _request_path(project_dir, name)
    _write_json(request_path, revised)
    state["status"] = "example_pending"
    state["example_revision"] = revision
    state["example_request_path"] = str(request_path)
    state["example_result_path"] = None
    state["updated_at"] = protocol.utc_now()
    _write_json(_state_path(project_dir), state)
    return {"status": state["status"], "request_path": str(request_path), "state": state}


def confirm_example(project_dir: Path) -> dict[str, Any]:
    state = _require_state(project_dir)
    if state.get("status") != "example_ready":
        raise ValueError("请先生成并查看示例图")
    state["status"] = "awaiting_batch_count"
    state["updated_at"] = protocol.utc_now()
    _write_json(_state_path(project_dir), state)
    return {"status": state["status"], "state": state}


def _split_batches(rows: list[dict[str, Any]], count: int) -> list[list[dict[str, Any]]]:
    if not rows:
        return []
    count = min(count, len(rows))
    base, remainder = divmod(len(rows), count)
    batches: list[list[dict[str, Any]]] = []
    start = 0
    for index in range(count):
        size = base + (1 if index < remainder else 0)
        batches.append(rows[start:start + size])
        start += size
    return batches


def set_batch_count(project_dir: Path, count: int) -> dict[str, Any]:
    state = _require_state(project_dir)
    if state.get("status") != "awaiting_batch_count":
        raise ValueError("请先确认示例图，再选择剩余图片的生成批次")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1 or count > 5:
        raise ValueError("生成批次必须是1到5之间的整数")
    manifest, final_receipt, payload = _require_final_image_plan(project_dir)
    rows = _image_rows(payload)[1:]
    batches = _split_batches(rows, count)
    pending: list[dict[str, Any]] = []
    for index, batch_rows in enumerate(batches, 1):
        batch = {
            **_base_context(manifest, final_receipt, payload),
            "kind": "bid_delivery_local_image_generation_batch_request",
            "request_type": "batch",
            "batch_number": index,
            "requested_batch_count": count,
            "images": batch_rows,
            "instruction": "仅生成本批次列出的图片，逐图使用对应 ai_prompt；不得补生成、改写或插入Word。",
        }
        name = f"batch-{index:02d}.json"
        path = _request_path(project_dir, name)
        _write_json(path, batch)
        pending.append({"batch_number": index, "request_path": str(path), "image_ids": [row["id"] for row in batch_rows]})
    state["status"] = "generating" if pending else "complete"
    state["requested_batch_count"] = count
    state["effective_batch_count"] = len(pending)
    state["pending_batches"] = pending
    state["updated_at"] = protocol.utc_now()
    _write_json(_state_path(project_dir), state)
    return {"status": state["status"], "requested_batch_count": count, "effective_batch_count": len(pending), "batches": pending, "state": state}


def record_batch(project_dir: Path, batch_number: int, image_paths: list[str]) -> dict[str, Any]:
    state = _require_state(project_dir)
    if state.get("status") != "generating":
        raise ValueError("当前不在等待批量生图结果的状态")
    if isinstance(batch_number, bool) or not isinstance(batch_number, int) or batch_number < 1:
        raise ValueError("批次编号无效")
    pending = next((item for item in state.get("pending_batches", []) if item.get("batch_number") == batch_number), None)
    if not pending:
        raise ValueError("该批次不存在、已记录或不属于当前生图任务")
    request = protocol.read_json(Path(pending["request_path"]))
    expected_rows = request.get("images") or []
    if not isinstance(image_paths, list) or len(image_paths) != len(expected_rows):
        raise ValueError(f"第{batch_number}批结果数量必须为{len(expected_rows)}张")
    results = []
    for row, value in zip(expected_rows, image_paths):
        path = _require_local_image(value)
        results.append({"image_id": row["id"], "figure_no": row["figure_no"], "image_path": str(path), "image_sha256": protocol.sha256_file(path)})
    result_path = _result_path(project_dir, f"batch-{batch_number:02d}.json")
    result = {
        "schema_version": 1,
        "kind": "bid_delivery_local_image_generation_batch_result",
        "project_id": state["project_id"],
        "final_confirmation_sha256": state["final_confirmation_sha256"],
        "batch_number": batch_number,
        "results": results,
        "recorded_at": protocol.utc_now(),
    }
    _write_json(result_path, result)
    state["pending_batches"] = [item for item in state["pending_batches"] if item.get("batch_number") != batch_number]
    state["status"] = "complete" if not state["pending_batches"] else "generating"
    state["updated_at"] = protocol.utc_now()
    _write_json(_state_path(project_dir), state)
    return {"status": state["status"], "result_path": str(result_path), "remaining_batches": state["pending_batches"], "state": state}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_dir", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("start-example", help="写入首张示例图请求")
    record = subparsers.add_parser("record-example", help="记录本机示例图结果")
    record.add_argument("image_path")
    revise = subparsers.add_parser("revise-example", help="写入示例图修改请求")
    revise.add_argument("instruction")
    subparsers.add_parser("confirm-example", help="确认示例图视觉风格")
    batches = subparsers.add_parser("set-batch-count", help="选择剩余图片的生成批次")
    batches.add_argument("count", type=int)
    result = subparsers.add_parser("record-batch", help="记录一批本机生图结果")
    result.add_argument("batch_number", type=int)
    result.add_argument("image_paths", nargs="+")
    args = parser.parse_args()
    project_dir = args.project_dir.expanduser().resolve()
    handlers = {
        "start-example": lambda: start_example(project_dir),
        "record-example": lambda: record_example(project_dir, args.image_path),
        "revise-example": lambda: revise_example(project_dir, args.instruction),
        "confirm-example": lambda: confirm_example(project_dir),
        "set-batch-count": lambda: set_batch_count(project_dir, args.count),
        "record-batch": lambda: record_batch(project_dir, args.batch_number, args.image_paths),
    }
    try:
        print(json.dumps(handlers[args.command](), ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
