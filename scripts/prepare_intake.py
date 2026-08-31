#!/usr/bin/env python3
"""Seed the one-time background/reference material confirmation gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import uuid
from datetime import datetime
from pathlib import Path

import project_workspace


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def unique_paths(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        path = str(Path(value).expanduser().resolve())
        if path not in result:
            result.append(path)
    return result


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temp, path)


def archive_previous_run(data_dir: Path) -> str | None:
    """Move prior workflow state aside before starting a fresh intake run."""
    names = [
        "intake-recommendations.json", "intake-confirmation.json", "workflow-state.json",
        "agent-wait.json", "callback-events.jsonl", "page-presence.json",
        "stage1-recommendations.json", "stage1-confirmation.json", "stage1-edit-draft.json",
        "stage2-recommendations.json", "stage2-confirmation.json", "stage2-edit-draft.json", "stage2-rebalance-request.json",
        "stage3-recommendations.json", "stage3-confirmation.json", "stage3-edit-draft.json",
        "stage4-recommendations.json", "stage4-confirmation.json", "stage4-edit-draft.json",
    ]
    existing = [data_dir / name for name in names if (data_dir / name).exists()]
    if not existing:
        return None
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    target = data_dir / "history" / f"{stamp}-new-intake"
    target.mkdir(parents=True, exist_ok=True)
    for source in existing:
        os.replace(source, target / source.name)
    return str(target)


def archive_previous_delivery(project_dir: Path) -> str | None:
    """Move the previous delivery workspace aside before a fresh intake.

    A new intake reuses the project directory but must not leave an old
    manifest available to the new confirmation chain. Keeping the old tree in
    a sibling history directory preserves its audit trail and exported files.
    """

    source = project_dir / "bid_delivery"
    if not source.exists():
        return None
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    target = project_dir / "bid_delivery-history" / f"{stamp}-new-intake"
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, target)
    return str(target)


def existing_intake(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_dir", type=Path)
    parser.add_argument("--background", default="", help="Background extracted from the invoking conversation")
    parser.add_argument("--background-file", type=Path, help="UTF-8 text file containing a longer background")
    parser.add_argument("--background-path", action="append", default=[], help="Authoritative tender/project material path; repeat as needed")
    parser.add_argument("--reference-path", action="append", default=[], help="Optional reference material path; repeat as needed")
    parser.add_argument(
        "--path", action="append", default=[],
        help="Deprecated compatibility alias for --background-path",
    )
    parser.add_argument("--project-id")
    parser.add_argument("--tender-position", choices=("main", "companion"), default="main", help="Initial selection shown in the intake dialog")
    parser.add_argument("--resume", action="store_true", help="保留当前项目运行状态，不创建新的入口运行")
    args = parser.parse_args()

    project_dir = args.project_dir.expanduser().resolve()
    data_dir = project_dir / "bid_confirm_ui"
    target = data_dir / "intake-recommendations.json"
    if args.resume:
        if any((args.background.strip(), args.background_file, args.background_path, args.reference_path, args.path, args.project_id)) or args.tender_position != "main":
            parser.error("--resume 不能同时提交新的项目背景、资料路径、项目ID或标书定位")
        current = existing_intake(target)
        if not current or not str(current.get("project_id", "")).strip() or not str(current.get("run_id", "")).strip():
            parser.error("当前项目没有可恢复的入口运行；请去掉 --resume 创建新一轮")
        print(target)
        print("resumed_existing=true")
        print(f"project_id={current['project_id']}")
        print(f"run_id={current['run_id']}")
        return 0

    archived = archive_previous_run(data_dir)
    archived_delivery = archive_previous_delivery(project_dir)
    background = args.background
    if args.background_file:
        background = args.background_file.expanduser().read_text(encoding="utf-8")
    background_paths = unique_paths(args.background_path + args.path)
    reference_paths = unique_paths(args.reference_path)
    overlap = sorted(set(background_paths) & set(reference_paths))
    if overlap:
        parser.error(f"同一路径不能同时归入背景资料和参考资料: {overlap[0]}")
    all_paths = background_paths + reference_paths
    missing = [path for path in all_paths if not Path(path).exists()]
    if missing:
        parser.error(f"local path does not exist: {missing[0]}")

    managed_project_id = project_workspace.project_id_for(project_dir)
    if args.project_id and managed_project_id and args.project_id != managed_project_id:
        parser.error("--project-id 与当前项目工作区身份不一致")
    project_id = args.project_id or managed_project_id or hashlib.sha256(str(project_dir).encode("utf-8")).hexdigest()[:20]
    run_id = uuid.uuid4().hex
    write_json(target, {
        "schema_version": 2,
        "stage": "intake",
        "project_id": project_id,
        "run_id": run_id,
        "generated_at": now(),
        "prefill_ready": True,
        "background": background.strip(),
        "background_paths": background_paths,
        "reference_paths": reference_paths,
        "material_policy": {
            "background": "需求书、招标文件、评分表、澄清文件和客户正式资料是本项目写作的权威底层依据。",
            "reference": "历史项目、成熟策略和公司经验仅作为可选参考；采用前必须核对与本项目背景的一致性，不得直接当作项目事实。",
        },
        "tender_position": args.tender_position,
    })
    print(target)
    if archived:
        print(f"archived_previous_run={archived}")
    if archived_delivery:
        print(f"archived_previous_delivery={archived_delivery}")
    project_workspace.record_run(project_dir, run_id, project_id)
    print(f"project_id={project_id}")
    print(f"run_id={run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
