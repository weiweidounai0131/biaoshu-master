#!/usr/bin/env python3
"""Lifecycle tests for the local Stage 3 confirmation API."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server as bid_server


class Stage3LifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.project_dir = Path(self.temp.name)
        self.data_dir = self.project_dir / bid_server.DATA_DIR_NAME
        self.data_dir.mkdir()
        self._write_legacy_stage1_and_stage2()
        self._write_stage3_recommendation()
        self.httpd = bid_server.BidConfirmServer(self.project_dir, 0)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://{bid_server.HOST}:{self.httpd.server_port}"

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def _write_receipt(self, name: str, receipt: dict) -> dict:
        receipt["confirmation_sha256"] = bid_server.sha256_data(receipt)
        bid_server.atomic_write_json(self.data_dir / name, receipt)
        return receipt

    def _write_legacy_stage1_and_stage2(self) -> None:
        project_id = "legacy-project-without-intake"
        stage1 = {
            "schema_version": 1,
            "stage": "stage1",
            "project_id": project_id,
            "project": {"project_name": "测试项目"},
            "scoring": {},
            "formatting": {"target_pages": 10},
            "boundaries": {},
        }
        bid_server.atomic_write_json(self.data_dir / bid_server.STAGE1_INPUT, stage1)
        stage1_receipt = self._write_receipt(bid_server.STAGE1_RECEIPT, {
            "schema_version": 1,
            "stage": "stage1",
            "status": "confirmed",
            "project_id": project_id,
            "source_sha256": bid_server.sha256_data(stage1),
            "data": stage1,
            "confirmed_at": bid_server.utc_now(),
        })
        chapters = [{
            "id": "chapter-1",
            "number": "1",
            "title": "项目理解",
            "level": 1,
            "order": 1,
            "pages": 10,
            "score_refs": [],
            "requirement_refs": [],
            "allow_deeper": False,
            "children": [],
        }]
        stage2 = {
            "schema_version": 1,
            "stage": "stage2",
            "project_id": project_id,
            "stage1_confirmation_sha256": stage1_receipt["confirmation_sha256"],
            "target_pages": 10,
            "coverage": {"total": 0, "mapped": 0, "unmapped": []},
            "chapters": chapters,
        }
        bid_server.atomic_write_json(self.data_dir / bid_server.STAGE2_INPUT, stage2)
        self.stage2_receipt = self._write_receipt(bid_server.STAGE2_RECEIPT, {
            "schema_version": 1,
            "stage": "stage2",
            "status": "confirmed",
            "project_id": project_id,
            "stage1_confirmation_sha256": stage1_receipt["confirmation_sha256"],
            "source_sha256": bid_server.sha256_data(stage2),
            "data": {"chapters": chapters, "coverage": stage2["coverage"], "planned_pages": 10},
            "confirmed_at": bid_server.utc_now(),
        })

    def _write_stage3_recommendation(self) -> None:
        self.image = {
            "id": "image-1",
            "figure_no": "图1-1",
            "order": 1,
            "chapter_id": "chapter-1",
            "chapter_number": "1",
            "chapter_title": "项目理解",
            "position": {
                "outline_node_id": "chapter-1",
                "outline_number": "1",
                "outline_title": "项目理解",
                "placement_note": "章导语之后",
            },
            "name": "项目服务总览",
            "type": "章首总览图",
            "purpose": "展示整体服务逻辑",
            "core_nodes": ["目标", "流程", "交付"],
            "composition": "左右分栏的总览图",
            "orientation": "landscape",
            "is_chapter_overview": True,
            "origin": "planned",
        }
        self.stage3 = {
            "schema_version": 1,
            "stage": "stage3",
            "project_id": self.stage2_receipt["project_id"],
            "stage2_confirmation_sha256": self.stage2_receipt["confirmation_sha256"],
            "visual_direction": {
                "palette": "深蓝、红色与白色",
                "style": "红蓝现代商务信息图",
                "background": "白色或浅灰底",
                "density": "适中，突出核心信息",
                "avoid": ["复杂渐变", "密集小字"],
            },
            "chapter_settings": [{
                "chapter_id": "chapter-1",
                "chapter_number": "1",
                "chapter_title": "项目理解",
                "overview_policy": "required",
                "overview_reason": "需展示章节全局",
            }],
            "images": [self.image],
            "cleanup_actions": [{
                "id": "cleanup-1",
                "action": "remove_placeholder",
                "target": "图1-1占位符",
                "reason": "正式图片插入后删除",
            }],
        }
        bid_server.atomic_write_json(self.data_dir / bid_server.STAGE3_INPUT, self.stage3)

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

    def test_per_chapter_order_and_position_binding(self) -> None:
        chapters = deepcopy(self.stage2_receipt["data"]["chapters"])
        chapters.append({
            "id": "chapter-2",
            "number": "2",
            "title": "服务方案",
            "level": 1,
            "order": 2,
            "pages": 10,
            "score_refs": [],
            "requirement_refs": [],
            "allow_deeper": False,
            "children": [],
        })
        receipt = deepcopy(self.stage2_receipt)
        receipt["data"]["chapters"] = chapters
        second = deepcopy(self.image)
        second.update({
            "id": "image-2",
            "figure_no": "图2-1",
            "order": 1,
            "chapter_id": "chapter-2",
            "chapter_number": "2",
            "chapter_title": "服务方案",
            "position": {
                "outline_node_id": "chapter-2",
                "outline_number": "2",
                "outline_title": "服务方案",
                "placement_note": "章导语之后",
            },
        })
        data = {
            "visual_direction": self.stage3["visual_direction"],
            "chapter_settings": [
                self.stage3["chapter_settings"][0],
                {"chapter_id": "chapter-2", "chapter_number": "2", "chapter_title": "服务方案", "overview_policy": "exempt", "overview_reason": "用流程图代替"},
            ],
            "images": [self.image, second],
            "cleanup_actions": self.stage3["cleanup_actions"],
        }
        bid_server.validate_image_plan(data, receipt)

        wrong_position = deepcopy(data)
        wrong_position["images"][1]["position"] = deepcopy(self.image["position"])
        with self.assertRaisesRegex(ValueError, "position must belong"):
            bid_server.validate_image_plan(wrong_position, receipt)

        wrong_figure = deepcopy(data)
        wrong_figure["images"][1]["figure_no"] = "图2-2"
        with self.assertRaisesRegex(ValueError, "figure number"):
            bid_server.validate_image_plan(wrong_figure, receipt)

    def test_visual_direction_requires_prefill_and_accepts_legacy_aliases(self) -> None:
        incomplete = deepcopy(self.stage3)
        incomplete["visual_direction"]["palette"] = ""
        with self.assertRaisesRegex(ValueError, "visual_direction.palette"):
            bid_server.validate_image_plan(incomplete, self.stage2_receipt)

        legacy = deepcopy(self.stage3)
        legacy["visual_direction"] = {
            "primary_colors": "深海蓝、青绿和暖橙",
            "style": "专业运营服务信息图",
            "background": "纯白或极浅灰底",
            "information_density": "中等，突出流程和数据闭环",
            "avoid": "复杂渐变、密集小字",
        }
        bid_server.validate_image_plan(legacy, self.stage2_receipt)

    def test_confirm_read_only_reopen_and_reconfirm(self) -> None:
        status, stage2 = self.request("/api/stage2")
        self.assertEqual(status, 200)
        self.assertTrue(stage2["stage3_available"])
        self.assertEqual(stage2["workflow"]["active_stage"], "stage3")

        status, stage3 = self.request("/api/stage3")
        self.assertEqual(status, 200)
        self.assertIsNone(stage3["receipt"])
        self.assertEqual(stage3["workflow"]["mode"], "editing")
        confirmation_data = {
            "source_sha256": stage3["source_sha256"],
            "data": {
                "visual_direction": self.stage3["visual_direction"],
                "chapter_settings": self.stage3["chapter_settings"],
                "images": [self.image],
                "cleanup_actions": self.stage3["cleanup_actions"],
            },
        }

        status, confirmed = self.request("/api/stage3/confirm", confirmation_data)
        self.assertEqual(status, 200)
        self.assertEqual(confirmed["receipt"]["status"], "confirmed")
        image_fields = {
            "id", "figure_no", "order", "chapter_id", "chapter_number", "chapter_title",
            "position", "name", "type", "purpose", "core_nodes", "composition",
            "orientation", "is_chapter_overview", "origin",
        }
        self.assertEqual(set(confirmed["receipt"]["data"]["images"][0]), image_fields)
        status, read_only = self.request("/api/stage3")
        self.assertEqual(status, 200)
        self.assertIsNotNone(read_only["receipt"])
        self.assertEqual(read_only["workflow"]["mode"], "confirmed")
        self.assertEqual(set(read_only["receipt"]["data"]["images"][0]), image_fields)

        status, duplicate = self.request("/api/stage3/confirm", confirmation_data)
        self.assertEqual(status, 200)
        self.assertTrue(duplicate["ok"])
        self.assertTrue(duplicate["already_confirmed"])

        status, reopened = self.request("/api/stage3/reopen", {})
        self.assertEqual(status, 200)
        self.assertEqual(reopened["mode"], "editing")
        status, editing = self.request("/api/stage3")
        self.assertEqual(status, 200)
        self.assertIsNone(editing["receipt"])
        self.assertEqual(editing["draft"]["data"], confirmation_data["data"])

        status, reconfirmed = self.request("/api/stage3/confirm", confirmation_data)
        self.assertEqual(status, 200)
        self.assertEqual(reconfirmed["receipt"]["status"], "confirmed")
        self.assertEqual(bid_server.wait_for_stage(self.project_dir, "stage3", 1), 0)

        for name in (bid_server.STAGE4_INPUT, bid_server.STAGE4_RECEIPT, bid_server.STAGE4_DRAFT):
            bid_server.atomic_write_json(self.data_dir / name, {"test": True})

        status, stage2_reopened = self.request("/api/stage2/reopen", {})
        self.assertEqual(status, 200)
        self.assertEqual(stage2_reopened["stage"], "stage2")
        self.assertTrue((self.data_dir / bid_server.STAGE2_DRAFT).exists())
        self.assertFalse((self.data_dir / bid_server.STAGE2_RECEIPT).exists())
        self.assertFalse((self.data_dir / bid_server.STAGE3_INPUT).exists())
        self.assertFalse((self.data_dir / bid_server.STAGE3_RECEIPT).exists())
        self.assertFalse((self.data_dir / bid_server.STAGE3_DRAFT).exists())
        self.assertFalse((self.data_dir / bid_server.STAGE4_INPUT).exists())
        self.assertFalse((self.data_dir / bid_server.STAGE4_RECEIPT).exists())
        self.assertFalse((self.data_dir / bid_server.STAGE4_DRAFT).exists())

    def test_stage3_ai_adjust_request_round_trip(self) -> None:
        status, stage3 = self.request("/api/stage3")
        self.assertEqual(status, 200)
        submitted_data = {
            "source_sha256": stage3["source_sha256"],
            "instruction": "减少重复流程图，统一蓝白信息图风格，并重新检查所有图片的放置标题",
            "data": self.stage3,
        }
        status, submitted = self.request("/api/stage3/ai-adjust", submitted_data)
        self.assertEqual(status, 200)
        request = submitted["request"]
        self.assertEqual(request["status"], "pending")
        generating = bid_server.read_json(self.data_dir / bid_server.STAGE3_INPUT)
        self.assertEqual(generating["generation_status"], "generating")
        self.assertEqual(generating["ai_adjust_request_id"], request["request_id"])
        status, waiting = self.request("/api/stage3/ai-adjust-status")
        self.assertEqual(status, 200)
        self.assertEqual(waiting["status"], "waiting")

        regenerated = dict(self.stage3, generation_status="complete", ai_adjust_request_id=request["request_id"])
        bid_server.atomic_write_json(self.data_dir / bid_server.STAGE3_INPUT, regenerated)
        status, ready = self.request("/api/stage3/ai-adjust-status")
        self.assertEqual(status, 200)
        self.assertEqual(ready["status"], "ready")

        status, resubmitted = self.request("/api/stage3/ai-adjust", {
            "source_sha256": bid_server.sha256_data(regenerated),
            "instruction": "再次调整图片规划，减少重复图并强化章节总览图",
            "data": regenerated,
        })
        self.assertEqual(status, 200)
        self.assertNotEqual(resubmitted["request"]["request_id"], request["request_id"])
        self.assertEqual(bid_server.read_json(self.data_dir / bid_server.STAGE3_AI_ADJUST_REQUEST)["status"], "pending")

    def test_wait_is_superseded_when_upstream_stage_reopens(self) -> None:
        self.assertTrue(bid_server.wait_prerequisite_valid(self.data_dir, "stage3"))
        status, reopened = self.request("/api/stage1/reopen", {})
        self.assertEqual(status, 200)
        self.assertEqual(reopened["stage"], "stage1")
        self.assertFalse(bid_server.wait_prerequisite_valid(self.data_dir, "stage3"))
        self.assertEqual(bid_server.wait_for_stage(self.project_dir, "stage3", 20), 5)
        wait = bid_server.read_json(self.data_dir / bid_server.AGENT_WAIT)
        self.assertEqual(wait["status"], "superseded")
        self.assertIn("stage1", wait["details"])


if __name__ == "__main__":
    unittest.main()
