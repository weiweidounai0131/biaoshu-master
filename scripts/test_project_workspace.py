#!/usr/bin/env python3
"""Regression tests for persistent biaoshu-master project workspaces."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import project_workspace


class ProjectWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "workspaces"
        self.materials = Path(self.temp.name) / "materials"
        self.materials.mkdir()
        self.background_a = self.materials / "招标文件A.docx"
        self.background_b = self.materials / "招标文件B.docx"
        self.background_a.write_text("A", encoding="utf-8")
        self.background_b.write_text("B", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_creates_reuses_and_separates_projects(self) -> None:
        first = project_workspace.resolve_workspace(
            root=self.root,
            project_name="项目甲",
            client="招标人甲",
            background_paths=[str(self.background_a)],
        )
        repeated = project_workspace.resolve_workspace(
            root=self.root,
            project_name="项目甲",
            client="招标人甲",
            background_paths=[str(self.background_a)],
        )
        second = project_workspace.resolve_workspace(
            root=self.root,
            project_name="项目乙",
            client="招标人甲",
            background_paths=[str(self.background_b)],
        )

        self.assertTrue(first["created"])
        self.assertFalse(first["reused"])
        self.assertTrue(repeated["reused"])
        self.assertEqual(first["project_id"], repeated["project_id"])
        self.assertEqual(first["project_dir"], repeated["project_dir"])
        self.assertNotEqual(first["project_dir"], second["project_dir"])
        self.assertTrue(Path(first["project_dir"]).is_dir())
        self.assertTrue(Path(second["project_dir"]).is_dir())

    def test_same_display_identity_with_different_background_creates_new_workspace(self) -> None:
        first = project_workspace.resolve_workspace(
            root=self.root,
            project_name="年度服务项目",
            client="同一招标人",
            background_paths=[str(self.background_a)],
        )
        second = project_workspace.resolve_workspace(
            root=self.root,
            project_name="年度服务项目",
            client="同一招标人",
            background_paths=[str(self.background_b)],
        )
        self.assertNotEqual(first["project_id"], second["project_id"])
        self.assertNotEqual(first["project_dir"], second["project_dir"])

        repeated = project_workspace.resolve_workspace(
            root=self.root,
            project_name="年度服务项目",
            client="同一招标人",
            background_paths=[str(self.background_a)],
        )
        self.assertEqual(repeated["project_id"], first["project_id"])

    def test_force_new_requires_explicit_choice_afterward(self) -> None:
        project_workspace.resolve_workspace(root=self.root, project_name="重复名称", client="客户")
        forced = project_workspace.resolve_workspace(root=self.root, project_name="重复名称", client="客户", force_new=True)
        self.assertTrue(forced["created"])
        with self.assertRaises(project_workspace.WorkspaceResolutionError):
            project_workspace.resolve_workspace(root=self.root, project_name="重复名称", client="客户")

    def test_explicit_existing_directory_preserves_legacy_project_id(self) -> None:
        legacy_dir = Path(self.temp.name) / "legacy-project"
        intake = legacy_dir / project_workspace.INTAKE_DIR_NAME / project_workspace.INTAKE_FILENAME
        intake.parent.mkdir(parents=True)
        intake.write_text(json.dumps({"project_id": "legacy-project-id"}), encoding="utf-8")

        result = project_workspace.resolve_workspace(
            root=self.root,
            project_dir=legacy_dir,
            project_name="旧项目",
            client="旧招标人",
        )

        self.assertEqual(result["project_id"], "legacy-project-id")
        metadata = json.loads((legacy_dir / project_workspace.METADATA_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(metadata["project_id"], "legacy-project-id")
        self.assertTrue((self.root / project_workspace.INDEX_FILENAME).is_file())

    def test_slug_cannot_escape_workspace_root(self) -> None:
        result = project_workspace.resolve_workspace(root=self.root, project_name="../危险/项目", client="客户")
        path = Path(result["project_dir"])
        self.assertEqual(path.parent, self.root.resolve())
        self.assertNotIn("/", path.name)
        self.assertNotIn("\\", path.name)

    def test_list_returns_registered_projects(self) -> None:
        project_workspace.resolve_workspace(root=self.root, project_name="项目甲")
        project_workspace.resolve_workspace(root=self.root, project_name="项目乙")
        listed = project_workspace.list_workspaces(self.root)
        self.assertEqual(listed["kind"], "biaoshu_project_workspace_list")
        self.assertEqual(len(listed["projects"]), 2)

    def test_prepare_intake_resume_reuses_run_and_fresh_intake_archives_delivery(self) -> None:
        background = self.background_a
        resolution = project_workspace.resolve_workspace(
            root=self.root,
            project_name="项目甲",
            client="招标人甲",
            background_paths=[str(background)],
        )
        project_dir = Path(resolution["project_dir"])
        prepare = Path(__file__).with_name("prepare_intake.py")

        subprocess.run(
            [
                sys.executable,
                str(prepare),
                str(project_dir),
                "--project-id",
                resolution["project_id"],
                "--background",
                "项目甲",
                "--background-path",
                str(background),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        intake_path = project_dir / project_workspace.INTAKE_DIR_NAME / project_workspace.INTAKE_FILENAME
        first = json.loads(intake_path.read_text(encoding="utf-8"))

        resumed = subprocess.run(
            [sys.executable, str(prepare), str(project_dir), "--resume"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("resumed_existing=true", resumed.stdout)
        self.assertEqual(json.loads(intake_path.read_text(encoding="utf-8"))["run_id"], first["run_id"])

        delivery = project_dir / "bid_delivery"
        delivery.mkdir()
        (delivery / "old.txt").write_text("old", encoding="utf-8")
        subprocess.run(
            [
                sys.executable,
                str(prepare),
                str(project_dir),
                "--project-id",
                resolution["project_id"],
                "--background",
                "项目甲",
                "--background-path",
                str(background),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertFalse(delivery.exists())
        archived = list((project_dir / "bid_delivery-history").glob("*-new-intake/old.txt"))
        self.assertEqual(len(archived), 1)
        self.assertNotEqual(json.loads(intake_path.read_text(encoding="utf-8"))["run_id"], first["run_id"])


if __name__ == "__main__":
    unittest.main()
