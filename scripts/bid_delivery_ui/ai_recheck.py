#!/usr/bin/env python3
"""Record one completed AI semantic recheck for a Word delivery batch.

The script deliberately does not call a model.  The current AI reads the
project-local rules, source and exported Word, writes the report JSON, and
uses this command to pass the result through the durable delivery gate.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    from . import protocol
except ImportError:  # Direct CLI execution.
    import protocol


def main() -> int:
    parser = argparse.ArgumentParser(description="登记一批Word的AI复校结果")
    parser.add_argument("project_dir", type=Path)
    parser.add_argument("--batch", required=True, dest="batch_id")
    parser.add_argument("--report", required=True, type=Path, help="当前AI写入的完整复校报告JSON")
    args = parser.parse_args()

    try:
        project_dir = args.project_dir.expanduser().resolve()
        report = protocol.read_json(args.report.expanduser().resolve())
        result = protocol.record_ai_recheck(project_dir, args.batch_id, report)
        batch = next(item for item in result["manifest"]["word_batches"] if item["id"] == args.batch_id)
        print(json.dumps({
            "status": result["manifest"]["status"],
            "batch_status": batch["status"],
            "report_path": result["report_path"],
            "report_status": result["report"]["status"],
            "summary": result["report"]["summary"],
        }, ensure_ascii=False))
        return 0
    except (OSError, ValueError, StopIteration, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
