#!/usr/bin/env python3
"""Create an authoritative-bound AI recheck report template.

The template keeps project, batch, artifact and rule hashes sourced from the
local delivery manifest. The current AI only fills semantic review fields;
this command never marks a batch as passed and never calls a model.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

try:
    from . import protocol
except ImportError:  # Direct CLI execution.
    import protocol


def build_template(project_dir: Path, batch_id: str) -> dict[str, Any]:
    project_dir = project_dir.expanduser().resolve()
    manifest = protocol.load_manifest(project_dir)
    batch = next((item for item in manifest["word_batches"] if item["id"] == batch_id), None)
    if batch is None:
        raise ValueError("Word批次不存在")
    if batch["status"] != "ai_rechecking":
        raise ValueError("只有处于AI复校中的Word批次可以生成复校模板")

    protocol._require_recorded_artifacts(project_dir, batch)
    source = protocol._require_recorded_source(project_dir, batch, manifest)
    coverage = protocol._ai_recheck_coverage(source)
    return {
        "schema_version": protocol.SCHEMA_VERSION,
        "kind": "bid_delivery_ai_recheck",
        "status": "running",
        "project_id": manifest["project_id"],
        "batch_id": batch["id"],
        "batch_order": batch["order"],
        "stage4_confirmation_sha256": manifest["stage4_confirmation_sha256"],
        "source_sha256": batch["source_sha256"],
        "export_sha256": batch["export_sha256"],
        "writing_rules_sha256": manifest["writing_rules"]["project_sha256"],
        "checked_at": protocol.utc_now(),
        "checker": "请填写当前AI或模型名称",
        "rules_read": False,
        "scope": [],
        "summary": {
            "checked_blocks": coverage["checked_blocks"],
            "checked_paragraphs": coverage["checked_paragraphs"],
            "checked_sections": coverage["checked_sections"],
            "blocking_findings": 0,
            "warning_findings": 0,
            "info_findings": 0,
        },
        "findings": [],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="生成绑定当前项目摘要的AI复校报告模板")
    parser.add_argument("project_dir", type=Path)
    parser.add_argument("--batch", required=True, dest="batch_id")
    parser.add_argument("--output", type=Path, help="模板输出路径；默认写入项目results目录")
    parser.add_argument("--overwrite", action="store_true", help="允许覆盖已有模板文件")
    args = parser.parse_args()

    try:
        project_dir = args.project_dir.expanduser().resolve()
        manifest = protocol.load_manifest(project_dir)
        batch = next((item for item in manifest["word_batches"] if item["id"] == args.batch_id), None)
        if batch is None:
            raise ValueError("Word批次不存在")
        output = (args.output.expanduser().resolve() if args.output else protocol.delivery_dir(project_dir) / protocol.RESULTS_DIR_NAME / f"ai-recheck-template-batch-{batch['order']:02d}.json")
        if output.exists() and not args.overwrite:
            raise ValueError(f"模板文件已存在，如需覆盖请使用 --overwrite：{output}")
        protocol.atomic_write_json(output, build_template(project_dir, args.batch_id))
        print(json.dumps({"output": str(output), "batch_id": args.batch_id, "status": "template_only"}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, StopIteration, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
