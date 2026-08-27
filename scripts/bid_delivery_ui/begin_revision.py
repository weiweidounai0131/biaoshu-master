#!/usr/bin/env python3
"""Move one delivery batch from a persisted revision event into AI editing."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from . import protocol
except ImportError:
    import protocol


def main() -> int:
    parser = argparse.ArgumentParser(description="开始一批Word的审校版回修")
    parser.add_argument("project_dir", type=Path)
    parser.add_argument("--batch", required=True, dest="batch_id")
    args = parser.parse_args()
    manifest = protocol.begin_revision(args.project_dir.expanduser().resolve(), args.batch_id)
    print(json.dumps({
        "batch_id": args.batch_id,
        "status": manifest["status"],
        "output_filename": next(item["output_filename"] for item in manifest["word_batches"] if item["id"] == args.batch_id),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
