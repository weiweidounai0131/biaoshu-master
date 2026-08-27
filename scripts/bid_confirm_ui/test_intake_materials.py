#!/usr/bin/env python3
"""Small self-check for the separated intake material paths."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

import server


ROOT = Path(__file__).resolve().parents[1]
PREPARE = ROOT / "prepare_intake.py"
BUILD_STAGE1 = ROOT / "build_stage1_recommendations.py"


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        project = Path(directory)
        background = project / "需求书.docx"
        reference = project / "历史项目.docx"
        background.write_text("background", encoding="utf-8")
        reference.write_text("reference", encoding="utf-8")

        subprocess.run([
            sys.executable, str(PREPARE), str(project),
            "--background", "测试项目",
            "--background-path", str(background),
            "--reference-path", str(reference),
        ], check=True, capture_output=True, text=True)

        intake = json.loads((project / "bid_confirm_ui" / "intake-recommendations.json").read_text(encoding="utf-8"))
        assert intake["schema_version"] == 2
        assert intake["background_paths"] == [str(background.resolve())]
        assert intake["reference_paths"] == [str(reference.resolve())]
        server.validate_intake(intake)

        httpd = server.BidConfirmServer(project, 0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            payload = json.dumps({
                "source_sha256": server.sha256_data(intake),
                "background": "测试项目",
                "background_paths": intake["background_paths"],
                "reference_paths": intake["reference_paths"],
                "tender_position": "main",
            }, ensure_ascii=False).encode("utf-8")
            request = urllib.request.Request(
                f"http://{server.HOST}:{httpd.server_port}/api/intake/confirm",
                data=payload,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=3) as response:
                receipt = json.load(response)["receipt"]
            assert receipt["schema_version"] == 2
            assert receipt["background_paths"] == intake["background_paths"]
            assert receipt["reference_paths"] == intake["reference_paths"]
            subprocess.run([sys.executable, str(BUILD_STAGE1), str(project)], check=True, capture_output=True, text=True)
            stage1 = json.loads((project / "bid_confirm_ui" / "stage1-recommendations.json").read_text(encoding="utf-8"))
            assert stage1["materials"]["background_paths"] == intake["background_paths"]
            assert stage1["materials"]["reference_paths"] == intake["reference_paths"]
            assert stage1["source_summary"]["background_file_count"] == 1
            assert stage1["source_summary"]["reference_file_count"] == 1
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)

        overlap = subprocess.run([
            sys.executable, str(PREPARE), str(project),
            "--background-path", str(background),
            "--reference-path", str(background),
        ], capture_output=True, text=True)
        assert overlap.returncode != 0


if __name__ == "__main__":
    main()
