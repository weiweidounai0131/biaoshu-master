#!/usr/bin/env python3
"""Tests for local Stage4 rule profile management."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from rule_profiles import (
    default_selection,
    effective_rule_bytes,
    list_profiles,
    profile_descriptor,
    register_profile,
    set_default,
    validate_selection,
)


class RuleProfilesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "references").mkdir()
        (self.root / "rules" / "presets").mkdir(parents=True)
        (self.root / "rules" / "custom").mkdir()
        (self.root / "references" / "stage4-writing-rules.md").write_text("# 默认规则\n\n不得重复。\n", encoding="utf-8")
        (self.root / "rules" / "presets" / "telecom.md").write_text("# 电信覆盖层\n\n写清告警和回退。\n", encoding="utf-8")
        (self.root / "rules" / "rule-index.json").write_text(json.dumps({
            "schema_version": 1,
            "kind": "biaoshu_rule_index",
            "default_profile_id": "default",
            "profiles": [
                {"id": "default", "name": "默认规则", "kind": "default", "description": "基础", "path": "references/stage4-writing-rules.md", "base_id": "default", "read_only": True},
                {"id": "telecom", "name": "电信覆盖层", "kind": "preset", "description": "运维", "path": "rules/presets/telecom.md", "base_id": "default", "read_only": True},
            ],
        }, ensure_ascii=False), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_list_and_effective_rule_are_hash_bound(self) -> None:
        profiles = list_profiles(self.root)
        self.assertEqual(profiles["default_profile_id"], "default")
        self.assertEqual([item["id"] for item in profiles["profiles"]], ["default", "telecom"])
        data, descriptor = effective_rule_bytes("telecom", self.root)
        self.assertIn("默认规则", data.decode("utf-8"))
        self.assertIn("电信覆盖层", data.decode("utf-8"))
        self.assertEqual(descriptor["effective_sha256"], profile_descriptor("telecom", self.root)["effective_sha256"])

    def test_default_and_custom_registration(self) -> None:
        self.assertEqual(default_selection(self.root)["id"], "default")
        selected = set_default("telecom", self.root)
        self.assertEqual(selected["id"], "telecom")
        self.assertTrue(selected["is_default"])
        self.assertEqual(default_selection(self.root)["id"], "telecom")
        custom_source = self.root / "custom-source.md"
        custom_source.write_text("# 我的规则\n\n保留依据和验收证据。\n", encoding="utf-8")
        custom = register_profile(custom_source, "我的专属规则", "用户自定义规则", "my-custom-rule", self.root)
        self.assertEqual(custom["kind"], "custom")
        self.assertTrue((self.root / "rules" / "custom" / "my-custom-rule.md").is_file())
        selection = {key: custom[key] for key in {"id", "name", "kind", "description", "path", "sha256", "base_id", "base_sha256", "effective_sha256"}}
        validated = validate_selection(selection, self.root)
        self.assertEqual(validated["id"], custom["id"])
        self.assertEqual(validated["effective_sha256"], custom["effective_sha256"])

    def test_modified_profile_invalidates_old_selection(self) -> None:
        selection = default_selection(self.root)
        (self.root / "references" / "stage4-writing-rules.md").write_text("# 已变化的规则\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "规则已变化"):
            validate_selection(selection, self.root)


if __name__ == "__main__":
    unittest.main()
