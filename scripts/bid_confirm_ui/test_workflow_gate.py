#!/usr/bin/env python3
"""Regression tests for complete-output handoff and tender positioning."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server as bid_server


class WorkflowGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.project_dir = Path(self.temp.name)
        self.data_dir = self.project_dir / bid_server.DATA_DIR_NAME
        self.data_dir.mkdir()
        self.intake_source = {
            "schema_version": 1,
            "stage": "intake",
            "project_id": "gate-test",
            "generated_at": bid_server.utc_now(),
            "prefill_ready": True,
            "background": "测试项目",
            "source_paths": [],
            "tender_position": "companion",
        }
        bid_server.atomic_write_json(self.data_dir / bid_server.INTAKE_INPUT, self.intake_source)
        self.intake_receipt = self._receipt(bid_server.INTAKE_RECEIPT, {
            "schema_version": 1,
            "stage": "intake",
            "status": "confirmed",
            "project_id": "gate-test",
            "source_sha256": bid_server.sha256_data(self.intake_source),
            "background": "测试项目",
            "source_paths": [],
            "tender_position": "companion",
            "confirmed_at": bid_server.utc_now(),
        })
        bid_server.write_workflow_state(self.data_dir, "intake", "awaiting_analysis", ["intake"])
        self.httpd = bid_server.BidConfirmServer(self.project_dir, 0)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://{bid_server.HOST}:{self.httpd.server_port}"

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def _receipt(self, name: str, value: dict) -> dict:
        value["confirmation_sha256"] = bid_server.sha256_data(value)
        bid_server.atomic_write_json(self.data_dir / name, value)
        return value

    def _stage1(self, status: str) -> dict:
        return {
            "schema_version": 1,
            "stage": "stage1",
            "project_id": "gate-test",
            "generated_at": bid_server.utc_now(),
            "generation_status": status,
            "intake_confirmation_sha256": self.intake_receipt["confirmation_sha256"],
            "tender_position": "companion",
            "source_summary": {"file_count": 0, "description": "已完成"},
            "project": {"project_name": "测试陪标", "summary": "完整项目口径", "bidder_name": "我司"},
            "scoring": {},
            "formatting": {"target_pages": 100},
            "boundaries": {},
            "additional_notes": "评分表的商务部分不用写\n内容不要精准，选择2~3个不重要的评分点不写",
        }

    def request(self, path: str, data: dict | None = None) -> tuple[int, dict]:
        body = None if data is None else json.dumps(data, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            method="GET" if data is None else "POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.load(error)
            finally:
                error.close()

    def test_partial_recommendation_never_unlocks_page(self) -> None:
        stage1 = self._stage1("generating")
        bid_server.atomic_write_json(self.data_dir / bid_server.STAGE1_INPUT, stage1)
        status, session = self.request("/api/session")
        self.assertEqual(status, 200)
        self.assertEqual(session["stage"], "intake")
        self.assertFalse(session["handoff_ready"])
        status, payload = self.request("/api/stage1")
        self.assertEqual(status, 422)
        self.assertFalse(payload["ok"])
        with self.assertRaisesRegex(ValueError, "not complete"):
            bid_server.wait_for_stage(self.project_dir, "stage1", 1)

        stage1["generation_status"] = "complete"
        bid_server.atomic_write_json(self.data_dir / bid_server.STAGE1_INPUT, stage1)
        _, ready_hash = bid_server.recommendation_ready(self.data_dir, "stage1")
        bid_server.set_agent_wait(self.data_dir, "stage1", "waiting", "stale test", "stale-hash")
        status, session = self.request("/api/session")
        self.assertEqual(status, 200)
        self.assertFalse(session["handoff_ready"])
        bid_server.set_agent_wait(self.data_dir, "stage1", "waiting", "test", ready_hash)
        status, session = self.request("/api/session")
        self.assertEqual(status, 200)
        self.assertEqual(session["stage"], "stage1")
        self.assertTrue(session["handoff_ready"])
        self.assertEqual(session["recommendation_sha256"], ready_hash)

    def test_long_generation_is_not_reported_as_wait_failure(self) -> None:
        handoff = bid_server.callback_handoff({"active_stage": "stage2", "mode": "confirmed"})
        self.assertEqual(handoff, ("第三阶段图片规划", "stage3"))
        self.assertFalse(bid_server.handoff_wait_failed(
            {"stage": "stage2", "status": "received", "process_alive": False},
            "stage3",
        ))
        self.assertTrue(bid_server.handoff_wait_failed(
            {"stage": "stage3", "status": "timed_out", "process_alive": False},
            "stage3",
        ))

    def test_stage2_wait_returns_six_for_current_rebalance_request(self) -> None:
        stage1 = self._stage1("complete")
        stage1["formatting"]["target_pages"] = 20
        bid_server.atomic_write_json(self.data_dir / bid_server.STAGE1_INPUT, stage1)
        stage1_receipt = self._receipt(bid_server.STAGE1_RECEIPT, {
            "schema_version": 1, "stage": "stage1", "status": "confirmed",
            "project_id": "gate-test", "source_sha256": bid_server.sha256_data(stage1),
            "data": stage1, "confirmed_at": bid_server.utc_now(),
        })
        source = {
            "schema_version": 1, "stage": "stage2", "project_id": "gate-test",
            "generation_status": "complete",
            "stage1_confirmation_sha256": stage1_receipt["confirmation_sha256"],
            "target_pages": 20, "coverage": {"total": 5, "mapped": 3, "unmapped": ["次要1", "次要2"]},
            "chapters": [
                {"id": "c1", "number": "1", "title": "章节一", "level": 1, "order": 1, "pages": 10, "children": []},
                {"id": "c2", "number": "2", "title": "章节二", "level": 1, "order": 2, "pages": 10, "children": []},
            ], "tender_position": "companion",
        }
        bid_server.atomic_write_json(self.data_dir / bid_server.STAGE2_INPUT, source)
        source_hash = bid_server.sha256_data(source)
        request = {
            "schema_version": 1, "stage": "stage2-rebalance", "status": "pending",
            "request_id": "rebalance-test", "project_id": "gate-test",
            "stage1_confirmation_sha256": stage1_receipt["confirmation_sha256"],
            "source_sha256": source_hash, "chapter_pages": [],
        }
        original_ensure = bid_server.ensure_confirmation_page
        bid_server.ensure_confirmation_page = lambda *_args, **_kwargs: None
        try:
            result: list[int] = []
            thread = threading.Thread(target=lambda: result.append(bid_server.wait_for_stage(self.project_dir, "stage2", 2)))
            thread.start()
            time.sleep(0.15)
            source["generation_status"] = "generating"
            source["rebalance_request_id"] = request["request_id"]
            bid_server.atomic_write_json(self.data_dir / bid_server.STAGE2_INPUT, source)
            bid_server.atomic_write_json(self.data_dir / bid_server.STAGE2_REBALANCE_REQUEST, request)
            thread.join(timeout=2)
            self.assertEqual(result, [6])
        finally:
            bid_server.ensure_confirmation_page = original_ensure

    def test_stage2_ai_adjust_request_round_trip(self) -> None:
        stage1 = self._stage1("complete")
        stage1["formatting"]["target_pages"] = 20
        bid_server.atomic_write_json(self.data_dir / bid_server.STAGE1_INPUT, stage1)
        stage1_receipt = self._receipt(bid_server.STAGE1_RECEIPT, {
            "schema_version": 1, "stage": "stage1", "status": "confirmed",
            "project_id": "gate-test", "source_sha256": bid_server.sha256_data(stage1),
            "data": stage1, "confirmed_at": bid_server.utc_now(),
        })
        source = {
            "schema_version": 1, "stage": "stage2", "project_id": "gate-test",
            "generation_status": "complete", "stage1_confirmation_sha256": stage1_receipt["confirmation_sha256"],
            "target_pages": 20, "coverage": {"total": 5, "mapped": 3, "unmapped": ["次要1", "次要2"]},
            "chapters": [
                {"id": "c1", "number": "1", "title": "章节一", "level": 1, "order": 1, "pages": 10, "children": []},
                {"id": "c2", "number": "2", "title": "章节二", "level": 1, "order": 2, "pages": 10, "children": []},
            ], "tender_position": "companion",
        }
        bid_server.atomic_write_json(self.data_dir / bid_server.STAGE2_INPUT, source)
        bid_server.write_workflow_state(self.data_dir, "stage2", "editing", ["intake", "stage1"])
        original_ensure = bid_server.ensure_confirmation_page
        bid_server.ensure_confirmation_page = lambda *_args, **_kwargs: None
        wait_result: list[int] = []
        wait_thread = threading.Thread(target=lambda: wait_result.append(bid_server.wait_for_stage(self.project_dir, "stage2", 2)))
        wait_thread.start()
        time.sleep(0.15)
        try:
            status, submitted = self.request("/api/stage2/ai-adjust", {
                "source_sha256": bid_server.sha256_data(source),
                "instruction": "把两个一级章节拆成六个一级章节，重新分配页数并保留评分映射",
                "data": {"chapters": source["chapters"], "coverage": source["coverage"], "planned_pages": 20},
            })
        finally:
            wait_thread.join(timeout=2)
            bid_server.ensure_confirmation_page = original_ensure
        self.assertEqual(status, 200)
        self.assertEqual(wait_result, [7])
        request = submitted["request"]
        self.assertEqual(request["status"], "pending")
        self.assertTrue((self.data_dir / bid_server.STAGE2_AI_ADJUST_REQUEST).exists())
        generating = bid_server.read_json(self.data_dir / bid_server.STAGE2_INPUT)
        self.assertEqual(generating["generation_status"], "generating")
        self.assertEqual(generating["ai_adjust_request_id"], request["request_id"])
        status, waiting = self.request("/api/stage2/ai-adjust-status")
        self.assertEqual(status, 200)
        self.assertEqual(waiting["status"], "waiting")

        regenerated = dict(source, generation_status="complete", ai_adjust_request_id=request["request_id"])
        bid_server.atomic_write_json(self.data_dir / bid_server.STAGE2_INPUT, regenerated)
        status, ready = self.request("/api/stage2/ai-adjust-status")
        self.assertEqual(status, 200)
        self.assertEqual(ready["status"], "ready")

        status, resubmitted = self.request("/api/stage2/ai-adjust", {
            "source_sha256": bid_server.sha256_data(regenerated),
            "instruction": "再次调整目录，让项目负责人及团队配置位于第4章和第5章之间",
            "data": {"chapters": regenerated["chapters"], "coverage": regenerated["coverage"], "planned_pages": 20},
        })
        self.assertEqual(status, 200)
        self.assertNotEqual(resubmitted["request"]["request_id"], request["request_id"])
        self.assertEqual(bid_server.read_json(self.data_dir / bid_server.STAGE2_AI_ADJUST_REQUEST)["status"], "pending")

    def test_stale_intake_cannot_fall_back_to_old_stage1(self) -> None:
        old_stage1 = self._stage1("complete")
        bid_server.atomic_write_json(self.data_dir / bid_server.STAGE1_INPUT, old_stage1)
        changed = dict(self.intake_source, run_id="new-run", background="新一轮测试")
        bid_server.atomic_write_json(self.data_dir / bid_server.INTAKE_INPUT, changed)
        status, session = self.request("/api/session")
        self.assertEqual(status, 200)
        self.assertEqual(session["stage"], "intake")
        self.assertFalse(session["handoff_ready"])
        with self.assertRaisesRegex(ValueError, "入口确认回执无效"):
            bid_server.recommendation_ready(self.data_dir, "stage1")

    def test_confirmation_page_presence_heartbeat_is_recorded(self) -> None:
        status, payload = self.request("/api/page-presence", {"page": "stage1", "instance_id": "test-page-instance"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["presence"]["page_open"])
        self.assertEqual(payload["presence"]["page"], "stage1")
        self.assertTrue((self.data_dir / bid_server.PAGE_PRESENCE).is_file())

    def test_companion_defaults_to_100_pages_but_preserves_user_edit(self) -> None:
        build_script = Path(__file__).resolve().parents[1] / "build_stage1_recommendations.py"
        subprocess.run([sys.executable, str(build_script), str(self.project_dir)], check=True, capture_output=True, text=True)
        skeleton = bid_server.read_json(self.data_dir / bid_server.STAGE1_INPUT)
        self.assertEqual(skeleton["generation_status"], "generating")
        self.assertEqual(skeleton["formatting"]["target_pages"], 100)
        self.assertIn("评分表的商务部分不用写", skeleton["additional_notes"])
        self.assertIn("内容不要精准，选择2~3个不重要的评分点不写", skeleton["additional_notes"])

        stage1 = self._stage1("complete")
        bid_server.atomic_write_json(self.data_dir / bid_server.STAGE1_INPUT, stage1)
        status, loaded = self.request("/api/stage1")
        self.assertEqual(status, 200)
        editable = deepcopy(stage1)
        editable.pop("generation_status")
        editable.pop("intake_confirmation_sha256")
        editable.pop("tender_position")
        editable["formatting"]["target_pages"] = 999
        editable["additional_notes"] = "用户在前端修改后的要求"
        status, confirmed = self.request("/api/stage1/confirm", {
            "source_sha256": loaded["source_sha256"],
            "data": editable,
        })
        self.assertEqual(status, 200)
        self.assertEqual(confirmed["receipt"]["tender_position"], "companion")
        self.assertEqual(confirmed["receipt"]["data"]["formatting"]["target_pages"], 999)
        self.assertEqual(confirmed["receipt"]["data"]["additional_notes"], "用户在前端修改后的要求")
        bid_server.validate_stage2({
            "schema_version": 1,
            "stage": "stage2",
            "project_id": confirmed["receipt"]["project_id"],
            "stage1_confirmation_sha256": confirmed["receipt"]["confirmation_sha256"],
            "target_pages": 999,
            "coverage": {"total": 5, "mapped": 3, "unmapped": ["次要1", "次要2"]},
            "chapters": [{"id": "c1", "title": "章节", "level": 1, "order": 1, "pages": 999, "children": []}],
            "tender_position": "companion",
        }, confirmed["receipt"])

    def test_companion_coverage_requires_two_or_three_omissions(self) -> None:
        chapter = {"id": "c1", "title": "章节", "level": 1, "order": 1, "pages": 100, "children": []}
        self.assertEqual(
            bid_server.validate_outline([chapter], 100, {"total": 5, "mapped": 3, "unmapped": ["次要1", "次要2"]}, "companion"),
            100,
        )
        with self.assertRaisesRegex(ValueError, "2至3"):
            bid_server.validate_outline([chapter], 100, {"total": 5, "mapped": 5, "unmapped": []}, "companion")
        with self.assertRaisesRegex(ValueError, "全部评分点"):
            bid_server.validate_outline([chapter], 100, {"total": 5, "mapped": 3, "unmapped": ["次要1", "次要2"]}, "main")


if __name__ == "__main__":
    unittest.main()
