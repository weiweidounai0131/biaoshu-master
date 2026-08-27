#!/usr/bin/env python3
"""Persist a page count actually observed in WPS for one exported batch."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from . import protocol
except ImportError:
    import protocol


def main() -> int:
    parser = argparse.ArgumentParser(description="登记WPS实际页数")
    parser.add_argument("project_dir", type=Path)
    parser.add_argument("--batch", required=True, dest="batch_id")
    parser.add_argument("--actual-pages", required=True, type=int)
    parser.add_argument("--verifier", required=True)
    args = parser.parse_args()
    validation = protocol.record_wps_page_check(
        args.project_dir.expanduser().resolve(), args.batch_id, args.actual_pages, args.verifier,
    )
    print(json.dumps({"batch_id": args.batch_id, "page_verification": validation["page_verification"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
