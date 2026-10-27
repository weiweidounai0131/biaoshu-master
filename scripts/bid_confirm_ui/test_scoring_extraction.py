from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server as bid_server


def point(point_id: str, title: str, quote: str, order: int) -> dict:
    return {
        "point_id": point_id,
        "title": title,
        "order": order,
        "source_quotes": [{"field": "requirement_text", "text": quote}],
    }


def fixture_extraction() -> dict:
    return {
        "schema_version": 1,
        "status": "ready",
        "source_criterion_count": 3,
        "no_rubric_reason": "",
        "reviewed": True,
        "criteria": [
            {
                "criterion_id": "criterion-001",
                "factor_id": "factor-001",
                "factor": "资质能力",
                "requirement_text": "提供甲类资质、乙类资质。",
                "scoring_rule": "两项均须提交。",
                "weight": 2,
                "in_scope": True,
                "exclusion_reason": "",
                "points": [
                    point("point-001", "甲类资质说明", "甲类资质", 1),
                    point("point-002", "乙类资质说明", "乙类资质", 2),
                ],
            },
            {
                "criterion_id": "criterion-002",
                "factor_id": "factor-001",
                "factor": "资质能力",
                "requirement_text": "提供技术团队介绍。",
                "scoring_rule": "团队资料完整。",
                "weight": 1,
                "in_scope": True,
                "exclusion_reason": "",
                "points": [point("point-003", "技术团队介绍", "技术团队", 1)],
            },
            {
                "criterion_id": "criterion-003",
                "factor_id": "factor-002",
                "factor": "服务响应",
                "requirement_text": "承诺1小时响应，并提供服务流程。",
                "scoring_rule": "响应时限满足得分。",
                "weight": 3,
                "in_scope": True,
                "exclusion_reason": "",
                "points": [
                    point("point-004", "1小时响应机制", "1小时响应", 1),
                    point("point-005", "服务流程", "服务流程", 2),
                ],
            },
        ],
    }


def factor_chapter(node_id: str, factor_id: str, title: str, point_ids: list[tuple[str, str]], order: int) -> dict:
    children = [
        {
            "id": f"{node_id}-{point_id}",
            "title": point_title,
            "level": 2,
            "order": index,
            "score_point_id": point_id,
            "children": [],
        }
        for index, (point_id, point_title) in enumerate(point_ids, 1)
    ]
    return {
        "id": node_id,
        "title": title,
        "level": 1,
        "order": order,
        "pages": 1,
        "score_factor_id": factor_id,
        "children": children,
    }


def valid_outline() -> list[dict]:
    first = factor_chapter(
        "chapter-001",
        "factor-001",
        "资质能力",
        [("point-001", "甲类资质说明"), ("point-002", "乙类资质说明"), ("point-003", "技术团队介绍")],
        1,
    )
    first["children"].append({"id": "chapter-001-support", "title": "支撑机制", "level": 2, "order": 4, "children": []})
    second = factor_chapter(
        "chapter-002",
        "factor-002",
        "服务响应",
        [("point-004", "1小时响应机制"), ("point-005", "服务流程")],
        2,
    )
    return [first, second]


