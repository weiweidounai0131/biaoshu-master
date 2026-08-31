#!/usr/bin/env python3
"""Local browser confirmation service for biaoshu-master.

The browser never calls an AI provider. The agent writes stage recommendations;
the page edits them and this server persists a hash-bound user receipt.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import platform
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


HOST = "127.0.0.1"
DEFAULT_PORT = 5380
LOCK_NAME = ".bid_confirm_ui.lock"
DATA_DIR_NAME = "bid_confirm_ui"
INTAKE_INPUT = "intake-recommendations.json"
INTAKE_RECEIPT = "intake-confirmation.json"
STAGE1_INPUT = "stage1-recommendations.json"
STAGE1_RECEIPT = "stage1-confirmation.json"
STAGE2_INPUT = "stage2-recommendations.json"
STAGE2_RECEIPT = "stage2-confirmation.json"
STAGE3_INPUT = "stage3-recommendations.json"
STAGE3_RECEIPT = "stage3-confirmation.json"
STAGE4_INPUT = "stage4-recommendations.json"
STAGE4_RECEIPT = "stage4-confirmation.json"
WORKFLOW_STATE = "workflow-state.json"
CALLBACK_LOG = "callback-events.jsonl"
AGENT_WAIT = "agent-wait.json"
PAGE_PRESENCE = "page-presence.json"
PAGE_HEARTBEAT_STALE_SECONDS = 120
PAGE_REOPEN_COOLDOWN_SECONDS = 180
PROCESS_MAX_AGE_SECONDS = 4 * 60 * 60
PROCESS_REGISTRY = ".bid_confirm_ui.processes.json"
PROCESS_CLEANUP_INTERVAL_SECONDS = 60
STAGE1_DRAFT = "stage1-edit-draft.json"
STAGE2_DRAFT = "stage2-edit-draft.json"
STAGE2_REBALANCE_REQUEST = "stage2-rebalance-request.json"
STAGE2_AI_ADJUST_REQUEST = "stage2-ai-adjust-request.json"
STAGE3_DRAFT = "stage3-edit-draft.json"
STAGE3_AI_ADJUST_REQUEST = "stage3-ai-adjust-request.json"
STAGE4_DRAFT = "stage4-edit-draft.json"
HISTORY_DIR = "history"
STATIC_DIR = Path(__file__).resolve().parent / "static"
IMAGE_TYPES = {"章首总览图", "流程图", "泳道图", "矩阵图", "时间轴", "生命周期图", "对比图", "其他"}
DELIVERY_DIR_NAME = "bid_delivery"
DELIVERY_HISTORY_DIR_NAME = "bid_delivery-history"
MATERIAL_POLICY = {
    "background": "需求书、招标文件、评分表、澄清文件和客户正式资料是本项目写作的权威底层依据。",
    "reference": "历史项目、成熟策略和公司经验仅作为可选参考；采用前必须核对与本项目背景的一致性，不得直接当作项目事实。",
}

VISUAL_DIRECTION_TEXT_KEYS: dict[str, tuple[str, ...]] = {
    "palette": ("palette", "primary_colors", "main_colors", "color_palette", "colour_palette", "color_scheme", "colour_scheme", "colors", "colour", "配色", "配色方案", "主色", "主辅色"),
    "style": ("style", "visual_style", "style_description", "风格", "视觉风格"),
    "background": ("background", "background_style", "background_description", "背景", "背景风格"),
    "density": ("density", "information_density", "info_density", "density_description", "信息密度", "信息密度说明"),
}
VISUAL_DIRECTION_AVOID_KEYS = ("avoid", "avoid_styles", "avoid_style", "negative_prompt", "avoid_visuals", "avoid_visual_features", "避免的风格", "应避免", "避免")

SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import rule_profiles


def canonical_json(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_data(data: Any) -> str:
    return hashlib.sha256(canonical_json(data)).hexdigest()


def visual_direction_text(visual_direction: dict[str, Any], field: str) -> str:
    """Read canonical and legacy field names without changing the source hash."""
    for key in VISUAL_DIRECTION_TEXT_KEYS[field]:
        value = visual_direction.get(key)
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def visual_direction_avoid(visual_direction: dict[str, Any]) -> list[str]:
    for key in VISUAL_DIRECTION_AVOID_KEYS:
        value = visual_direction.get(key)
        if isinstance(value, list):
            result = [str(item).strip() for item in value if str(item).strip()]
            if result:
                return result
        elif isinstance(value, str) and value.strip():
            result = [item.strip() for item in value.replace("；", "\n").replace(";", "\n").replace("，", "\n").replace(",", "\n").splitlines() if item.strip()]
            if result:
                return result
    return []


def utc_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _timestamp_age(value: Any) -> int | None:
    try:
        timestamp = datetime.fromisoformat(str(value))
        return max(0, int((datetime.now().astimezone() - timestamp).total_seconds()))
    except (TypeError, ValueError):
        return None


def page_presence_status(data_dir: Path) -> dict[str, Any]:
    path = data_dir / PAGE_PRESENCE
    try:
        presence = read_json(path) if path.exists() else {}
    except (OSError, ValueError, json.JSONDecodeError):
        presence = {}
    seen_age = _timestamp_age(presence.get("last_seen_at"))
    opened_age = _timestamp_age(presence.get("last_open_attempt_at"))
    return {
        "page_open": seen_age is not None and seen_age <= PAGE_HEARTBEAT_STALE_SECONDS,
        "last_seen_at": presence.get("last_seen_at"),
        "last_seen_age_seconds": seen_age,
        "page": presence.get("page"),
        "last_open_attempt_at": presence.get("last_open_attempt_at"),
        "last_open_attempt_age_seconds": opened_age,
    }


def record_page_presence(data_dir: Path, page: Any, instance_id: Any) -> dict[str, Any]:
    if page not in {"stage1", "stage2", "stage3", "stage4"}:
        raise ValueError("确认台页面标识无效")
    if not isinstance(instance_id, str) or not 8 <= len(instance_id) <= 100:
        raise ValueError("确认台页面实例标识无效")
    path = data_dir / PAGE_PRESENCE
    try:
        state = read_json(path) if path.exists() else {}
    except (OSError, ValueError, json.JSONDecodeError):
        state = {}
    state.update({"page": page, "instance_id": instance_id, "last_seen_at": utc_now()})
    atomic_write_json(path, state)
    return page_presence_status(data_dir)


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return data


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temp, path)


def log_callback_event(data_dir: Path, event: str, stage: str, details: str = "") -> None:
    """Persist each confirmation handoff locally without exposing source files."""
    entry = {
        "timestamp": utc_now(),
        "event": event,
        "stage": stage,
        "details": details,
    }
    with (data_dir / CALLBACK_LOG).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def set_agent_wait(
    data_dir: Path,
    stage: str,
    status: str,
    details: str = "",
    recommendation_sha256: str | None = None,
) -> None:
    """Expose the active AI handoff without giving the browser model access."""
    payload = {
        "schema_version": 1,
        "stage": stage,
        "status": status,
        "pid": os.getpid(),
        "execution_mode": "foreground_required",
        "background_forbidden": True,
        "wait_command": f"python3 scripts/bid_confirm_ui/server.py <project_dir> --wait-only --wait-stage {stage} --wait-timeout 0",
        "updated_at": utc_now(),
        "details": details,
        "recommendation_sha256": recommendation_sha256,
    }
    atomic_write_json(data_dir / AGENT_WAIT, payload)
    log_callback_event(data_dir, f"agent_wait_{status}", stage, details)


def archive_files(data_dir: Path, names: list[str], reason: str) -> list[str]:
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    target = data_dir / HISTORY_DIR / f"{stamp}-{reason}"
    archived = []
    for name in names:
        source = data_dir / name
        if source.exists():
            target.mkdir(parents=True, exist_ok=True)
            destination = target / name
            os.replace(source, destination)
            archived.append(str(destination))
    return archived


def archive_delivery_workspace(project_dir: Path, reason: str) -> str | None:
    """Invalidate a running delivery round without deleting its audit trail.

    The production page reads its workflow link on a short interval.  Moving
    the manifest out of the active location makes any waiting/generating host
    fail closed on its next protocol check, while the still-open browser can
    receive the redirect instruction from its local service.
    """
    source = project_dir / DELIVERY_DIR_NAME
    if not source.exists():
        return None
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    target = project_dir / DELIVERY_HISTORY_DIR_NAME / f"{stamp}-{reason}"
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, target)
    return str(target)


def delivery_status(project_dir: Path) -> dict[str, Any]:
    """Small, host-neutral handoff status used for same-tab Stage-4 switching."""
    data_dir = project_dir / DATA_DIR_NAME
    confirmed = stage4_confirmation_valid(data_dir)
    state = workflow_state(data_dir)
    result: dict[str, Any] = {
        "authorized": bool(confirmed),
        "delivery_ready": False,
        "delivery_url": None,
        "workflow": state,
    }
    if not confirmed:
        return result
    _source, _stage3_receipt, stage4_receipt = confirmed
    manifest_path = project_dir / DELIVERY_DIR_NAME / "manifest.json"
    if not manifest_path.exists():
        return result
    try:
        manifest = read_json(manifest_path)
        if manifest.get("stage4_confirmation_sha256") != stage4_receipt.get("confirmation_sha256"):
            return result
        result["delivery_ready"] = True
        lock = read_json(project_dir / DELIVERY_DIR_NAME / "lock.json")
        pid = int(lock.get("pid", 0))
        port = int(lock.get("port", 0))
        if process_alive(pid) and 1 <= port <= 65535:
            result["delivery_url"] = f"http://{HOST}:{port}"
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return result


def workflow_state(data_dir: Path) -> dict[str, Any]:
    try:
        if stage4_confirmation_valid(data_dir):
            completed = ["intake", "stage1", "stage2", "stage3", "stage4"] if intake_receipt_valid(data_dir) else ["stage1", "stage2", "stage3", "stage4"]
            return {"active_stage": "stage4", "mode": "confirmed", "completed": completed}
    except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError):
        pass
    try:
        if recommendation_ready(data_dir, "stage4"):
            completed = ["intake", "stage1", "stage2", "stage3"] if intake_receipt_valid(data_dir) else ["stage1", "stage2", "stage3"]
            return {"active_stage": "stage4", "mode": "editing", "completed": completed}
    except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError):
        pass
    try:
        if stage3_confirmation_valid(data_dir):
            completed = ["intake", "stage1", "stage2", "stage3"] if intake_receipt_valid(data_dir) else ["stage1", "stage2", "stage3"]
            return {"active_stage": "stage3", "mode": "confirmed", "completed": completed}
    except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError):
        pass
    try:
        if recommendation_ready(data_dir, "stage3"):
            completed = ["intake", "stage1", "stage2"] if intake_receipt_valid(data_dir) else ["stage1", "stage2"]
            return {"active_stage": "stage3", "mode": "editing", "completed": completed}
    except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError):
        pass
    try:
        if stage2_confirmation_valid(data_dir):
            completed = ["intake", "stage1", "stage2"] if intake_receipt_valid(data_dir) else ["stage1", "stage2"]
            return {"active_stage": "stage2", "mode": "confirmed", "completed": completed}
    except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError):
        pass
    path = data_dir / WORKFLOW_STATE
    if path.exists():
        try:
            stored = read_json(path)
            active = stored.get("active_stage")
            mode = stored.get("mode")
            if active == "intake" and mode == "awaiting_analysis" and not intake_receipt_valid(data_dir):
                raise ValueError("stale intake workflow state")
            if active == "stage1" and mode == "editing" and not recommendation_ready(data_dir, "stage1"):
                raise ValueError("stale stage1 workflow state")
            if active == "stage1" and mode == "awaiting_stage2" and not stage1_confirmation_valid(data_dir):
                raise ValueError("stale stage1 confirmation state")
            if active == "stage1" and mode == "awaiting_stage2" and recommendation_ready(data_dir, "stage2"):
                raise ValueError("stage2 is ready; advance from stale stage1 waiting state")
            if active == "stage2" and mode == "editing":
                rebalance = stage2_rebalance_status(data_dir)
                adjustment = stage_ai_adjust_status(data_dir, "stage2")
                if rebalance.get("status") == "waiting" or adjustment.get("status") == "waiting":
                    return stored
            if active == "stage2" and not recommendation_ready(data_dir, "stage2"):
                raise ValueError("stale stage2 workflow state")
            if active == "stage2" and mode == "confirmed" and not stage2_confirmation_valid(data_dir):
                raise ValueError("stale stage2 confirmation state")
            if active == "stage2" and mode == "confirmed" and recommendation_ready(data_dir, "stage3"):
                raise ValueError("stage3 is ready; advance from stale stage2 waiting state")
            if active == "stage3" and mode == "editing" and stage_ai_adjust_status(data_dir, "stage3").get("status") == "waiting":
                return stored
            if active == "stage3" and not recommendation_ready(data_dir, "stage3"):
                raise ValueError("stale stage3 workflow state")
            if active == "stage3" and mode == "confirmed" and not stage3_confirmation_valid(data_dir):
                raise ValueError("stale stage3 confirmation state")
            if active == "stage3" and mode == "confirmed" and recommendation_ready(data_dir, "stage4"):
                raise ValueError("stage4 is ready; advance from stale stage3 waiting state")
            if active == "stage4" and not recommendation_ready(data_dir, "stage4"):
                raise ValueError("stale stage4 workflow state")
            if active == "stage4" and mode == "confirmed" and not stage4_confirmation_valid(data_dir):
                raise ValueError("stale stage4 confirmation state")
            return stored
        except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError):
            pass
    try:
        if recommendation_ready(data_dir, "stage2"):
            completed = ["intake", "stage1"] if intake_receipt_valid(data_dir) else ["stage1"]
            return {"active_stage": "stage2", "mode": "editing", "completed": completed}
    except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError):
        pass
    if stage1_confirmation_valid(data_dir):
        completed = ["intake", "stage1"] if intake_receipt_valid(data_dir) else ["stage1"]
        return {"active_stage": "stage1", "mode": "awaiting_stage2", "completed": completed}
    try:
        if recommendation_ready(data_dir, "stage1"):
            completed = ["intake"] if intake_receipt_valid(data_dir) else []
            return {"active_stage": "stage1", "mode": "editing", "completed": completed}
    except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError):
        pass
    if intake_receipt_valid(data_dir):
        return {"active_stage": "intake", "mode": "awaiting_analysis", "completed": ["intake"]}
    return {"active_stage": "intake", "mode": "editing", "completed": []}


def write_workflow_state(data_dir: Path, active_stage: str, mode: str, completed: list[str]) -> None:
    atomic_write_json(data_dir / WORKFLOW_STATE, {
        "schema_version": 1,
        "active_stage": active_stage,
        "mode": mode,
        "completed": completed,
        "updated_at": utc_now(),
    })
    log_callback_event(data_dir, "workflow_state_updated", active_stage, f"mode={mode}; completed={','.join(completed)}")


def normalized_paths(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        path = str(item).strip()
        if path and path not in result:
            result.append(path)
    return result


def intake_materials(data: dict[str, Any] | None) -> dict[str, list[str]]:
    """Return separated material paths while keeping legacy intake readable."""
    source = data or {}
    if "background_paths" in source or "reference_paths" in source:
        return {
            "background_paths": normalized_paths(source.get("background_paths")),
            "reference_paths": normalized_paths(source.get("reference_paths")),
        }
    return {
        "background_paths": normalized_paths(source.get("source_paths")),
        "reference_paths": [],
    }


def validate_material_separation(materials: dict[str, list[str]]) -> None:
    overlap = sorted(set(materials["background_paths"]) & set(materials["reference_paths"]))
    if overlap:
        raise ValueError("同一路径不能同时归入背景资料和参考资料：" + overlap[0])


def normalize_tender_position(value: Any) -> str:
    position = str(value or "main").strip().lower()
    if position not in {"main", "companion"}:
        raise ValueError("标书定位必须选择主标或陪标")
    return position


def validate_intake(source: dict[str, Any]) -> None:
    required = {"schema_version", "stage", "project_id", "background"}
    missing = sorted(required - source.keys())
    if missing:
        raise ValueError("missing intake fields: " + ", ".join(missing))
    if source.get("schema_version") not in {1, 2} or source.get("stage") != "intake":
        raise ValueError("unsupported intake schema")
    if not isinstance(source.get("background"), str):
        raise ValueError("intake background has invalid type")
    if source.get("schema_version") == 1:
        if not isinstance(source.get("source_paths"), list):
            raise ValueError("legacy intake source_paths has invalid type")
    elif not isinstance(source.get("background_paths"), list) or not isinstance(source.get("reference_paths"), list):
        raise ValueError("intake background_paths and reference_paths have invalid types")
    validate_material_separation(intake_materials(source))
    if "tender_position" in source:
        normalize_tender_position(source.get("tender_position"))


def validate_intake_prefill_ready(source: dict[str, Any]) -> None:
    """Require the AI to finish prefill before any browser window is opened."""
    validate_intake(source)
    if source.get("prefill_ready") is not True:
        raise ValueError("入口预填尚未完成；请先运行 prepare_intake.py 写入项目背景和资料路径，再启动确认台")


def default_intake(project_dir: Path) -> dict[str, Any]:
    project_id = hashlib.sha256(str(project_dir).encode("utf-8")).hexdigest()[:20]
    return {
        "schema_version": 2,
        "stage": "intake",
        "project_id": project_id,
        "generated_at": utc_now(),
        "prefill_ready": False,
        "background": "",
        "background_paths": [],
        "reference_paths": [],
        "tender_position": "main",
    }


def intake_receipt_valid(data_dir: Path) -> tuple[dict[str, Any], dict[str, Any]] | None:
    source_path = data_dir / INTAKE_INPUT
    receipt_path = data_dir / INTAKE_RECEIPT
    if not source_path.exists() or not receipt_path.exists():
        return None
    source = read_json(source_path)
    receipt = read_json(receipt_path)
    validate_intake(source)
    if receipt.get("status") != "confirmed" or receipt.get("source_sha256") != sha256_data(source):
        return None
    if source.get("run_id") is not None and source.get("run_id") != receipt.get("run_id"):
        return None
    if not isinstance(receipt.get("background"), str):
        return None
    if receipt.get("schema_version") == 2 and (
        not isinstance(receipt.get("background_paths"), list)
        or not isinstance(receipt.get("reference_paths"), list)
    ):
        return None
    validate_material_separation(intake_materials(receipt))
    return source, receipt


def intake_gate_present(data_dir: Path) -> bool:
    """New-style runs must fail closed when their receipt is stale or invalid."""
    return (data_dir / INTAKE_INPUT).exists() or (data_dir / INTAKE_RECEIPT).exists()


def validate_stage1_binding(data_dir: Path, stage1: dict[str, Any]) -> None:
    """Bind newly generated Stage 1 content to the one-time intake receipt.

    Legacy projects created before the intake gate remain readable when they do
    not have an intake receipt.
    """
    bound = intake_receipt_valid(data_dir)
    if not bound:
        if intake_gate_present(data_dir):
            raise ValueError("入口确认回执无效或已过期，请重新开始项目流程")
        return
    _, intake_receipt = bound
    if stage1.get("project_id") != intake_receipt.get("project_id"):
        raise ValueError("stage1 project does not match intake confirmation")
    if stage1.get("intake_confirmation_sha256") != intake_receipt.get("confirmation_sha256"):
        raise ValueError("stage1 recommendation is stale because intake changed")
    if stage1.get("run_id") is not None and stage1.get("run_id") != intake_receipt.get("run_id"):
        raise ValueError("stage1 recommendation belongs to another intake run")
    expected_position = normalize_tender_position(intake_receipt.get("tender_position"))
    if normalize_tender_position(stage1.get("tender_position")) != expected_position:
        raise ValueError("stage1 tender position does not match intake confirmation")


def tender_position_from_stage1_receipt(receipt: dict[str, Any]) -> str:
    data = receipt.get("data") if isinstance(receipt.get("data"), dict) else {}
    return normalize_tender_position(receipt.get("tender_position") or data.get("tender_position"))


def generation_complete(source: dict[str, Any], *, strict: bool) -> bool:
    """Only expose new-workflow recommendations after an explicit final write.

    Legacy projects without the intake gate remain readable. New workflows
    must atomically change this marker from ``generating`` to ``complete``.
    """
    return source.get("generation_status") == "complete" or not strict


def recommendation_ready(data_dir: Path, stage: str) -> tuple[dict[str, Any], str]:
    # Do not enter legacy compatibility mode merely because a new receipt is invalid.
    strict = intake_gate_present(data_dir)
    if stage == "intake":
        source = read_json(data_dir / INTAKE_INPUT)
        validate_intake_prefill_ready(source)
    elif stage == "stage1":
        source = read_json(data_dir / STAGE1_INPUT)
        validate_stage1(source)
        validate_stage1_binding(data_dir, source)
        project = source.get("project", {})
        if strict:
            if not str(project.get("project_name", "")).strip() or project.get("project_name") == "待AI分析后填写":
                raise ValueError("项目口径仍是生成骨架")
            if not str(project.get("summary", "")).strip():
                raise ValueError("项目口径尚未完整生成")
    elif stage == "stage2":
        stage1_bound = stage1_confirmation_valid(data_dir)
        if not stage1_bound:
            raise ValueError("项目口径确认回执已失效")
        _, stage1_receipt = stage1_bound
        source = read_json(data_dir / STAGE2_INPUT)
        validate_stage2(source, stage1_receipt)
        validate_outline(
            source.get("chapters"),
            int(source.get("target_pages", 0)),
            source.get("coverage"),
            tender_position_from_stage1_receipt(stage1_receipt),
        )
    elif stage == "stage3":
        bound = stage3_recommendation_valid(data_dir)
        if not bound:
            raise ValueError("图片规划尚未完整生成")
        source = bound[0]
    elif stage == "stage4":
        bound = stage4_recommendation_valid(data_dir)
        if not bound:
            raise ValueError("最终交付方案尚未完整生成")
        source = bound[0]
    else:
        raise ValueError(f"unsupported recommendation stage: {stage}")
    if not generation_complete(source, strict=strict and stage != "intake"):
        raise ValueError(f"{stage} recommendation generation is not complete")
    return source, sha256_data(source)


def is_recommendation_ready(data_dir: Path, stage: str) -> bool:
    try:
        recommendation_ready(data_dir, stage)
        return True
    except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError):
        return False


def maybe_advance_to_stage1(data_dir: Path) -> dict[str, Any]:
    state = workflow_state(data_dir)
    if state.get("active_stage") != "intake" or state.get("mode") != "awaiting_analysis":
        return state
    bound = intake_receipt_valid(data_dir)
    if not bound:
        return state
    try:
        recommendation_ready(data_dir, "stage1")
    except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError):
        return state
    write_workflow_state(data_dir, "stage1", "editing", ["intake"])
    return workflow_state(data_dir)


def choose_local_paths(kind: str) -> list[str]:
    if kind == "desktop":
        desktop = Path.home() / "Desktop"
        if not desktop.is_dir():
            raise ValueError("当前设备未找到桌面文件夹，请选择其他保存位置")
        return [str(desktop.resolve())]
    if sys.platform != "darwin":
        if kind not in {"folder", "output-folder"}:
            raise ValueError("当前设备请粘贴本机绝对路径添加资料")
        try:
            import tkinter
            from tkinter import filedialog
            root = tkinter.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            selected = filedialog.askdirectory(title="选择交付物保存文件夹")
            root.destroy()
        except Exception as exc:
            raise ValueError("无法打开本机文件夹选择器，请直接粘贴绝对路径") from exc
        return normalized_paths([selected]) if selected else []
    if kind == "files":
        script = '''
set chosenItems to choose file with prompt "选择要交给AI读取的标书资料" with multiple selections allowed
set outputPaths to {}
repeat with anItem in chosenItems
    set end of outputPaths to POSIX path of anItem
end repeat
set AppleScript's text item delimiters to linefeed
return outputPaths as text
'''
    elif kind in {"folder", "output-folder"}:
        script = '''
set chosenItem to choose folder with prompt "选择交付物保存文件夹"
return POSIX path of chosenItem
'''
    else:
        raise ValueError("kind must be files or folder")
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=180)
    if result.returncode != 0:
        if "User canceled" in result.stderr or "-128" in result.stderr:
            return []
        raise ValueError(result.stderr.strip() or "unable to open local path picker")
    return normalized_paths(result.stdout.splitlines())


def process_alive(pid: int) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if platform.system() == "Windows":
        # os.kill(pid, 0) raises WinError 87 on Windows even for a live PID.
        # Query the process handle instead of treating that platform-specific
        # error as proof that the agent wait has died.
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
            kernel32.GetExitCodeProcess.restype = ctypes.c_int
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_int
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
            if not handle:
                return False
            code = ctypes.c_uint32(0)
            ok = bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code)))
            kernel32.CloseHandle(handle)
            return ok and code.value == STILL_ACTIVE
        except (AttributeError, OSError, ValueError):
            return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (OSError, ProcessLookupError):
        return False


def _read_process_registry(project_dir: Path) -> list[dict[str, Any]]:
    path = project_dir / PROCESS_REGISTRY
    if not path.exists():
        return []
    try:
        data = read_json(path)
        records = data.get("processes", [])
        return records if isinstance(records, list) else []
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return []


def _write_process_registry(project_dir: Path, records: list[dict[str, Any]]) -> None:
    atomic_write_json(project_dir / PROCESS_REGISTRY, {"schema_version": 1, "processes": records})


def _terminate_process(pid: int) -> bool:
    if pid <= 0 or pid == os.getpid() or not process_alive(pid):
        return False
    try:
        if platform.system() == "Windows":
            result = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=10, check=False)
            return result.returncode == 0 or not process_alive(pid)
        os.kill(pid, signal.SIGTERM)
        for _ in range(20):
            if not process_alive(pid):
                return True
            time.sleep(0.1)
        os.kill(pid, signal.SIGKILL)
        return not process_alive(pid)
    except (OSError, subprocess.SubprocessError):
        return False


def cleanup_stale_processes(project_dir: Path, max_age_seconds: int = PROCESS_MAX_AGE_SECONDS) -> list[int]:
    """Terminate only server PIDs previously registered for this project."""
    now = datetime.now().astimezone()
    kept: list[dict[str, Any]] = []
    killed: list[int] = []
    for record in _read_process_registry(project_dir):
        try:
            pid = int(record.get("pid", 0))
            started = datetime.fromisoformat(str(record.get("started_at", "")))
            age = (now - started).total_seconds()
        except (TypeError, ValueError):
            continue
        if not process_alive(pid):
            continue
        if age >= max_age_seconds and _terminate_process(pid):
            killed.append(pid)
            continue
        kept.append(record)
    _write_process_registry(project_dir, kept)
    return killed


def register_process(project_dir: Path, pid: int) -> None:
    cleanup_stale_processes(project_dir)
    records = []
    for record in _read_process_registry(project_dir):
        try:
            if int(record.get("pid", 0)) != pid:
                records.append(record)
        except (TypeError, ValueError):
            continue
    records.append({"pid": pid, "project": str(project_dir), "started_at": utc_now()})
    _write_process_registry(project_dir, records)


def unregister_process(project_dir: Path, pid: int) -> None:
    records = []
    for record in _read_process_registry(project_dir):
        try:
            if int(record.get("pid", 0)) != pid:
                records.append(record)
        except (TypeError, ValueError):
            continue
    _write_process_registry(project_dir, records)


def load_lock(project_dir: Path) -> dict[str, Any] | None:
    lock_path = project_dir / LOCK_NAME
    if not lock_path.exists():
        return None
    try:
        lock = read_json(lock_path)
        pid = int(lock.get("pid", 0))
    except (ValueError, TypeError, json.JSONDecodeError, OSError):
        lock_path.unlink(missing_ok=True)
        return None
    age = _timestamp_age(lock.get("started_at"))
    if age is None:
        try:
            age = max(0, int(time.time() - lock_path.stat().st_mtime))
        except OSError:
            age = None
    if age is not None and age >= PROCESS_MAX_AGE_SECONDS:
        _terminate_process(pid)
        unregister_process(project_dir, pid)
        lock_path.unlink(missing_ok=True)
        return None
    if not process_alive(pid):
        unregister_process(project_dir, pid)
        lock_path.unlink(missing_ok=True)
        return None
    return lock


def find_port(start: int) -> int:
    for port in range(start, start + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((HOST, port))
            except OSError:
                continue
            return port
    raise RuntimeError("No free local port found")


def validate_stage1(source: dict[str, Any]) -> None:
    required = {"schema_version", "stage", "project_id", "project", "scoring", "formatting", "boundaries"}
    missing = sorted(required - source.keys())
    if missing:
        raise ValueError("missing fields: " + ", ".join(missing))
    if source.get("schema_version") != 1 or source.get("stage") != "stage1":
        raise ValueError("unsupported stage1 schema")
    for key in ("project", "scoring", "formatting", "boundaries"):
        if not isinstance(source.get(key), dict):
            raise ValueError(f"{key} must be an object")
    if source.get("generation_status") not in {None, "generating", "complete"}:
        raise ValueError("stage1 generation_status is invalid")
    if "tender_position" in source:
        normalize_tender_position(source.get("tender_position"))


def validate_stage2(source: dict[str, Any], stage1_receipt: dict[str, Any]) -> None:
    required = {"schema_version", "stage", "project_id", "stage1_confirmation_sha256", "target_pages", "chapters"}
    missing = sorted(required - source.keys())
    if missing:
        raise ValueError("missing fields: " + ", ".join(missing))
    if source.get("schema_version") != 1 or source.get("stage") != "stage2":
        raise ValueError("unsupported stage2 schema")
    if source.get("project_id") != stage1_receipt.get("project_id"):
        raise ValueError("stage2 project does not match stage1")
    if source.get("stage1_confirmation_sha256") != stage1_receipt.get("confirmation_sha256"):
        raise ValueError("stage2 recommendation is stale because stage1 changed")
    try:
        confirmed_target = int(stage1_receipt.get("data", {}).get("formatting", {}).get("target_pages", 0))
        source_target = int(source.get("target_pages", 0))
    except (TypeError, ValueError):
        raise ValueError("stage2 target pages are invalid")
    if source_target != confirmed_target:
        raise ValueError("stage2 target pages do not match the confirmed project settings")
    if not isinstance(source.get("chapters"), list) or not source["chapters"]:
        raise ValueError("stage2 chapters must be a non-empty array")
    if source.get("generation_status") not in {None, "generating", "complete"}:
        raise ValueError("stage2 generation_status is invalid")
    if "tender_position" in source and normalize_tender_position(source.get("tender_position")) != tender_position_from_stage1_receipt(stage1_receipt):
        raise ValueError("stage2 tender position does not match stage1")


def validate_outline(chapters: Any, target_pages: int, coverage: Any = None, tender_position: str = "main") -> int:
    if not isinstance(chapters, list) or not chapters:
        raise ValueError("at least one chapter is required")
    seen_ids: set[str] = set()

    def walk(nodes: Any, expected_level: int) -> None:
        if not isinstance(nodes, list):
            raise ValueError("children must be an array")
        for order, node in enumerate(nodes, 1):
            if not isinstance(node, dict):
                raise ValueError("outline node must be an object")
            node_id = str(node.get("id", "")).strip()
            title = str(node.get("title", "")).strip()
            if not node_id or node_id in seen_ids:
                raise ValueError("outline node IDs must be unique")
            if not title:
                raise ValueError("outline titles cannot be blank")
            if int(node.get("level", 0)) != expected_level:
                raise ValueError("outline level is invalid")
            if int(node.get("order", 0)) != order:
                raise ValueError("sibling order is invalid")
            seen_ids.add(node_id)
            children = node.get("children", [])
            if expected_level >= 3 and children:
                raise ValueError("the confirmation UI supports headings through level 3")
            walk(children, expected_level + 1)

    walk(chapters, 1)
    total = 0
    for chapter in chapters:
        try:
            pages = int(chapter.get("pages", 0))
        except (TypeError, ValueError):
            raise ValueError("chapter pages must be integers")
        if pages < 0:
            raise ValueError("chapter pages cannot be negative")
        total += pages
    if total < target_pages:
        raise ValueError(f"planned pages {total} are below the confirmed target {target_pages}")
    # Scoring coverage is retained as optional metadata when present, but it
    # is not a Stage2 confirmation gate. A missing or partial AI coverage
    # object must not block the outline, page-budget, and handoff workflow.
    return total


def stage2_rebalance_status(data_dir: Path) -> dict[str, Any]:
    """Report the one active chapter-page rebalance without treating old data as done."""
    path = data_dir / STAGE2_REBALANCE_REQUEST
    if not path.exists():
        return {"active": False}
    request = read_json(path)
    request_id = str(request.get("request_id", "")).strip()
    source_path = data_dir / STAGE2_INPUT
    source_hash = None
    ready = False
    if source_path.exists():
        try:
            source = read_json(source_path)
            source_hash = sha256_data(source)
            stage1_bound = stage1_confirmation_valid(data_dir)
            if stage1_bound:
                validate_stage2(source, stage1_bound[1])
                ready = (
                    source.get("generation_status") == "complete"
                    and source.get("rebalance_request_id") == request_id
                    and source_hash != request.get("source_sha256")
                )
        except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError):
            ready = False
    return {
        "active": True,
        "request_id": request_id,
        "status": "ready" if ready else "waiting",
        "source_sha256": source_hash,
        "requested_at": request.get("requested_at"),
        "chapter_pages": request.get("chapter_pages", []),
    }


def pending_stage2_rebalance(data_dir: Path, waiting_source_hash: str) -> dict[str, Any] | None:
    """Return a current rebalance request bound to this stage2 wait."""
    path = data_dir / STAGE2_REBALANCE_REQUEST
    if not path.exists():
        return None
    try:
        request = read_json(path)
        if (
            request.get("schema_version") != 1
            or request.get("stage") != "stage2-rebalance"
            or request.get("status") != "pending"
            or request.get("source_sha256") != waiting_source_hash
            or not str(request.get("request_id", "")).strip()
        ):
            return None
        bound = stage1_confirmation_valid(data_dir)
        if not bound or request.get("stage1_confirmation_sha256") != bound[1].get("confirmation_sha256"):
            return None
        source = read_json(data_dir / STAGE2_INPUT)
        if source.get("generation_status") != "generating":
            return None
        if source.get("rebalance_request_id") != request.get("request_id"):
            return None
        return request
    except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError):
        return None


AI_ADJUST_REQUESTS = {
    "stage2": STAGE2_AI_ADJUST_REQUEST,
    "stage3": STAGE3_AI_ADJUST_REQUEST,
}


def ai_adjust_request_path(data_dir: Path, stage: str) -> Path | None:
    name = AI_ADJUST_REQUESTS.get(stage)
    return data_dir / name if name else None


def pending_stage_ai_adjust(data_dir: Path, stage: str, waiting_source_hash: str) -> dict[str, Any] | None:
    """Return an overall-adjustment request bound to the current stage wait."""
    path = ai_adjust_request_path(data_dir, stage)
    if path is None or not path.exists():
        return None
    try:
        request = read_json(path)
        if (
            request.get("schema_version") != 1
            or request.get("stage") != f"{stage}-ai-adjust"
            or request.get("status") != "pending"
            or request.get("source_sha256") != waiting_source_hash
            or not str(request.get("request_id", "")).strip()
        ):
            return None
        if stage == "stage2":
            bound = stage1_confirmation_valid(data_dir)
            binding_key = "stage1_confirmation_sha256"
        else:
            bound = stage2_confirmation_valid(data_dir)
            binding_key = "stage2_confirmation_sha256"
        if not bound or request.get(binding_key) != bound[1].get("confirmation_sha256"):
            return None
        source = read_json(data_dir / (STAGE2_INPUT if stage == "stage2" else STAGE3_INPUT))
        if source.get("generation_status") != "generating":
            return None
        if source.get("ai_adjust_request_id") != request.get("request_id"):
            return None
        return request
    except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError):
        return None


def stage_ai_adjust_status(data_dir: Path, stage: str) -> dict[str, Any]:
    path = ai_adjust_request_path(data_dir, stage)
    if path is None or not path.exists():
        return {"active": False}
    request = read_json(path)
    request_id = str(request.get("request_id", "")).strip()
    source_path = data_dir / (STAGE2_INPUT if stage == "stage2" else STAGE3_INPUT)
    source_hash = None
    ready = False
    if source_path.exists():
        try:
            source = read_json(source_path)
            source_hash = sha256_data(source)
            bound = stage1_confirmation_valid(data_dir) if stage == "stage2" else stage2_confirmation_valid(data_dir)
            if bound:
                if stage == "stage2":
                    validate_stage2(source, bound[1])
                else:
                    validate_stage3(source, bound[1])
                ready = (
                    source.get("generation_status") == "complete"
                    and source.get("ai_adjust_request_id") == request_id
                    and source_hash != request.get("source_sha256")
                )
        except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError):
            ready = False
    request_status = request.get("status")
    status = "ready" if ready else request_status if request_status in {"failed", "superseded"} else "waiting"
    return {
        "active": True,
        "request_id": request_id,
        "status": status,
        "source_sha256": source_hash,
        "requested_at": request.get("requested_at"),
    }


def confirmation_digest_valid(receipt: dict[str, Any]) -> bool:
    digest = receipt.get("confirmation_sha256")
    if not isinstance(digest, str) or not digest:
        return False
    content = dict(receipt)
    content.pop("confirmation_sha256", None)
    return digest == sha256_data(content)


def stage1_confirmation_valid(data_dir: Path) -> tuple[dict[str, Any], dict[str, Any]] | None:
    source_path = data_dir / STAGE1_INPUT
    receipt_path = data_dir / STAGE1_RECEIPT
    if not source_path.exists() or not receipt_path.exists():
        return None
    source = read_json(source_path)
    receipt = read_json(receipt_path)
    validate_stage1(source)
    validate_stage1_binding(data_dir, source)
    if receipt.get("schema_version") != 1 or receipt.get("stage") != "stage1" or receipt.get("status") != "confirmed":
        return None
    if receipt.get("project_id") != source.get("project_id"):
        return None
    if source.get("run_id") is not None and receipt.get("run_id") != source.get("run_id"):
        return None
    if receipt.get("source_sha256") != sha256_data(source) or not confirmation_digest_valid(receipt):
        return None
    return source, receipt


def stage2_confirmation_valid(data_dir: Path) -> tuple[dict[str, Any], dict[str, Any]] | None:
    source_path = data_dir / STAGE2_INPUT
    receipt_path = data_dir / STAGE2_RECEIPT
    stage1_bound = stage1_confirmation_valid(data_dir)
    if not source_path.exists() or not receipt_path.exists() or not stage1_bound:
        return None
    _, stage1_receipt = stage1_bound
    source = read_json(source_path)
    receipt = read_json(receipt_path)
    validate_stage2(source, stage1_receipt)
    if receipt.get("schema_version") != 1 or receipt.get("stage") != "stage2" or receipt.get("status") != "confirmed":
        return None
    if receipt.get("project_id") != source.get("project_id"):
        return None
    if receipt.get("stage1_confirmation_sha256") != stage1_receipt.get("confirmation_sha256"):
        return None
    if receipt.get("source_sha256") != sha256_data(source) or not confirmation_digest_valid(receipt):
        return None
    data = receipt.get("data")
    if not isinstance(data, dict):
        return None
    try:
        target_pages = int(source.get("target_pages", 0))
    except (TypeError, ValueError):
        return None
    validate_outline(
        data.get("chapters"),
        target_pages,
        data.get("coverage"),
        tender_position_from_stage1_receipt(stage1_receipt),
    )
    return source, receipt


def validate_image_plan(data: Any, stage2_receipt: dict[str, Any]) -> None:
    if not isinstance(data, dict):
        raise ValueError("stage3 data must be an object")
    required_top = {"visual_direction", "chapter_settings", "images", "cleanup_actions"}
    missing_top = sorted(required_top - data.keys())
    if missing_top:
        raise ValueError("missing image-plan fields: " + ", ".join(missing_top))
    visual_direction = data.get("visual_direction")
    if not isinstance(visual_direction, dict) or not visual_direction:
        raise ValueError("visual_direction must be a non-empty object")
    for field in ("palette", "style", "background", "density"):
        if not visual_direction_text(visual_direction, field):
            raise ValueError(f"visual_direction.{field} must contain an AI suggestion")
    if not visual_direction_avoid(visual_direction):
        raise ValueError("visual_direction.avoid must contain at least one visual restriction")
    chapter_settings = data.get("chapter_settings")
    cleanup_actions = data.get("cleanup_actions")
    if not isinstance(chapter_settings, list):
        raise ValueError("chapter_settings must be an array")
    if not isinstance(cleanup_actions, list):
        raise ValueError("cleanup_actions must be an array")
    images = data.get("images")
    if not isinstance(images, list) or not images:
        raise ValueError("at least one planned image is required")
    stage2_data = stage2_receipt.get("data")
    chapters = stage2_data.get("chapters") if isinstance(stage2_data, dict) else None
    if not isinstance(chapters, list) or not chapters:
        raise ValueError("confirmed stage2 chapters are unavailable")
    chapter_map: dict[str, tuple[str, str]] = {}
    outline_map: dict[str, tuple[str, str, str]] = {}

    def collect_outline(nodes: Any, prefix: str = "", root_chapter_id: str = "") -> None:
        if not isinstance(nodes, list):
            raise ValueError("confirmed stage2 outline children are invalid")
        for index, node in enumerate(nodes, 1):
            if not isinstance(node, dict):
                raise ValueError("confirmed stage2 outline node is invalid")
            node_id = str(node.get("id", "")).strip()
            number = str(node.get("number", f"{prefix}.{index}" if prefix else index)).strip()
            title = str(node.get("title", "")).strip()
            if not node_id or not number or not title:
                raise ValueError("confirmed stage2 outline node is incomplete")
            root_id = root_chapter_id or node_id
            outline_map[node_id] = (number, title, root_id)
            collect_outline(node.get("children", []), number, root_id)

    collect_outline(chapters)
    for index, chapter in enumerate(chapters, 1):
        if not isinstance(chapter, dict):
            raise ValueError("confirmed stage2 chapter is invalid")
        chapter_id = str(chapter.get("id", "")).strip()
        if not chapter_id:
            raise ValueError("confirmed stage2 chapter ID is missing")
        chapter_number = str(chapter.get("number", index)).strip() or str(index)
        chapter_title = str(chapter.get("title", "")).strip()
        chapter_map[chapter_id] = (chapter_number, chapter_title)

    seen_settings: set[str] = set()
    for setting in chapter_settings:
        if not isinstance(setting, dict):
            raise ValueError("chapter setting must be an object")
        required_setting = {"chapter_id", "chapter_number", "chapter_title", "overview_policy", "overview_reason"}
        missing_setting = sorted(required_setting - setting.keys())
        if missing_setting:
            raise ValueError("missing chapter-setting fields: " + ", ".join(missing_setting))
        if set(setting) != required_setting:
            raise ValueError("chapter setting contains unsupported fields")
        chapter_id = str(setting.get("chapter_id", "")).strip()
        if not chapter_id or chapter_id not in chapter_map:
            raise ValueError("chapter setting references an unknown chapter")
        if chapter_id in seen_settings:
            raise ValueError("chapter settings must be unique per chapter")
        expected = chapter_map[chapter_id]
        if str(setting.get("chapter_number", "")).strip() != expected[0] or str(setting.get("chapter_title", "")).strip() != expected[1]:
            raise ValueError("chapter setting number or title is stale")
        if setting.get("overview_policy") not in {"required", "exempt"}:
            raise ValueError("chapter setting overview_policy must be required or exempt")
        reason = setting.get("overview_reason")
        if not isinstance(reason, str):
            raise ValueError("chapter setting overview_reason must be a string")
        if setting.get("overview_policy") == "exempt" and not reason.strip():
            raise ValueError("exempt chapter setting requires overview_reason")
        seen_settings.add(chapter_id)
    if seen_settings != set(chapter_map):
        raise ValueError("chapter_settings must contain exactly one item for every stage2 chapter")

    seen_actions: set[str] = set()
    for action in cleanup_actions:
        if not isinstance(action, dict):
            raise ValueError("cleanup action must be an object")
        action_fields = {"id", "action", "target", "reason"}
        if set(action) != action_fields:
            raise ValueError("cleanup action contains unsupported fields")
        for field in action_fields:
            value = action.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"cleanup action {field} must be a non-empty string")
        action_id = action["id"].strip()
        if action_id in seen_actions:
            raise ValueError("cleanup action IDs must be unique")
        seen_actions.add(action_id)

    seen_ids: set[str] = set()
    seen_figures: set[str] = set()
    orders_by_chapter: dict[str, set[int]] = {}
    text_fields = (
        "id", "figure_no", "chapter_id", "chapter_number", "chapter_title",
        "name", "type", "purpose", "composition", "orientation", "origin",
    )
    for image in images:
        if not isinstance(image, dict):
            raise ValueError("planned image must be an object")
        image_fields = {
            "id", "figure_no", "order", "chapter_id", "chapter_number", "chapter_title",
            "position", "name", "type", "purpose", "core_nodes", "composition",
            "orientation", "is_chapter_overview", "origin",
        }
        allowed_image_fields = image_fields | {"ai_prompt"}
        if set(image) != image_fields and set(image) != allowed_image_fields:
            raise ValueError("planned image contains unsupported fields")
        if "ai_prompt" in image and (not isinstance(image["ai_prompt"], str) or not image["ai_prompt"].strip()):
            raise ValueError("planned image ai_prompt must be a non-empty string")
        values: dict[str, str] = {}
        for field in text_fields:
            value = image.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"planned image {field} must be a non-empty string")
            values[field] = value.strip()
        if values["id"] in seen_ids:
            raise ValueError("planned image IDs must be unique")
        if values["figure_no"] in seen_figures:
            raise ValueError("planned figure numbers must be unique")
        order = image.get("order")
        if isinstance(order, bool) or not isinstance(order, int) or order < 1:
            raise ValueError("planned image order must be a positive integer")
        chapter_orders = orders_by_chapter.setdefault(values["chapter_id"], set())
        if order in chapter_orders:
            raise ValueError("planned image orders must be unique within each chapter")
        seen_ids.add(values["id"])
        seen_figures.add(values["figure_no"])
        chapter_orders.add(order)
        expected = chapter_map.get(values["chapter_id"])
        if not expected:
            raise ValueError(f"planned image references unknown chapter: {values['chapter_id']}")
        if values["chapter_number"] != expected[0] or values["chapter_title"] != expected[1]:
            raise ValueError("planned image chapter number or title is stale")
        if values["figure_no"] != f"图{values['chapter_number']}-{order}":
            raise ValueError("planned figure number must match its chapter number and order")
        if values["type"] not in IMAGE_TYPES:
            raise ValueError("planned image type is unsupported")
        if not isinstance(image.get("is_chapter_overview"), bool):
            raise ValueError("planned image is_chapter_overview must be a boolean")
        position = image.get("position")
        if not isinstance(position, dict):
            raise ValueError("planned image position must be an object")
        position_fields = {"outline_node_id", "outline_number", "outline_title", "placement_note"}
        if set(position) != position_fields:
            raise ValueError("planned image position contains unsupported fields")
        position_values: dict[str, str] = {}
        for field in position_fields:
            value = position.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"planned image position.{field} must be a non-empty string")
            position_values[field] = value.strip()
        outline = outline_map.get(position_values["outline_node_id"])
        if not outline:
            raise ValueError("planned image position references an unknown outline node")
        if position_values["outline_number"] != outline[0] or position_values["outline_title"] != outline[1]:
            raise ValueError("planned image outline number or title is stale")
        if outline[2] != values["chapter_id"]:
            raise ValueError("planned image position must belong to its chapter")
        core_nodes = image.get("core_nodes")
        if not isinstance(core_nodes, list) or not core_nodes:
            raise ValueError("planned image core_nodes must be a non-empty array")
        if any(not isinstance(node, str) or not node.strip() for node in core_nodes):
            raise ValueError("planned image core_nodes must contain non-empty strings")
    for chapter_id, orders in orders_by_chapter.items():
        if orders != set(range(1, len(orders) + 1)):
            raise ValueError(f"planned image orders must be continuous within chapter {chapter_id}")


def stage3_plan_warnings(data: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    overview_chapters = {
        str(image.get("chapter_id", "")).strip()
        for image in data.get("images", [])
        if isinstance(image, dict) and image.get("is_chapter_overview") is True
    }
    for setting in data.get("chapter_settings", []):
        if not isinstance(setting, dict) or setting.get("overview_policy") != "required":
            continue
        chapter_id = str(setting.get("chapter_id", "")).strip()
        if chapter_id not in overview_chapters:
            number = str(setting.get("chapter_number", "")).strip()
            title = str(setting.get("chapter_title", "")).strip()
            warnings.append(f"第{number}章 {title} 设为建议总览图，但当前图片清单中已无总览图")
    return warnings


def validate_stage3(source: dict[str, Any], stage2_receipt: dict[str, Any]) -> None:
    required = {
        "schema_version", "stage", "project_id", "stage2_confirmation_sha256",
        "visual_direction", "chapter_settings", "images", "cleanup_actions",
    }
    missing = sorted(required - source.keys())
    if missing:
        raise ValueError("missing stage3 fields: " + ", ".join(missing))
    if source.get("schema_version") != 1 or source.get("stage") != "stage3":
        raise ValueError("unsupported stage3 schema")
    if source.get("project_id") != stage2_receipt.get("project_id"):
        raise ValueError("stage3 project does not match stage2")
    if source.get("stage2_confirmation_sha256") != stage2_receipt.get("confirmation_sha256"):
        raise ValueError("stage3 recommendation is stale because stage2 changed")
    if source.get("generation_status") not in {None, "generating", "complete"}:
        raise ValueError("stage3 generation_status is invalid")
    validate_image_plan(source, stage2_receipt)


def stage3_recommendation_valid(data_dir: Path) -> tuple[dict[str, Any], dict[str, Any]] | None:
    source_path = data_dir / STAGE3_INPUT
    stage2_bound = stage2_confirmation_valid(data_dir)
    if not source_path.exists() or not stage2_bound:
        return None
    _, stage2_receipt = stage2_bound
    source = read_json(source_path)
    validate_stage3(source, stage2_receipt)
    return source, stage2_receipt


def stage3_confirmation_valid(data_dir: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | None:
    bound = stage3_recommendation_valid(data_dir)
    receipt_path = data_dir / STAGE3_RECEIPT
    if not bound or not receipt_path.exists():
        return None
    source, stage2_receipt = bound
    receipt = read_json(receipt_path)
    if receipt.get("schema_version") != 1 or receipt.get("stage") != "stage3" or receipt.get("status") != "confirmed":
        return None
    if receipt.get("project_id") != source.get("project_id"):
        return None
    if receipt.get("stage2_confirmation_sha256") != stage2_receipt.get("confirmation_sha256"):
        return None
    if receipt.get("source_sha256") != sha256_data(source) or not confirmation_digest_valid(receipt):
        return None
    data = receipt.get("data")
    if not isinstance(data, dict):
        return None
    validate_image_plan(data, stage2_receipt)
    return source, stage2_receipt, receipt


def confirmed_stage2_chapters(data_dir: Path) -> list[dict[str, Any]]:
    bound = stage2_confirmation_valid(data_dir)
    if not bound:
        raise ValueError("标书框架确认回执已失效")
    chapters = bound[1].get("data", {}).get("chapters")
    if not isinstance(chapters, list) or not chapters:
        raise ValueError("已确认的标书框架缺少一级章节")
    return chapters


def validate_delivery_plan(data: Any, chapters: list[dict[str, Any]], image_count: int) -> None:
    if not isinstance(data, dict):
        raise ValueError("stage4 data must be an object")
    required = {"word_batch_count", "word_batches", "image_plan_workbook", "skill_boundary", "additional_notes"}
    allowed = required | {"delivery_output_dir", "generation_rule"}
    if not required.issubset(data) or not set(data).issubset(allowed):
        raise ValueError("stage4 data fields are incomplete or unsupported")
    batch_count = data.get("word_batch_count")
    if isinstance(batch_count, bool) or not isinstance(batch_count, int) or not 1 <= batch_count <= 5:
        raise ValueError("Word生成批次必须是1至5之间的整数")
    batches = data.get("word_batches")
    if not isinstance(batches, list) or len(batches) != batch_count:
        raise ValueError("Word批次明细数量必须与选择的生成批次一致")
    chapter_map = {str(chapter.get("id", "")).strip(): chapter for chapter in chapters}
    expected_ids = [str(chapter.get("id", "")).strip() for chapter in chapters]
    if any(not chapter_id for chapter_id in expected_ids):
        raise ValueError("标书框架包含无效章节ID")
    seen_chapters: list[str] = []
    seen_files: set[str] = set()
    for index, batch in enumerate(batches, 1):
        if not isinstance(batch, dict):
            raise ValueError("Word批次明细必须是对象")
        fields = {"id", "order", "chapter_ids", "chapter_numbers", "chapter_titles", "planned_pages", "output_filename"}
        if set(batch) != fields:
            raise ValueError("Word批次明细包含缺失或不支持的字段")
        if batch.get("order") != index or str(batch.get("id", "")).strip() != f"word-batch-{index}":
            raise ValueError("Word批次顺序必须从1连续编号")
        ids = batch.get("chapter_ids")
        numbers = batch.get("chapter_numbers")
        titles = batch.get("chapter_titles")
        if not isinstance(ids, list) or not ids or not isinstance(numbers, list) or not isinstance(titles, list):
            raise ValueError("每个Word批次必须包含至少一个一级章节")
        if len(ids) != len(numbers) or len(ids) != len(titles):
            raise ValueError("Word批次的章节ID、编号和标题数量不一致")
        for position, chapter_id in enumerate(ids):
            chapter_id = str(chapter_id).strip()
            chapter = chapter_map.get(chapter_id)
            if not chapter:
                raise ValueError("Word批次引用了未知章节")
            if str(numbers[position]).strip() != str(chapter.get("number", "")).strip() or str(titles[position]).strip() != str(chapter.get("title", "")).strip():
                raise ValueError("Word批次章节编号或标题已过期")
            seen_chapters.append(chapter_id)
        planned_pages = batch.get("planned_pages")
        expected_pages = sum(int(chapter_map[str(chapter_id).strip()].get("pages", 0) or 0) for chapter_id in ids)
        if isinstance(planned_pages, bool) or not isinstance(planned_pages, int) or planned_pages != expected_pages:
            raise ValueError("Word批次预计页数与所含章节不一致")
        filename = str(batch.get("output_filename", "")).strip()
        if not filename.lower().endswith(".docx") or filename in seen_files:
            raise ValueError("Word批次文件名必须唯一且以.docx结尾")
        seen_files.add(filename)
    if seen_chapters != expected_ids:
        raise ValueError("Word批次必须按原顺序完整覆盖全部一级章节，且不得重复")

    workbook = data.get("image_plan_workbook")
    if not isinstance(workbook, dict):
        raise ValueError("图片规划Excel交付定义无效")
    workbook_fields = {"count", "format", "filename", "purpose", "worksheet_names", "columns", "image_count"}
    if set(workbook) != workbook_fields:
        raise ValueError("图片规划Excel交付字段不完整")
    if workbook.get("count") != 1 or workbook.get("format") != ".xlsx":
        raise ValueError("图片规划交付物必须固定为1个.xlsx文件")
    if not str(workbook.get("filename", "")).strip().lower().endswith(".xlsx"):
        raise ValueError("图片规划Excel文件名必须以.xlsx结尾")
    if workbook.get("image_count") != image_count:
        raise ValueError("图片规划Excel中的图片数量与已确认清单不一致")
    if not isinstance(workbook.get("worksheet_names"), list) or not workbook["worksheet_names"]:
        raise ValueError("图片规划Excel必须定义工作表")
    if not isinstance(workbook.get("columns"), list) or not workbook["columns"]:
        raise ValueError("图片规划Excel必须定义输出字段")
    if not isinstance(workbook.get("purpose"), str) or not workbook["purpose"].strip():
        raise ValueError("图片规划Excel必须说明用途")

    boundary = data.get("skill_boundary")
    expected_boundary = {
        "generate_word_documents": True,
        "generate_image_plan_excel": True,
        "generate_images": False,
        "insert_images": False,
    }
    if boundary != expected_boundary:
        raise ValueError("技能交付边界必须是Word加图片规划Excel，且不得生成或插入图片")
    if not isinstance(data.get("additional_notes"), str):
        raise ValueError("additional_notes must be a string")
    if "delivery_output_dir" in data and not isinstance(data.get("delivery_output_dir"), str):
        raise ValueError("交付物保存位置必须是文本")
    if "generation_rule" in data:
        rule_profiles.validate_selection(data["generation_rule"])


def validate_delivery_output_dir(data: dict[str, Any]) -> str:
    """Require an existing local folder at the point a user grants production."""
    raw = str(data.get("delivery_output_dir", "")).strip()
    if not raw:
        raise ValueError("请选择交付物保存位置后再授权开始")
    destination = Path(raw).expanduser()
    if not destination.is_absolute() or not destination.is_dir():
        raise ValueError("交付物保存位置必须是当前设备中已存在的文件夹")
    return str(destination.resolve())


def validate_stage4(source: dict[str, Any], stage3_receipt: dict[str, Any], chapters: list[dict[str, Any]]) -> None:
    required = {"schema_version", "stage", "project_id", "generated_at", "stage3_confirmation_sha256", "summary", "delivery"}
    allowed = required | {"generation_status"}
    if not required.issubset(source) or not set(source).issubset(allowed):
        raise ValueError("stage4 recommendation fields are incomplete or unsupported")
    if source.get("schema_version") != 1 or source.get("stage") != "stage4":
        raise ValueError("unsupported stage4 schema")
    if source.get("project_id") != stage3_receipt.get("project_id"):
        raise ValueError("stage4 project does not match stage3")
    if source.get("stage3_confirmation_sha256") != stage3_receipt.get("confirmation_sha256"):
        raise ValueError("stage4 recommendation is stale because stage3 changed")
    if source.get("generation_status") not in {None, "generating", "complete"}:
        raise ValueError("stage4 generation_status is invalid")
    summary = source.get("summary")
    if not isinstance(summary, dict):
        raise ValueError("stage4 summary must be an object")
    image_count = len(stage3_receipt.get("data", {}).get("images", []))
    if summary.get("image_count") != image_count or summary.get("chapter_count") != len(chapters):
        raise ValueError("stage4 summary counts are stale")
    expected_pages = sum(int(chapter.get("pages", 0) or 0) for chapter in chapters)
    if summary.get("planned_pages") != expected_pages:
        raise ValueError("stage4 summary planned pages are stale")
    for field in ("project_name", "client", "project_overview"):
        if not isinstance(summary.get(field), str):
            raise ValueError(f"stage4 summary {field} must be a string")
    validate_delivery_plan(source.get("delivery"), chapters, image_count)


def stage4_recommendation_valid(data_dir: Path) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]] | None:
    source_path = data_dir / STAGE4_INPUT
    stage3_bound = stage3_confirmation_valid(data_dir)
    if not source_path.exists() or not stage3_bound:
        return None
    _, _, stage3_receipt = stage3_bound
    chapters = confirmed_stage2_chapters(data_dir)
    source = read_json(source_path)
    validate_stage4(source, stage3_receipt, chapters)
    return source, stage3_receipt, chapters


def stage4_confirmation_valid(data_dir: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | None:
    bound = stage4_recommendation_valid(data_dir)
    receipt_path = data_dir / STAGE4_RECEIPT
    if not bound or not receipt_path.exists():
        return None
    source, stage3_receipt, chapters = bound
    receipt = read_json(receipt_path)
    if receipt.get("schema_version") != 1 or receipt.get("stage") != "stage4" or receipt.get("status") != "confirmed":
        return None
    if receipt.get("project_id") != source.get("project_id"):
        return None
    if receipt.get("stage3_confirmation_sha256") != stage3_receipt.get("confirmation_sha256"):
        return None
    if receipt.get("source_sha256") != sha256_data(source) or not confirmation_digest_valid(receipt):
        return None
    try:
        validate_delivery_plan(receipt.get("data"), chapters, len(stage3_receipt.get("data", {}).get("images", [])))
    except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError):
        return None
    return source, stage3_receipt, receipt


def callback_handoff(state: dict[str, Any]) -> tuple[str, str] | None:
    return {
        ("intake", "awaiting_analysis"): ("第一阶段项目口径", "stage1"),
        ("stage1", "awaiting_stage2"): ("第二阶段标书框架", "stage2"),
        ("stage2", "confirmed"): ("第三阶段图片规划", "stage3"),
        ("stage3", "confirmed"): ("第四阶段交付方案", "stage4"),
    }.get((state.get("active_stage"), state.get("mode")))


def handoff_wait_failed(active_wait: dict[str, Any] | None, target_stage: str | None) -> bool:
    if not active_wait or active_wait.get("stage") != target_stage:
        return False
    return bool(
        active_wait.get("status") in {"timed_out", "interrupted", "superseded"}
        or (active_wait.get("status") == "waiting" and not active_wait.get("process_alive"))
    )


def callback_status(data_dir: Path) -> dict[str, Any]:
    """Report an observable local handoff state for pages waiting on the agent."""
    state = workflow_state(data_dir)
    handoff = callback_handoff(state)
    waiting_for = handoff[0] if handoff else None
    target_stage = handoff[1] if handoff else None
    seconds = 0
    state_path = data_dir / WORKFLOW_STATE
    if waiting_for and state_path.exists():
        try:
            updated_at = datetime.fromisoformat(str(read_json(state_path).get("updated_at", "")))
            seconds = max(0, int((datetime.now().astimezone() - updated_at).total_seconds()))
        except (OSError, ValueError, TypeError):
            seconds = 0
    active_wait = None
    wait_path = data_dir / AGENT_WAIT
    if wait_path.exists():
        try:
            candidate = read_json(wait_path)
            candidate["process_alive"] = process_alive(int(candidate.get("pid", 0)))
            active_wait = candidate
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            active_wait = None
    wait_targets_handoff = bool(active_wait and active_wait.get("stage") == target_stage)
    handoff_failed = handoff_wait_failed(active_wait, target_stage)
    return {
        "ok": True,
        "workflow": state,
        "waiting_for": waiting_for,
        "target_stage": target_stage,
        "waiting_seconds": seconds,
        # Long generation is not a timeout. Only an explicit failure belonging
        # to the target stage is allowed to surface as a handoff error.
        "generation_delayed": bool(waiting_for and seconds >= 90 and not handoff_failed),
        "handoff_failed": handoff_failed,
        "timed_out": bool(
            wait_targets_handoff and active_wait.get("status") == "timed_out"
        ),
        "log_file": str(data_dir / CALLBACK_LOG),
        "agent_wait": active_wait,
    }


class BidConfirmHandler(SimpleHTTPRequestHandler):
    server: "BidConfirmServer"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[bid-confirm-ui] " + fmt % args + "\n")

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def send_json(self, data: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/api/health":
            self.send_json({"ok": True, "service": "biaoshu-master-confirm-ui", "project": str(self.server.project_dir), "pid": os.getpid(), "port": self.server.server_port})
            return
        if self.path == "/api/session":
            state = maybe_advance_to_stage1(self.server.data_dir)
            stage = state["active_stage"]
            ready_hash = None
            try:
                _, ready_hash = recommendation_ready(self.server.data_dir, stage)
            except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError):
                pass
            wait = None
            wait_path = self.server.data_dir / AGENT_WAIT
            if wait_path.exists():
                try:
                    wait = read_json(wait_path)
                except (OSError, ValueError, json.JSONDecodeError):
                    wait = None
            handoff_ready = bool(
                ready_hash
                and wait
                and wait.get("stage") == stage
                and wait.get("status") == "waiting"
                and wait.get("recommendation_sha256") == ready_hash
                and process_alive(int(wait.get("pid", 0)))
            )
            self.send_json({
                "ok": True,
                "stage": stage,
                "mode": state["mode"],
                "completed": state.get("completed", []),
                "project": str(self.server.project_dir),
                "recommendation_ready": bool(ready_hash),
                "recommendation_sha256": ready_hash,
                "handoff_ready": handoff_ready,
            })
            return
        if self.path == "/api/callback-status":
            self.send_json(callback_status(self.server.data_dir))
            return
        if self.path == "/api/delivery-status":
            self.send_json({"ok": True, **delivery_status(self.server.project_dir)})
            return
        if self.path == "/api/generation-rules":
            try:
                self.send_json({"ok": True, "generation_rules": rule_profiles.list_profiles()})
            except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.UNPROCESSABLE_ENTITY)
            return
        if self.path == "/api/intake":
            try:
                source_path = self.server.data_dir / INTAKE_INPUT
                if not source_path.exists():
                    raise ValueError("入口预填尚未完成；请先生成项目背景和资料路径")
                source = read_json(source_path)
                validate_intake_prefill_ready(source)
                receipt_path = self.server.data_dir / INTAKE_RECEIPT
                receipt = read_json(receipt_path) if receipt_path.exists() else None
                valid = bool(receipt and receipt.get("status") == "confirmed" and receipt.get("source_sha256") == sha256_data(source))
                if valid:
                    valid = intake_receipt_valid(self.server.data_dir) is not None
                self.send_json({
                    "ok": True,
                    "recommendation": source,
                    "source_sha256": sha256_data(source),
                    "receipt": receipt if valid else None,
                    "materials": intake_materials(source),
                    "receipt_materials": intake_materials(receipt) if valid else None,
                    "material_policy": source.get("material_policy", MATERIAL_POLICY),
                    "workflow": maybe_advance_to_stage1(self.server.data_dir),
                })
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.UNPROCESSABLE_ENTITY)
            return
        if self.path == "/api/stage3":
            try:
                bound = stage3_recommendation_valid(self.server.data_dir)
                if not bound:
                    raise ValueError("图片规划尚未生成，或标书框架确认回执已经失效")
                source, stage2_receipt = bound
                source_hash = sha256_data(source)
                confirmed = stage3_confirmation_valid(self.server.data_dir)
                receipt = confirmed[2] if confirmed else None
                if not receipt and stage_ai_adjust_status(self.server.data_dir, "stage3").get("status") != "waiting":
                    recommendation_ready(self.server.data_dir, "stage3")
                state = workflow_state(self.server.data_dir)
                draft_path = self.server.data_dir / STAGE3_DRAFT
                draft = read_json(draft_path) if draft_path.exists() and state.get("active_stage") == "stage3" and state.get("mode") == "editing" else None
                if draft and (
                    draft.get("source_sha256") != source_hash
                    or draft.get("stage2_confirmation_sha256") != stage2_receipt.get("confirmation_sha256")
                ):
                    draft = None
                self.send_json({
                    "ok": True,
                    "recommendation": source,
                    "source_sha256": source_hash,
                    "receipt": receipt,
                    "draft": draft,
                    "workflow": state,
                    "warnings": stage3_plan_warnings(receipt.get("data", source) if receipt else draft.get("data", source) if draft else source),
                })
            except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.UNPROCESSABLE_ENTITY)
            return
        if self.path == "/api/stage4":
            try:
                bound = stage4_recommendation_valid(self.server.data_dir)
                if not bound:
                    raise ValueError("最终交付方案尚未生成，或图片规划确认回执已经失效")
                source, stage3_receipt, chapters = bound
                source_hash = sha256_data(source)
                confirmed = stage4_confirmation_valid(self.server.data_dir)
                receipt = confirmed[2] if confirmed else None
                if not receipt:
                    recommendation_ready(self.server.data_dir, "stage4")
                state = workflow_state(self.server.data_dir)
                draft_path = self.server.data_dir / STAGE4_DRAFT
                draft = read_json(draft_path) if draft_path.exists() and state.get("active_stage") == "stage4" and state.get("mode") == "editing" else None
                if draft and (
                    draft.get("source_sha256") != source_hash
                    or draft.get("stage3_confirmation_sha256") != stage3_receipt.get("confirmation_sha256")
                ):
                    draft = None
                self.send_json({
                    "ok": True,
                    "recommendation": source,
                    "source_sha256": source_hash,
                    "receipt": receipt,
                    "draft": draft,
                    "workflow": state,
                    "chapters": chapters,
                })
            except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.UNPROCESSABLE_ENTITY)
            return
        if self.path == "/api/stage2":
            try:
                stage1_bound = stage1_confirmation_valid(self.server.data_dir)
                if not stage1_bound:
                    raise ValueError("项目口径确认回执已失效，请返回第一阶段重新确认")
                _, stage1_receipt = stage1_bound
                source = read_json(self.server.data_dir / STAGE2_INPUT)
                validate_stage2(source, stage1_receipt)
                source_hash = sha256_data(source)
                confirmed = stage2_confirmation_valid(self.server.data_dir)
                receipt = confirmed[1] if confirmed else None
                if not receipt and stage_ai_adjust_status(self.server.data_dir, "stage2").get("status") != "waiting" and stage2_rebalance_status(self.server.data_dir).get("status") != "waiting":
                    recommendation_ready(self.server.data_dir, "stage2")
                state = workflow_state(self.server.data_dir)
                draft_path = self.server.data_dir / STAGE2_DRAFT
                draft = read_json(draft_path) if draft_path.exists() and state.get("active_stage") == "stage2" and state.get("mode") == "editing" else None
                if draft and draft.get("source_sha256") != source_hash:
                    draft = None
                try:
                    stage3_available = bool(stage3_recommendation_valid(self.server.data_dir))
                except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError):
                    stage3_available = False
                self.send_json({"ok": True, "recommendation": source, "source_sha256": source_hash, "receipt": receipt, "draft": draft, "workflow": state, "stage3_available": stage3_available, "tender_position": tender_position_from_stage1_receipt(stage1_receipt)})
            except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.UNPROCESSABLE_ENTITY)
            return
        if self.path == "/api/stage2/rebalance-status":
            try:
                self.send_json({"ok": True, **stage2_rebalance_status(self.server.data_dir)})
            except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.UNPROCESSABLE_ENTITY)
            return
        if self.path == "/api/stage2/ai-adjust-status":
            try:
                self.send_json({"ok": True, **stage_ai_adjust_status(self.server.data_dir, "stage2")})
            except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.UNPROCESSABLE_ENTITY)
            return
        if self.path == "/api/stage3/ai-adjust-status":
            try:
                self.send_json({"ok": True, **stage_ai_adjust_status(self.server.data_dir, "stage3")})
            except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.UNPROCESSABLE_ENTITY)
            return
        if self.path == "/api/stage1":
            try:
                source = read_json(self.server.data_dir / STAGE1_INPUT)
                validate_stage1(source)
                validate_stage1_binding(self.server.data_dir, source)
                receipt_path = self.server.data_dir / STAGE1_RECEIPT
                receipt = read_json(receipt_path) if receipt_path.exists() else None
                valid = bool(receipt and receipt.get("source_sha256") == sha256_data(source))
                if not valid:
                    recommendation_ready(self.server.data_dir, "stage1")
                draft_path = self.server.data_dir / STAGE1_DRAFT
                draft = read_json(draft_path) if draft_path.exists() else None
                self.send_json({
                    "ok": True,
                    "recommendation": source,
                    "source_sha256": sha256_data(source),
                    "receipt": receipt if valid else None,
                    "draft": draft,
                    "workflow": maybe_advance_to_stage1(self.server.data_dir),
                    "stage2_available": is_recommendation_ready(self.server.data_dir, "stage2"),
                })
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.UNPROCESSABLE_ENTITY)
            return
        if self.path in ("/", "/index.html"):
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self) -> None:
        if self.path == "/api/page-presence":
            self.record_page_presence()
            return
        if self.path == "/api/intake/confirm":
            self.confirm_intake()
            return
        if self.path == "/api/stage1/confirm":
            self.confirm_stage1()
            return
        if self.path == "/api/stage2/confirm":
            self.confirm_stage2()
            return
        if self.path == "/api/stage2/rebalance":
            self.rebalance_stage2()
            return
        if self.path == "/api/stage2/ai-adjust":
            self.adjust_stage_ai("stage2")
            return
        if self.path == "/api/stage3/confirm":
            self.confirm_stage3()
            return
        if self.path == "/api/stage3/ai-adjust":
            self.adjust_stage_ai("stage3")
            return
        if self.path == "/api/stage4/confirm":
            self.confirm_stage4()
            return
        if self.path == "/api/generation-rules/default":
            self.set_generation_rule_default()
            return
        if self.path == "/api/stage1/reopen":
            self.reopen_stage1()
            return
        if self.path == "/api/stage2/reopen":
            self.reopen_stage2()
            return
        if self.path == "/api/stage3/reopen":
            self.reopen_stage3()
            return
        if self.path == "/api/stage4/reopen":
            self.reopen_stage4()
            return
        if self.path == "/api/materials/select":
            self.select_materials()
            return
        if self.path == "/api/shutdown":
            self.send_json({"ok": True})
            self.server.stop_requested = True
            return
        self.send_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)

    def record_page_presence(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 10_000:
                raise ValueError("确认台页面状态请求无效")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("确认台页面状态请求无效")
            status = record_page_presence(self.server.data_dir, payload.get("page"), payload.get("instance_id"))
            self.send_json({"ok": True, "presence": status})
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.UNPROCESSABLE_ENTITY)

    def confirm_stage1(self) -> None:
        try:
            state = workflow_state(self.server.data_dir)
            if state.get("active_stage") != "stage1" or state.get("mode") != "editing":
                raise ValueError("项目口径已经确认；如需修改，请先使用“修改本阶段”重新开启")
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 2_000_000:
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            source = read_json(self.server.data_dir / STAGE1_INPUT)
            validate_stage1(source)
            validate_stage1_binding(self.server.data_dir, source)
            source_hash = sha256_data(source)
            if payload.get("source_sha256") != source_hash:
                raise ValueError("项目口径已更新，请刷新页面后重新确认")
            editable = payload.get("data")
            if not isinstance(editable, dict):
                raise ValueError("data must be an object")
            project = editable.get("project")
            formatting = editable.get("formatting")
            if not isinstance(project, dict) or not str(project.get("project_name", "")).strip():
                raise ValueError("project name is required")
            if not isinstance(formatting, dict):
                raise ValueError("formatting is required")
            intake_bound = intake_receipt_valid(self.server.data_dir)
            tender_position = normalize_tender_position(
                intake_bound[1].get("tender_position") if intake_bound else source.get("tender_position")
            )
            try:
                target_pages = int(formatting.get("target_pages", 0))
            except (TypeError, ValueError):
                raise ValueError("target pages must be an integer")
            if target_pages < 1 or target_pages > 5000:
                raise ValueError("target pages must be between 1 and 5000")
            receipt = {
                "schema_version": 1,
                "stage": "stage1",
                "status": "confirmed",
                "project_id": source["project_id"],
                "run_id": source.get("run_id"),
                "tender_position": tender_position,
                "source_sha256": source_hash,
                "data": editable,
                "confirmed_at": utc_now(),
            }
            receipt["confirmation_sha256"] = sha256_data(receipt)
            archive_files(self.server.data_dir, [STAGE2_INPUT, STAGE2_RECEIPT, STAGE2_DRAFT, STAGE2_REBALANCE_REQUEST, STAGE2_AI_ADJUST_REQUEST, STAGE3_INPUT, STAGE3_RECEIPT, STAGE3_DRAFT, STAGE3_AI_ADJUST_REQUEST, STAGE4_INPUT, STAGE4_RECEIPT, STAGE4_DRAFT], "confirm-stage1")
            atomic_write_json(self.server.data_dir / STAGE1_RECEIPT, receipt)
            archive_files(self.server.data_dir, [STAGE1_DRAFT], "confirm-stage1-draft")
            completed = ["intake", "stage1"] if intake_receipt_valid(self.server.data_dir) else ["stage1"]
            write_workflow_state(self.server.data_dir, "stage1", "awaiting_stage2", completed)
            self.send_json({"ok": True, "receipt": receipt})
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.UNPROCESSABLE_ENTITY)

    def adjust_stage_ai(self, stage: str) -> None:
        try:
            if stage not in {"stage2", "stage3"}:
                raise ValueError("不支持的阶段整体调整")
            state = workflow_state(self.server.data_dir)
            if state.get("active_stage") != stage or state.get("mode") != "editing":
                label = "标书框架" if stage == "stage2" else "图片规划"
                raise ValueError(f"请先将{label}置于可编辑状态")
            if stage == "stage2" and stage2_rebalance_status(self.server.data_dir).get("status") == "waiting":
                raise ValueError("已有篇幅重新调整请求正在处理，请等待AI完成")
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 8_000_000:
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            instruction = payload.get("instruction")
            if not isinstance(instruction, str) or not instruction.strip():
                raise ValueError("请填写整体调整要求")
            instruction = instruction.strip()
            if len(instruction) > 20_000:
                raise ValueError("整体调整要求不能超过20000字")
            current_data = payload.get("data")
            if current_data is not None and not isinstance(current_data, dict):
                raise ValueError("data must be an object")
            if stage == "stage2":
                bound = stage1_confirmation_valid(self.server.data_dir)
                source_path = self.server.data_dir / STAGE2_INPUT
            else:
                bound = stage2_confirmation_valid(self.server.data_dir)
                source_path = self.server.data_dir / STAGE3_INPUT
            if not bound:
                raise ValueError("前序确认回执已失效，请先恢复前序阶段")
            source = read_json(source_path)
            if stage == "stage2":
                validate_stage2(source, bound[1])
                binding_key = "stage1_confirmation_sha256"
            else:
                validate_stage3(source, bound[1])
                binding_key = "stage2_confirmation_sha256"
            source_hash = sha256_data(source)
            if payload.get("source_sha256") != source_hash:
                raise ValueError("当前推荐内容已更新，请刷新页面后重新提交整体调整")
            request_path = ai_adjust_request_path(self.server.data_dir, stage)
            if request_path is None:
                raise ValueError("不支持的阶段整体调整")
            if request_path.exists():
                existing = read_json(request_path)
                if existing.get("status") == "pending":
                    existing_status = stage_ai_adjust_status(self.server.data_dir, stage)
                    if existing_status.get("status") == "waiting":
                        raise ValueError("已有整体调整请求正在处理，请等待AI完成")
                archive_files(self.server.data_dir, [request_path.name], f"replace-{stage}-ai-adjust")
            request = {
                "schema_version": 1,
                "stage": f"{stage}-ai-adjust",
                "status": "pending",
                "request_id": hashlib.sha256(f"{stage}:{source_hash}:{time.time_ns()}".encode("utf-8")).hexdigest()[:24],
                "project_id": source["project_id"],
                binding_key: bound[1]["confirmation_sha256"],
                "source_sha256": source_hash,
                "instruction": instruction,
                "current_data": current_data,
                "requested_at": utc_now(),
            }
            atomic_write_json(request_path, request)
            generating = dict(source)
            generating["generation_status"] = "generating"
            generating["ai_adjust_request_id"] = request["request_id"]
            if stage == "stage2":
                generating.pop("rebalance_request_id", None)
                archive_files(
                    self.server.data_dir,
                    [STAGE2_REBALANCE_REQUEST, STAGE3_INPUT, STAGE3_RECEIPT, STAGE3_DRAFT, STAGE3_AI_ADJUST_REQUEST, STAGE4_INPUT, STAGE4_RECEIPT, STAGE4_DRAFT],
                    "ai-adjust-stage2",
                )
            else:
                archive_files(self.server.data_dir, [STAGE4_INPUT, STAGE4_RECEIPT, STAGE4_DRAFT], "ai-adjust-stage3")
            atomic_write_json(source_path, generating)
            completed = ["intake", "stage1"] if stage == "stage2" and intake_receipt_valid(self.server.data_dir) else ["stage1"] if stage == "stage2" else ["intake", "stage1", "stage2"] if intake_receipt_valid(self.server.data_dir) else ["stage1", "stage2"]
            write_workflow_state(self.server.data_dir, stage, "editing", completed)
            self.send_json({"ok": True, "request": request})
        except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError) as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.UNPROCESSABLE_ENTITY)

    def rebalance_stage2(self) -> None:
        try:
            state = workflow_state(self.server.data_dir)
            if state.get("active_stage") != "stage2" or state.get("mode") != "editing":
                raise ValueError("请先将标书框架置于可编辑状态")
            if stage_ai_adjust_status(self.server.data_dir, "stage2").get("status") == "waiting":
                raise ValueError("已有整体调整请求正在处理，请等待AI完成")
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 200_000:
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            stage1_bound = stage1_confirmation_valid(self.server.data_dir)
            if not stage1_bound:
                raise ValueError("项目口径确认回执已失效，请返回第一阶段重新确认")
            _, stage1_receipt = stage1_bound
            source = read_json(self.server.data_dir / STAGE2_INPUT)
            validate_stage2(source, stage1_receipt)
            source_hash = sha256_data(source)
            if payload.get("source_sha256") != source_hash:
                raise ValueError("标书框架已更新，请刷新页面后重新调整")
            requested = payload.get("chapter_pages")
            if not isinstance(requested, list) or len(requested) != len(source["chapters"]):
                raise ValueError("章节页数必须完整覆盖全部一级章节")
            old_map = {str(chapter.get("id", "")).strip(): chapter for chapter in source["chapters"]}
            seen: set[str] = set()
            chapter_pages: list[dict[str, Any]] = []
            total = 0
            for item in requested:
                if not isinstance(item, dict):
                    raise ValueError("章节页数项必须是对象")
                chapter_id = str(item.get("id", "")).strip()
                if not chapter_id or chapter_id in seen or chapter_id not in old_map:
                    raise ValueError("章节页数包含重复或未知章节")
                raw_pages = item.get("pages")
                if isinstance(raw_pages, bool) or not isinstance(raw_pages, int) or raw_pages < 1:
                    raise ValueError("每个一级章节至少保留1页")
                chapter = old_map[chapter_id]
                chapter_pages.append({"id": chapter_id, "number": chapter.get("number"), "title": chapter.get("title"), "pages": raw_pages})
                seen.add(chapter_id)
                total += raw_pages
            if seen != set(old_map):
                raise ValueError("章节页数必须完整覆盖全部一级章节")
            old_total = sum(int(chapter.get("pages", 0) or 0) for chapter in source["chapters"])
            if total != old_total:
                raise ValueError("重新调整只能重新分配页数，章节总页数必须保持不变")
            request = {
                "schema_version": 1,
                "stage": "stage2-rebalance",
                "status": "pending",
                "request_id": hashlib.sha256(f"{source_hash}:{time.time_ns()}".encode("utf-8")).hexdigest()[:24],
                "project_id": source["project_id"],
                "stage1_confirmation_sha256": stage1_receipt["confirmation_sha256"],
                "source_sha256": source_hash,
                "target_pages": int(source["target_pages"]),
                "total_pages": total,
                "chapter_pages": chapter_pages,
                "requested_at": utc_now(),
            }
            atomic_write_json(self.server.data_dir / STAGE2_REBALANCE_REQUEST, request)
            generating = dict(source)
            generating["generation_status"] = "generating"
            generating["rebalance_request_id"] = request["request_id"]
            atomic_write_json(self.server.data_dir / STAGE2_INPUT, generating)
            archive_files(self.server.data_dir, [STAGE3_INPUT, STAGE3_RECEIPT, STAGE3_DRAFT, STAGE4_INPUT, STAGE4_RECEIPT, STAGE4_DRAFT], "rebalance-stage2")
            write_workflow_state(self.server.data_dir, "stage2", "editing", ["intake", "stage1"] if intake_receipt_valid(self.server.data_dir) else ["stage1"])
            self.send_json({"ok": True, "request": request})
        except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError) as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.UNPROCESSABLE_ENTITY)

    def confirm_intake(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 2_000_000:
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            source_path = self.server.data_dir / INTAKE_INPUT
            if not source_path.exists():
                raise ValueError("入口预填尚未完成；请先生成项目背景和资料路径")
            source = read_json(source_path)
            validate_intake_prefill_ready(source)
            source_hash = sha256_data(source)
            if payload.get("source_sha256") != source_hash:
                raise ValueError("入口信息已更新，请刷新页面后重新提交")
            background = str(payload.get("background", "")).strip()
            tender_position = normalize_tender_position(payload.get("tender_position"))
            background_value = payload.get("background_paths")
            if background_value is None:
                background_value = payload.get("source_paths")
            materials = {
                "background_paths": normalized_paths(background_value),
                "reference_paths": normalized_paths(payload.get("reference_paths")),
            }
            validate_material_separation(materials)
            all_paths = materials["background_paths"] + materials["reference_paths"]
            relative_paths = [path for path in all_paths if not Path(path).is_absolute()]
            if relative_paths:
                raise ValueError("资料路径必须是本地绝对路径：" + relative_paths[0])
            missing_paths = [path for path in all_paths if not Path(path).exists()]
            if missing_paths:
                raise ValueError("本地资料路径不存在：" + missing_paths[0])
            if not background:
                raise ValueError("请填写项目背景后再开始分析")
            receipt_path = self.server.data_dir / INTAKE_RECEIPT
            existing_receipt = read_json(receipt_path) if receipt_path.exists() else None
            if existing_receipt:
                valid_existing = existing_receipt.get("status") == "confirmed" and existing_receipt.get("source_sha256") == source_hash
                if (valid_existing and existing_receipt.get("background") == background
                        and intake_materials(existing_receipt) == materials
                        and normalize_tender_position(existing_receipt.get("tender_position")) == tender_position):
                    self.send_json({"ok": True, "action": "analysis_required", "receipt": existing_receipt, "idempotent": True})
                    return
                self.send_json({"ok": False, "error": "背景与资料已经提交。如需更换，请重新开始整个项目流程。"}, HTTPStatus.CONFLICT)
                return
            state = workflow_state(self.server.data_dir)
            if state.get("active_stage") != "intake" or state.get("mode") != "editing":
                self.send_json({"ok": False, "error": "当前项目已经进入正式流程，不能再次修改背景与资料。"}, HTTPStatus.CONFLICT)
                return
            receipt = {
                "schema_version": 2,
                "stage": "intake",
                "status": "confirmed",
                "project_id": source["project_id"],
                "run_id": source.get("run_id"),
                "source_sha256": source_hash,
                "background": background,
                "background_paths": materials["background_paths"],
                "reference_paths": materials["reference_paths"],
                "material_policy": MATERIAL_POLICY,
                "tender_position": tender_position,
                "confirmed_at": utc_now(),
            }
            receipt["confirmation_sha256"] = sha256_data(receipt)
            archive_files(self.server.data_dir, [STAGE1_INPUT, STAGE1_RECEIPT, STAGE2_INPUT, STAGE2_RECEIPT, STAGE3_INPUT, STAGE3_RECEIPT, STAGE4_INPUT, STAGE4_RECEIPT, STAGE1_DRAFT, STAGE2_DRAFT, STAGE3_DRAFT, STAGE4_DRAFT, STAGE2_REBALANCE_REQUEST, STAGE2_AI_ADJUST_REQUEST, STAGE3_AI_ADJUST_REQUEST], "new-intake")
            atomic_write_json(self.server.data_dir / INTAKE_RECEIPT, receipt)
            write_workflow_state(self.server.data_dir, "intake", "awaiting_analysis", ["intake"])
            self.send_json({"ok": True, "action": "analysis_required", "receipt": receipt})
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.UNPROCESSABLE_ENTITY)

    def confirm_stage2(self) -> None:
        try:
            state = workflow_state(self.server.data_dir)
            if state.get("active_stage") != "stage2" or state.get("mode") != "editing":
                raise ValueError("标书框架已经确认；如需修改，请先使用“修改本阶段”重新开启")
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 8_000_000:
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            stage1_bound = stage1_confirmation_valid(self.server.data_dir)
            if not stage1_bound:
                raise ValueError("项目口径确认回执已失效，请返回第一阶段重新确认")
            _, stage1_receipt = stage1_bound
            source = read_json(self.server.data_dir / STAGE2_INPUT)
            validate_stage2(source, stage1_receipt)
            source_hash = sha256_data(source)
            if payload.get("source_sha256") != source_hash:
                raise ValueError("标书框架已更新，请刷新页面后重新确认")
            data = payload.get("data")
            if not isinstance(data, dict):
                raise ValueError("data must be an object")
            target = int(source["target_pages"])
            planned = validate_outline(
                data.get("chapters"),
                target,
                data.get("coverage"),
                tender_position_from_stage1_receipt(stage1_receipt),
            )
            if not isinstance(data.get("coverage"), dict):
                data["coverage"] = {}
            data["planned_pages"] = planned
            receipt = {
                "schema_version": 1,
                "stage": "stage2",
                "status": "confirmed",
                "project_id": source["project_id"],
                "stage1_confirmation_sha256": stage1_receipt["confirmation_sha256"],
                "source_sha256": source_hash,
                "data": data,
                "authority": {
                    "latest": True,
                    "overrides_stage1_generated_outline": True,
                    "scope": ["chapters", "coverage", "planned_pages"],
                },
                "confirmed_at": utc_now(),
            }
            receipt["confirmation_sha256"] = sha256_data(receipt)
            archive_files(self.server.data_dir, [STAGE3_INPUT, STAGE3_RECEIPT, STAGE3_DRAFT, STAGE3_AI_ADJUST_REQUEST, STAGE4_INPUT, STAGE4_RECEIPT, STAGE4_DRAFT, STAGE2_REBALANCE_REQUEST, STAGE2_AI_ADJUST_REQUEST], "confirm-stage2")
            atomic_write_json(self.server.data_dir / STAGE2_RECEIPT, receipt)
            archive_files(self.server.data_dir, [STAGE2_DRAFT], "confirm-stage2-draft")
            completed = ["intake", "stage1", "stage2"] if intake_receipt_valid(self.server.data_dir) else ["stage1", "stage2"]
            write_workflow_state(self.server.data_dir, "stage2", "confirmed", completed)
            self.send_json({"ok": True, "receipt": receipt})
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.UNPROCESSABLE_ENTITY)

    def confirm_stage3(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 8_000_000:
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            bound = stage3_recommendation_valid(self.server.data_dir)
            if not bound:
                raise ValueError("图片规划尚未生成，或标书框架确认回执已经失效")
            source, stage2_receipt = bound
            source_hash = sha256_data(source)
            if payload.get("source_sha256") != source_hash:
                raise ValueError("图片规划已更新，请刷新页面后重新确认")
            data = payload.get("data")
            if not isinstance(data, dict):
                raise ValueError("data must be an object")
            state = workflow_state(self.server.data_dir)
            existing_path = self.server.data_dir / STAGE3_RECEIPT
            if state.get("active_stage") != "stage3" or state.get("mode") != "editing":
                if existing_path.exists():
                    existing = read_json(existing_path)
                    if (existing.get("source_sha256") == source_hash and existing.get("data") == data
                            and confirmation_digest_valid(existing)):
                        self.send_json({"ok": True, "receipt": existing,
                                        "warnings": stage3_plan_warnings(existing["data"]),
                                        "already_confirmed": True})
                        return
                raise ValueError("图片规划已经确认；如需修改，请先使用“修改本阶段”重新开启")
            validate_image_plan(data, stage2_receipt)
            receipt = {
                "schema_version": 1,
                "stage": "stage3",
                "status": "confirmed",
                "project_id": source["project_id"],
                "stage2_confirmation_sha256": stage2_receipt["confirmation_sha256"],
                "source_sha256": source_hash,
                "data": {
                    "visual_direction": data["visual_direction"],
                    "chapter_settings": data["chapter_settings"],
                    "images": data["images"],
                    "cleanup_actions": data["cleanup_actions"],
                },
                "authority": {"latest": True, "overrides_stage3_recommendation": True},
                "confirmed_at": utc_now(),
            }
            receipt["confirmation_sha256"] = sha256_data(receipt)
            atomic_write_json(self.server.data_dir / STAGE3_RECEIPT, receipt)
            archive_files(self.server.data_dir, [STAGE3_DRAFT, STAGE3_AI_ADJUST_REQUEST], "confirm-stage3-draft")
            completed = ["intake", "stage1", "stage2", "stage3"] if intake_receipt_valid(self.server.data_dir) else ["stage1", "stage2", "stage3"]
            write_workflow_state(self.server.data_dir, "stage3", "confirmed", completed)
            self.send_json({"ok": True, "receipt": receipt, "warnings": stage3_plan_warnings(receipt["data"])})
        except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError) as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.UNPROCESSABLE_ENTITY)

    def confirm_stage4(self) -> None:
        try:
            state = workflow_state(self.server.data_dir)
            if state.get("active_stage") != "stage4" or state.get("mode") != "editing":
                raise ValueError("最终交付方案已经确认；如需修改，请先使用“修改本阶段”重新开启")
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 2_000_000:
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            bound = stage4_recommendation_valid(self.server.data_dir)
            if not bound:
                raise ValueError("最终交付方案尚未生成，或图片规划确认回执已经失效")
            source, stage3_receipt, chapters = bound
            source_hash = sha256_data(source)
            if payload.get("source_sha256") != source_hash:
                raise ValueError("最终交付建议已经变化，请刷新后重新确认")
            data = payload.get("data")
            image_count = len(stage3_receipt.get("data", {}).get("images", []))
            if "generation_rule" not in data:
                data["generation_rule"] = rule_profiles.default_selection()
            validate_delivery_plan(data, chapters, image_count)
            rule_profiles.validate_selection(data["generation_rule"])
            data["delivery_output_dir"] = validate_delivery_output_dir(data)
            receipt = {
                "schema_version": 1,
                "stage": "stage4",
                "status": "confirmed",
                "project_id": source["project_id"],
                "stage3_confirmation_sha256": stage3_receipt["confirmation_sha256"],
                "source_sha256": source_hash,
                "data": data,
                "confirmed_at": utc_now(),
            }
            receipt["confirmation_sha256"] = sha256_data(receipt)
            atomic_write_json(self.server.data_dir / STAGE4_RECEIPT, receipt)
            archive_files(self.server.data_dir, [STAGE4_DRAFT], "confirm-stage4-draft")
            completed = ["intake", "stage1", "stage2", "stage3", "stage4"] if intake_receipt_valid(self.server.data_dir) else ["stage1", "stage2", "stage3", "stage4"]
            write_workflow_state(self.server.data_dir, "stage4", "confirmed", completed)
            self.send_json({"ok": True, "receipt": receipt})
        except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError) as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.UNPROCESSABLE_ENTITY)

    def reopen_stage1(self) -> None:
        try:
            receipt = read_json(self.server.data_dir / STAGE1_RECEIPT)
            atomic_write_json(self.server.data_dir / STAGE1_DRAFT, {
                "data": receipt["data"],
                "based_on_confirmation_sha256": receipt["confirmation_sha256"],
                "reason": "user_reopened_stage1",
                "saved_at": utc_now(),
            })
            archived_delivery = archive_delivery_workspace(self.server.project_dir, "reopen-stage1")
            archived = archive_files(self.server.data_dir, [STAGE1_RECEIPT, STAGE2_INPUT, STAGE2_RECEIPT, STAGE2_DRAFT, STAGE2_REBALANCE_REQUEST, STAGE2_AI_ADJUST_REQUEST, STAGE3_INPUT, STAGE3_RECEIPT, STAGE3_DRAFT, STAGE3_AI_ADJUST_REQUEST, STAGE4_INPUT, STAGE4_RECEIPT, STAGE4_DRAFT], "reopen-stage1")
            completed = ["intake"] if intake_receipt_valid(self.server.data_dir) else []
            write_workflow_state(self.server.data_dir, "stage1", "editing", completed)
            self.send_json({"ok": True, "stage": "stage1", "mode": "editing", "archived": archived, "archived_delivery": archived_delivery})
        except (OSError, ValueError, json.JSONDecodeError, KeyError) as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.UNPROCESSABLE_ENTITY)

    def reopen_stage2(self) -> None:
        try:
            state = workflow_state(self.server.data_dir)
            if state.get("active_stage") == "stage2" and state.get("mode") == "editing":
                raise ValueError("只有已确认的标书框架可以重新开启修改")
            bound = stage2_confirmation_valid(self.server.data_dir)
            if not bound:
                raise ValueError("标书框架确认回执已失效，请刷新后重试")
            source, receipt = bound
            source_hash = sha256_data(source)
            atomic_write_json(self.server.data_dir / STAGE2_DRAFT, {
                "data": receipt["data"],
                "source_sha256": source_hash,
                "based_on_confirmation_sha256": receipt["confirmation_sha256"],
                "reason": "user_reopened_stage2",
                "saved_at": utc_now(),
            })
            archived_delivery = archive_delivery_workspace(self.server.project_dir, "reopen-stage2")
            archived = archive_files(self.server.data_dir, [STAGE2_RECEIPT, STAGE3_INPUT, STAGE3_RECEIPT, STAGE3_DRAFT, STAGE3_AI_ADJUST_REQUEST, STAGE4_INPUT, STAGE4_RECEIPT, STAGE4_DRAFT, STAGE2_REBALANCE_REQUEST, STAGE2_AI_ADJUST_REQUEST], "reopen-stage2")
            completed = ["intake", "stage1"] if intake_receipt_valid(self.server.data_dir) else ["stage1"]
            write_workflow_state(self.server.data_dir, "stage2", "editing", completed)
            self.send_json({"ok": True, "stage": "stage2", "mode": "editing", "archived": archived, "archived_delivery": archived_delivery})
        except (OSError, ValueError, json.JSONDecodeError, KeyError) as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.UNPROCESSABLE_ENTITY)

    def reopen_stage3(self) -> None:
        try:
            state = workflow_state(self.server.data_dir)
            if state.get("active_stage") == "stage3" and state.get("mode") == "editing":
                raise ValueError("图片规划已处于可编辑状态")
            confirmed = stage3_confirmation_valid(self.server.data_dir)
            if not confirmed:
                raise ValueError("图片规划确认回执已失效，请刷新后重试")
            source, stage2_receipt, receipt = confirmed
            source_hash = sha256_data(source)
            atomic_write_json(self.server.data_dir / STAGE3_DRAFT, {
                "data": receipt["data"],
                "source_sha256": source_hash,
                "stage2_confirmation_sha256": stage2_receipt["confirmation_sha256"],
                "based_on_confirmation_sha256": receipt["confirmation_sha256"],
                "reason": "user_reopened_stage3",
                "saved_at": utc_now(),
            })
            archived_delivery = archive_delivery_workspace(self.server.project_dir, "reopen-stage3")
            archived = archive_files(self.server.data_dir, [STAGE3_RECEIPT, STAGE4_INPUT, STAGE4_RECEIPT, STAGE4_DRAFT, STAGE3_AI_ADJUST_REQUEST], "reopen-stage3")
            completed = ["intake", "stage1", "stage2"] if intake_receipt_valid(self.server.data_dir) else ["stage1", "stage2"]
            write_workflow_state(self.server.data_dir, "stage3", "editing", completed)
            self.send_json({"ok": True, "stage": "stage3", "mode": "editing", "archived": archived, "archived_delivery": archived_delivery})
        except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError) as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.UNPROCESSABLE_ENTITY)

    def reopen_stage4(self) -> None:
        try:
            state = workflow_state(self.server.data_dir)
            if state.get("active_stage") == "stage4" and state.get("mode") == "editing":
                raise ValueError("最终交付方案已处于可编辑状态")
            confirmed = stage4_confirmation_valid(self.server.data_dir)
            if not confirmed:
                raise ValueError("最终交付确认回执已失效，请刷新后重试")
            source, stage3_receipt, receipt = confirmed
            atomic_write_json(self.server.data_dir / STAGE4_DRAFT, {
                "data": receipt["data"],
                "source_sha256": sha256_data(source),
                "stage3_confirmation_sha256": stage3_receipt["confirmation_sha256"],
                "based_on_confirmation_sha256": receipt["confirmation_sha256"],
                "reason": "user_reopened_stage4",
                "saved_at": utc_now(),
            })
            archived_delivery = archive_delivery_workspace(self.server.project_dir, "reopen-stage4")
            archived = archive_files(self.server.data_dir, [STAGE4_RECEIPT], "reopen-stage4")
            completed = ["intake", "stage1", "stage2", "stage3"] if intake_receipt_valid(self.server.data_dir) else ["stage1", "stage2", "stage3"]
            write_workflow_state(self.server.data_dir, "stage4", "editing", completed)
            self.send_json({"ok": True, "stage": "stage4", "mode": "editing", "archived": archived, "archived_delivery": archived_delivery})
        except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError) as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.UNPROCESSABLE_ENTITY)

    def select_materials(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            self.send_json({"ok": True, "paths": choose_local_paths(str(payload.get("kind", "files")))})
        except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.UNPROCESSABLE_ENTITY)

    def set_generation_rule_default(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 10_000:
                raise ValueError("规则默认设置请求无效")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("规则默认设置请求必须是对象")
            profile = rule_profiles.set_default(payload.get("id"))
            self.send_json({"ok": True, "default_profile": profile})
        except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError) as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.UNPROCESSABLE_ENTITY)


class BidConfirmServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, project_dir: Path, port: int):
        self.project_dir = project_dir
        self.data_dir = project_dir / DATA_DIR_NAME
        self.stop_requested = False
        super().__init__((HOST, port), lambda *args, **kwargs: BidConfirmHandler(*args, directory=str(STATIC_DIR), **kwargs))


def health(port: int, timeout: float = 1.0) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/api/health", timeout=timeout) as response:
            return json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return None


def open_local_browser(url: str) -> tuple[bool, str]:
    """Open the local confirmation page through the native desktop mechanism."""
    system = platform.system()
    try:
        if system == "Windows":
            os.startfile(url)  # type: ignore[attr-defined]
            return True, "windows-startfile"
        if system == "Darwin":
            result = subprocess.run(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False)
            if result.returncode == 0:
                return True, "macos-open"
            return False, f"macos-open-exit-{result.returncode}"
    except (OSError, subprocess.SubprocessError) as exc:
        native_error = str(exc)
    else:
        native_error = ""
    try:
        opened = bool(webbrowser.open(url, new=0, autoraise=True))
        return opened, "webbrowser" if opened else "webbrowser-returned-false"
    except OSError as exc:
        detail = native_error or str(exc)
        return False, f"browser-error:{detail}"


def mark_page_open_attempt(data_dir: Path, url: str, opened: bool, method: str) -> None:
    path = data_dir / PAGE_PRESENCE
    try:
        state = read_json(path) if path.exists() else {}
    except (OSError, ValueError, json.JSONDecodeError):
        state = {}
    state.update({"last_open_attempt_at": utc_now(), "last_open_url": url, "last_opened": opened, "last_open_method": method})
    atomic_write_json(path, state)


def ensure_confirmation_page(project_dir: Path, stage: str) -> dict[str, Any]:
    """Best-effort restore of a stale local confirmation page before waiting.

    A live heartbeat is the only cross-platform proof available to a local
    server. The cooldown avoids opening repeated tabs while Chrome restores or
    throttles a background page.
    """
    data_dir = project_dir / DATA_DIR_NAME
    status = page_presence_status(data_dir)
    if status["page_open"]:
        return {"action": "already_open", **status}
    attempt_age = status["last_open_attempt_age_seconds"]
    if attempt_age is not None and attempt_age < PAGE_REOPEN_COOLDOWN_SECONDS:
        return {"action": "cooldown", **status}
    lock = load_lock(project_dir)
    if not lock:
        return {"action": "service_unavailable", **status}
    port = int(lock["port"])
    healthy = health(port)
    if not healthy or healthy.get("service") != "biaoshu-master-confirm-ui":
        return {"action": "service_unavailable", **status}
    url = f"http://{HOST}:{port}/"
    opened, method = open_local_browser(url)
    mark_page_open_attempt(data_dir, url, opened, method)
    log_callback_event(data_dir, "confirmation_page_restore_attempt", stage, f"opened={opened}; method={method}")
    print(f"{url} [page_restore={'opened' if opened else 'failed'} method={method}]", flush=True)
    return {"action": "opened" if opened else "open_failed", **page_presence_status(data_dir)}


def run_server(project_dir: Path, port: int) -> int:
    data_dir = project_dir / DATA_DIR_NAME
    data_dir.mkdir(parents=True, exist_ok=True)
    intake_path = data_dir / INTAKE_INPUT
    stage1_path = data_dir / STAGE1_INPUT
    if not intake_path.exists() and not stage1_path.exists():
        raise ValueError("入口预填尚未完成；请先运行 prepare_intake.py，再启动确认台")
    if intake_path.exists():
        validate_intake_prefill_ready(read_json(intake_path))
    if stage1_path.exists():
        stage1 = read_json(stage1_path)
        validate_stage1(stage1)
        validate_stage1_binding(data_dir, stage1)
    server = BidConfirmServer(project_dir, port)
    server.timeout = 5
    register_process(project_dir, os.getpid())
    atomic_write_json(project_dir / LOCK_NAME, {"pid": os.getpid(), "port": port, "project": str(project_dir), "started_at": utc_now()})
    last_process_cleanup = time.monotonic()
    try:
        print(f"http://{HOST}:{port}", flush=True)
        while not server.stop_requested:
            server.handle_request()
            if time.monotonic() - last_process_cleanup >= PROCESS_CLEANUP_INTERVAL_SECONDS:
                cleanup_stale_processes(project_dir)
                last_process_cleanup = time.monotonic()
    finally:
        server.server_close()
        unregister_process(project_dir, os.getpid())
        lock_path = project_dir / LOCK_NAME
        try:
            current_lock = read_json(lock_path)
            if int(current_lock.get("pid", 0)) == os.getpid():
                lock_path.unlink(missing_ok=True)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
    return 0


def launch_daemon(project_dir: Path, requested_port: int | None, no_browser: bool) -> int:
    project_dir = project_dir.expanduser().resolve()
    cleanup_stale_processes(project_dir)
    existing = load_lock(project_dir)
    if existing:
        port = int(existing["port"])
        url = f"http://{HOST}:{port}"
        # A live service already opened its page on first launch.  Reattaching
        # after a retry or workflow recovery must not create another tab.
        print(url)
        return 0
    port = requested_port if requested_port is not None else find_port(DEFAULT_PORT)
    # Keep browser ownership in this launcher so the result can be reported to
    # the calling AI. The detached child only serves HTTP and never opens a tab.
    # The legacy --no-browser flag is intentionally ignored for --daemon: a
    # confirmation workflow must always try to open its local confirmation page.
    command = [sys.executable, str(Path(__file__).resolve()), str(project_dir), "--serve", "--port", str(port), "--no-browser"]
    subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.time() + 10
    while time.time() < deadline:
        result = health(port)
        if result and result.get("service") == "biaoshu-master-confirm-ui" and result.get("project") == str(project_dir):
            url = f"http://{HOST}:{port}"
            browser_opened, browser_method = open_local_browser(url)
            mark_page_open_attempt(project_dir / DATA_DIR_NAME, url, browser_opened, browser_method)
            if not browser_opened:
                print(f"自动打开浏览器失败（{browser_method}）；确认台服务已启动，请使用输出地址恢复。", file=sys.stderr)
            print(f"{url} [browser_opened={'true' if browser_opened else 'false'} method={browser_method}]")
            return 0
        time.sleep(0.15)
    print("Confirmation service failed to start", file=sys.stderr)
    return 3


def wait_prerequisite_valid(data_dir: Path, stage: str) -> bool:
    """Return whether the confirmation immediately before ``stage`` still binds.

    A user may reopen an earlier stage while an agent is blocked waiting for a
    later receipt.  Once that upstream receipt is archived, the old wait must
    end instead of timing out against a receipt that can no longer exist.
    """
    if stage == "stage2":
        return bool(stage1_confirmation_valid(data_dir))
    if stage == "stage3":
        return bool(stage2_confirmation_valid(data_dir))
    if stage == "stage4":
        return bool(stage3_confirmation_valid(data_dir))
    return True


STAGE_WAIT_RULES = {
    "intake": "入口回执后读取资料并生成完整项目口径；不得跳过阶段1。",
    "stage1": "按已确认项目口径生成完整框架；不得自行改变用户确认值。",
    "stage2": "按已确认项目口径生成完整标书框架；收到整体调整请求时先处理请求，不得开始图片规划或正文生产。",
    "stage3": "按已确认标书框架生成完整图片规划；收到整体调整请求时先处理请求，不得开始正文生产。",
    "stage4": "仅在最终授权后初始化生产审校台；逐批确认并保持前台等待。",
}


def stage_runtime_guard(project_dir: Path, stage: str) -> str:
    """Validate a ready stage and print the minimal non-negotiable runtime rule."""
    data_dir = project_dir / DATA_DIR_NAME
    if stage not in STAGE_WAIT_RULES:
        raise ValueError(f"unsupported confirmation stage: {stage}")
    if not wait_prerequisite_valid(data_dir, stage):
        raise ValueError("前序确认无效，不能进入当前阶段等待")
    _source, ready_hash = recommendation_ready(data_dir, stage)
    wait_command = (
        f"python3 scripts/bid_confirm_ui/server.py <project_dir> "
        f"--wait-only --wait-stage {stage} --wait-timeout 0"
    )
    print(
        f"[stage-guard:{stage}] {STAGE_WAIT_RULES[stage]} "
        f"必须以前台阻塞方式执行：{wait_command}；"
        "禁止工具参数 run_in_background=true、background=true、异步/分离任务或结束对话。",
        flush=True,
    )
    return ready_hash


def wait_for_stage(project_dir: Path, stage: str, timeout: int) -> int:
    data_dir = project_dir / DATA_DIR_NAME
    paths = {
        "intake": (data_dir / INTAKE_INPUT, data_dir / INTAKE_RECEIPT),
        "stage1": (data_dir / STAGE1_INPUT, data_dir / STAGE1_RECEIPT),
        "stage2": (data_dir / STAGE2_INPUT, data_dir / STAGE2_RECEIPT),
        "stage3": (data_dir / STAGE3_INPUT, data_dir / STAGE3_RECEIPT),
        "stage4": (data_dir / STAGE4_INPUT, data_dir / STAGE4_RECEIPT),
    }
    if stage not in paths:
        raise ValueError(f"unsupported confirmation stage: {stage}")
    source_path, receipt_path = paths[stage]
    if not wait_prerequisite_valid(data_dir, stage):
        current = workflow_state(data_dir)
        details = (
            "检测到前序阶段已重新开启，已撤销旧等待；"
            f"请从{current.get('active_stage', '当前阶段')}"
            f"（{current.get('mode', 'unknown')}）恢复"
        )
        set_agent_wait(data_dir, stage, "superseded", details)
        print(details, file=sys.stderr)
        return 5
    ready_hash = stage_runtime_guard(project_dir, stage)
    ensure_confirmation_page(project_dir, stage)
    set_agent_wait(
        data_dir,
        stage,
        "waiting",
        "推荐内容已完整生成，当前调用技能的AI正在等待本阶段用户确认回执",
        ready_hash,
    )
    try:
        deadline = None if timeout == 0 else time.time() + timeout
        while deadline is None or time.time() < deadline:
            try:
                if not wait_prerequisite_valid(data_dir, stage):
                    current = workflow_state(data_dir)
                    details = (
                        "检测到前序阶段已重新开启，已撤销旧等待；"
                        f"请从{current.get('active_stage', '当前阶段')}"
                        f"（{current.get('mode', 'unknown')}）恢复"
                    )
                    set_agent_wait(data_dir, stage, "superseded", details)
                    print(details, file=sys.stderr)
                    return 5
                if stage == "stage2":
                    rebalance = pending_stage2_rebalance(data_dir, ready_hash)
                    if rebalance:
                        details = (
                            "检测到阶段2章节页数重新调整请求；请读取 "
                            f"{STAGE2_REBALANCE_REQUEST}，重生成 stage2-recommendations.json，"
                            "然后重新执行阶段2前台等待。"
                        )
                        set_agent_wait(data_dir, stage, "rebalance_requested", details, ready_hash)
                        print(details, file=sys.stderr)
                        print(str(data_dir / STAGE2_REBALANCE_REQUEST))
                        return 6
                if stage in {"stage2", "stage3"}:
                    adjustment = pending_stage_ai_adjust(data_dir, stage, ready_hash)
                    if adjustment:
                        label = "阶段2标书框架" if stage == "stage2" else "阶段3图片规划"
                        details = (
                            f"检测到{label}整体调整请求；请读取 "
                            f"{AI_ADJUST_REQUESTS[stage]}，结合其中的current_data和instruction重生成当前阶段推荐，"
                            "完成后更新请求状态并重新执行当前阶段前台等待。"
                        )
                        set_agent_wait(data_dir, stage, "ai_adjust_requested", details, ready_hash)
                        print(details, file=sys.stderr)
                        print(str(data_dir / AI_ADJUST_REQUESTS[stage]))
                        return 7
                _, current_hash = recommendation_ready(data_dir, stage)
                if current_hash != ready_hash:
                    details = "等待期间推荐内容发生变化，旧等待已撤销；请完成新版本后重新进入等待"
                    set_agent_wait(data_dir, stage, "superseded", details, current_hash)
                    print(details, file=sys.stderr)
                    return 5
                if stage == "stage2":
                    valid = bool(stage2_confirmation_valid(data_dir))
                elif stage == "stage3":
                    valid = bool(stage3_confirmation_valid(data_dir))
                elif stage == "stage4":
                    valid = bool(stage4_confirmation_valid(data_dir))
                else:
                    source = read_json(source_path)
                    receipt = read_json(receipt_path) if receipt_path.exists() else {}
                    valid = receipt.get("status") == "confirmed" and receipt.get("source_sha256") == sha256_data(source)
                    if stage == "stage1":
                        validate_stage1_binding(data_dir, source)
                if valid:
                    set_agent_wait(data_dir, stage, "received", "检测到有效用户确认回执，当前调用技能的AI可继续下一步", ready_hash)
                    print(str(receipt_path))
                    return 0
            except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError):
                pass
            time.sleep(0.5)
        set_agent_wait(data_dir, stage, "timed_out", f"等待{timeout}秒未收到有效用户确认回执", ready_hash)
        print(f"Timed out waiting for {stage} confirmation; the page remains available", file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        set_agent_wait(data_dir, stage, "interrupted", "等待被执行宿主中断")
        raise


def shutdown(project_dir: Path) -> int:
    lock = load_lock(project_dir)
    if not lock:
        (project_dir / LOCK_NAME).unlink(missing_ok=True)
        return 0
    port = int(lock["port"])
    request = urllib.request.Request(f"http://{HOST}:{port}/api/shutdown", data=b"{}", method="POST", headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(request, timeout=2).read()
    except (OSError, urllib.error.URLError):
        try:
            os.kill(int(lock["pid"]), signal.SIGTERM)
        except OSError:
            pass
    for _ in range(30):
        if not process_alive(int(lock["pid"])):
            break
        time.sleep(0.1)
    (project_dir / LOCK_NAME).unlink(missing_ok=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir", type=Path)
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--wait", action="store_true", help="启动确认台后等待本阶段用户确认回执")
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--wait-only", action="store_true")
    parser.add_argument("--wait-stage", choices=["intake", "stage1", "stage2", "stage3", "stage4"], default="stage1")
    parser.add_argument("--wait-timeout", type=int, default=0, help="Seconds to wait; 0 waits indefinitely (default)")
    parser.add_argument("--shutdown", action="store_true")
    parser.add_argument("--port", type=int)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    project_dir = args.project_dir.expanduser().resolve()
    project_dir.mkdir(parents=True, exist_ok=True)
    if args.shutdown:
        return shutdown(project_dir)
    if args.wait_only:
        return wait_for_stage(project_dir, args.wait_stage, args.wait_timeout)
    if args.daemon:
        status = launch_daemon(project_dir, args.port, args.no_browser)
        return wait_for_stage(project_dir, args.wait_stage, args.wait_timeout) if status == 0 and args.wait else status
    port = args.port if args.port is not None else find_port(DEFAULT_PORT)
    if not args.no_browser:
        webbrowser.open(f"http://{HOST}:{port}")
    return run_server(project_dir, port)


if __name__ == "__main__":
    raise SystemExit(main())
