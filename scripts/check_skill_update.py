#!/usr/bin/env python3
"""Check, and only after explicit approval pull, a GitHub skill update.

The command supports both a Git checkout and a ZIP-installed skill package.
ZIP packages do not contain ``.git``; they use ``skill-update.json`` plus a
shallow clone of the configured remote for content comparison. The calling
AI must ask the user before passing ``--update``; this script never auto-updates
a skill.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any


DEFAULT_REPOSITORY = "https://github.com/weiweidounai0131/biaoshu-master.git"
DEFAULT_BRANCH = "main"
UPDATE_CONFIG_NAME = "skill-update.json"
IGNORED_PACKAGE_DIRS = {".git", "__pycache__", "evals", "bid_delivery", "outputs"}
IGNORED_PACKAGE_FILES = {".DS_Store"}


def _run(skill_dir: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    command = ["git", "-C", str(skill_dir), *arguments]
    try:
        return subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        return subprocess.CompletedProcess(command, 127, "", str(exc))


def _run_git(*arguments: str) -> subprocess.CompletedProcess[str]:
    command = ["git", *arguments]
    try:
        return subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        return subprocess.CompletedProcess(command, 127, "", str(exc))


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


def _load_update_config(skill_dir: Path) -> tuple[str, str]:
    """Load the remote used by ZIP installs, with a safe built-in fallback."""
    path = skill_dir / UPDATE_CONFIG_NAME
    if not path.is_file():
        return DEFAULT_REPOSITORY, DEFAULT_BRANCH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return DEFAULT_REPOSITORY, DEFAULT_BRANCH
    if not isinstance(data, dict):
        return DEFAULT_REPOSITORY, DEFAULT_BRANCH
    repository = str(data.get("repository") or data.get("remote_url") or DEFAULT_REPOSITORY).strip()
    branch = str(data.get("branch") or DEFAULT_BRANCH).strip() or DEFAULT_BRANCH
    return repository or DEFAULT_REPOSITORY, branch


def _package_files(root: Path) -> list[tuple[str, Path]]:
    files: list[tuple[str, Path]] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.name in IGNORED_PACKAGE_FILES:
            continue
        relative = path.relative_to(root)
        if any(part in IGNORED_PACKAGE_DIRS for part in relative.parts):
            continue
        files.append((relative.as_posix(), path))
    return sorted(files, key=lambda item: item[0])


def _package_sha256(root: Path) -> str:
    """Return a deterministic package digest without requiring Git metadata."""
    digest = hashlib.sha256()
    for relative, path in _package_files(root):
        content_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content_digest.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _remote_head(repository: str, branch: str) -> tuple[str, subprocess.CompletedProcess[str]]:
    result = _run_git("ls-remote", repository, f"refs/heads/{branch}")
    line = next((line for line in _output(result).splitlines() if line.strip()), "")
    remote_sha = line.split()[0] if line else ""
    return remote_sha, result


def _clone_remote(repository: str, branch: str):
    holder = tempfile.TemporaryDirectory(prefix="biaoshu-master-update-")
    target = Path(holder.name) / "package"
    result = _run_git("clone", "--depth", "1", "--no-tags", "--branch", branch, repository, str(target))
    if result.returncode != 0 or not target.is_dir():
        holder.cleanup()
        return None, None, result
    return holder, target, result


def _package_check(skill_dir: Path) -> dict[str, Any]:
    """Check a ZIP install by comparing its files with the remote branch."""
    repository, branch = _load_update_config(skill_dir)
    result = _base(skill_dir)
    result.update(
        {
            "remote_provider": "github" if re.search(r"github\.com", repository, re.IGNORECASE) else "git",
            "repository": repository,
            "branch": branch,
            "install_type": "package",
            "local_package_sha256": _package_sha256(skill_dir),
        }
    )
    remote_sha, remote_result = _remote_head(repository, branch)
    if not remote_sha:
        result.update({"status": "check_failed", "reason": _error_kind(remote_result)})
        return result
    holder, remote_dir, clone_result = _clone_remote(repository, branch)
    if not holder or not remote_dir:
        result.update({"status": "check_failed", "reason": _error_kind(clone_result or remote_result)})
        return result
    try:
        remote_package_sha = _package_sha256(remote_dir)
    finally:
        holder.cleanup()
    result.update({"remote_sha": remote_sha, "remote_package_sha256": remote_package_sha})
    result["status"] = "up_to_date" if result["local_package_sha256"] == remote_package_sha else "update_available"
    return result


def check(skill_dir: Path) -> dict[str, Any]:
    skill_dir = skill_dir.expanduser().resolve()
    probe = _run(skill_dir, "rev-parse", "--is-inside-work-tree")
    if probe.returncode == 0 and _output(probe) == "true":
        result = _base(skill_dir)
        remote = _run(skill_dir, "remote", "get-url", "origin")
        if remote.returncode != 0 or not _output(remote):
            result.update({"status": "unavailable", "reason": "origin_not_configured"})
            return result
        remote_url = _output(remote)
        result["remote_provider"] = "github" if re.search(r"github\.com", remote_url, re.IGNORECASE) else "git"
        result["repository"] = remote_url

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
        remote_sha, remote_result = _remote_head(remote_url, branch)
        if not remote_sha:
            result.update({"status": "check_failed", "reason": _error_kind(remote_result), "branch": branch})
            return result
        result.update({"branch": branch, "local_sha": local_sha, "remote_sha": remote_sha, "install_type": "git"})
        result["status"] = "up_to_date" if local_sha == remote_sha else "update_available"
        return result

    return _package_check(skill_dir)


def _ignore_package(_directory: str, names: list[str]) -> list[str]:
    return [name for name in names if name in IGNORED_PACKAGE_DIRS or name in IGNORED_PACKAGE_FILES]


def _replace_package(skill_dir: Path, remote_dir: Path) -> None:
    """Replace a ZIP install with a validated remote package, with rollback."""
    backup_root = Path(tempfile.mkdtemp(prefix="biaoshu-master-backup-", dir=str(skill_dir.parent)))
    backup = backup_root / skill_dir.name
    moved = False
    try:
        skill_dir.rename(backup)
        moved = True
        shutil.copytree(remote_dir, skill_dir, ignore=_ignore_package)
    except Exception:
        if skill_dir.exists():
            shutil.rmtree(skill_dir)
        if moved and backup.exists():
            backup.rename(skill_dir)
        raise
    finally:
        shutil.rmtree(backup_root, ignore_errors=True)


def update(skill_dir: Path) -> dict[str, Any]:
    result = check(skill_dir)
    result["update_requested"] = True
    if result.get("install_type") == "git":
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

    if result.get("status") != "update_available":
        return result
    repository, branch = _load_update_config(skill_dir)
    holder, remote_dir, clone_result = _clone_remote(repository, branch)
    if not holder or not remote_dir:
        result.update({"status": "update_failed", "reason": _error_kind(clone_result or subprocess.CompletedProcess([], 1, "", ""))})
        return result
    try:
        required = (remote_dir / "SKILL.md", remote_dir / "scripts" / "check_skill_update.py")
        if not all(path.is_file() for path in required):
            result.update({"status": "update_failed", "reason": "remote_package_invalid"})
            return result
        _replace_package(skill_dir, remote_dir)
        result.update(
            {
                "status": "updated",
                "before_package_sha256": result.get("local_package_sha256"),
                "after_package_sha256": _package_sha256(skill_dir),
                "after_sha": result.get("remote_sha"),
            }
        )
        return result
    except (OSError, shutil.Error, ValueError) as exc:
        result.update({"status": "update_failed", "reason": "package_replace_failed", "detail": str(exc)})
        return result
    finally:
        holder.cleanup()


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