class ScoringExtractionTest(unittest.TestCase):
    def test_confirmed_extraction_requires_exact_source_quotes(self) -> None:
        bid_server.validate_scoring_extraction(fixture_extraction())

        value = fixture_extraction()
        value["criteria"][0]["points"][0]["source_quotes"][0]["text"] = "未出现在原文的短语"
        with self.assertRaisesRegex(ValueError, "原文标记"):
            bid_server.validate_scoring_extraction(value)

    def test_factor_can_be_the_source_for_a_required_heading(self) -> None:
        value = fixture_extraction()
        value["criteria"][0]["points"][0]["source_quotes"] = [{"field": "factor", "text": "资质能力"}]
        bid_server.validate_scoring_extraction(value)

        value["criteria"][0]["points"][0]["source_quotes"][0]["text"] = "错误评分因素"
        with self.assertRaisesRegex(ValueError, "原文标记"):
            bid_server.validate_scoring_extraction(value)

    def test_criterion_count_must_match_all_source_rows(self) -> None:
        value = fixture_extraction()
        value["source_criterion_count"] = 4
        with self.assertRaisesRegex(ValueError, "识别到 4 条标准"):
            bid_server.validate_scoring_extraction(value)

    def test_stage1_requires_an_explicit_user_review(self) -> None:
        value = fixture_extraction()
        value["reviewed"] = False
        with self.assertRaisesRegex(ValueError, "勾选已核对"):
            bid_server.validate_scoring_extraction(value)

    def test_excluded_criteria_remain_listed_with_a_reason(self) -> None:
        value = fixture_extraction()
        value["criteria"][0]["in_scope"] = False
        value["criteria"][0]["exclusion_reason"] = "本次范围明确不包含该部分"
        bid_server.validate_scoring_extraction(value)
        value["criteria"][0]["exclusion_reason"] = ""
        with self.assertRaisesRegex(ValueError, "未纳入框架"):
            bid_server.validate_scoring_extraction(value)

    def test_no_rubric_requires_a_reason_and_user_confirmation(self) -> None:
        no_rubric = {
            "schema_version": 1,
            "status": "no_rubric",
            "source_criterion_count": 0,
            "no_rubric_reason": "没有提供评分表",
            "reviewed": True,
            "criteria": [],
        }
        bid_server.validate_scoring_extraction(no_rubric)
        no_rubric["no_rubric_reason"] = ""
        with self.assertRaisesRegex(ValueError, "说明为什么"):
            bid_server.validate_scoring_extraction(no_rubric)

    def test_stage2_requires_exact_level_two_titles_in_score_order_before_support(self) -> None:
        extraction = fixture_extraction()
        chapters = valid_outline()
        bid_server.validate_scorepoint_outline(chapters, extraction)

        wrong_order = deepcopy(chapters)
        wrong_order[0]["children"][0], wrong_order[0]["children"][1] = wrong_order[0]["children"][1], wrong_order[0]["children"][0]
        with self.assertRaisesRegex(ValueError, "顺序或二级标题位置"):
            bid_server.validate_scorepoint_outline(wrong_order, extraction)

        title_changed = deepcopy(chapters)
        title_changed[0]["children"][0]["title"] = "甲类资质"
        with self.assertRaisesRegex(ValueError, "必须与已确认的提取标题一致"):
            bid_server.validate_scorepoint_outline(title_changed, extraction)

        moved_after_support = deepcopy(chapters)
        support = moved_after_support[0]["children"].pop()
        moved_after_support[0]["children"].insert(0, support)
        with self.assertRaisesRegex(ValueError, "排在该评分因素章节"):
            bid_server.validate_scorepoint_outline(moved_after_support, extraction)

        nested_point = deepcopy(chapters)
        nested_point[0]["children"][0]["children"] = [{
            "id": "nested-duplicate",
            "title": "甲类资质说明",
            "level": 3,
            "order": 1,
            "score_point_id": "point-001",
            "children": [],
        }]
        with self.assertRaisesRegex(ValueError, "只能放在二级标题"):
            bid_server.validate_scorepoint_outline(nested_point, extraction)

    def test_stage2_rejects_a_missing_score_factor_or_point(self) -> None:
        extraction = fixture_extraction()
        chapters = valid_outline()
        chapters[0].pop("score_factor_id")
        with self.assertRaisesRegex(ValueError, "必须放在对应的评分因素一级章节"):
            bid_server.validate_scorepoint_outline(chapters, extraction)

        chapters = valid_outline()
        chapters[1]["children"].pop()
        chapters[1]["children"][0]["order"] = 1
        with self.assertRaisesRegex(ValueError, "缺少已确认的评分点"):
            bid_server.validate_scorepoint_outline(chapters, extraction)

    def test_stage2_recommendation_gate_uses_stage1_extraction(self) -> None:
        extraction = fixture_extraction()
        receipt = {
            "project_id": "test-project",
            "confirmation_sha256": "confirmed-stage1",
            "data": {"formatting": {"target_pages": 2}, "scoring_extraction": extraction},
        }
        source = {
            "schema_version": 1,
            "stage": "stage2",
            "project_id": "test-project",
            "stage1_confirmation_sha256": "confirmed-stage1",
            "target_pages": 2,
            "generation_status": "complete",
            "chapters": valid_outline(),
        }
        bid_server.validate_stage2(source, receipt)
        source["chapters"][1]["children"].pop()
        source["chapters"][1]["children"][0]["order"] = 1
        with self.assertRaisesRegex(ValueError, "缺少已确认的评分点"):
            bid_server.validate_stage2(source, receipt)

    def test_legacy_projects_without_score_extraction_remain_readable(self) -> None:
        bid_server.validate_scorepoint_outline(valid_outline(), None)


if __name__ == "__main__":
    unittest.main()
