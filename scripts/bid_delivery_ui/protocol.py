#!/usr/bin/env python3
"""Host-neutral delivery manifest and state protocol.

This module deliberately contains no model calls and no DOCX/XLSX generation.
Any compatible AI host may use the persisted manifest and events to continue
the same bid-delivery workflow after a chat, browser, or host restart.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any


SCRIPT_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from bid_confirm_ui import server as confirm_ui
from bid_delivery_ui.image_prompt import compose_ai_image_prompt, prompt_for_image


SCHEMA_VERSION = 1
DELIVERY_DIR_NAME = "bid_delivery"
MANIFEST_NAME = "manifest.json"
EVENTS_DIR_NAME = "events"
SOURCE_DIR_NAME = "source"
REQUESTS_DIR_NAME = "requests"
RESULTS_DIR_NAME = "results"
CONFIRMATIONS_DIR_NAME = "confirmations"
EXPORTS_DIR_NAME = "exports"
HISTORY_DIR_NAME = "history"
LOCK_NAME = "lock.json"

PROJECT_STATUSES = {
    "preparing",
    "generating",
    "awaiting_batch_review",
    "revision_pending",
    "revising",
    "awaiting_next_batch",
    "all_batches_confirmed",
    "final_ready",
    "final_confirmed",
    "export_pending",
}
BATCH_STATUSES = {
    "pending",
    "generating",
    "ready_for_review",
    "revision_pending",
    "regenerating",
    "confirmed",
    "export_pending",
}
WORKBOOK_STATUSES = {"pending", "generating", "ready_for_review", "revision_pending", "regenerating", "export_pending", "confirmed"}
# ``direct-edit`` is deliberately not an AI wake-up event.  The local review
# service re-exports deterministic user changes itself, so a host does not
# need to spend a model turn merely to mirror a manual replacement.
EVENT_TYPES = {"revision", "direct-edit", "batch-confirmed", "image-plan-confirmed", "final-confirmed"}
REQUEST_STATUSES = {"pending", "applying", "applied", "superseded"}
SOURCE_BLOCK_TYPES = {
    "heading",
    "paragraph",
    "list",
    "table",
    "image_placeholder",
    "material_placeholder",
    "page_break",
}
MAX_READER_BLOCKS = 80
PAGE_TOLERANCE = 0.10
# This is a source-density guard, not a substitute for the final WPS check.
ESTIMATED_PAGE_UNITS = 900
MAX_LEVEL3_PER_LEVEL2 = 7
MAX_LEVEL4_PER_LEVEL3 = 7
MIN_DEEP_SUPPORT_BLOCKS = 2
PAGE_CALIBRATION_FILE = "page-calibration.json"
REPLAN_ACTUAL_RATIO = 0.85
WRITING_RULES_FILE = "stage4-writing-rules.md"


def utc_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def canonical_json(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_data(data: Any) -> str:
    return hashlib.sha256(canonical_json(data)).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return data


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temp_path, path)


def delivery_dir(project_dir: Path) -> Path:
    return project_dir / DELIVERY_DIR_NAME


def manifest_path(project_dir: Path) -> Path:
    return delivery_dir(project_dir) / MANIFEST_NAME


def event_dir(project_dir: Path) -> Path:
    return delivery_dir(project_dir) / EVENTS_DIR_NAME


def required_delivery_dirs(project_dir: Path) -> list[Path]:
    root = delivery_dir(project_dir)
    return [
        root,
        root / SOURCE_DIR_NAME,
        root / REQUESTS_DIR_NAME,
        root / RESULTS_DIR_NAME,
        root / CONFIRMATIONS_DIR_NAME,
        root / EXPORTS_DIR_NAME,
        root / HISTORY_DIR_NAME,
        root / EVENTS_DIR_NAME,
    ]


def ensure_delivery_dirs(project_dir: Path) -> None:
    for path in required_delivery_dirs(project_dir):
        path.mkdir(parents=True, exist_ok=True)


def writing_rules_path(project_dir: Path) -> Path:
    return delivery_dir(project_dir) / WRITING_RULES_FILE


def writing_rules_source_path() -> Path:
    return SCRIPT_ROOT.parent / "references" / WRITING_RULES_FILE


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_bytes(data)
    os.replace(temp_path, path)


def _read_writing_rules_source() -> tuple[bytes, str]:
    source_path = writing_rules_source_path()
    if not source_path.is_file():
        raise ValueError(f"缺少Stage4标书生成规则文件：{source_path}")
    data = source_path.read_bytes()
    if not data.strip():
        raise ValueError("Stage4标书生成规则文件为空")
    return data, hashlib.sha256(data).hexdigest()


def snapshot_writing_rules(project_dir: Path, expected_source_sha256: str | None = None, *, overwrite: bool = False) -> dict[str, str]:
    """Materialize the skill rules into the authorized project workspace."""
    data, source_sha256 = _read_writing_rules_source()
    if expected_source_sha256 is not None and source_sha256 != expected_source_sha256:
        raise ValueError("Skill内Stage4标书生成规则已变化，不能继续使用旧授权")
    target = writing_rules_path(project_dir)
    if overwrite or not target.is_file() or sha256_file(target) != source_sha256:
        _atomic_write_bytes(target, data)
    project_sha256 = sha256_file(target)
    if project_sha256 != source_sha256:
        raise ValueError("项目内Stage4标书生成规则快照写入失败")
    return {"path": WRITING_RULES_FILE, "source_sha256": source_sha256, "project_sha256": project_sha256}


def _validate_writing_rules_metadata(project_dir: Path | None, metadata: Any) -> None:
    if not isinstance(metadata, dict):
        raise ValueError("Stage4标书生成规则状态无效")
    _require_exact_keys(metadata, {"path", "source_sha256", "project_sha256"}, "Stage4标书生成规则状态")
    if _require_relative_path(metadata.get("path"), "Stage4标书生成规则路径") != WRITING_RULES_FILE:
        raise ValueError("Stage4标书生成规则路径无效")
    _require_sha256(metadata.get("source_sha256"), "Skill标书生成规则摘要")
    _require_sha256(metadata.get("project_sha256"), "项目标书生成规则摘要")
    if project_dir is not None:
        target = writing_rules_path(project_dir)
        if not target.is_file() or sha256_file(target) != metadata["project_sha256"]:
            raise ValueError("项目内Stage4标书生成规则缺失或已被替换，请重新初始化交付工作台")


def page_calibration_path(project_dir: Path) -> Path:
    return delivery_dir(project_dir) / RESULTS_DIR_NAME / PAGE_CALIBRATION_FILE


def load_page_calibration(project_dir: Path) -> dict[str, Any]:
    """Return the project-local WPS/source estimate ratio, if calibrated."""
    path = page_calibration_path(project_dir)
    if not path.is_file():
        return {"ratio": 1.0, "sample_count": 0, "samples": []}
    data = read_json(path)
    if data.get("kind") != "bid_delivery_page_calibration":
        raise ValueError("页数校准记录类型无效")
    ratio = data.get("ratio", 1.0)
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not math.isfinite(float(ratio)) or ratio <= 0:
        raise ValueError("页数校准比例无效")
    samples = data.get("samples", [])
    if not isinstance(samples, list):
        raise ValueError("页数校准样本无效")
    return {"ratio": float(ratio), "sample_count": len(samples), "samples": samples}


def record_page_calibration(project_dir: Path, manifest: dict[str, Any], batch: dict[str, Any], raw_estimated_pages: int, actual_pages: int) -> dict[str, Any]:
    """Persist a weighted project ratio from a real WPS observation."""
    if raw_estimated_pages <= 0:
        return load_page_calibration(project_dir)
    current = load_page_calibration(project_dir)
    samples = [sample for sample in current["samples"] if sample.get("batch_id") != batch["id"]]
    samples.append({
        "batch_id": batch["id"],
        "planned_pages": batch["planned_pages"],
        "estimated_pages": raw_estimated_pages,
        "actual_pages": actual_pages,
        "ratio": actual_pages / raw_estimated_pages,
        "recorded_at": utc_now(),
    })
    total_estimated = sum(float(sample.get("estimated_pages", 0)) for sample in samples)
    total_actual = sum(float(sample.get("actual_pages", 0)) for sample in samples)
    ratio = total_actual / total_estimated if total_estimated > 0 else 1.0
    calibration = {
        "schema_version": 1,
        "kind": "bid_delivery_page_calibration",
        "project_id": manifest["project_id"],
        "updated_at": utc_now(),
        "ratio": ratio,
        "sample_count": len(samples),
        "samples": samples,
    }
    atomic_write_json(page_calibration_path(project_dir), calibration)
    return {"ratio": ratio, "sample_count": len(samples), "samples": samples}


def _require_exact_keys(data: dict[str, Any], expected: set[str], label: str) -> None:
    if set(data) != expected:
        missing = sorted(expected - set(data))
        extra = sorted(set(data) - expected)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if extra:
            details.append("unsupported " + ", ".join(extra))
        raise ValueError(f"{label} fields are invalid: " + "; ".join(details))


def _require_nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _require_sha256(value: Any, label: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{label} must be a SHA-256 string")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"{label} must be a SHA-256 string") from exc
    return value


def _require_relative_path(value: Any, label: str) -> str:
    raw = _require_nonempty_string(value, label)
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or raw.startswith("./"):
        raise ValueError(f"{label} must be a safe delivery-relative path")
    return raw


def _safe_filename(value: Any, suffix: str, label: str) -> str:
    raw = _require_nonempty_string(value, label)
    if Path(raw).name != raw or not raw.lower().endswith(suffix):
        raise ValueError(f"{label} must be a plain filename ending in {suffix}")
    return raw


def _next_revision_filename(filename: str) -> str:
    """Return the next audit/review filename while preserving the base name."""
    path = Path(filename)
    stem = path.stem
    match = re.match(r"^(.*)—审校版(\d+)$", stem)
    if match:
        base, number = match.groups()
        revision = int(number) + 1
    else:
        base, revision = stem, 1
    return f"{base}—审校版{revision}{path.suffix}"


def _load_stage4_authorization(project_dir: Path) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Return the fully validated stage-4 recommendation, receipt and chapters."""
    data_dir = project_dir / confirm_ui.DATA_DIR_NAME
    confirmed = confirm_ui.stage4_confirmation_valid(data_dir)
    recommendation_bound = confirm_ui.stage4_recommendation_valid(data_dir)
    if not confirmed or not recommendation_bound:
        raise ValueError("最终交付授权回执不存在、已失效，或其上游确认已变化")
    source, _stage3_receipt, receipt = confirmed
    recommendation, _bound_stage3_receipt, chapters = recommendation_bound
    if sha256_data(source) != sha256_data(recommendation):
        raise ValueError("最终交付推荐与确认回执不一致")
    if receipt.get("source_sha256") != sha256_data(recommendation):
        raise ValueError("最终交付授权未绑定当前推荐")
    return recommendation, receipt, chapters


def _batch_manifest_item(batch: dict[str, Any]) -> dict[str, Any]:
    filename = _safe_filename(batch.get("output_filename"), ".docx", "Word批次文件名")
    order = batch.get("order")
    if isinstance(order, bool) or not isinstance(order, int) or order < 1:
        raise ValueError("Word批次顺序无效")
    return {
        "id": _require_nonempty_string(batch.get("id"), "Word批次ID"),
        "order": order,
        "chapter_ids": copy.deepcopy(batch.get("chapter_ids")),
        "chapter_numbers": copy.deepcopy(batch.get("chapter_numbers")),
        "chapter_titles": copy.deepcopy(batch.get("chapter_titles")),
        "planned_pages": batch.get("planned_pages"),
        "output_filename": filename,
        "source_path": f"{SOURCE_DIR_NAME}/batch-{order:02d}.json",
        "export_path": f"{EXPORTS_DIR_NAME}/{filename}",
        "status": "pending",
        "source_sha256": None,
        "export_sha256": None,
        "review_confirmation_sha256": None,
    }


def build_manifest(project_dir: Path) -> dict[str, Any]:
    recommendation, receipt, _chapters = _load_stage4_authorization(project_dir)
    _rules_data, rules_sha256 = _read_writing_rules_source()
    delivery = receipt.get("data")
    if not isinstance(delivery, dict):
        raise ValueError("最终交付授权缺少交付配置")
    batches = delivery.get("word_batches")
    if not isinstance(batches, list) or not batches:
        raise ValueError("最终交付授权缺少Word批次")
    manifest_batches = [_batch_manifest_item(batch) for batch in batches]
    workbook = delivery.get("image_plan_workbook")
    if not isinstance(workbook, dict):
        raise ValueError("最终交付授权缺少图片规划Excel")
    workbook_filename = _safe_filename(workbook.get("filename"), ".xlsx", "图片规划Excel文件名")
    now = utc_now()
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "bid_delivery_manifest",
        "project_id": receipt["project_id"],
        "stage4_confirmation_sha256": receipt["confirmation_sha256"],
        "stage4_source_sha256": receipt["source_sha256"],
        "writing_rules": {
            "path": WRITING_RULES_FILE,
            "source_sha256": rules_sha256,
            "project_sha256": rules_sha256,
        },
        "created_at": now,
        "updated_at": now,
        "status": "preparing",
        "active_batch_id": manifest_batches[0]["id"],
        "word_batch_count": delivery["word_batch_count"],
        "delivery_output_dir": str(delivery.get("delivery_output_dir", "")).strip(),
        "word_batches": manifest_batches,
        "image_plan_workbook": {
            "filename": workbook_filename,
            "source_path": f"{SOURCE_DIR_NAME}/image-plan.json",
            "export_path": f"{EXPORTS_DIR_NAME}/{workbook_filename}",
            "status": "pending",
            "source_sha256": None,
            "export_sha256": None,
        },
        "final_confirmation_sha256": None,
        "pending_request_count": 0,
        "last_event_id": 0,
    }


