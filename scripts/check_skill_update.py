#!/usr/bin/env python3
"""Check for a public biaoshu-master version and optionally update the package.

The check is deliberately based on a small semantic-version manifest rather
than on Git metadata. This keeps the same behavior for Git checkouts and ZIP
installations, including packages installed by hosts that do not preserve a
``.git`` directory. The script never updates automatically: ``--update`` is
only for an explicit user-approved update.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Optional
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request


DEFAULT_REPOSITORY = "https://github.com/weiweidounai0131/biaoshu-master.git"
DEFAULT_BRANCH = "main"
DEFAULT_VERSION_FILE = "skill-version.json"
UPDATE_CONFIG_NAME = "skill-update.json"
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_MEMBER_BYTES = 64 * 1024 * 1024

IGNORED_PACKAGE_DIRS = {".git", "__pycache__", "evals", "bid_delivery", "outputs"}
IGNORED_PACKAGE_FILES = {".DS_Store"}
PRESERVED_LOCAL_DIRS = {"evals", "bid_delivery", "outputs"}

_SEMVER_RE = re.compile(r"^[vV]?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-+].*)?$")


def _run(skill_dir: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    command = ["git", "-C", str(skill_dir), *arguments]
    try:
        return subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        return subprocess.CompletedProcess(command, 127, "", str(exc))


def _output(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stdout or "").strip()


def _base(skill_dir: Path) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "biaoshu_master_skill_update_check",
        "skill_dir": str(skill_dir),
        "update_requested": False,
    }


def _safe_relative_path(value: Any, fallback: str) -> str:
    candidate = str(value or fallback).strip().replace("\\", "/")
    path = PurePosixPath(candidate)
    if not candidate or path.is_absolute() or ".." in path.parts:
        return fallback
    return path.as_posix()


def _github_raw_url(repository: str, branch: str, relative_path: str) -> Optional[str]:
    match = re.match(
        r"^https?://github\.com/([^/]+)/([^/#]+?)(?:\.git)?/?$",
        repository,
        re.IGNORECASE,
    )
    if not match:
        return None
    owner, repo = match.groups()
    encoded_branch = urllib_parse.quote(branch, safe="")
    encoded_path = urllib_parse.quote(relative_path, safe="/")
    return "https://raw.githubusercontent.com/{}/{}/{}/{}".format(
        owner,
        repo,
        encoded_branch,
        encoded_path,
    )


def _github_archive_url(repository: str, branch: str) -> Optional[str]:
    match = re.match(
        r"^https?://github\.com/([^/]+)/([^/#]+?)(?:\.git)?/?$",
        repository,
        re.IGNORECASE,
    )
    if not match:
        return None
    owner, repo = match.groups()
    encoded_branch = urllib_parse.quote(branch, safe="")
    return "https://github.com/{}/{}/archive/refs/heads/{}.zip".format(owner, repo, encoded_branch)


def _load_update_config(skill_dir: Path) -> dict[str, Any]:
    """Read public update metadata and retain safe defaults for old packages."""
    data: dict[str, Any] = {}
    path = skill_dir / UPDATE_CONFIG_NAME
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError, TypeError):
            data = {}

    repository = str(data.get("repository") or data.get("remote_url") or DEFAULT_REPOSITORY).strip()
    branch = str(data.get("branch") or DEFAULT_BRANCH).strip() or DEFAULT_BRANCH
    version_file = _safe_relative_path(data.get("version_file"), DEFAULT_VERSION_FILE)

    urls: list[str] = []
    configured_urls = data.get("version_urls")
    if isinstance(configured_urls, list):
        for value in configured_urls:
            url = str(value or "").strip()
            if url.startswith("https://") and url not in urls:
                urls.append(url)
    if not urls:
        derived = _github_raw_url(repository, branch, version_file)
        if derived:
            urls.append(derived)

    download_url = str(data.get("download_url") or "").strip()
    if not download_url:
        download_url = _github_archive_url(repository, branch) or ""

    return {
        "schema_version": data.get("schema_version", 1),
        "name": str(data.get("name") or "biaoshu-master").strip(),
        "repository": repository or DEFAULT_REPOSITORY,
        "branch": branch,
        "version_file": version_file,
        "version_urls": urls,
        "download_url": download_url,
    }


def _parse_version(value: Any) -> Optional[tuple[int, int, int]]:
    match = _SEMVER_RE.match(str(value or "").strip())
    if not match:
        return None
    return tuple(int(part or 0) for part in match.groups())


def _read_manifest_version(path: Path) -> Optional[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    version = str(data.get("version") or data.get("latest_version") or "").strip()
    return version if _parse_version(version) else None


def _local_version(skill_dir: Path, config: dict[str, Any]) -> tuple[str, str, Optional[str]]:
    version_file = skill_dir / str(config["version_file"])
    if version_file.is_file():
        version = _read_manifest_version(version_file)
        if version:
            return version, str(version_file), None
        return "", str(version_file), "local_version_invalid"

    package_file = skill_dir / "package.json"
    if package_file.is_file():
        version = _read_manifest_version(package_file)
        if version:
            return version, str(package_file), None
        return "", str(package_file), "local_version_invalid"

    # Packages created before the version manifest existed can still bootstrap
    # from the public latest package; 0.0.0 makes that intent explicit.
    return "0.0.0", "legacy_default", None


def _fetch_json(url: str) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    request = urllib_request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "biaoshu-master-update-check/1",
        },
    )
    try:
        with urllib_request.urlopen(request, timeout=10) as response:
            payload = response.read(MAX_MANIFEST_BYTES + 1)
        if len(payload) > MAX_MANIFEST_BYTES:
            return None, "manifest_too_large"
        data = json.loads(payload.decode("utf-8"))
        if not isinstance(data, dict):
            return None, "manifest_not_object"
        return data, None
    except urllib_error.HTTPError as exc:
        return None, "http_{}".format(exc.code)
    except urllib_error.URLError:
        return None, "network_unreachable"
    except (OSError, TimeoutError, UnicodeError, ValueError):
        return None, "manifest_read_failed"


def _fetch_remote_version(urls: list[str]) -> tuple[Optional[str], Optional[str], list[dict[str, str]]]:
    errors: list[dict[str, str]] = []
    for url in urls:
        data, reason = _fetch_json(url)
        if data is None:
            errors.append({"url": url, "reason": reason or "manifest_read_failed"})
            continue
        version = str(data.get("version") or data.get("latest_version") or "").strip()
        if not _parse_version(version):
            errors.append({"url": url, "reason": "remote_version_invalid"})
            continue
        return version, url, errors
    return None, None, errors


def _install_type(skill_dir: Path) -> str:
    probe = _run(skill_dir, "rev-parse", "--is-inside-work-tree")
    return "git" if probe.returncode == 0 and _output(probe) == "true" else "package"


def check(skill_dir: Path) -> dict[str, Any]:
    skill_dir = skill_dir.expanduser().resolve()
    config = _load_update_config(skill_dir)
    result = _base(skill_dir)
    result.update(
        {
            "name": config["name"],
            "install_type": _install_type(skill_dir),
            "repository": config["repository"],
            "branch": config["branch"],
            "version_file": config["version_file"],
            "version_urls": config["version_urls"],
        }
    )

    local_version, local_source, local_error = _local_version(skill_dir, config)
    result.update({"local_version": local_version or None, "local_version_source": local_source})
    if local_error:
        result.update({"status": "check_failed", "reason": local_error})
        return result

    remote_version, version_url, fetch_errors = _fetch_remote_version(config["version_urls"])
    if not remote_version or not version_url:
        result.update(
            {
                "status": "check_failed",
                "reason": "remote_version_unavailable",
                "attempts": fetch_errors,
            }
        )
        return result

    local_parsed = _parse_version(local_version)
    remote_parsed = _parse_version(remote_version)
    if not local_parsed or not remote_parsed:
        result.update({"status": "check_failed", "reason": "version_compare_failed"})
        return result

    if remote_parsed > local_parsed:
        relation = "remote_newer"
        status = "update_available"
    elif remote_parsed == local_parsed:
        relation = "same"
        status = "up_to_date"
    else:
        relation = "local_newer"
        status = "up_to_date"
    result.update(
        {
            "status": status,
            "version_relation": relation,
            "remote_version": remote_version,
            "version_url": version_url,
        }
    )
    return result


def _error_kind(result: subprocess.CompletedProcess[str]) -> str:
    text = ((result.stderr or "") + " " + (result.stdout or "")).lower()
    if "could not resolve host" in text or "network is unreachable" in text or "failed to connect" in text:
        return "network_unreachable"
    if "authentication" in text or "permission denied" in text or "repository not found" in text:
        return "remote_authentication_failed"
    return "git_command_failed"


def _download_bytes(url: str) -> bytes:
    if not url.startswith("https://"):
        raise ValueError("download_url_invalid")
    request = urllib_request.Request(
        url,
        headers={
            "Accept": "application/zip, application/octet-stream",
            "User-Agent": "biaoshu-master-updater/1",
        },
    )
    with urllib_request.urlopen(request, timeout=30) as response:
        payload = response.read(MAX_ARCHIVE_BYTES + 1)
    if len(payload) > MAX_ARCHIVE_BYTES:
        raise ValueError("archive_too_large")
    return payload


def _safe_extract(archive: zipfile.ZipFile, destination: Path) -> None:
    root = destination.resolve()
    for member in archive.infolist():
        raw_name = member.filename.replace("\\", "/")
        path = PurePosixPath(raw_name)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("archive_path_invalid")
        if not raw_name or raw_name == ".":
            continue
        mode = (member.external_attr >> 16) & 0o170000
        if mode == stat.S_IFLNK:
            raise ValueError("archive_symlink_not_allowed")
        if member.file_size > MAX_ARCHIVE_MEMBER_BYTES:
            raise ValueError("archive_member_too_large")

        target = (destination.joinpath(*path.parts)).resolve()
        if os.path.commonpath([str(root), str(target)]) != str(root):
            raise ValueError("archive_path_invalid")
        if raw_name.endswith("/"):
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(member, "r") as source, target.open("wb") as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)


def _find_package_root(extracted: Path) -> Path:
    direct = extracted / "SKILL.md"
    if direct.is_file():
        return extracted
    candidates = [path.parent for path in extracted.rglob("SKILL.md") if path.is_file()]
    if len(candidates) != 1:
        raise ValueError("remote_package_root_invalid")
    return candidates[0]


def _ignore_package(_directory: str, names: list[str]) -> list[str]:
    return [name for name in names if name in IGNORED_PACKAGE_DIRS or name in IGNORED_PACKAGE_FILES]


def _copy_preserved_path(source: Path, destination: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, destination)
    elif source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _replace_package(skill_dir: Path, remote_dir: Path) -> None:
    """Replace a ZIP package with rollback and preservation of local runtime data."""
    backup_root = Path(tempfile.mkdtemp(prefix="biaoshu-master-backup-", dir=str(skill_dir.parent)))
    backup = backup_root / skill_dir.name
    moved = False
    try:
        skill_dir.rename(backup)
        moved = True
        shutil.copytree(remote_dir, skill_dir, ignore=_ignore_package)
        for name in sorted(PRESERVED_LOCAL_DIRS):
            source = backup / name
            destination = skill_dir / name
            if source.exists() and not destination.exists():
                _copy_preserved_path(source, destination)
    except Exception:
        if skill_dir.exists():
            shutil.rmtree(skill_dir)
        if moved and backup.exists():
            backup.rename(skill_dir)
        raise
    finally:
        shutil.rmtree(backup_root, ignore_errors=True)


def _update_package(skill_dir: Path, config: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    download_url = str(config.get("download_url") or "").strip()
    if not download_url:
        result.update({"status": "update_failed", "reason": "download_url_missing"})
        return result
    result["download_url"] = download_url

    archive_bytes = _download_bytes(download_url)
    with tempfile.TemporaryDirectory(prefix="biaoshu-master-update-", dir=str(skill_dir.parent)) as holder:
        extracted = Path(holder) / "extracted"
        extracted.mkdir()
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            _safe_extract(archive, extracted)
        remote_dir = _find_package_root(extracted)
        required = (
            remote_dir / "SKILL.md",
            remote_dir / "scripts" / "check_skill_update.py",
            remote_dir / str(config["version_file"]),
        )
        if not all(path.is_file() for path in required):
            result.update({"status": "update_failed", "reason": "remote_package_invalid"})
            return result
        _replace_package(skill_dir, remote_dir)

    after_version, _source, _error = _local_version(skill_dir, config)
    result.update(
        {
            "status": "updated",
            "before_version": result.get("local_version"),
            "after_version": after_version or None,
        }
    )
    return result


def update(skill_dir: Path) -> dict[str, Any]:
    skill_dir = skill_dir.expanduser().resolve()
    result = check(skill_dir)
    result["update_requested"] = True
    if result.get("status") != "update_available":
        return result

    if result.get("install_type") == "git":
        dirty = _run(skill_dir, "status", "--porcelain")
        if dirty.returncode != 0:
            result.update({"status": "update_failed", "reason": _error_kind(dirty)})
            return result
        if _output(dirty):
            result.update({"status": "update_blocked_dirty", "reason": "working_tree_dirty"})
            return result
        branch = str(result["branch"])
        pulled = _run(skill_dir, "pull", "--ff-only", "origin", branch)
        if pulled.returncode != 0:
            result.update({"status": "update_failed", "reason": _error_kind(pulled)})
            return result
        after_version, _source, _error = _local_version(skill_dir, _load_update_config(skill_dir))
        result.update(
            {
                "status": "updated",
                "before_version": result.get("local_version"),
                "after_version": after_version or None,
            }
        )
        return result

    config = _load_update_config(skill_dir)
    try:
        return _update_package(skill_dir, config, result)
    except (OSError, ValueError, urllib_error.URLError, zipfile.BadZipFile, shutil.Error) as exc:
        result.update({"status": "update_failed", "reason": str(exc) or "package_update_failed"})
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill-dir", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--update", action="store_true", help="仅在用户已明确同意后执行更新")
    args = parser.parse_args()
    result = update(args.skill_dir) if args.update else check(args.skill_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") not in {"check_failed", "update_failed"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
