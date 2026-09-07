#!/usr/bin/env python3
"""Verify the confirmation-stage continuation hint stays consistent."""

from pathlib import Path
import unittest


STATIC_DIR = Path(__file__).resolve().parent / "static"
CONTINUATION_HINT = "AI处理中，若对话已结束请回复“接续”让对话继续"


class TransitionCopyTests(unittest.TestCase):
    def test_all_stage_transition_modals_use_the_same_hint(self) -> None:
        for filename in ("index.html", "framework.html", "images.html", "final.html"):
            content = (STATIC_DIR / filename).read_text(encoding="utf-8")
            self.assertIn(CONTINUATION_HINT, content, filename)

    def test_all_wait_pollers_use_the_shared_hint(self) -> None:
        shared = (STATIC_DIR / "workflow-shared.js").read_text(encoding="utf-8")
        self.assertIn(CONTINUATION_HINT, shared)
        for filename in ("app.js", "framework.js", "images.js", "final.js"):
            content = (STATIC_DIR / filename).read_text(encoding="utf-8")
            self.assertIn("window.BiaoshuWorkflow.continuationHint", content, filename)


if __name__ == "__main__":
    unittest.main()