def _validate_manifest_batch(batch: Any, index: int) -> None:
    if not isinstance(batch, dict):
        raise ValueError("Word批次状态必须是对象")
    expected = {
        "id", "order", "chapter_ids", "chapter_numbers", "chapter_titles", "planned_pages", "output_filename",
        "source_path", "export_path", "status", "source_sha256", "export_sha256", "review_confirmation_sha256",
    }
    _require_exact_keys(batch, expected, "Word批次状态")
    if batch.get("order") != index:
        raise ValueError("Word批次状态顺序必须从1连续编号")
    _require_nonempty_string(batch.get("id"), "Word批次ID")
    filename = _safe_filename(batch.get("output_filename"), ".docx", "Word批次文件名")
    source_path = _require_relative_path(batch.get("source_path"), "Word源稿路径")
    export_path = _require_relative_path(batch.get("export_path"), "Word导出路径")
    if source_path != f"{SOURCE_DIR_NAME}/batch-{index:02d}.json":
        raise ValueError("Word源稿路径与批次不一致")
    if export_path != f"{EXPORTS_DIR_NAME}/{filename}":
        raise ValueError("Word导出路径与文件名不一致")
    if batch.get("status") not in BATCH_STATUSES:
        raise ValueError("Word批次状态不支持")
    _require_sha256(batch.get("source_sha256"), "Word源稿摘要", nullable=True)
    _require_sha256(batch.get("export_sha256"), "Word导出摘要", nullable=True)
    _require_sha256(batch.get("review_confirmation_sha256"), "Word确认摘要", nullable=True)
    for field in ("chapter_ids", "chapter_numbers", "chapter_titles"):
        if not isinstance(batch.get(field), list) or not batch[field]:
            raise ValueError("Word批次缺少章节范围")
    if len(batch["chapter_ids"]) != len(batch["chapter_numbers"]) or len(batch["chapter_ids"]) != len(batch["chapter_titles"]):
        raise ValueError("Word批次章节范围长度不一致")
    if isinstance(batch.get("planned_pages"), bool) or not isinstance(batch.get("planned_pages"), int) or batch["planned_pages"] < 0:
        raise ValueError("Word批次计划页数无效")


def validate_manifest(manifest: dict[str, Any], project_dir: Path | None = None) -> None:
    expected = {
        "schema_version", "kind", "project_id", "stage4_confirmation_sha256", "stage4_source_sha256",
        "created_at", "updated_at", "status", "active_batch_id", "word_batch_count", "delivery_output_dir", "word_batches",
        "image_plan_workbook", "final_confirmation_sha256", "pending_request_count", "last_event_id",
    }
    optional = {"writing_rules"}
    missing = expected - set(manifest)
    extra = set(manifest) - expected - optional
    if missing or extra:
        details = []
        if missing:
            details.append("missing " + ", ".join(sorted(missing)))
        if extra:
            details.append("unsupported " + ", ".join(sorted(extra)))
        raise ValueError("交付清单 fields are invalid: " + "; ".join(details))
    if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("kind") != "bid_delivery_manifest":
        raise ValueError("交付清单版本不支持")
    _require_nonempty_string(manifest.get("project_id"), "项目ID")
    _require_sha256(manifest.get("stage4_confirmation_sha256"), "最终授权摘要")
    _require_sha256(manifest.get("stage4_source_sha256"), "最终授权来源摘要")
    if "writing_rules" in manifest:
        _validate_writing_rules_metadata(project_dir, manifest["writing_rules"])
    _require_sha256(manifest.get("final_confirmation_sha256"), "最终交付确认摘要", nullable=True)
    _require_nonempty_string(manifest.get("created_at"), "创建时间")
    _require_nonempty_string(manifest.get("updated_at"), "更新时间")
    if manifest.get("status") not in PROJECT_STATUSES:
        raise ValueError("交付项目状态不支持")
    count = manifest.get("word_batch_count")
    batches = manifest.get("word_batches")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 5:
        raise ValueError("Word批次数必须是1至5之间的整数")
    output_dir = manifest.get("delivery_output_dir")
    if not isinstance(output_dir, str):
        raise ValueError("交付物保存位置无效")
    if not isinstance(batches, list) or len(batches) != count:
        raise ValueError("Word批次与授权数量不一致")
    seen_ids: set[str] = set()
    for index, batch in enumerate(batches, 1):
        _validate_manifest_batch(batch, index)
        if batch["id"] in seen_ids:
            raise ValueError("Word批次ID重复")
        seen_ids.add(batch["id"])
    active = manifest.get("active_batch_id")
    if active is not None and active not in seen_ids:
        raise ValueError("当前Word批次不存在")
    if manifest["status"] in {"all_batches_confirmed", "final_ready", "final_confirmed"} and active is not None:
        raise ValueError("全部批次完成后不得保留当前Word批次")
    workbook = manifest.get("image_plan_workbook")
    if not isinstance(workbook, dict):
        raise ValueError("图片规划Excel状态无效")
    _require_exact_keys(workbook, {"filename", "source_path", "export_path", "status", "source_sha256", "export_sha256"}, "图片规划Excel状态")
    filename = _safe_filename(workbook.get("filename"), ".xlsx", "图片规划Excel文件名")
    if _require_relative_path(workbook.get("source_path"), "图片规划源稿路径") != f"{SOURCE_DIR_NAME}/image-plan.json":
        raise ValueError("图片规划源稿路径无效")
    if _require_relative_path(workbook.get("export_path"), "图片规划导出路径") != f"{EXPORTS_DIR_NAME}/{filename}":
        raise ValueError("图片规划导出路径无效")
    if workbook.get("status") not in WORKBOOK_STATUSES:
        raise ValueError("图片规划Excel状态不支持")
    _require_sha256(workbook.get("source_sha256"), "图片规划源稿摘要", nullable=True)
    _require_sha256(workbook.get("export_sha256"), "图片规划导出摘要", nullable=True)
    if isinstance(manifest.get("pending_request_count"), bool) or not isinstance(manifest.get("pending_request_count"), int) or manifest["pending_request_count"] < 0:
        raise ValueError("待处理修改数量无效")
    if isinstance(manifest.get("last_event_id"), bool) or not isinstance(manifest.get("last_event_id"), int) or manifest["last_event_id"] < 0:
        raise ValueError("事件序号无效")
    _validate_state_consistency(manifest)
    if project_dir is not None:
        recommendation, receipt, _chapters = _load_stage4_authorization(project_dir)
        if manifest["project_id"] != receipt.get("project_id"):
            raise ValueError("交付清单项目与最终授权不一致")
        if manifest["stage4_confirmation_sha256"] != receipt.get("confirmation_sha256"):
            raise ValueError("最终授权已经变化，当前交付清单不能继续使用")
        if manifest["stage4_source_sha256"] != sha256_data(recommendation):
            raise ValueError("最终交付建议已经变化，当前交付清单不能继续使用")
        for batch in batches:
            if batch["status"] in {"ready_for_review", "revision_pending", "confirmed"}:
                _require_recorded_artifacts(project_dir, batch)
            if batch["status"] == "confirmed":
                _require_batch_confirmation(project_dir, manifest, batch)
            elif batch["status"] == "export_pending":
                _require_recorded_source(project_dir, batch, manifest)
        if workbook["status"] == "confirmed":
            _require_image_plan_confirmation(project_dir, manifest)
        if manifest["status"] == "final_confirmed":
            _require_final_confirmation(project_dir, manifest)


def _validate_state_consistency(manifest: dict[str, Any]) -> None:
    """Reject state combinations that would let a host skip a review gate."""
    batches = manifest["word_batches"]
    active_id = manifest["active_batch_id"]
    status = manifest["status"]
    by_id = {batch["id"]: batch for batch in batches}
    active = by_id.get(active_id) if isinstance(active_id, str) else None
    confirmed = [batch for batch in batches if batch["status"] == "confirmed"]
    if status == "preparing":
        if not active or active["status"] != "pending" or confirmed:
            raise ValueError("准备状态必须指向首个待生成Word批次")
    elif status == "generating":
        if not active or active["status"] != "generating":
            raise ValueError("生成状态必须指向正在生成的Word批次")
    elif status == "awaiting_batch_review":
        if not active or active["status"] != "ready_for_review":
            raise ValueError("审校状态必须指向待审校Word批次")
    elif status == "revision_pending":
        workbook_pending = manifest["image_plan_workbook"]["status"] == "revision_pending"
        if (not active or active["status"] != "revision_pending") and not workbook_pending:
            raise ValueError("修改待处理状态必须指向待修改Word批次或图片规划")
        if manifest["pending_request_count"] < 1:
            raise ValueError("修改待处理状态必须指向待修改Word批次")
    elif status == "revising":
        workbook_revising = manifest["image_plan_workbook"]["status"] == "regenerating"
        if (not active or active["status"] != "regenerating") and not workbook_revising:
            raise ValueError("修改中状态必须指向重新生成的Word批次")
    elif status == "export_pending":
        workbook_pending = manifest["image_plan_workbook"]["status"] == "export_pending"
        if ((not active or active["status"] != "export_pending") and not workbook_pending) or manifest["pending_request_count"]:
            raise ValueError("待重新导出状态必须指向已修改的Word批次")
    elif status == "awaiting_next_batch":
        if not active or active["status"] != "pending" or not confirmed:
            raise ValueError("下一批等待状态必须保留已确认批次并指向待生成批次")
    elif status in {"all_batches_confirmed", "final_ready", "final_confirmed"}:
        if active is not None or len(confirmed) != len(batches):
            raise ValueError("最终阶段只能在全部Word批次确认后进入")
    if status == "final_confirmed" and not manifest.get("final_confirmation_sha256"):
        raise ValueError("最终确认状态缺少确认摘要")
    if status != "final_confirmed" and manifest.get("final_confirmation_sha256") is not None:
        raise ValueError("非最终确认状态不得保留最终确认摘要")


def load_manifest(project_dir: Path) -> dict[str, Any]:
    path = manifest_path(project_dir)
    if not path.exists():
        raise ValueError("尚未初始化生产与审校交付清单")
    manifest = read_json(path)
    # A pre-location manifest remains readable so that an already-open project
    # can be reopened at stage 4 and explicitly select its delivery folder.
    if "delivery_output_dir" not in manifest:
        manifest["delivery_output_dir"] = ""
        atomic_write_json(path, manifest)
    if "writing_rules" not in manifest:
        # Migrate an older authorized workspace without discarding its source
        # or export history. New batches will carry the snapshot hash.
        manifest["writing_rules"] = snapshot_writing_rules(project_dir, overwrite=True)
        atomic_write_json(path, manifest)
    validate_manifest(manifest, project_dir)
    return manifest


def initialize_delivery(project_dir: Path) -> tuple[dict[str, Any], bool]:
    """Create an empty-but-authorized manifest, or safely resume one.

    The bool is True only when a new manifest was written. Existing manifests
    are never silently replaced: they must still bind to the current stage-4
    authorization and pass strict validation.
    """
    path = manifest_path(project_dir)
    if path.exists():
        return load_manifest(project_dir), False
    manifest = build_manifest(project_dir)
    validate_manifest(manifest)
    ensure_delivery_dirs(project_dir)
    manifest["writing_rules"] = snapshot_writing_rules(project_dir, manifest["writing_rules"]["source_sha256"], overwrite=True)
    atomic_write_json(path, manifest)
    return manifest, True


