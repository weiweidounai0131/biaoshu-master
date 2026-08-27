#!/usr/bin/env python3
"""HTTP and service-lifecycle tests for the delivery skeleton."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = SCRIPT_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from bid_delivery_ui import export_image_plan, export_word, protocol, server
from bid_delivery_ui import test_protocol as protocol_fixtures


class DeliveryServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = protocol_fixtures.DeliveryProtocolTest(methodName="runTest")
        self.fixture.setUp()
        self.project_dir = self.fixture.project_dir
        protocol.initialize_delivery(self.project_dir)
        self.httpd = server.BidDeliveryServer(self.project_dir, 0)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://{server.HOST}:{self.httpd.server_port}"

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        self.fixture.tearDown()

    def request(self, path: str, method: str = "GET", payload: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(payload if payload is not None else {}, ensure_ascii=False).encode("utf-8") if method == "POST" else None
        request = urllib.request.Request(self.base_url + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.load(error)
            finally:
                error.close()

    def test_status_surface_is_local_and_read_only(self) -> None:
        status, health = self.request("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["service"], "biaoshu-master-delivery-ui")
        self.assertEqual(health["project"], str(self.project_dir))
        status, session = self.request("/api/session")
        self.assertEqual(status, 200)
        self.assertEqual(session["status"], "preparing")
        self.assertEqual(session["word_batch_count"], 2)
        status, manifest = self.request("/api/manifest")
        self.assertEqual(status, 200)
        self.assertEqual(manifest["manifest"]["project_id"], "delivery-protocol-test")
        status, overview = self.request("/api/overview")
        self.assertEqual(status, 200)
        self.assertTrue(overview["overview"]["read_only"])
        status, unavailable = self.request("/api/batches/word-batch-1/reader?offset=0&limit=2")
        self.assertEqual(status, 422)
        self.assertIn("尚未生成", unavailable["error"])
        status, rejected = self.request("/api/not-yet-implemented", "POST")
        self.assertEqual(status, 404)
        self.assertIn("不支持", rejected["error"])

    def test_workflow_link_detects_stage4_reopen(self) -> None:
        status, active = self.request("/api/workflow-link")
        self.assertEqual(status, 200)
        self.assertTrue(active["delivery_active"])
        data_dir = self.project_dir / protocol.confirm_ui.DATA_DIR_NAME
        receipt = protocol.confirm_ui.read_json(data_dir / protocol.confirm_ui.STAGE4_RECEIPT)
        protocol.confirm_ui.atomic_write_json(data_dir / protocol.confirm_ui.STAGE4_DRAFT, {
            "data": receipt["data"],
            "source_sha256": receipt["source_sha256"],
            "stage3_confirmation_sha256": receipt["stage3_confirmation_sha256"],
            "based_on_confirmation_sha256": receipt["confirmation_sha256"],
            "reason": "test_reopen",
            "saved_at": protocol.confirm_ui.utc_now(),
        })
        (data_dir / protocol.confirm_ui.STAGE4_RECEIPT).unlink()
        status, inactive = self.request("/api/workflow-link")
        self.assertEqual(status, 200)
        self.assertFalse(inactive["delivery_active"])

    def test_reader_returns_registered_source_in_pages(self) -> None:
        protocol.begin_active_batch(self.project_dir)
        self.fixture._write_artifacts("word-batch-1", "http-reader")
        protocol.register_batch_artifacts(self.project_dir, "word-batch-1")
        status, overview = self.request("/api/overview")
        self.assertEqual(status, 200)
        self.assertTrue(overview["overview"]["batches"][0]["readable"])
        status, page = self.request("/api/batches/word-batch-1/reader?offset=0&limit=2")
        self.assertEqual(status, 200)
        self.assertTrue(page["reader"]["read_only"])
        self.assertEqual(len(page["reader"]["blocks"]), 2)
        self.assertEqual(page["reader"]["paging"]["next_offset"], 2)

    def test_web_review_actions_persist_without_calling_ai(self) -> None:
        protocol.begin_active_batch(self.project_dir)
        self.fixture._write_artifacts("word-batch-1", "web-review")
        protocol.register_batch_artifacts(self.project_dir, "word-batch-1")
        source_hash = protocol.load_manifest(self.project_dir)["word_batches"][0]["source_sha256"]
        status, edited = self.request("/api/batches/word-batch-1/direct-edits", "POST", {"block_id": "block-1", "source_sha256": source_hash, "replacement_text": "网页直接修改后的正文。"})
        self.assertEqual(status, 200)
        self.assertEqual(edited["direct_edit"]["manifest"]["status"], "export_pending")
        self.assertEqual(edited["export"]["manifest"]["status"], "awaiting_batch_review")
        status, created = self.request("/api/batches/word-batch-1/ai-requests", "POST", {"block_id": "block-1", "source_sha256": edited["direct_edit"]["source_sha256"], "instruction": "需要AI进一步修改。"})
        self.assertEqual(status, 200)
        self.assertEqual(created["ai_request"]["request"]["status"], "pending")

    def test_final_delivery_requires_wps_page_check(self) -> None:
        protocol.begin_active_batch(self.project_dir)
        self.fixture._write_artifacts("word-batch-1", "wps-http")
        export_word.export_word(self.project_dir, "word-batch-1")
        protocol.confirm_batch(self.project_dir, "word-batch-1")
        protocol.begin_active_batch(self.project_dir)
        self.fixture._write_artifacts("word-batch-2", "wps-http-2")
        export_word.export_word(self.project_dir, "word-batch-2")
        protocol.confirm_batch(self.project_dir, "word-batch-2")
        export_image_plan.export_image_plan(self.project_dir)
        protocol.confirm_image_plan(self.project_dir)
        status, final_before = self.request("/api/final-delivery")
        self.assertEqual(status, 200)
        self.assertFalse(final_before["final_delivery"]["eligible"], final_before)
        protocol.record_wps_page_check(self.project_dir, "word-batch-1", 10, "测试WPS")
        protocol.record_wps_page_check(self.project_dir, "word-batch-2", 10, "测试WPS")
        status, final_after = self.request("/api/final-delivery")
        self.assertTrue(final_after["final_delivery"]["eligible"], final_after)

    def test_web_can_save_ai_request_without_model_execution(self) -> None:
        protocol.begin_active_batch(self.project_dir)
        self.fixture._write_artifacts("word-batch-1", "web-ai")
        protocol.register_batch_artifacts(self.project_dir, "word-batch-1")
        source_hash = protocol.load_manifest(self.project_dir)["word_batches"][0]["source_sha256"]
        status, created = self.request("/api/batches/word-batch-1/ai-requests", "POST", {"block_id": "block-1", "source_sha256": source_hash, "instruction": "请补充可执行的闭环描述。"})
        self.assertEqual(status, 200)
        request_id = created["ai_request"]["request"]["id"]
        self.assertTrue((protocol.delivery_dir(self.project_dir) / "requests" / f"{request_id}.json").is_file())
        status, records = self.request("/api/batches/word-batch-1/requests")
        self.assertEqual(status, 200)
        self.assertEqual(records["requests"][0]["status"], "pending")

    def test_export_validation_and_image_plan_surfaces_support_local_editing(self) -> None:
        protocol.begin_active_batch(self.project_dir)
        self.fixture._write_artifacts("word-batch-1", "http-export")
        export_word.export_word(self.project_dir, "word-batch-1")
        status, word_validation = self.request("/api/batches/word-batch-1/validation")
        self.assertEqual(status, 200)
        self.assertTrue(word_validation["word_validation"]["read_only"])
        self.assertEqual(word_validation["word_validation"]["validation"]["page_verification"]["status"], "pending_wps_check")
        export_image_plan.export_image_plan(self.project_dir)
        status, image_plan = self.request("/api/image-plan")
        self.assertEqual(status, 200)
        self.assertFalse(image_plan["image_plan"]["read_only"])
        self.assertEqual(len(image_plan["image_plan"]["images"]), 1)
        image = image_plan["image_plan"]["images"][0]
        status, edited_plan = self.request("/api/image-plan/direct-edits", "POST", {
            "image_id": image["id"], "source_sha256": image_plan["image_plan"]["source_sha256"],
            "replacement": {"name": "HTTP更新图", "type": "流程图", "purpose": "表达闭环", "core_nodes": ["目标"],
                            "composition": "横向流程", "orientation": "landscape", "is_chapter_overview": True, "placement_note": "第1章导语后"},
        })
        self.assertEqual(status, 200)
        self.assertEqual(edited_plan["direct_edit"]["manifest"]["image_plan_workbook"]["status"], "export_pending")

    def test_http_batch_and_final_confirmation_flow(self) -> None:
        export_image_plan.export_image_plan(self.project_dir)
        status, image_confirmation = self.request("/api/image-plan/confirm", "POST", {})
        self.assertEqual(status, 200)
        self.assertEqual(image_confirmation["event"]["type"], "image-plan-confirmed")
        for batch_id, pages in (("word-batch-1", 9), ("word-batch-2", 10)):
            protocol.begin_active_batch(self.project_dir)
            self.fixture._write_artifacts(batch_id, "http-final")
            export_word.export_word(self.project_dir, batch_id)
            protocol.record_wps_page_check(self.project_dir, batch_id, pages, "测试WPS")
            status, reply = self.request(f"/api/batches/{batch_id}/confirm", "POST", {})
            self.assertEqual(status, 200)
            self.assertEqual(reply["event"]["type"], "batch-confirmed")
        status, checklist = self.request("/api/final-delivery")
        self.assertEqual(status, 200)
        self.assertTrue(checklist["final_delivery"]["eligible"])
        status, final = self.request("/api/final-delivery/confirm", "POST", {})
        self.assertEqual(status, 200)
        self.assertEqual(final["manifest"]["status"], "final_confirmed")

    def test_shutdown_without_live_lock_is_idempotent(self) -> None:
        self.assertEqual(server.shutdown(self.project_dir), 0)
        self.assertEqual(server.shutdown(self.project_dir), 0)


class DeliveryDaemonTest(unittest.TestCase):
    """Exercise the real detached-process lifecycle, including lock recovery."""

    def setUp(self) -> None:
        self.fixture = protocol_fixtures.DeliveryProtocolTest(methodName="runTest")
        self.fixture.setUp()
        self.project_dir = self.fixture.project_dir
        protocol.initialize_delivery(self.project_dir)

    def tearDown(self) -> None:
        server.shutdown(self.project_dir)
        self.fixture.tearDown()

    def test_daemon_health_and_shutdown(self) -> None:
        self.assertEqual(server.launch_daemon(self.project_dir, None, no_browser=True), 0)
        lock = server.load_lock(self.project_dir)
        self.assertIsNotNone(lock)
        health = server.health(int(lock["port"]))
        self.assertIsNotNone(health)
        self.assertEqual(health["service"], "biaoshu-master-delivery-ui")
        self.assertEqual(server.shutdown(self.project_dir), 0)
        self.assertIsNone(server.load_lock(self.project_dir))


if __name__ == "__main__":
    unittest.main()
