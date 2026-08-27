#!/usr/bin/env python3
"""Host-neutral bridge for applying one saved AI review request.

The browser never calls a model. A compatible AI host first claims a pending
request with ``--begin``, performs its own reasoning/writing, then stores the
replacement paragraph in a local UTF-8 text file and runs ``--apply-text-file``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    from . import protocol, export_word, export_image_plan
except ImportError:
    import protocol
    import export_word
    import export_image_plan


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir", type=Path)
    parser.add_argument("--request", required=True, help="例如 request-0001")
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--begin", action="store_true", help="领取请求并输出其绑定信息")
    actions.add_argument("--apply-text-file", type=Path, help="写入AI处理后的完整段落")
    actions.add_argument("--apply-json-file", type=Path, help="写入AI处理后的图片规划字段对象")
    args = parser.parse_args()
    project_dir = args.project_dir.expanduser().resolve()
    if args.begin:
        result = protocol.begin_ai_request(project_dir, args.request)
        print(json.dumps({"request": result["request"], "status": result["manifest"]["status"]}, ensure_ascii=False, indent=2))
        return 0
    if args.apply_json_file:
        json_path = args.apply_json_file.expanduser().resolve()
        if not json_path.is_file(): parser.error("--apply-json-file必须指向现有JSON文件")
        replacement = json.loads(json_path.read_text(encoding="utf-8"))
        result = protocol.apply_image_plan_ai_request_result(project_dir, args.request, replacement)
        export_image_plan.export_image_plan(project_dir)
    else:
        text_path = args.apply_text_file.expanduser().resolve()
        if not text_path.is_file(): parser.error("--apply-text-file必须指向现有的UTF-8文本文件")
        text = text_path.read_text(encoding="utf-8").strip()
        result = protocol.apply_ai_request_result(project_dir, args.request, text)
        export_word.export_word(project_dir, result["request"]["batch_id"])
    print(json.dumps({"request_id": args.request, "status": result["manifest"]["status"], "source_sha256": result["source_sha256"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