def _write_manifest(project_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    manifest["updated_at"] = utc_now()
    validate_manifest(manifest, project_dir)
    atomic_write_json(manifest_path(project_dir), manifest)
    return manifest


def _batch_by_id(manifest: dict[str, Any], batch_id: str) -> dict[str, Any]:
    for batch in manifest["word_batches"]:
        if batch["id"] == batch_id:
            return batch
    raise ValueError("Word批次不存在")


def _safe_delivery_file(project_dir: Path, relative_path: str) -> Path:
    root = delivery_dir(project_dir).resolve()
    candidate = (root / PurePosixPath(relative_path)).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("交付文件路径越出项目目录")
    return candidate


def _delivery_output_dir(manifest: dict[str, Any]) -> Path:
    raw = str(manifest.get("delivery_output_dir", "")).strip()
    if not raw:
        raise ValueError("最终授权未设置交付物保存位置，请返回第04阶段重新授权")
    destination = Path(raw).expanduser()
    if not destination.is_absolute() or not destination.is_dir():
        raise ValueError("交付物保存位置当前不可用，请返回第04阶段重新选择")
    return destination.resolve()


def publish_confirmed_file(project_dir: Path, manifest: dict[str, Any], internal_path: Path, filename: str) -> dict[str, str]:
    """Copy a verified internal export to the user-authorized output folder."""
    destination = _delivery_output_dir(manifest)
    target = destination / _safe_filename(filename, Path(filename).suffix.lower(), "交付文件名")
    shutil.copy2(internal_path, target)
    source_digest = sha256_file(internal_path)
    target_digest = sha256_file(target)
    if target_digest != source_digest:
        raise ValueError("交付物保存后的文件校验不一致")
    return {"path": str(target), "sha256": target_digest}


def _next_sequence(root: Path, prefix: str) -> int:
    highest = 0
    for path in root.glob(f"{prefix}-*.json"):
        # Keep the delivery runtime compatible with Python 3.8, which is
        # still bundled by some local AI hosts.
        suffix = path.stem[len(prefix) + 1:]
        if suffix.isdigit():
            highest = max(highest, int(suffix))
    return highest + 1


def _request_path(project_dir: Path, request_id: str) -> Path:
    return _safe_delivery_file(project_dir, f"{REQUESTS_DIR_NAME}/{request_id}.json")


def _result_path(project_dir: Path, request_id: str) -> Path:
    return _safe_delivery_file(project_dir, f"{RESULTS_DIR_NAME}/{request_id}-result.json")


def _history_path(project_dir: Path, record_id: str) -> Path:
    return _safe_delivery_file(project_dir, f"{HISTORY_DIR_NAME}/{record_id}.json")


def _batch_confirmation_path(project_dir: Path, batch: dict[str, Any]) -> Path:
    return _safe_delivery_file(project_dir, f"{CONFIRMATIONS_DIR_NAME}/batch-{batch['order']:02d}-confirmation.json")


def _final_confirmation_path(project_dir: Path) -> Path:
    return _safe_delivery_file(project_dir, f"{CONFIRMATIONS_DIR_NAME}/final-confirmation.json")


def _image_plan_confirmation_path(project_dir: Path) -> Path:
    return _safe_delivery_file(project_dir, f"{CONFIRMATIONS_DIR_NAME}/image-plan-confirmation.json")


def _archive_confirmation(path: Path, project_dir: Path, label: str) -> None:
    """Keep an invalidated receipt as an auditable history record."""
    if not path.exists():
        return
    root = _safe_delivery_file(project_dir, HISTORY_DIR_NAME)
    root.mkdir(parents=True, exist_ok=True)
    destination = root / f"{label}-{int(time.time() * 1000)}.json"
    os.replace(path, destination)


def _prepare_revision_filename(project_dir: Path, batch: dict[str, Any]) -> str:
    """Allocate an audit-version export name and retain the current export.

    The old confirmed file remains in the user output directory.  A copy in
    the new internal export path lets the strict manifest validator continue
    to read the in-progress revision until the AI writes and re-exports the
    replacement source.
    """
    old_filename = batch["output_filename"]
    new_filename = _next_revision_filename(old_filename)
    old_path = _safe_delivery_file(project_dir, batch["export_path"])
    new_path = _safe_delivery_file(project_dir, f"{EXPORTS_DIR_NAME}/{new_filename}")
    if old_path.is_file() and old_path != new_path:
        new_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(old_path, new_path)
    batch["output_filename"] = new_filename
    batch["export_path"] = f"{EXPORTS_DIR_NAME}/{new_filename}"
    return new_filename


def _confirmation_digest(record: dict[str, Any]) -> str:
    unsigned = dict(record)
    unsigned.pop("confirmation_sha256", None)
    return sha256_data(unsigned)


def _require_batch_confirmation(project_dir: Path, manifest: dict[str, Any], batch: dict[str, Any]) -> dict[str, Any]:
    path = _batch_confirmation_path(project_dir, batch)
    if not path.is_file():
        raise ValueError("已确认Word批次缺少确认回执")
    record = read_json(path)
    expected = {
        "schema_version", "kind", "status", "project_id", "batch_id", "batch_order",
        "stage4_confirmation_sha256", "source_sha256", "export_sha256", "delivery_output_path", "delivery_output_sha256", "confirmed_at", "confirmation_sha256",
    }
    _require_exact_keys(record, expected, "Word批次确认回执")
    if record.get("schema_version") != SCHEMA_VERSION or record.get("kind") != "bid_delivery_batch_confirmation" or record.get("status") != "confirmed":
        raise ValueError("Word批次确认回执版本或状态无效")
    if record.get("project_id") != manifest["project_id"] or record.get("batch_id") != batch["id"] or record.get("batch_order") != batch["order"]:
        raise ValueError("Word批次确认回执范围不匹配")
    if record.get("stage4_confirmation_sha256") != manifest["stage4_confirmation_sha256"]:
        raise ValueError("Word批次确认回执未绑定当前最终授权")
    if record.get("source_sha256") != batch["source_sha256"] or record.get("export_sha256") != batch["export_sha256"]:
        raise ValueError("Word批次确认回执已过期")
    output_path = Path(_require_nonempty_string(record.get("delivery_output_path"), "Word交付保存路径"))
    if not output_path.is_absolute() or not output_path.is_file() or sha256_file(output_path) != record.get("delivery_output_sha256") or record.get("delivery_output_sha256") != batch["export_sha256"]:
        raise ValueError("Word交付保存文件不存在或摘要不一致")
    _require_nonempty_string(record.get("confirmed_at"), "Word批次确认时间")
    digest = _require_sha256(record.get("confirmation_sha256"), "Word批次确认摘要")
    if digest != _confirmation_digest(record) or batch.get("review_confirmation_sha256") != digest:
        raise ValueError("Word批次确认摘要不匹配")
    return record


def _require_final_confirmation(project_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    path = _final_confirmation_path(project_dir)
    if not path.is_file():
        raise ValueError("最终交付确认缺少确认回执")
    record = read_json(path)
    expected = {
        "schema_version", "kind", "status", "project_id", "stage4_confirmation_sha256",
        "word_batches", "image_plan_workbook", "confirmed_at", "confirmation_sha256",
    }
    _require_exact_keys(record, expected, "最终交付确认回执")
    if record.get("schema_version") != SCHEMA_VERSION or record.get("kind") != "bid_delivery_final_confirmation" or record.get("status") != "confirmed":
        raise ValueError("最终交付确认回执版本或状态无效")
    if record.get("project_id") != manifest["project_id"] or record.get("stage4_confirmation_sha256") != manifest["stage4_confirmation_sha256"]:
        raise ValueError("最终交付确认回执项目或授权不匹配")
    expected_batches = [{"id": item["id"], "source_sha256": item["source_sha256"], "export_sha256": item["export_sha256"], "batch_confirmation_sha256": item["review_confirmation_sha256"]} for item in manifest["word_batches"]]
    if record.get("word_batches") != expected_batches:
        raise ValueError("最终交付确认回执Word批次不匹配")
    workbook = manifest["image_plan_workbook"]
    expected_workbook = {
        "filename": workbook["filename"], "source_sha256": workbook["source_sha256"], "export_sha256": workbook["export_sha256"],
        "delivery_output_path": str((Path(manifest["delivery_output_dir"]) / workbook["filename"]).resolve()),
        "delivery_output_sha256": workbook["export_sha256"],
    }
    if record.get("image_plan_workbook") != expected_workbook:
        raise ValueError("最终交付确认回执图片规划Excel不匹配")
    output_path = Path(expected_workbook["delivery_output_path"])
    if not output_path.is_file() or sha256_file(output_path) != expected_workbook["delivery_output_sha256"]:
        raise ValueError("图片规划Excel交付保存文件不存在或摘要不一致")
    _require_nonempty_string(record.get("confirmed_at"), "最终确认时间")
    digest = _require_sha256(record.get("confirmation_sha256"), "最终确认摘要")
    if digest != _confirmation_digest(record) or manifest.get("final_confirmation_sha256") != digest:
        raise ValueError("最终交付确认摘要不匹配")
    return record


def _require_image_plan_confirmation(project_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Verify the independently delivered image-plan workbook."""
    path = _image_plan_confirmation_path(project_dir)
    if not path.is_file():
        raise ValueError("已确认图片规划Excel缺少确认回执")
    record = read_json(path)
    expected = {
        "schema_version", "kind", "status", "project_id", "stage4_confirmation_sha256",
        "filename", "source_sha256", "export_sha256", "delivery_output_path",
        "delivery_output_sha256", "confirmed_at", "confirmation_sha256",
    }
    _require_exact_keys(record, expected, "图片规划Excel确认回执")
    workbook = manifest["image_plan_workbook"]
    if record.get("schema_version") != SCHEMA_VERSION or record.get("kind") != "bid_delivery_image_plan_confirmation" or record.get("status") != "confirmed":
        raise ValueError("图片规划Excel确认回执版本或状态无效")
    if record.get("project_id") != manifest["project_id"] or record.get("stage4_confirmation_sha256") != manifest["stage4_confirmation_sha256"]:
        raise ValueError("图片规划Excel确认回执项目或授权不匹配")
    if record.get("filename") != workbook["filename"] or record.get("source_sha256") != workbook["source_sha256"] or record.get("export_sha256") != workbook["export_sha256"]:
        raise ValueError("图片规划Excel确认回执已过期")
    output_path = Path(_require_nonempty_string(record.get("delivery_output_path"), "图片规划Excel交付保存路径"))
    if not output_path.is_absolute() or not output_path.is_file() or record.get("delivery_output_sha256") != workbook["export_sha256"] or sha256_file(output_path) != record.get("delivery_output_sha256"):
        raise ValueError("图片规划Excel交付保存文件不存在或摘要不一致")
    _require_nonempty_string(record.get("confirmed_at"), "图片规划Excel确认时间")
    digest = _require_sha256(record.get("confirmation_sha256"), "图片规划Excel确认摘要")
    if digest != _confirmation_digest(record):
        raise ValueError("图片规划Excel确认摘要不匹配")
    return record


def _require_positive_int(value: Any, label: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label}必须是大于等于{minimum}的整数")
    return value


def _require_string_list(value: Any, label: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not allow_empty and not value):
        raise ValueError(f"{label}必须是{'可为空的' if allow_empty else '非空的'}文本列表")
    cleaned: list[str] = []
    for item in value:
        cleaned.append(_require_nonempty_string(item, label))
    return cleaned


def _require_table_rows(value: Any, columns: list[str]) -> list[list[str]]:
    if not isinstance(value, list):
        raise ValueError("表格行必须是列表")
    rows: list[list[str]] = []
    for row in value:
        if not isinstance(row, list) or len(row) != len(columns):
            raise ValueError("表格行列数必须与表头一致")
        rows.append([_require_nonempty_string(item, "表格单元格") for item in row])
    return rows


def _source_chapter_map(chapters: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    seen_ids: set[str] = set()
    numbers: set[str] = set()
    output: dict[str, dict[str, Any]] = {}
    for index, chapter in enumerate(chapters, 1):
        if not isinstance(chapter, dict):
            raise ValueError("源稿章节必须是对象")
        _require_exact_keys(chapter, {"id", "number", "title", "level", "order"}, "源稿章节")
        chapter_id = _require_nonempty_string(chapter.get("id"), "源稿章节ID")
        number = _require_nonempty_string(chapter.get("number"), "源稿章节编号")
        _require_nonempty_string(chapter.get("title"), "源稿章节标题")
        if chapter_id in seen_ids or number in numbers:
            raise ValueError("源稿章节ID或编号重复")
        if _require_positive_int(chapter.get("level"), "源稿章节层级") > 6:
            raise ValueError("源稿章节层级不能超过六级")
        if chapter.get("order") != index:
            raise ValueError("源稿章节顺序必须从1连续编号")
        seen_ids.add(chapter_id)
        numbers.add(number)
        output[chapter_id] = chapter
    return output


def source_structure_metrics(source: dict[str, Any]) -> dict[str, Any]:
    """Validate heading attachment and return density metrics for review."""
    chapter_map = {str(chapter["id"]): chapter for chapter in source["chapters"]}
    records: list[dict[str, Any]] = []
    stacks: dict[str, list[dict[str, Any]]] = {}
    seen_numbers: set[tuple[str, str]] = set()
    support_types = {"paragraph", "list", "table", "image_placeholder", "material_placeholder"}

    for block in source["blocks"]:
        chapter_id = str(block["chapter_id"])
        stack = stacks.setdefault(chapter_id, [])
        if block["type"] == "heading":
            level = int(block["level"])
            number = str(block["number"]).strip()
            key = (chapter_id, number)
            if key in seen_numbers:
                raise ValueError(f"源稿标题编号重复：{number}")
            while stack and int(stack[-1]["level"]) >= level:
                stack.pop()
            parent = stack[-1] if stack else None
            if level == 2:
                chapter_number = str(chapter_map[chapter_id].get("number", "")).strip()
                if chapter_number and not number.startswith(chapter_number + "."):
                    raise ValueError(f"二级标题{number}未挂靠一级章节{chapter_number}")
            elif parent is None or int(parent["level"]) != level - 1:
                raise ValueError(f"标题{number}缺少有效的{level - 1}级父标题")
            elif not number.startswith(parent["number"] + "."):
                raise ValueError(f"标题{number}未挂靠父标题{parent['number']}")
            record = {
                "chapter_id": chapter_id,
                "level": level,
                "number": number,
                "children": [],
                "support_blocks": 0,
                "paragraph_lengths": [],
            }
            if parent is not None:
                parent["children"].append(record)
            records.append(record)
            seen_numbers.add(key)
            stack.append(record)
        elif block["type"] in support_types and stack:
            current = stack[-1]
            current["support_blocks"] += 1
            if block["type"] == "paragraph":
                current["paragraph_lengths"].append(len(str(block["text"]).strip()))

    heading_counts = {str(level): 0 for level in range(2, 7)}
    support_counts: dict[str, int] = {}
    average_paragraph_chars: dict[str, float] = {}
    warnings: list[str] = []
    for record in records:
        level = int(record["level"])
        heading_counts[str(level)] += 1
        child_count = len(record["children"])
        if level == 2 and child_count > MAX_LEVEL3_PER_LEVEL2:
            raise ValueError(f"三级标题数量超限：{record['number']}下有{child_count}个，最多{MAX_LEVEL3_PER_LEVEL2}个")
        if level == 3 and child_count > MAX_LEVEL4_PER_LEVEL3:
            raise ValueError(f"四级标题数量超限：{record['number']}下有{child_count}个，最多{MAX_LEVEL4_PER_LEVEL3}个")
        if level >= 2 and record["support_blocks"] == 0 and not record["children"]:
            raise ValueError(f"标题{record['number']}下没有正文、列表或表格支撑")
        if level >= 4 and not record["children"] and record["support_blocks"] < MIN_DEEP_SUPPORT_BLOCKS:
            raise ValueError(f"四级及以下标题{record['number']}正文支撑不足，至少需要{MIN_DEEP_SUPPORT_BLOCKS}个内容块")
        if level == 2 and 0 < child_count < 4:
            warnings.append(f"{record['number']}下三级标题较少（{child_count}个），请确认是否需要合并或展开")
        if level == 3 and child_count == 0:
            warnings.append(f"{record['number']}未设置四级标题，正文应优先在现有段落内讲透")
        support_counts[record["number"]] = record["support_blocks"]
        lengths = record["paragraph_lengths"]
        if lengths:
            average_paragraph_chars[record["number"]] = round(sum(lengths) / len(lengths), 1)

    return {
        "heading_counts": heading_counts,
        "support_blocks_by_heading": support_counts,
        "average_paragraph_chars": average_paragraph_chars,
        "warnings": warnings,
        "limits": {
            "max_level3_per_level2": MAX_LEVEL3_PER_LEVEL2,
            "max_level4_per_level3": MAX_LEVEL4_PER_LEVEL3,
            "min_deep_support_blocks": MIN_DEEP_SUPPORT_BLOCKS,
        },
    }


def validate_source_against_confirmed_outline(project_dir: Path, source: dict[str, Any], batch: dict[str, Any]) -> None:
    """Keep Stage-2 level 2/3 headings locked during Stage-4 production."""
    bound = confirm_ui.stage2_confirmation_valid(project_dir / confirm_ui.DATA_DIR_NAME)
    if not bound:
        raise ValueError("标书框架确认回执不存在或已失效，不能校验正文目录")
    _stage2_source, receipt = bound
    data = receipt.get("data")
    roots = data.get("chapters") if isinstance(data, dict) else None
    if not isinstance(roots, list):
        raise ValueError("已确认标书框架缺少目录树")

    expected: list[tuple[str, int, str, str]] = []

    def walk(nodes: Any, chapter_id: str, prefix: str) -> None:
        if not isinstance(nodes, list):
            raise ValueError("已确认目录树结构无效")
        for index, node in enumerate(nodes, 1):
            if not isinstance(node, dict):
                raise ValueError("已确认目录节点无效")
            number = str(node.get("number") or (f"{prefix}.{index}" if prefix else str(index))).strip()
            level = int(node.get("level") or len(number.split(".")))
            title = str(node.get("title", "")).strip()
            if level in {2, 3}:
                expected.append((chapter_id, level, number, title))
            walk(node.get("children", []), chapter_id, number)

    batch_chapter_ids = set(batch["chapter_ids"])
    for root in roots:
        if not isinstance(root, dict):
            raise ValueError("已确认一级章节无效")
        chapter_id = str(root.get("id", "")).strip()
        if chapter_id in batch_chapter_ids:
            root_number = str(root.get("number", "")).strip()
            walk(root.get("children", []), chapter_id, root_number)

    # Older manually-created confirmations may contain no level-2/3 outline.
    # Keep those projects readable while enforcing the lock for current runs.
    if not expected:
        return
    actual = [
        (str(block["chapter_id"]), int(block["level"]), str(block["number"]).strip(), str(block["title"]).strip())
        for block in source["blocks"]
        if block["type"] == "heading" and int(block["level"]) in {2, 3}
    ]
    if actual == expected:
        return
    expected_numbers = {item[2] for item in expected}
    missing = [item[2] for item in expected if item not in actual]
    extra = [item[2] for item in actual if item not in expected]
    changed = [item[2] for item in actual if item[2] in expected_numbers and item not in expected]
    details = []
    if missing:
        details.append("缺少" + "、".join(missing[:8]))
    if extra:
        details.append("新增" + "、".join(extra[:8]))
    if changed:
        details.append("标题或层级变化" + "、".join(changed[:8]))
    raise ValueError("正文一至三级目录未遵守阶段2确认骨架：" + "；".join(details or ["顺序不一致"]) + "；请回到阶段2重新确认目录")


def validate_batch_source(source: dict[str, Any], manifest: dict[str, Any], batch: dict[str, Any]) -> None:
    """Validate the host-neutral, read-only source used by the review page."""
    _require_exact_keys(source, {
        "schema_version", "kind", "project_id", "stage4_confirmation_sha256", "batch_id", "batch_order",
        "source_version", "generated_at", "updated_at", "writing_rules_sha256", "planned_pages", "actual_pages", "chapters", "blocks",
    }, "Word结构化源稿")
    if source.get("schema_version") != SCHEMA_VERSION or source.get("kind") != "bid_delivery_source":
        raise ValueError("Word结构化源稿版本不支持")
    if source.get("project_id") != manifest["project_id"]:
        raise ValueError("Word结构化源稿项目不匹配")
    if source.get("stage4_confirmation_sha256") != manifest["stage4_confirmation_sha256"]:
        raise ValueError("Word结构化源稿未绑定当前最终授权")
    _require_sha256(source.get("writing_rules_sha256"), "Word结构化源稿标书生成规则摘要")
    if source["writing_rules_sha256"] != manifest["writing_rules"]["project_sha256"]:
        raise ValueError("Word结构化源稿未读取当前Stage4标书生成规则")
    if source.get("batch_id") != batch["id"] or source.get("batch_order") != batch["order"]:
        raise ValueError("Word结构化源稿批次不匹配")
    _require_positive_int(source.get("source_version"), "Word结构化源稿版本号")
    _require_nonempty_string(source.get("generated_at"), "Word结构化源稿生成时间")
    _require_nonempty_string(source.get("updated_at"), "Word结构化源稿更新时间")
    if source.get("planned_pages") != batch["planned_pages"]:
        raise ValueError("Word结构化源稿计划页数与批次不一致")
    actual_pages = source.get("actual_pages")
    if actual_pages is not None:
        _require_positive_int(actual_pages, "Word结构化源稿实际页数")
    chapters = source.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        raise ValueError("Word结构化源稿缺少章节")
    chapter_map = _source_chapter_map(chapters)
    if list(chapter_map) != batch["chapter_ids"]:
        raise ValueError("Word结构化源稿章节范围与批次授权不一致")
    if [chapter_map[item]["number"] for item in batch["chapter_ids"]] != batch["chapter_numbers"]:
        raise ValueError("Word结构化源稿章节编号与批次授权不一致")
    if [chapter_map[item]["title"] for item in batch["chapter_ids"]] != batch["chapter_titles"]:
        raise ValueError("Word结构化源稿章节标题与批次授权不一致")
    blocks = source.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise ValueError("Word结构化源稿缺少内容块")
    seen_ids: set[str] = set()
    for index, block in enumerate(blocks, 1):
        if not isinstance(block, dict):
            raise ValueError("Word结构化源稿内容块必须是对象")
        block_type = block.get("type")
        if block_type not in SOURCE_BLOCK_TYPES:
            raise ValueError("Word结构化源稿内容块类型不支持")
        common = {"id", "order", "type", "chapter_id"}
        expected = common | ({"level", "number", "title"} if block_type == "heading" else {"text"} if block_type == "paragraph" else {"items"} if block_type == "list" else {"columns", "rows"} if block_type == "table" else {"figure_no", "name", "note"} if block_type == "image_placeholder" else {"label", "note"} if block_type == "material_placeholder" else set())
        _require_exact_keys(block, expected, "Word结构化源稿内容块")
        block_id = _require_nonempty_string(block.get("id"), "内容块ID")
        if block_id in seen_ids:
            raise ValueError("Word结构化源稿内容块ID重复")
        if block.get("order") != index:
            raise ValueError("Word结构化源稿内容块顺序必须从1连续编号")
        if block.get("chapter_id") not in chapter_map:
            raise ValueError("Word结构化源稿内容块未关联本批章节")
        if block_type == "heading":
            level = _require_positive_int(block.get("level"), "小标题层级")
            if not 2 <= level <= 6:
                raise ValueError("小标题层级必须为2至6")
            _require_nonempty_string(block.get("number"), "小标题编号")
            _require_nonempty_string(block.get("title"), "小标题名称")
        elif block_type == "paragraph":
            _require_nonempty_string(block.get("text"), "正文内容")
        elif block_type == "list":
            _require_string_list(block.get("items"), "列表项")
        elif block_type == "table":
            columns = _require_string_list(block.get("columns"), "表格表头")
            _require_table_rows(block.get("rows"), columns)
        elif block_type == "image_placeholder":
            _require_nonempty_string(block.get("figure_no"), "图片占位图号")
            _require_nonempty_string(block.get("name"), "图片占位名称")
            _require_nonempty_string(block.get("note"), "图片占位说明")
        elif block_type == "material_placeholder":
            _require_nonempty_string(block.get("label"), "材料待补标签")
            _require_nonempty_string(block.get("note"), "材料待补说明")
        seen_ids.add(block_id)
    source_structure_metrics(source)


def page_bounds(planned_pages: int) -> tuple[int, int]:
    """Return the inclusive WPS acceptance range for a planned page count."""
    planned = _require_positive_int(planned_pages, "计划页数")
    return max(1, int(planned * (1 - PAGE_TOLERANCE) + 0.999999)), int(planned * (1 + PAGE_TOLERANCE))


def estimate_source_pages(source: dict[str, Any], calibration_ratio: float = 1.0) -> dict[str, Any]:
    """Estimate pagination pressure from structured source blocks.

    Tables, lists and placeholders consume vertical space beyond their literal
    text. The result catches materially thin drafts before Word review; WPS
    remains the rendered-document authority.
    """
    if isinstance(calibration_ratio, bool) or not isinstance(calibration_ratio, (int, float)) or not math.isfinite(float(calibration_ratio)) or calibration_ratio <= 0:
        raise ValueError("页数校准比例无效")
    chapter_units = {chapter["id"]: 0 for chapter in source["chapters"]}
    forced_breaks = 0
    for block in source["blocks"]:
        kind = block["type"]
        if kind == "paragraph":
            units = len(block["text"])
        elif kind == "heading":
            units = max(50, len(block["title"]) + 24)
        elif kind == "list":
            units = sum(max(70, len(item) + 28) for item in block["items"])
        elif kind == "table":
            units = 110 + sum(max(130, sum(len(cell) for cell in row) + 42) for row in block["rows"])
        elif kind in {"image_placeholder", "material_placeholder"}:
            units = 150
        else:
            forced_breaks += 1
            units = 0
        chapter_units[block["chapter_id"]] += units
    total_units = sum(chapter_units.values())
    raw_estimated_pages = max(1, (total_units + ESTIMATED_PAGE_UNITS - 1) // ESTIMATED_PAGE_UNITS + forced_breaks)
    estimated_pages = max(1, int(math.ceil(raw_estimated_pages * float(calibration_ratio))))
    return {
        "estimated_pages": estimated_pages,
        "raw_estimated_pages": raw_estimated_pages,
        "calibration_ratio": float(calibration_ratio),
        "estimated_units": total_units,
        "units_per_page": ESTIMATED_PAGE_UNITS,
        "chapter_estimates": [
            {"chapter_id": chapter["id"], "title": chapter["title"], "estimated_units": chapter_units[chapter["id"]]}
            for chapter in source["chapters"]
        ],
    }


def _require_source_page_floor(source: dict[str, Any], batch: dict[str, Any], calibration_ratio: float = 1.0) -> dict[str, Any]:
    estimate = estimate_source_pages(source, calibration_ratio)
    lower_bound, _upper_bound = page_bounds(batch["planned_pages"])
    if estimate["estimated_pages"] < lower_bound:
        chapters = sorted(estimate["chapter_estimates"], key=lambda item: item["estimated_units"])
        thin = "、".join(item["title"] for item in chapters[:3])
        raise ValueError(
            f"源稿预计约{estimate['estimated_pages']}页，低于计划{batch['planned_pages']}页的允许下限{lower_bound}页；"
            f"请优先补强内容密度较低的章节：{thin}，不得使用通用凑页文字"
        )
    return estimate


def validate_batch_image_alignment(project_dir: Path, source: dict[str, Any], batch: dict[str, Any]) -> None:
    """Reject source/Stage-3 drift before it reaches Word or the review page."""
    stage3_source, _stage3_receipt = _stage3_image_plan(project_dir)
    expected = {
        image["figure_no"]: (image["chapter_id"], image["name"])
        for image in stage3_source["images"]
        if image["chapter_id"] in batch["chapter_ids"]
    }
    actual = {
        block["figure_no"]: (block["chapter_id"], block["name"])
        for block in source["blocks"]
        if block["type"] == "image_placeholder"
    }
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        changed = sorted(number for number in set(expected) & set(actual) if expected[number] != actual[number])
        details = []
        if missing:
            details.append(f"缺少{'、'.join(missing)}")
        if extra:
            details.append(f"多出{'、'.join(extra)}")
        if changed:
            details.append(f"位置或名称不一致{'、'.join(changed)}")
        raise ValueError("Word源稿图片占位与已确认图片规划不一致：" + "；".join(details))


def load_batch_source_for_export(project_dir: Path, batch_id: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Load a generated or edited source before its Word file is registered.

    This is intentionally separate from ``load_batch_source``: a host may only
    export a source that is bound to the active final-delivery receipt, but the
    export itself must exist before the normal review reader can be opened.
    """
    manifest = load_manifest(project_dir)
    batch = _batch_by_id(manifest, batch_id)
    if batch["status"] not in {"generating", "regenerating", "export_pending"}:
        raise ValueError("当前Word批次不处于可导出状态")
    source_path = _safe_delivery_file(project_dir, batch["source_path"])
    if not source_path.is_file():
        raise ValueError("需要先生成结构化源稿后才能导出Word")
    source = read_json(source_path)
    validate_batch_source(source, manifest, batch)
    validate_source_against_confirmed_outline(project_dir, source, batch)
    validate_batch_image_alignment(project_dir, source, batch)
    return manifest, batch, source


def _stage3_image_plan(project_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    confirmed = confirm_ui.stage3_confirmation_valid(project_dir / confirm_ui.DATA_DIR_NAME)
    if not confirmed:
        raise ValueError("图片规划确认回执不存在、已失效，或其上游确认已变化")
    _source, _stage2_receipt, receipt = confirmed
    data = receipt.get("data")
    if not isinstance(data, dict):
        raise ValueError("图片规划确认回执缺少用户确认的数据")
    return data, receipt


def build_image_plan_source(project_dir: Path) -> dict[str, Any]:
    """Persist the one Excel source from the confirmed Stage-3 image plan.

    It records planning instructions only. No asset path, generated image, or
    image insertion operation is part of this source.
    """
    manifest = load_manifest(project_dir)
    stage3_source, stage3_receipt = _stage3_image_plan(project_dir)
    workbook = manifest["image_plan_workbook"]
    visual_direction = copy.deepcopy(stage3_source["visual_direction"])
    images = copy.deepcopy(stage3_source["images"])
    for image in images:
        image["ai_prompt"] = prompt_for_image(image, visual_direction)
    source = {
        "schema_version": SCHEMA_VERSION,
        "kind": "bid_delivery_image_plan_source",
        "project_id": manifest["project_id"],
        "stage4_confirmation_sha256": manifest["stage4_confirmation_sha256"],
        "stage3_confirmation_sha256": stage3_receipt["confirmation_sha256"],
        "generated_at": utc_now(),
        "visual_direction": visual_direction,
        "images": images,
        "cleanup_actions": copy.deepcopy(stage3_source["cleanup_actions"]),
    }
    validate_image_plan_source(source, manifest, project_dir)
    path = _safe_delivery_file(project_dir, workbook["source_path"])
    atomic_write_json(path, source)
    workbook["source_sha256"] = sha256_file(path)
    workbook["export_sha256"] = None
    workbook["status"] = "generating"
    _write_manifest(project_dir, manifest)
    return source


def validate_image_plan_source(source: dict[str, Any], manifest: dict[str, Any], project_dir: Path) -> None:
    expected = {
        "schema_version", "kind", "project_id", "stage4_confirmation_sha256",
        "stage3_confirmation_sha256", "generated_at", "visual_direction", "images", "cleanup_actions",
    }
    _require_exact_keys(source, expected, "图片规划结构化源稿")
    if source.get("schema_version") != SCHEMA_VERSION or source.get("kind") != "bid_delivery_image_plan_source":
        raise ValueError("图片规划结构化源稿版本不支持")
    if source.get("project_id") != manifest["project_id"]:
        raise ValueError("图片规划结构化源稿项目不匹配")
    if source.get("stage4_confirmation_sha256") != manifest["stage4_confirmation_sha256"]:
        raise ValueError("图片规划结构化源稿未绑定当前最终授权")
    _require_sha256(source.get("stage3_confirmation_sha256"), "图片规划阶段三确认摘要")
    _require_nonempty_string(source.get("generated_at"), "图片规划源稿生成时间")
    stage3_source, stage3_receipt = _stage3_image_plan(project_dir)
    if source["stage3_confirmation_sha256"] != stage3_receipt["confirmation_sha256"]:
        raise ValueError("图片规划结构化源稿未绑定当前图片确认")
    # The confirmed Stage-3 plan is the immutable baseline.  A delivery-stage
    # Excel may carry a locally saved revision, but it must still satisfy the
    # same chapter, figure-number and placement constraints as that baseline.
    candidate = copy.deepcopy(stage3_source)
    candidate["visual_direction"] = source["visual_direction"]
    candidate["images"] = source["images"]
    candidate["cleanup_actions"] = source["cleanup_actions"]
    confirmed = confirm_ui.stage3_confirmation_valid(project_dir / confirm_ui.DATA_DIR_NAME)
    if not confirmed:
        raise ValueError("图片规划确认回执不存在、已失效，或其上游确认已变化")
    _baseline, stage2_receipt, _stage3_receipt = confirmed
    confirm_ui.validate_image_plan(candidate, stage2_receipt)


def load_batch_source(project_dir: Path, batch_id: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    manifest = load_manifest(project_dir)
    batch = _batch_by_id(manifest, batch_id)
    if batch["status"] not in {"ready_for_review", "revision_pending", "confirmed", "export_pending"}:
        raise ValueError("当前Word批次尚未生成可审校内容")
    source = _require_recorded_source(project_dir, batch, manifest) if batch["status"] == "export_pending" else None
    if source is None:
        _require_recorded_artifacts(project_dir, batch)
        source_path = _safe_delivery_file(project_dir, batch["source_path"])
        source = read_json(source_path)
        validate_batch_source(source, manifest, batch)
        validate_source_against_confirmed_outline(project_dir, source, batch)
        validate_batch_image_alignment(project_dir, source, batch)
    return manifest, batch, source


def batch_reader_payload(project_dir: Path, batch_id: str, offset: int = 0, limit: int = 30) -> dict[str, Any]:
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("阅读起始位置无效")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_READER_BLOCKS:
        raise ValueError(f"每次最多读取{MAX_READER_BLOCKS}个内容块")
    manifest, batch, source = load_batch_source(project_dir, batch_id)
    blocks = source["blocks"]
    slice_end = min(offset + limit, len(blocks))
    validation_page: dict[str, Any] = {}
    try:
        validation_page = _validated_word_check(project_dir, manifest, batch).get("page_verification", {})
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    lower_bound, upper_bound = page_bounds(batch["planned_pages"])
    return {
        "batch": {
            "id": batch["id"], "order": batch["order"], "chapter_numbers": batch["chapter_numbers"],
            "chapter_titles": batch["chapter_titles"], "planned_pages": batch["planned_pages"],
            "actual_pages": source["actual_pages"], "status": batch["status"], "source_version": source["source_version"],
            "source_sha256": batch["source_sha256"], "export_filename": batch["output_filename"],
            "page_verification": validation_page,
            "page_bounds": {"min": lower_bound, "max": upper_bound},
        },
        "chapters": source["chapters"],
        "blocks": source["blocks"][offset:slice_end],
        "paging": {"offset": offset, "limit": limit, "total": len(blocks), "next_offset": slice_end if slice_end < len(blocks) else None},
        "read_only": True,
        "manifest_status": manifest["status"],
    }


def batch_validation_payload(project_dir: Path, batch_id: str) -> dict[str, Any]:
    manifest, batch, _source = load_batch_source(project_dir, batch_id)
    path = _safe_delivery_file(project_dir, f"{RESULTS_DIR_NAME}/word-batch-{batch['order']:02d}-validation.json")
    if not path.is_file():
        raise ValueError("当前Word批次尚未完成本地导出校验")
    validation = read_json(path)
    if validation.get("project_id") != manifest["project_id"] or validation.get("batch_id") != batch_id:
        raise ValueError("当前Word批次导出校验记录不匹配")
    if validation.get("source_sha256") != batch["source_sha256"] or validation.get("export_sha256") != batch["export_sha256"]:
        raise ValueError("当前Word批次导出校验记录已过期")
    return {"batch_id": batch_id, "validation": validation, "read_only": True}


def _validated_word_check(project_dir: Path, manifest: dict[str, Any], batch: dict[str, Any]) -> dict[str, Any]:
    path = _safe_delivery_file(project_dir, f"{RESULTS_DIR_NAME}/word-batch-{batch['order']:02d}-validation.json")
    if not path.is_file():
        raise ValueError("缺少Word本地导出校验记录")
    validation = read_json(path)
    if validation.get("project_id") != manifest["project_id"] or validation.get("batch_id") != batch["id"]:
        raise ValueError("Word本地导出校验记录不匹配")
    if validation.get("source_sha256") != batch["source_sha256"] or validation.get("export_sha256") != batch["export_sha256"]:
        raise ValueError("Word本地导出校验记录已过期")
    page = validation.get("page_verification")
    if not isinstance(page, dict):
        raise ValueError("Word页数校验记录无效")
    return validation


def record_wps_page_check(project_dir: Path, batch_id: str, actual_pages: int, verifier: str) -> dict[str, Any]:
    """Record a page count observed in WPS without altering the source draft.

    The execution host must call this only after opening the exported DOCX in
    WPS. It is intentionally a separate result record because pagination is a
    rendered-document fact, not generated bid text.
    """
    manifest, batch, _source = load_batch_source(project_dir, batch_id)
    if manifest["status"] == "final_confirmed":
        raise ValueError("最终交付已确认，不能再登记页数或开启审校版")
    if batch["status"] not in {"ready_for_review", "confirmed"}:
        raise ValueError("当前Word批次不能登记WPS页数")
    pages = _require_positive_int(actual_pages, "WPS实际页数")
    actor = _require_nonempty_string(verifier, "WPS校验执行者")
    validation = _validated_word_check(project_dir, manifest, batch)
    page = validation["page_verification"]
    raw_estimated_pages = page.get("raw_estimated_pages", page.get("estimated_pages"))
    if not isinstance(raw_estimated_pages, int) or raw_estimated_pages < 1:
        raise ValueError("Word页数校验记录缺少原始预计页数，无法校准")
    calibration = record_page_calibration(project_dir, manifest, batch, raw_estimated_pages, pages)
    lower_bound, upper_bound = page_bounds(batch["planned_pages"])
    within_tolerance = lower_bound <= pages <= upper_bound
    calibrated_estimated_pages = max(1, int(math.ceil(raw_estimated_pages * calibration["ratio"])))
    replan_required = pages < batch["planned_pages"] * REPLAN_ACTUAL_RATIO
    page.update({"status": "verified_wps", "actual_pages": pages, "verified_at": utc_now(), "verifier": actor,
                 "raw_estimated_pages": raw_estimated_pages, "estimated_pages": calibrated_estimated_pages,
                 "calibration_ratio": calibration["ratio"], "calibration_sample_count": calibration["sample_count"],
                 "within_tolerance": within_tolerance, "revision_required": not within_tolerance,
                 "replan_required": replan_required,
                 "allowed_min_pages": lower_bound, "allowed_max_pages": upper_bound})
    path = _safe_delivery_file(project_dir, f"{RESULTS_DIR_NAME}/word-batch-{batch['order']:02d}-validation.json")
    atomic_write_json(path, validation)
    if not within_tolerance:
        action = "expand" if pages < lower_bound else "compress"
        prior_status = batch["status"]
        if prior_status == "confirmed":
            _prepare_revision_filename(project_dir, batch)
            _archive_confirmation(_batch_confirmation_path(project_dir, batch), project_dir, f"batch-{batch['order']:02d}-page-mismatch")
            batch["review_confirmation_sha256"] = None
        batch["status"] = "revision_pending"
        manifest["active_batch_id"] = batch["id"]
        manifest["status"] = "revision_pending"
        manifest["pending_request_count"] += 1
        updated, event = _persist_event_and_manifest(
            project_dir,
            manifest,
            "revision",
            {
                "batch_id": batch["id"],
                "kind": "page-count-mismatch",
                "actual_pages": pages,
                "planned_pages": batch["planned_pages"],
                "allowed_min_pages": lower_bound,
                "allowed_max_pages": upper_bound,
                "action": action,
                "replan_required": replan_required,
                "previous_status": prior_status,
                "revision_filename": batch["output_filename"],
                "instruction": (
                    f"WPS实际{pages}页，明显低于计划{batch['planned_pages']}页；请先复核已确认目录和章节粒度，"
                    "停止新增无归属独立模块，再按深度优先扩展现有段落或挂靠四级标题后重新导出登记。"
                    if replan_required and action == "expand" else
                    f"WPS实际{pages}页，不在允许范围{lower_bound}-{upper_bound}页；请{('扩充' if action == 'expand' else '压缩')}内容后重新导出并登记。"
                ),
            },
        )
        validation["revision_required"] = True
        validation["revision_action"] = action
        validation["replan_required"] = replan_required
        atomic_write_json(path, validation)
        validation["manifest"] = updated
        validation["event"] = event
        return validation
    if all(item["status"] == "confirmed" for item in manifest["word_batches"]) and manifest["image_plan_workbook"]["status"] == "confirmed" and _final_inputs_ready(project_dir, manifest):
        manifest["status"] = "final_ready"
        _write_manifest(project_dir, manifest)
    return validation


def _final_inputs_ready(project_dir: Path, manifest: dict[str, Any]) -> bool:
    if manifest["pending_request_count"] or manifest["image_plan_workbook"]["status"] != "confirmed":
        return False
    try:
        image_plan_payload(project_dir)
        _require_image_plan_confirmation(project_dir, manifest)
        for batch in manifest["word_batches"]:
            if batch["status"] != "confirmed":
                return False
            _require_recorded_artifacts(project_dir, batch)
            _require_batch_confirmation(project_dir, manifest, batch)
            page = _validated_word_check(project_dir, manifest, batch).get("page_verification", {})
            lower_bound, upper_bound = page_bounds(batch["planned_pages"])
            if page.get("status") == "pending_wps_check":
                estimated = page.get("estimated_pages")
                if not isinstance(estimated, int) or not lower_bound <= estimated <= upper_bound:
                    return False
            elif page.get("status") == "verified_wps" and isinstance(page.get("actual_pages"), int):
                if not lower_bound <= page["actual_pages"] <= upper_bound:
                    return False
            else:
                return False
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return True


def final_delivery_payload(project_dir: Path) -> dict[str, Any]:
    manifest = load_manifest(project_dir)
    entries: list[dict[str, Any]] = []
    for batch in manifest["word_batches"]:
        item: dict[str, Any] = {"batch_id": batch["id"], "filename": batch["output_filename"], "status": batch["status"], "ready": False, "detail": "尚未确认"}
        if batch["status"] == "confirmed":
            try:
                validation = _validated_word_check(project_dir, manifest, batch)
                page = validation.get("page_verification", {})
                lower_bound, upper_bound = page_bounds(batch["planned_pages"])
                actual = page.get("actual_pages")
                if page.get("status") != "verified_wps":
                    estimate = page.get("estimated_pages")
                    estimate_ok = isinstance(estimate, int) and lower_bound <= estimate <= upper_bound
                    item.update({"actual_pages": actual, "estimated_pages": estimate, "ready": estimate_ok,
                                 "detail": (f"AI预计约{estimate}页，符合{lower_bound}-{upper_bound}页；WPS实际页数可选登记"
                                            if estimate_ok else f"AI预计约{estimate}页，暂不满足{lower_bound}-{upper_bound}页；请先调整内容")})
                elif not isinstance(actual, int) or not lower_bound <= actual <= upper_bound:
                    item.update({"actual_pages": actual, "detail": f"WPS实际页数{actual}不在允许范围{lower_bound}-{upper_bound}页"})
                else:
                    item.update({"ready": True, "actual_pages": actual, "detail": f"Word已确认，WPS页数符合{lower_bound}-{upper_bound}页"})
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                item["detail"] = str(exc)
        entries.append(item)
    workbook = manifest["image_plan_workbook"]
    try:
        image_ready = workbook["status"] == "confirmed" and bool(image_plan_payload(project_dir)) and bool(_require_image_plan_confirmation(project_dir, manifest))
        image_detail = "图片规划Excel已确认并导出" if image_ready else "图片规划Excel待确认、导出或校验"
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        image_ready, image_detail = False, str(exc)
    eligible = _final_inputs_ready(project_dir, manifest)
    if eligible and manifest["status"] == "all_batches_confirmed":
        manifest["status"] = "final_ready"
        _write_manifest(project_dir, manifest)
    return {
        "status": manifest["status"], "eligible": eligible, "final_confirmed": manifest["status"] == "final_confirmed",
        "word_batches": entries,
        "image_plan_workbook": {"filename": workbook["filename"], "ready": image_ready, "detail": image_detail},
        "pending_request_count": manifest["pending_request_count"], "read_only": True,
    }


def delivery_overview_payload(project_dir: Path) -> dict[str, Any]:
    """Return only display-safe metadata; do not load every long source file."""
    manifest = load_manifest(project_dir)
    recommendation, _receipt, _chapters = _load_stage4_authorization(project_dir)
    summary = recommendation.get("summary")
    if not isinstance(summary, dict):
        raise ValueError("最终交付推荐缺少项目摘要")
    batches: list[dict[str, Any]] = []
    for batch in manifest["word_batches"]:
        readable = batch["status"] in {"ready_for_review", "revision_pending", "confirmed", "export_pending"}
        wps_status = "not_available"
        actual_pages = None
        if batch["status"] in {"ready_for_review", "confirmed"}:
            try:
                page = _validated_word_check(project_dir, manifest, batch).get("page_verification", {})
                wps_status = str(page.get("status", "pending_wps_check"))
                actual_pages = page.get("actual_pages")
            except (OSError, ValueError, json.JSONDecodeError):
                wps_status = "pending_wps_check"
        batches.append({
            "id": batch["id"], "order": batch["order"], "chapter_numbers": batch["chapter_numbers"],
            "chapter_titles": batch["chapter_titles"], "planned_pages": batch["planned_pages"],
            "status": batch["status"], "readable": readable, "export_filename": batch["output_filename"],
            "wps_status": wps_status, "actual_pages": actual_pages,
            "page_bounds": {"min": page_bounds(batch["planned_pages"])[0], "max": page_bounds(batch["planned_pages"])[1]},
        })
    workbook = manifest["image_plan_workbook"]
    return {
        "project": {
            "name": summary.get("project_name", ""), "client": summary.get("client", ""),
            "overview": summary.get("project_overview", ""), "planned_pages": summary.get("planned_pages"),
            "chapter_count": summary.get("chapter_count"), "image_count": summary.get("image_count"),
        },
        "delivery": {"status": manifest["status"], "word_batch_count": manifest["word_batch_count"], "active_batch_id": manifest["active_batch_id"], "pending_request_count": manifest["pending_request_count"]},
        "batches": batches,
        "image_plan_workbook": {"filename": workbook["filename"], "status": workbook["status"], "planned_image_count": summary.get("image_count")},
        "read_only": True,
    }


def _revision_record(project_dir: Path, *, project_id: str, record_id: str, kind: str, batch_id: str, block_id: str | None, before_source_sha256: str, after_source_sha256: str | None, detail: dict[str, Any]) -> None:
    record = {
        "schema_version": SCHEMA_VERSION, "kind": "bid_delivery_revision", "id": record_id, "revision_kind": kind,
        "project_id": project_id, "batch_id": batch_id, "block_id": block_id,
        "before_source_sha256": before_source_sha256, "after_source_sha256": after_source_sha256,
        "created_at": utc_now(), "detail": detail,
    }
    atomic_write_json(_history_path(project_dir, record_id), record)


def _write_source_and_mark_export_pending(project_dir: Path, manifest: dict[str, Any], batch: dict[str, Any], source: dict[str, Any], *, record_id: str, kind: str, block_id: str | None, detail: dict[str, Any]) -> dict[str, Any]:
    source_path = _safe_delivery_file(project_dir, batch["source_path"])
    before_hash = batch["source_sha256"]
    if not isinstance(before_hash, str):
        raise ValueError("Word批次缺少源稿摘要")
    source["source_version"] += 1
    source["updated_at"] = utc_now()
    if batch["status"] == "confirmed":
        _prepare_revision_filename(project_dir, batch)
    validate_batch_source(source, manifest, batch)
    validate_source_against_confirmed_outline(project_dir, source, batch)
    validate_batch_image_alignment(project_dir, source, batch)
    atomic_write_json(source_path, source)
    if batch["status"] == "confirmed":
        _archive_confirmation(_batch_confirmation_path(project_dir, batch), project_dir, f"batch-{batch['order']:02d}-confirmation-invalidated")
    after_hash = sha256_file(source_path)
    batch["source_sha256"] = after_hash
    batch["export_sha256"] = None
    batch["review_confirmation_sha256"] = None
    batch["status"] = "export_pending"
    manifest["active_batch_id"] = batch["id"]
    manifest["status"] = "export_pending"
    _revision_record(project_dir, project_id=manifest["project_id"], record_id=record_id, kind=kind, batch_id=batch["id"], block_id=block_id, before_source_sha256=before_hash, after_source_sha256=after_hash, detail=detail)
    return _write_manifest(project_dir, manifest)


def _editable_block(source: dict[str, Any], block_id: str) -> dict[str, Any]:
    for block in source["blocks"]:
        if block["id"] == block_id:
            if block["type"] not in {"paragraph", "list", "table"}:
                raise ValueError("直接编辑仅支持正文、列表和表格")
            return block
    raise ValueError("要编辑的内容块不存在")


def apply_direct_edit(project_dir: Path, batch_id: str, block_id: str, expected_source_sha256: str,
                      replacement_text: str | None = None, replacement_items: list[str] | None = None,
                      replacement_columns: list[str] | None = None, replacement_rows: list[list[str]] | None = None) -> dict[str, Any]:
    """Apply a deterministic paragraph, list, or table replacement with audit history."""
    manifest = load_manifest(project_dir)
    if manifest["status"] == "final_confirmed":
        raise ValueError("最终交付已确认；请先建立新的修订版本再修改")
    batch = _batch_by_id(manifest, batch_id)
    if batch["status"] == "export_pending":
        raise ValueError("当前批次已有修改，Word需重新导出后才能继续修改")
    if batch["status"] not in {"ready_for_review", "confirmed"}:
        raise ValueError("当前Word批次不能直接修改")
    source = _require_recorded_source(project_dir, batch, manifest)
    if expected_source_sha256 != batch["source_sha256"]:
        raise ValueError("源稿已更新，请刷新页面后再提交修改")
    block = _editable_block(source, block_id)
    block_type = block["type"]
    if block_type == "paragraph":
        text = _require_nonempty_string(replacement_text, "替换正文")
        if block["text"] == text:
            raise ValueError("替换正文与当前内容一致")
        before, after = block["text"], text
        block["text"] = text
    elif block_type == "list":
        if not isinstance(replacement_items, list) or not replacement_items:
            raise ValueError("替换列表必须至少包含一项")
        items = [_require_nonempty_string(item, "替换列表项") for item in replacement_items]
        if block["items"] == items:
            raise ValueError("替换列表与当前内容一致")
        before, after = copy.deepcopy(block["items"]), items
        block["items"] = items
    else:
        if not isinstance(replacement_columns, list) or not replacement_columns:
            raise ValueError("替换表格必须包含表头")
        if not isinstance(replacement_rows, list):
            raise ValueError("替换表格行格式无效")
        columns = [_require_nonempty_string(item, "替换表头") for item in replacement_columns]
        rows: list[list[str]] = []
        for row in replacement_rows:
            if not isinstance(row, list) or len(row) != len(columns):
                raise ValueError("替换表格每行列数必须与表头一致")
            rows.append([_require_nonempty_string(item, "替换表格单元格") for item in row])
        if block["columns"] == columns and block["rows"] == rows:
            raise ValueError("替换表格与当前内容一致")
        before = {"columns": copy.deepcopy(block["columns"]), "rows": copy.deepcopy(block["rows"])}
        after = {"columns": columns, "rows": rows}
        block["columns"], block["rows"] = columns, rows
    record_id = f"direct-{_next_sequence(_safe_delivery_file(project_dir, HISTORY_DIR_NAME), 'direct'):04d}"
    updated = _write_source_and_mark_export_pending(project_dir, manifest, batch, source, record_id=record_id, kind="direct_edit", block_id=block_id, detail={"block_type": block_type, "before": before, "after": after})
    updated, event = _persist_event_and_manifest(project_dir, updated, "direct-edit", {"batch_id": batch_id, "record_id": record_id, "kind": "direct_edit"})
    return {"manifest": updated, "record_id": record_id, "source_sha256": batch["source_sha256"], "event": event}


def validate_ai_request(request: dict[str, Any]) -> None:
    _require_exact_keys(request, {"schema_version", "kind", "id", "project_id", "batch_id", "block_id", "source_sha256", "instruction", "status", "created_at", "updated_at"}, "AI修改请求")
    if request.get("schema_version") != SCHEMA_VERSION or request.get("kind") not in {"bid_delivery_ai_request", "bid_delivery_image_plan_ai_request"}:
        raise ValueError("AI修改请求版本不支持")
    _require_nonempty_string(request.get("id"), "AI修改请求ID")
    _require_nonempty_string(request.get("project_id"), "AI修改请求项目ID")
    _require_nonempty_string(request.get("batch_id"), "AI修改请求批次ID")
    if request.get("block_id") is not None:
        _require_nonempty_string(request.get("block_id"), "AI修改请求内容块ID")
    _require_sha256(request.get("source_sha256"), "AI修改请求源稿摘要")
    _require_nonempty_string(request.get("instruction"), "AI修改说明")
    if request.get("status") not in REQUEST_STATUSES:
        raise ValueError("AI修改请求状态不支持")
    _require_nonempty_string(request.get("created_at"), "AI修改请求创建时间")
    _require_nonempty_string(request.get("updated_at"), "AI修改请求更新时间")


def create_ai_request(project_dir: Path, batch_id: str, block_id: str | None, expected_source_sha256: str, instruction: str) -> dict[str, Any]:
    manifest = load_manifest(project_dir)
    if manifest["status"] == "final_confirmed":
        raise ValueError("最终交付已确认；请先建立新的修订版本再提交修改")
    batch = _batch_by_id(manifest, batch_id)
    if batch["status"] == "export_pending":
        raise ValueError("当前批次已有修改，Word需重新导出后才能提交AI修改")
    if batch["status"] not in {"ready_for_review", "confirmed"}:
        raise ValueError("当前Word批次不能提交AI修改")
    source = _require_recorded_source(project_dir, batch, manifest)
    if expected_source_sha256 != batch["source_sha256"]:
        raise ValueError("源稿已更新，请刷新页面后再提交AI修改")
    if block_id is not None:
        _editable_block(source, block_id)
    clean_instruction = _require_nonempty_string(instruction, "AI修改说明")
    request_id = f"request-{_next_sequence(_safe_delivery_file(project_dir, REQUESTS_DIR_NAME), 'request'):04d}"
    now = utc_now()
    request = {"schema_version": SCHEMA_VERSION, "kind": "bid_delivery_ai_request", "id": request_id, "project_id": manifest["project_id"], "batch_id": batch_id, "block_id": block_id, "source_sha256": batch["source_sha256"], "instruction": clean_instruction, "status": "pending", "created_at": now, "updated_at": now}
    validate_ai_request(request)
    atomic_write_json(_request_path(project_dir, request_id), request)
    batch["status"] = "revision_pending"
    manifest["status"] = "revision_pending"
    manifest["pending_request_count"] += 1
    updated, event = _persist_event_and_manifest(project_dir, manifest, "revision", {"batch_id": batch_id, "request_id": request_id})
    return {"manifest": updated, "request": request, "event": event}


def create_image_plan_ai_request(project_dir: Path, image_id: str, expected_source_sha256: str, instruction: str) -> dict[str, Any]:
    """Queue an AI rewrite for one confirmed image-plan record.

    This is intentionally separate from direct editing: a browser only records
    the request; the host claims it and supplies the replacement JSON.
    """
    manifest = load_manifest(project_dir)
    if manifest["status"] == "final_confirmed":
        raise ValueError("最终交付已确认；请先建立新的修订版本再提交修改")
    workbook = manifest["image_plan_workbook"]
    if workbook["status"] == "export_pending":
        raise ValueError("图片规划已有修改，Excel需重新导出后才能提交AI修改")
    if workbook["status"] not in {"ready_for_review", "confirmed"}:
        raise ValueError("当前图片规划Excel不能提交AI修改")
    source_path = _safe_delivery_file(project_dir, workbook["source_path"])
    source = read_json(source_path)
    validate_image_plan_source(source, manifest, project_dir)
    if expected_source_sha256 != workbook["source_sha256"]:
        raise ValueError("图片规划已更新，请刷新页面后再提交AI修改")
    if not any(item.get("id") == image_id for item in source["images"]):
        raise ValueError("要修改的图片规划记录不存在")
    request_id = f"request-{_next_sequence(_safe_delivery_file(project_dir, REQUESTS_DIR_NAME), 'request'):04d}"
    now = utc_now()
    request = {"schema_version": SCHEMA_VERSION, "kind": "bid_delivery_image_plan_ai_request", "id": request_id, "project_id": manifest["project_id"], "batch_id": "image-plan", "block_id": image_id, "source_sha256": workbook["source_sha256"], "instruction": _require_nonempty_string(instruction, "AI修改说明"), "status": "pending", "created_at": now, "updated_at": now}
    validate_ai_request(request)
    atomic_write_json(_request_path(project_dir, request_id), request)
    workbook["status"] = "revision_pending"
    manifest["status"] = "revision_pending"
    manifest["pending_request_count"] += 1
    updated, event = _persist_event_and_manifest(project_dir, manifest, "revision", {"batch_id": "image-plan", "request_id": request_id, "kind": "image_plan_ai_request"})
    return {"manifest": updated, "request": request, "event": event}


def _load_request(project_dir: Path, request_id: str) -> dict[str, Any]:
    request = read_json(_request_path(project_dir, request_id))
    validate_ai_request(request)
    return request


def begin_ai_request(project_dir: Path, request_id: str) -> dict[str, Any]:
    manifest = load_manifest(project_dir)
    request = _load_request(project_dir, request_id)
    if request["kind"] == "bid_delivery_image_plan_ai_request":
        workbook = manifest["image_plan_workbook"]
        if request["project_id"] != manifest["project_id"] or request["status"] != "pending" or manifest["status"] != "revision_pending" or workbook["status"] != "revision_pending":
            raise ValueError("当前图片规划没有待处理的AI修改")
        if request["source_sha256"] != workbook["source_sha256"]:
            request["status"] = "superseded"; request["updated_at"] = utc_now(); atomic_write_json(_request_path(project_dir, request_id), request)
            manifest["pending_request_count"] -= 1; workbook["status"] = "export_pending"; manifest["status"] = "export_pending"; _write_manifest(project_dir, manifest)
            raise ValueError("AI修改请求对应的图片规划已变化，已自动失效")
        request["status"] = "applying"; request["updated_at"] = utc_now(); atomic_write_json(_request_path(project_dir, request_id), request)
        workbook["status"] = "regenerating"; manifest["status"] = "revising"; manifest["pending_request_count"] -= 1
        return {"manifest": _write_manifest(project_dir, manifest), "request": request}
    batch = _batch_by_id(manifest, request["batch_id"])
    if request["project_id"] != manifest["project_id"] or request["status"] != "pending":
        raise ValueError("AI修改请求不可处理")
    if manifest["status"] != "revision_pending" or batch["status"] != "revision_pending":
        raise ValueError("当前批次没有待处理的AI修改")
    if request["source_sha256"] != batch["source_sha256"]:
        request["status"] = "superseded"
        request["updated_at"] = utc_now()
        atomic_write_json(_request_path(project_dir, request_id), request)
        manifest["pending_request_count"] -= 1
        batch["status"] = "export_pending"
        manifest["status"] = "export_pending"
        _write_manifest(project_dir, manifest)
        raise ValueError("AI修改请求对应的源稿已变化，已自动失效")
    request["status"] = "applying"
    request["updated_at"] = utc_now()
    atomic_write_json(_request_path(project_dir, request_id), request)
    batch["status"] = "regenerating"
    manifest["status"] = "revising"
    manifest["pending_request_count"] -= 1
    return {"manifest": _write_manifest(project_dir, manifest), "request": request}


def list_ai_requests(project_dir: Path, batch_id: str | None = None) -> list[dict[str, Any]]:
    manifest = load_manifest(project_dir)
    records: list[dict[str, Any]] = []
    for path in sorted(_safe_delivery_file(project_dir, REQUESTS_DIR_NAME).glob("request-*.json")):
        request = read_json(path)
        validate_ai_request(request)
        if request["project_id"] != manifest["project_id"]:
            raise ValueError("AI修改请求项目不匹配")
        if batch_id is None or request["batch_id"] == batch_id:
            records.append(request)
    return records


def apply_ai_request_result(project_dir: Path, request_id: str, replacement_text: str) -> dict[str, Any]:
    manifest = load_manifest(project_dir)
    request = _load_request(project_dir, request_id)
    batch = _batch_by_id(manifest, request["batch_id"])
    if request["status"] != "applying" or manifest["status"] != "revising" or batch["status"] != "regenerating":
        raise ValueError("AI修改请求当前不能写入结果")
    source = _require_recorded_source(project_dir, batch, manifest)
    if request["source_sha256"] != batch["source_sha256"]:
        raise ValueError("AI修改请求源稿已变化，拒绝覆盖新版本")
    if request["block_id"] is None:
        raise ValueError("当前协议仅支持对指定正文段落写入AI修改结果")
    block = _editable_block(source, request["block_id"])
    text = _require_nonempty_string(replacement_text, "AI修改后的正文")
    result = {"schema_version": SCHEMA_VERSION, "kind": "bid_delivery_ai_result", "request_id": request_id, "project_id": manifest["project_id"], "batch_id": batch["id"], "block_id": request["block_id"], "before_source_sha256": batch["source_sha256"], "replacement_text": text, "created_at": utc_now()}
    atomic_write_json(_result_path(project_dir, request_id), result)
    before_text = block["text"]
    block["text"] = text
    request["status"] = "applied"
    request["updated_at"] = utc_now()
    atomic_write_json(_request_path(project_dir, request_id), request)
    updated = _write_source_and_mark_export_pending(project_dir, manifest, batch, source, record_id=f"ai-{request_id}", kind="ai_request", block_id=request["block_id"], detail={"request_id": request_id, "instruction": request["instruction"], "before_text": before_text, "after_text": text})
    return {"manifest": updated, "request": request, "result": result, "source_sha256": batch["source_sha256"]}


def _require_recorded_artifacts(project_dir: Path, batch: dict[str, Any]) -> None:
    source_hash = _require_sha256(batch.get("source_sha256"), "Word源稿摘要", nullable=True)
    export_hash = _require_sha256(batch.get("export_sha256"), "Word导出摘要", nullable=True)
    if not source_hash or not export_hash:
        raise ValueError("Word批次尚未登记源稿和导出文件，不能进入审校或确认")
    source_path = _safe_delivery_file(project_dir, batch["source_path"])
    export_path = _safe_delivery_file(project_dir, batch["export_path"])
    if not source_path.is_file() or not export_path.is_file():
        raise ValueError("Word批次源稿或导出文件不存在")
    if sha256_file(source_path) != source_hash or sha256_file(export_path) != export_hash:
        raise ValueError("Word批次源稿或导出文件已变化，需要重新登记")


def _require_recorded_source(project_dir: Path, batch: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    source_hash = _require_sha256(batch.get("source_sha256"), "Word源稿摘要", nullable=True)
    if not source_hash:
        raise ValueError("Word批次缺少已登记的结构化源稿")
    source_path = _safe_delivery_file(project_dir, batch["source_path"])
    if not source_path.is_file() or sha256_file(source_path) != source_hash:
        raise ValueError("Word批次结构化源稿已变化，需要重新登记")
    source = read_json(source_path)
    validate_batch_source(source, manifest, batch)
    validate_source_against_confirmed_outline(project_dir, source, batch)
    validate_batch_image_alignment(project_dir, source, batch)
    return source


def begin_active_batch(project_dir: Path) -> dict[str, Any]:
    manifest = load_manifest(project_dir)
    if manifest["status"] == "generating":
        active = _batch_by_id(manifest, manifest["active_batch_id"])
        if active["status"] == "generating":
            return manifest
    if manifest["status"] not in {"preparing", "awaiting_next_batch"}:
        raise ValueError("当前交付状态不能开始新的Word批次")
    batch_id = manifest.get("active_batch_id")
    if not isinstance(batch_id, str):
        raise ValueError("当前没有待生成的Word批次")
    batch = _batch_by_id(manifest, batch_id)
    if batch["status"] != "pending":
        raise ValueError("当前Word批次不是待生成状态")
    batch["status"] = "generating"
    manifest["status"] = "generating"
    return _write_manifest(project_dir, manifest)


def pause_active_batch(project_dir: Path) -> dict[str, Any]:
    """Return an unstarted active batch to the explicit '准备中' state."""
    manifest = load_manifest(project_dir)
    batch_id = manifest.get("active_batch_id")
    if manifest["status"] != "generating" or not isinstance(batch_id, str):
        return manifest
    batch = _batch_by_id(manifest, batch_id)
    if batch["status"] != "generating" or batch.get("source_sha256") or batch.get("export_sha256"):
        raise ValueError("当前批次已开始产出，不能直接暂停；请先在生产台完成或回退第四阶段")
    batch["status"] = "pending"
    manifest["status"] = "preparing"
    return _write_manifest(project_dir, manifest)


def register_batch_artifacts(project_dir: Path, batch_id: str) -> dict[str, Any]:
    """Record files generated by a host without generating them here."""
    manifest = load_manifest(project_dir)
    batch = _batch_by_id(manifest, batch_id)
    if batch["status"] not in {"generating", "regenerating", "export_pending"}:
        raise ValueError("只能为正在生成的Word批次登记文件")
    source_path = _safe_delivery_file(project_dir, batch["source_path"])
    export_path = _safe_delivery_file(project_dir, batch["export_path"])
    if not source_path.is_file() or not export_path.is_file():
        raise ValueError("需要先由AI生成结构化源稿和Word导出文件")
    batch["source_sha256"] = sha256_file(source_path)
    batch["export_sha256"] = sha256_file(export_path)
    source = read_json(source_path)
    validate_batch_source(source, manifest, batch)
    validate_source_against_confirmed_outline(project_dir, source, batch)
    validate_batch_image_alignment(project_dir, source, batch)
    _require_source_page_floor(source, batch, load_page_calibration(project_dir)["ratio"])
    batch["status"] = "ready_for_review"
    manifest["status"] = "awaiting_batch_review"
    return _write_manifest(project_dir, manifest)


def register_image_plan_artifacts(project_dir: Path) -> dict[str, Any]:
    """Register the sole planning workbook after a local XLSX export.

    The workbook is a hand-off brief for another image-generation AI. It is
    deliberately not treated as evidence that any image has been generated.
    """
    manifest = load_manifest(project_dir)
    workbook = manifest["image_plan_workbook"]
    if workbook["status"] not in {"generating", "export_pending"}:
        raise ValueError("图片规划Excel当前不处于可登记状态")
    source_path = _safe_delivery_file(project_dir, workbook["source_path"])
    export_path = _safe_delivery_file(project_dir, workbook["export_path"])
    if not source_path.is_file() or not export_path.is_file():
        raise ValueError("需要先生成图片规划结构化源稿和Excel导出文件")
    validate_image_plan_source(read_json(source_path), manifest, project_dir)
    workbook["source_sha256"] = sha256_file(source_path)
    workbook["export_sha256"] = sha256_file(export_path)
    workbook["status"] = "ready_for_review"
    # Image-only edits do not own a Word batch. Restore the surrounding Word
    # workflow state before validating the manifest after deterministic export.
    if manifest["status"] == "export_pending":
        active_id = manifest.get("active_batch_id")
        active = _batch_by_id(manifest, active_id) if isinstance(active_id, str) else None
        if active and active["status"] == "pending":
            manifest["status"] = "awaiting_next_batch" if any(item["status"] == "confirmed" for item in manifest["word_batches"]) else "preparing"
        elif active and active["status"] == "generating":
            manifest["status"] = "generating"
        elif active and active["status"] == "ready_for_review":
            manifest["status"] = "awaiting_batch_review"
        elif all(item["status"] == "confirmed" for item in manifest["word_batches"]):
            manifest["status"] = "all_batches_confirmed"
    updated = _write_manifest(project_dir, manifest)
    if all(item["status"] == "confirmed" for item in updated["word_batches"]) and _final_inputs_ready(project_dir, updated):
        updated["status"] = "final_ready"
        return _write_manifest(project_dir, updated)
    return updated


def image_plan_payload(project_dir: Path) -> dict[str, Any]:
    manifest = load_manifest(project_dir)
    workbook = manifest["image_plan_workbook"]
    if workbook["status"] not in {"ready_for_review", "export_pending", "confirmed"}:
        raise ValueError("图片规划Excel尚未导出")
    source_path = _safe_delivery_file(project_dir, workbook["source_path"])
    export_path = _safe_delivery_file(project_dir, workbook["export_path"])
    if not source_path.is_file() or not export_path.is_file():
        raise ValueError("图片规划结构化源稿或Excel文件不存在")
    if sha256_file(source_path) != workbook["source_sha256"]:
        raise ValueError("图片规划结构化源稿或Excel文件已变化，需要重新导出")
    if workbook["status"] != "export_pending" and sha256_file(export_path) != workbook["export_sha256"]:
        raise ValueError("图片规划结构化源稿或Excel文件已变化，需要重新导出")
    source = read_json(source_path)
    validate_image_plan_source(source, manifest, project_dir)
    return {
        "filename": workbook["filename"], "status": workbook["status"],
        "source_sha256": workbook["source_sha256"], "export_sha256": workbook["export_sha256"],
        "image_count": len(source["images"]), "visual_direction": source["visual_direction"],
        "cleanup_actions": source["cleanup_actions"], "images": source["images"],
        "read_only": workbook["status"] == "confirmed",
    }


def apply_image_plan_direct_edit(project_dir: Path, image_id: str, expected_source_sha256: str, replacement: dict[str, Any]) -> dict[str, Any]:
    """Save a deterministic delivery-stage Excel row edit.

    The edit remains local and marks only the XLSX for re-export.  It does not
    call a model or mutate the confirmed Stage-3 receipt.
    """
    manifest = load_manifest(project_dir)
    if manifest["status"] == "final_confirmed":
        raise ValueError("最终交付已确认；请先建立新的修订版本再修改")
    workbook = manifest["image_plan_workbook"]
    if workbook["status"] not in {"ready_for_review", "export_pending", "confirmed"}:
        raise ValueError("图片规划Excel尚未生成可编辑内容")
    if workbook["status"] == "confirmed":
        _archive_confirmation(_image_plan_confirmation_path(project_dir), project_dir, "image-plan-confirmation-invalidated")
    source_path = _safe_delivery_file(project_dir, workbook["source_path"])
    source = read_json(source_path)
    validate_image_plan_source(source, manifest, project_dir)
    if expected_source_sha256 != workbook["source_sha256"]:
        raise ValueError("图片规划已更新，请刷新页面后再提交修改")
    if not isinstance(replacement, dict):
        raise ValueError("图片规划修改内容必须是对象")
    editable = {"name", "type", "purpose", "core_nodes", "composition", "orientation", "is_chapter_overview", "placement_note", "ai_prompt"}
    if not replacement or set(replacement) - editable:
        raise ValueError("图片规划修改字段无效")
    image = next((item for item in source["images"] if item.get("id") == image_id), None)
    if image is None:
        raise ValueError("要编辑的图片规划记录不存在")
    before = copy.deepcopy(image)
    for key, value in replacement.items():
        if key == "placement_note":
            image["position"]["placement_note"] = _require_nonempty_string(value, "具体放置说明")
        elif key == "core_nodes":
            image[key] = _require_string_list(value, "核心节点")
        elif key == "is_chapter_overview":
            if not isinstance(value, bool):
                raise ValueError("是否章首总览图必须为布尔值")
            image[key] = value
        elif key == "ai_prompt":
            image[key] = _require_nonempty_string(value, "ai_prompt")
        else:
            image[key] = _require_nonempty_string(value, key)
    if "ai_prompt" not in replacement:
        image["ai_prompt"] = compose_ai_image_prompt(image, source.get("visual_direction") or {})
    if image == before:
        raise ValueError("图片规划修改内容与当前记录一致")
    validate_image_plan_source(source, manifest, project_dir)
    atomic_write_json(source_path, source)
    before_hash = workbook["source_sha256"]
    workbook["source_sha256"] = sha256_file(source_path)
    workbook["export_sha256"] = None
    workbook["status"] = "export_pending"
    if manifest["status"] in {"final_ready", "all_batches_confirmed"}:
        manifest["status"] = "all_batches_confirmed"
    record_id = f"image-plan-direct-{_next_sequence(_safe_delivery_file(project_dir, HISTORY_DIR_NAME), 'image-plan-direct'):04d}"
    _revision_record(project_dir, project_id=manifest["project_id"], record_id=record_id, kind="image_plan_direct_edit", batch_id="image-plan", block_id=image_id, before_source_sha256=before_hash, after_source_sha256=workbook["source_sha256"], detail={"before": before, "after": image})
    updated, event = _persist_event_and_manifest(project_dir, manifest, "direct-edit", {"batch_id": "image-plan", "record_id": record_id, "kind": "image_plan_direct_edit"})
    return {"manifest": updated, "record_id": record_id, "source_sha256": workbook["source_sha256"], "event": event}


def apply_image_plan_ai_request_result(project_dir: Path, request_id: str, replacement: dict[str, Any]) -> dict[str, Any]:
    """Apply one host-produced image-plan replacement, then require re-export."""
    manifest = load_manifest(project_dir)
    request = _load_request(project_dir, request_id)
    workbook = manifest["image_plan_workbook"]
    if request["kind"] != "bid_delivery_image_plan_ai_request" or request["status"] != "applying" or manifest["status"] != "revising" or workbook["status"] != "regenerating":
        raise ValueError("图片规划AI修改请求当前不能写入结果")
    source_path = _safe_delivery_file(project_dir, workbook["source_path"])
    source = read_json(source_path)
    validate_image_plan_source(source, manifest, project_dir)
    if request["source_sha256"] != workbook["source_sha256"]:
        raise ValueError("图片规划AI修改请求源稿已变化，拒绝覆盖新版本")
    image_id = request["block_id"]
    image = next((item for item in source["images"] if item.get("id") == image_id), None)
    if image is None or not isinstance(replacement, dict):
        raise ValueError("图片规划AI修改结果无效")
    editable = {"name", "type", "purpose", "core_nodes", "composition", "orientation", "is_chapter_overview", "placement_note", "ai_prompt"}
    if not replacement or set(replacement) - editable:
        raise ValueError("图片规划AI修改字段无效")
    before = copy.deepcopy(image)
    for key, value in replacement.items():
        if key == "placement_note": image["position"]["placement_note"] = _require_nonempty_string(value, "具体放置说明")
        elif key == "core_nodes": image[key] = _require_string_list(value, "核心节点")
        elif key == "is_chapter_overview":
            if not isinstance(value, bool): raise ValueError("是否章首总览图必须为布尔值")
            image[key] = value
        elif key == "ai_prompt":
            image[key] = _require_nonempty_string(value, "ai_prompt")
        else: image[key] = _require_nonempty_string(value, key)
    if "ai_prompt" not in replacement:
        image["ai_prompt"] = compose_ai_image_prompt(image, source.get("visual_direction") or {})
    validate_image_plan_source(source, manifest, project_dir)
    result = {"schema_version": SCHEMA_VERSION, "kind": "bid_delivery_image_plan_ai_result", "request_id": request_id, "project_id": manifest["project_id"], "image_id": image_id, "before_source_sha256": workbook["source_sha256"], "replacement": replacement, "created_at": utc_now()}
    atomic_write_json(_result_path(project_dir, request_id), result)
    atomic_write_json(source_path, source)
    before_hash = workbook["source_sha256"]
    workbook["source_sha256"] = sha256_file(source_path); workbook["export_sha256"] = None; workbook["status"] = "export_pending"
    request["status"] = "applied"; request["updated_at"] = utc_now(); atomic_write_json(_request_path(project_dir, request_id), request)
    record_id = f"ai-{request_id}"
    _revision_record(project_dir, project_id=manifest["project_id"], record_id=record_id, kind="image_plan_ai_request", batch_id="image-plan", block_id=image_id, before_source_sha256=before_hash, after_source_sha256=workbook["source_sha256"], detail={"request_id": request_id, "instruction": request["instruction"], "before": before, "after": image})
    manifest["status"] = "export_pending"
    return {"manifest": _write_manifest(project_dir, manifest), "request": request, "result": result, "source_sha256": workbook["source_sha256"]}


def confirm_image_plan(project_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Publish and lock the independently reviewed image-plan Excel."""
    manifest = load_manifest(project_dir)
    if manifest["status"] == "final_confirmed":
        raise ValueError("最终交付已确认；不能再次确认图片规划Excel")
    workbook = manifest["image_plan_workbook"]
    if workbook["status"] != "ready_for_review":
        raise ValueError("只有已导出并待审校的图片规划Excel可以确认")
    image_plan_payload(project_dir)
    published = publish_confirmed_file(
        project_dir,
        manifest,
        _safe_delivery_file(project_dir, workbook["export_path"]),
        workbook["filename"],
    )
    receipt = {
        "schema_version": SCHEMA_VERSION, "kind": "bid_delivery_image_plan_confirmation", "status": "confirmed",
        "project_id": manifest["project_id"], "stage4_confirmation_sha256": manifest["stage4_confirmation_sha256"],
        "filename": workbook["filename"], "source_sha256": workbook["source_sha256"], "export_sha256": workbook["export_sha256"],
        "delivery_output_path": published["path"], "delivery_output_sha256": published["sha256"], "confirmed_at": utc_now(),
    }
    receipt["confirmation_sha256"] = _confirmation_digest(receipt)
    atomic_write_json(_image_plan_confirmation_path(project_dir), receipt)
    workbook["status"] = "confirmed"
    if all(item["status"] == "confirmed" for item in manifest["word_batches"]) and _final_inputs_ready(project_dir, manifest):
        manifest["status"] = "final_ready"
    updated, event = _persist_event_and_manifest(project_dir, manifest, "image-plan-confirmed", {"confirmation_sha256": receipt["confirmation_sha256"], "delivery_output_path": published["path"]})
    return updated, event


def _event_payload(manifest: dict[str, Any], event_type: str, payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if event_type not in EVENT_TYPES:
        raise ValueError("不支持的本地交付事件")
    updated = copy.deepcopy(manifest)
    event_id = updated["last_event_id"] + 1
    updated["last_event_id"] = event_id
    updated["updated_at"] = utc_now()
    event = {
        "schema_version": SCHEMA_VERSION,
        "kind": "bid_delivery_event",
        "event_id": event_id,
        "type": event_type,
        "project_id": updated["project_id"],
        "manifest_sha256": sha256_data(updated),
        "created_at": utc_now(),
        "payload": payload,
    }
    event["event_sha256"] = sha256_data(event)
    return updated, event


def validate_event(event: dict[str, Any]) -> None:
    _require_exact_keys(event, {"schema_version", "kind", "event_id", "type", "project_id", "manifest_sha256", "created_at", "payload", "event_sha256"}, "交付事件")
    if event.get("schema_version") != SCHEMA_VERSION or event.get("kind") != "bid_delivery_event":
        raise ValueError("交付事件版本不支持")
    if isinstance(event.get("event_id"), bool) or not isinstance(event.get("event_id"), int) or event["event_id"] < 1:
        raise ValueError("交付事件序号无效")
    if event.get("type") not in EVENT_TYPES:
        raise ValueError("交付事件类型无效")
    _require_nonempty_string(event.get("project_id"), "交付事件项目ID")
    _require_sha256(event.get("manifest_sha256"), "交付事件清单摘要")
    _require_nonempty_string(event.get("created_at"), "交付事件时间")
    if not isinstance(event.get("payload"), dict):
        raise ValueError("交付事件内容必须是对象")
    claimed = event.get("event_sha256")
    if not isinstance(claimed, str):
        raise ValueError("交付事件摘要无效")
    unsigned = dict(event)
    unsigned.pop("event_sha256", None)
    if claimed != sha256_data(unsigned):
        raise ValueError("交付事件摘要不匹配")


def _persist_event_and_manifest(project_dir: Path, manifest: dict[str, Any], event_type: str, payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    updated, event = _event_payload(manifest, event_type, payload)
    validate_manifest(updated, project_dir)
    event_path = event_dir(project_dir) / f"event-{event['event_id']:06d}.json"
    if event_path.exists():
        raise ValueError("交付事件序号冲突")
    atomic_write_json(event_path, event)
    atomic_write_json(manifest_path(project_dir), updated)
    return updated, event


def request_revision(project_dir: Path, batch_id: str, reason: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Move one reviewed batch into a persisted revision-pending state.

    The natural-language request file/UI is intentionally deferred to the next
    development stage. This function establishes the durable state and event
    contract needed by a compatible AI host.
    """
    manifest = load_manifest(project_dir)
    batch = _batch_by_id(manifest, batch_id)
    if manifest["status"] != "awaiting_batch_review" or batch["status"] != "ready_for_review":
        raise ValueError("只有待审校的Word批次可以请求修改")
    _require_recorded_artifacts(project_dir, batch)
    clean_reason = _require_nonempty_string(reason, "修改说明")
    batch["status"] = "revision_pending"
    manifest["status"] = "revision_pending"
    manifest["pending_request_count"] += 1
    return _persist_event_and_manifest(project_dir, manifest, "revision", {"batch_id": batch_id, "reason": clean_reason})


def begin_revision(project_dir: Path, batch_id: str) -> dict[str, Any]:
    manifest = load_manifest(project_dir)
    batch = _batch_by_id(manifest, batch_id)
    if manifest["status"] != "revision_pending" or batch["status"] != "revision_pending":
        raise ValueError("当前Word批次没有待处理的修改请求")
    # AI requests raised against an already delivered batch start a new
    # review-version filename.  A first-pass revision has no prior receipt and
    # keeps the original filename until it is delivered for the first time.
    if batch.get("review_confirmation_sha256"):
        _prepare_revision_filename(project_dir, batch)
    batch["status"] = "regenerating"
    manifest["status"] = "revising"
    manifest["pending_request_count"] -= 1
    return _write_manifest(project_dir, manifest)


def confirm_batch(project_dir: Path, batch_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = load_manifest(project_dir)
    batch = _batch_by_id(manifest, batch_id)
    if manifest["status"] != "awaiting_batch_review" or batch["status"] != "ready_for_review":
        raise ValueError("只有待审校的Word批次可以确认")
    _require_recorded_artifacts(project_dir, batch)
    published = publish_confirmed_file(project_dir, manifest, _safe_delivery_file(project_dir, batch["export_path"]), batch["output_filename"])
    batch["status"] = "confirmed"
    receipt = {
        "schema_version": SCHEMA_VERSION, "kind": "bid_delivery_batch_confirmation", "status": "confirmed",
        "project_id": manifest["project_id"], "batch_id": batch_id, "batch_order": batch["order"],
        "stage4_confirmation_sha256": manifest["stage4_confirmation_sha256"],
        "source_sha256": batch["source_sha256"], "export_sha256": batch["export_sha256"],
        "delivery_output_path": published["path"], "delivery_output_sha256": published["sha256"],
        "confirmed_at": utc_now(),
    }
    receipt["confirmation_sha256"] = _confirmation_digest(receipt)
    atomic_write_json(_batch_confirmation_path(project_dir, batch), receipt)
    batch["review_confirmation_sha256"] = receipt["confirmation_sha256"]
    remaining = [item for item in manifest["word_batches"] if item["status"] != "confirmed"]
    if remaining:
        next_batch = remaining[0]
        manifest["active_batch_id"] = next_batch["id"]
        next_batch["status"] = "generating"
        manifest["status"] = "generating"
    else:
        manifest["active_batch_id"] = None
        manifest["status"] = "all_batches_confirmed"
    updated, event = _persist_event_and_manifest(project_dir, manifest, "batch-confirmed", {"batch_id": batch_id, "confirmation_sha256": receipt["confirmation_sha256"], "delivery_output_path": published["path"]})
    if updated["status"] == "all_batches_confirmed" and _final_inputs_ready(project_dir, updated):
        updated["status"] = "final_ready"
        updated = _write_manifest(project_dir, updated)
    return updated, event


def confirm_final_delivery(project_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Lock the exact N Word files plus the one planning Excel as final output."""
    manifest = load_manifest(project_dir)
    if manifest["status"] not in {"all_batches_confirmed", "final_ready"}:
        raise ValueError("当前交付尚未完成全部批次确认，不能最终确认")
    if not _final_inputs_ready(project_dir, manifest):
        raise ValueError("最终交付条件未完成：请确认全部Word和图片规划Excel均已校验")
    word_batches = [
        {"id": batch["id"], "source_sha256": batch["source_sha256"], "export_sha256": batch["export_sha256"], "batch_confirmation_sha256": batch["review_confirmation_sha256"]}
        for batch in manifest["word_batches"]
    ]
    workbook = manifest["image_plan_workbook"]
    image_receipt = _require_image_plan_confirmation(project_dir, manifest)
    receipt = {
        "schema_version": SCHEMA_VERSION, "kind": "bid_delivery_final_confirmation", "status": "confirmed",
        "project_id": manifest["project_id"], "stage4_confirmation_sha256": manifest["stage4_confirmation_sha256"],
        "word_batches": word_batches,
        "image_plan_workbook": {"filename": workbook["filename"], "source_sha256": workbook["source_sha256"], "export_sha256": workbook["export_sha256"], "delivery_output_path": image_receipt["delivery_output_path"], "delivery_output_sha256": image_receipt["delivery_output_sha256"]},
        "confirmed_at": utc_now(),
    }
    receipt["confirmation_sha256"] = _confirmation_digest(receipt)
    atomic_write_json(_final_confirmation_path(project_dir), receipt)
    manifest["final_confirmation_sha256"] = receipt["confirmation_sha256"]
    manifest["active_batch_id"] = None
    manifest["status"] = "final_confirmed"
    return _persist_event_and_manifest(project_dir, manifest, "final-confirmed", {"confirmation_sha256": receipt["confirmation_sha256"], "word_batch_count": manifest["word_batch_count"], "image_plan_filename": workbook["filename"], "delivery_output_path": image_receipt["delivery_output_path"]})


def list_events(project_dir: Path) -> list[dict[str, Any]]:
    root = event_dir(project_dir)
    if not root.exists():
        return []
    events: list[dict[str, Any]] = []
    for path in sorted(root.glob("event-*.json")):
        event = read_json(path)
        validate_event(event)
        events.append(event)
    return events


def wait_for_event(project_dir: Path, event_type: str, timeout: int, after_event_id: int = 0) -> Path | None:
    if event_type not in EVENT_TYPES | {"user-action"}:
        raise ValueError("不支持的等待事件")
    if isinstance(after_event_id, bool) or not isinstance(after_event_id, int) or after_event_id < 0:
        raise ValueError("等待事件序号无效")
    deadline = None if timeout == 0 else time.time() + timeout
    while deadline is None or time.time() < deadline:
        manifest = load_manifest(project_dir)
        for event in list_events(project_dir):
            if event["project_id"] != manifest["project_id"]:
                raise ValueError("交付事件项目与清单不一致")
            # A direct edit is fully handled by the local deterministic
            # exporter.  Only AI requests and real confirmation gates resume
            # the host conversation.
            matches = event["type"] == event_type if event_type != "user-action" else event["type"] in {"revision", "batch-confirmed", "image-plan-confirmed", "final-confirmed"}
            if event["event_id"] > after_event_id and matches:
                return event_dir(project_dir) / f"event-{event['event_id']:06d}.json"
        time.sleep(0.2)
    return None
