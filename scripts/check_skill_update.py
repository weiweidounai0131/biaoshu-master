#!/usr/bin/env python3
"""Check, and only after explicit approval pull, a GitHub skill update.

The command is intentionally non-interactive.  The calling AI must ask the
user before passing ``--update``; this script never auto-updates a skill.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any


def _run(skill_dir: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(skill_dir), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )


def _output(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stdout or "").strip()


def _error_kind(result: subprocess.CompletedProcess[str]) -> str:
    text = ((result.stderr or "") + " " + (result.stdout or "")).lower()
    if "could not resolve host" in text or "network is unreachable" in text or "failed to connect" in text:
        return "network_unreachable"
    if "authentication" in text or "permission denied" in text or "repository not found" in text:
        return "remote_authentication_failed"
    return "git_command_failed"


def _base(skill_dir: Path) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "biaoshu_master_skill_update_check",
        "skill_dir": str(skill_dir),
        "update_requested": False,
    }


def check(skill_dir: Path) -> dict[str, Any]:
    skill_dir = skill_dir.expanduser().resolve()
    result = _base(skill_dir)
    probe = _run(skill_dir, "rev-parse", "--is-inside-work-tree")
    if probe.returncode != 0 or _output(probe) != "true":
        result.update({"status": "unavailable", "reason": "not_git_repository"})
        return result

    remote = _run(skill_dir, "remote", "get-url", "origin")
    if remote.returncode != 0 or not _output(remote):
        result.update({"status": "unavailable", "reason": "origin_not_configured"})
        return result
    remote_url = _output(remote)
    result["remote_provider"] = "github" if re.search(r"github\.com", remote_url, re.IGNORECASE) else "git"

    branch_result = _run(skill_dir, "symbolic-ref", "--quiet", "--short", "HEAD")
    branch = _output(branch_result)
    if branch_result.returncode != 0 or not branch:
        result.update({"status": "unavailable", "reason": "detached_head"})
        return result
    local_result = _run(skill_dir, "rev-parse", "HEAD")
    if local_result.returncode != 0 or not _output(local_result):
        result.update({"status": "unavailable", "reason": "local_revision_unavailable"})
        return result
    local_sha = _output(local_result)
    remote_result = _run(skill_dir, "ls-remote", "origin", f"refs/heads/{branch}")
    if remote_result.returncode != 0:
        result.update({"status": "check_failed", "reason": _error_kind(remote_result), "branch": branch})
        return result
    remote_line = next((line for line in _output(remote_result).splitlines() if line.strip()), "")
    remote_sha = remote_line.split()[0] if remote_line else ""
    if not remote_sha:
        result.update({"status": "check_failed", "reason": "remote_branch_not_found", "branch": branch})
        return result
    result.update({"branch": branch, "local_sha": local_sha, "remote_sha": remote_sha})
    result["status"] = "up_to_date" if local_sha == remote_sha else "update_available"
    return result


def update(skill_dir: Path) -> dict[str, Any]:
    result = check(skill_dir)
    result["update_requested"] = True
    if result.get("status") != "update_available":
        return result
    dirty = _run(skill_dir, "status", "--porcelain")
    if dirty.returncode != 0:
        result.update({"status": "check_failed", "reason": _error_kind(dirty)})
        return result
    if _output(dirty):
        result.update({"status": "update_blocked_dirty", "reason": "working_tree_dirty"})
        return result
    branch = str(result["branch"])
    pulled = _run(skill_dir, "pull", "--ff-only", "origin", branch)
    if pulled.returncode != 0:
        result.update({"status": "update_failed", "reason": _error_kind(pulled)})
        return result
    after = _run(skill_dir, "rev-parse", "HEAD")
    result.update({"status": "updated", "before_sha": result.get("local_sha"), "after_sha": _output(after)})
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill-dir", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--update", action="store_true", help="仅在用户已明确同意后执行快进更新")
    args = parser.parse_args()
    result = update(args.skill_dir) if args.update else check(args.skill_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") not in {"check_failed", "update_failed"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
