#!/usr/bin/env python3
"""Tests for the explicit post-delivery local image-generation state machine."""

from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = SCRIPT_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from bid_delivery_ui import export_image_plan, export_word, image_generation, protocol


class ImageGenerationTest(unittest.TestCase):
    def test_post_delivery_state_machine_requires_final_confirmation_and_is_idempotent(self) -> None:
        try:
            fixture_module = importlib.import_module("test_protocol")
        except ModuleNotFoundError:
            fixture_module = importlib.import_module("scripts.bid_delivery_ui.test_protocol")
        fixture = fixture_module.DeliveryProtocolTest("test_initialization_is_authorized_idempotent_and_empty")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        project_dir = fixture.project_dir

        protocol.initialize_delivery(project_dir)
        export_image_plan.export_image_plan(project_dir)
        protocol.confirm_image_plan(project_dir)
        for batch_id in ("word-batch-1", "word-batch-2"):
            protocol.begin_active_batch(project_dir)
            fixture._write_artifacts(batch_id, "image-generation")
            export_word.export_word(project_dir, batch_id)
            protocol.confirm_batch(project_dir, batch_id)
        protocol.record_wps_page_check(project_dir, "word-batch-1", 10, "测试WPS")
        protocol.record_wps_page_check(project_dir, "word-batch-2", 10, "测试WPS")
        protocol.confirm_final_delivery(project_dir)

        started = image_generation.start_example(project_dir)
        self.assertEqual(started["status"], "example_pending")
        self.assertTrue(Path(started["request_path"]).is_file())
        self.assertTrue(started["image"]["ai_prompt"])

        repeated = image_generation.start_example(project_dir)
        self.assertEqual(repeated["status"], "already_started")

        example_path = project_dir / "example.png"
        example_path.write_bytes(b"example-image")
        ready = image_generation.record_example(project_dir, str(example_path))
        self.assertEqual(ready["status"], "example_ready")
        revised = image_generation.revise_example(project_dir, "改为更清晰的浅色信息图，保留原有内容结构")
        self.assertEqual(revised["status"], "example_pending")
        revised_path = project_dir / "example-revised.png"
        revised_path.write_bytes(b"revised-image")
        image_generation.record_example(project_dir, str(revised_path))
        image_generation.confirm_example(project_dir)
        complete = image_generation.set_batch_count(project_dir, 5)
        self.assertEqual(complete["status"], "complete")
        self.assertEqual(complete["effective_batch_count"], 0)
        self.assertEqual(image_generation._split_batches([{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}], 3), [[{"id": 1}, {"id": 2}], [{"id": 3}], [{"id": 4}]])


if __name__ == "__main__":
    unittest.main()
