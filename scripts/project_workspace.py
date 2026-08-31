#!/usr/bin/env python3
"""Create, find, and register persistent biaoshu-master project workspaces.

The workspace manager keeps project identity separate from the skill package.
It never copies tender materials; it only records the local paths supplied by
the caller and creates the project state directory used by the workflow.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import tempfile
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = 1
INDEX_FILENAME = "project-index.json"
LOCK_FILENAME = ".project-index.lock"
METADATA_FILENAME = ".biaoshu-project.json"
DEFAULT_ROOT_NAME = "biaoshu-master-projects"
INTAKE_DIR_NAME = "bid_confirm_ui"
INTAKE_FILENAME = "intake-recommendations.json"


class WorkspaceResolutionError(ValueError):
    """An actionable workspace resolution failure."""

    def __init__(self, message: str, *, candidates: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.candidates = candidates or []


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def default_root() -> Path:
    configured = os.environ.get("BIAOSHU_PROJECTS_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / "Documents" / DEFAULT_ROOT_NAME).resolve()


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(text.strip().split()).casefold()


def display_text(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).strip().split())


def safe_slug(value: Any) -> str:
    text = display_text(value)
    text = re.sub(r"[\\/]+", "-", text)
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"[^0-9A-Za-z\u3400-\u9fff._-]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip(" ._-")
    return (text[:64].rstrip(" ._-") or "未命名项目")


def canonical_path(value: Any) -> str:
    return str(Path(str(value)).expanduser().resolve())


def unique_paths(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        path = canonical_path(value)
        if path not in result:
            result.append(path)
    return result


def background_fingerprint(paths: list[str]) -> str:
    canonical = sorted(unique_paths(paths))
    if not canonical:
        return ""
    return hashlib.sha256(json.dumps(canonical, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def project_key(
    project_name: str = "",
    client: str = "",
    tender_reference: str = "",
    explicit_key: str = "",
    background_paths: list[str] | None = None,
) -> tuple[str | None, str]:
    """Return a stable logical key and its source label.

    Explicit keys are authoritative. Otherwise the normalized project name,
    client, and tender reference identify a project. If those fields are not
    available, the authoritative background-path set is the safest fallback;
    completely anonymous calls intentionally create a new workspace.
    """

    if display_text(explicit_key):
        return f"explicit:{normalize_text(explicit_key)}", "explicit"
    fields = [normalize_text(project_name), normalize_text(client), normalize_text(tender_reference)]
    if any(fields):
        return "identity:" + json.dumps(fields, ensure_ascii=False, separators=(",", ":")), "identity"
    fingerprint = background_fingerprint(background_paths or [])
    if fingerprint:
        return f"background:{fingerprint}", "background"
    return None, "anonymous"


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary_name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary_name)


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


@contextlib.contextmanager
def index_lock(root: Path) -> Iterator[None]:
    """Serialize registry updates where the host provides file locking."""

    root.mkdir(parents=True, exist_ok=True)
    path = root / LOCK_FILENAME
    with path.open("a+", encoding="utf-8") as handle:
        lock_module = None
        try:
            import fcntl as lock_module  # type: ignore[import-not-found]
        except ImportError:
            lock_module = None
        if lock_module is not None:
            lock_module.flock(handle.fileno(), lock_module.LOCK_EX)
        try:
            yield
        finally:
            if lock_module is not None:
                lock_module.flock(handle.fileno(), lock_module.LOCK_UN)


def empty_index() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "kind": "biaoshu_project_index", "updated_at": now(), "projects": []}


def load_index(root: Path) -> dict[str, Any]:
    path = root / INDEX_FILENAME
    if not path.exists():
        return empty_index()
    data = read_json(path)
    if data.get("schema_version") != SCHEMA_VERSION or data.get("kind") != "biaoshu_project_index":
        raise ValueError(f"项目索引版本不支持：{path}")
    if not isinstance(data.get("projects"), list):
        raise ValueError(f"项目索引 projects 字段无效：{path}")
    return data


def save_index(root: Path, index: dict[str, Any]) -> None:
    index["updated_at"] = now()
    atomic_write_json(root / INDEX_FILENAME, index)


def metadata_path(project_dir: Path) -> Path:
    return project_dir / METADATA_FILENAME


def load_metadata(project_dir: Path) -> dict[str, Any] | None:
    path = metadata_path(project_dir)
    if not path.exists():
        return None
    data = read_json(path)
    if data.get("schema_version") != SCHEMA_VERSION or data.get("kind") != "biaoshu_project_workspace":
        raise ValueError(f"项目工作区元数据版本不支持：{path}")
    if not isinstance(data.get("project_id"), str) or not data["project_id"].strip():
        raise ValueError(f"项目工作区元数据缺少 project_id：{path}")
    return data


def read_legacy_project_id(project_dir: Path) -> str | None:
    path = project_dir / INTAKE_DIR_NAME / INTAKE_FILENAME
    if not path.is_file():
        return None
    try:
        data = read_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    value = data.get("project_id")
    return value.strip() if isinstance(value, str) and value.strip() else None


def project_id_for(project_dir: Path) -> str | None:
    metadata = load_metadata(project_dir)
    if metadata:
        return str(metadata["project_id"])
    return read_legacy_project_id(project_dir)


def write_metadata(project_dir: Path, metadata: dict[str, Any]) -> None:
    atomic_write_json(metadata_path(project_dir), metadata)


def candidate_view(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "project_id": entry.get("project_id"),
        "project_name": entry.get("project_name", ""),
        "client": entry.get("client", ""),
        "project_dir": entry.get("workspace_path", ""),
        "updated_at": entry.get("updated_at", ""),
    }


def _replace_entry(index: dict[str, Any], entry: dict[str, Any]) -> None:
    projects = index["projects"]
    for position, existing in enumerate(projects):
        if existing.get("project_id") == entry.get("project_id") or existing.get("workspace_path") == entry.get("workspace_path"):
            projects[position] = entry
            return
    projects.append(entry)


def _metadata_from_entry(entry: dict[str, Any], *, registry_path: Path) -> dict[str, Any]:
    metadata = dict(entry)
    metadata["kind"] = "biaoshu_project_workspace"
    metadata["schema_version"] = SCHEMA_VERSION
    metadata["registry_path"] = str(registry_path)
    return metadata


def _new_entry(
    project_dir: Path,
    project_name: str,
    client: str,
    tender_reference: str,
    key: str | None,
    key_source: str,
    paths: list[str],
    *,
    project_id: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    timestamp = created_at or now()
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "biaoshu_project_workspace",
        "project_id": project_id or uuid.uuid4().hex,
        "project_key": key,
        "project_key_source": key_source,
        "project_name": display_text(project_name),
        "client": display_text(client),
        "tender_reference": display_text(tender_reference),
        "background_paths": unique_paths(paths),
        "background_fingerprint": background_fingerprint(paths),
        "workspace_path": str(project_dir),
        "created_at": timestamp,
        "updated_at": timestamp,
        "last_run_id": None,
        "last_run_at": None,
    }


def _merge_entry(
    entry: dict[str, Any],
    *,
    project_name: str,
    client: str,
    tender_reference: str,
    paths: list[str],
    key: str | None,
    key_source: str,
) -> dict[str, Any]:
    updated = dict(entry)
    if display_text(project_name):
        updated["project_name"] = display_text(project_name)
    if display_text(client):
        updated["client"] = display_text(client)
    if display_text(tender_reference):
        updated["tender_reference"] = display_text(tender_reference)
    if key is not None:
        updated["project_key"] = key
        updated["project_key_source"] = key_source
    if paths:
        updated["background_paths"] = unique_paths(paths)
        updated["background_fingerprint"] = background_fingerprint(paths)
    updated["updated_at"] = now()
    return updated


def _path_entry(index: dict[str, Any], project_dir: Path) -> dict[str, Any] | None:
    target = str(project_dir.resolve())
    for entry in index["projects"]:
        if str(entry.get("workspace_path", "")).strip() == target:
            return entry
    return None


def _matching_entries(index: dict[str, Any], key: str | None, fingerprint: str, key_source: str) -> list[dict[str, Any]]:
    if key is None:
        return []
    candidates = [entry for entry in index["projects"] if entry.get("project_key") == key]
    if not candidates:
        return []
    if key_source == "explicit" or not fingerprint:
        return candidates
    exact = [entry for entry in candidates if entry.get("background_fingerprint") == fingerprint]
    if exact:
        return exact
    # A project that was first registered before its materials were supplied
    # can safely gain its first fingerprint. A non-empty mismatch is a new
    # project, even when the display name happens to be the same.
    return [entry for entry in candidates if not entry.get("background_fingerprint")]


def _create_directory(root: Path, slug: str, project_id: str) -> tuple[Path, str]:
    root.mkdir(parents=True, exist_ok=True)
    for _ in range(5):
        candidate = root / f"{slug}--{project_id[:12]}"
        try:
            candidate.mkdir(parents=False, exist_ok=False)
            return candidate.resolve(), project_id
        except FileExistsError:
            project_id = uuid.uuid4().hex
    raise WorkspaceResolutionError("无法创建不冲突的项目工作区目录")


def resolve_workspace(
    *,
    project_name: str = "",
    client: str = "",
    tender_reference: str = "",
    explicit_key: str = "",
    background_paths: list[str] | None = None,
    root: Path | None = None,
    project_dir: Path | None = None,
    force_new: bool = False,
) -> dict[str, Any]:
    """Resolve a workspace, creating it only when no safe match exists."""

    root_path = (root or default_root()).expanduser().resolve()
    paths = unique_paths(background_paths or [])
    key, key_source = project_key(project_name, client, tender_reference, explicit_key, paths)
    fingerprint = background_fingerprint(paths)

    if project_dir is not None:
        target = project_dir.expanduser().resolve()
        target_exists = target.exists()
        target.mkdir(parents=True, exist_ok=True)
        with index_lock(root_path):
            index = load_index(root_path)
            existing = _path_entry(index, target)
            metadata = load_metadata(target)
            legacy_id = read_legacy_project_id(target)
            existing_id = (metadata or existing or {}).get("project_id") or legacy_id
            if metadata and key is not None and metadata.get("project_key") not in {None, key}:
                raise WorkspaceResolutionError("指定项目目录已经绑定其他项目标识，请使用正确目录或显式新建项目")
            if existing:
                entry = _merge_entry(
                    existing,
                    project_name=project_name or str(existing.get("project_name", "")),
                    client=client or str(existing.get("client", "")),
                    tender_reference=tender_reference or str(existing.get("tender_reference", "")),
                    paths=paths,
                    key=key or existing.get("project_key"),
                    key_source=key_source if key is not None else str(existing.get("project_key_source", "identity")),
                )
            elif metadata:
                entry = _merge_entry(
                    metadata,
                    project_name=project_name,
                    client=client,
                    tender_reference=tender_reference,
                    paths=paths,
                    key=key or metadata.get("project_key"),
                    key_source=key_source if key is not None else str(metadata.get("project_key_source", "identity")),
                )
            else:
                entry = _new_entry(
                    target,
                    project_name,
                    client,
                    tender_reference,
                    key or f"workspace:{hashlib.sha256(str(target).encode('utf-8')).hexdigest()}",
                    key_source if key is not None else "explicit-path",
                    paths,
                    project_id=str(existing_id) if existing_id else None,
                    created_at=(metadata or {}).get("created_at") if metadata else None,
                )
            entry["workspace_path"] = str(target)
            write_metadata(target, _metadata_from_entry(entry, registry_path=root_path / INDEX_FILENAME))
            _replace_entry(index, entry)
            save_index(root_path, index)
        return _resolution(entry, root_path, created=not target_exists, reused=target_exists, reason="explicit_project_dir")

    if not force_new and key is not None:
        with index_lock(root_path):
            index = load_index(root_path)
            matches = _matching_entries(index, key, fingerprint, key_source)
            existing = [entry for entry in matches if Path(str(entry.get("workspace_path", ""))).expanduser().resolve().is_dir()]
            if len(existing) > 1:
                raise WorkspaceResolutionError(
                    "找到多个可能的同名项目，无法安全自动复用；请提供 --project-key 或明确的 --project-dir",
                    candidates=[candidate_view(entry) for entry in existing],
                )
            if len(existing) == 1:
                entry = _merge_entry(
                    existing[0],
                    project_name=project_name,
                    client=client,
                    tender_reference=tender_reference,
                    paths=paths,
                    key=key,
                    key_source=key_source,
                )
                target = Path(str(entry["workspace_path"])).expanduser().resolve()
                target.mkdir(parents=True, exist_ok=True)
                write_metadata(target, _metadata_from_entry(entry, registry_path=root_path / INDEX_FILENAME))
                _replace_entry(index, entry)
                save_index(root_path, index)
                return _resolution(entry, root_path, created=False, reused=True, reason="matched_existing_project")

            project_id = uuid.uuid4().hex
            target, project_id = _create_directory(root_path, safe_slug(project_name or client or "未命名项目"), project_id)
            entry = _new_entry(target, project_name, client, tender_reference, key, key_source, paths, project_id=project_id)
            write_metadata(target, _metadata_from_entry(entry, registry_path=root_path / INDEX_FILENAME))
            _replace_entry(index, entry)
            save_index(root_path, index)
            return _resolution(entry, root_path, created=True, reused=False, reason="created_new_project")

    with index_lock(root_path):
        index = load_index(root_path)
        project_id = uuid.uuid4().hex
        target, project_id = _create_directory(root_path, safe_slug(project_name or client or "未命名项目"), project_id)
        entry = _new_entry(target, project_name, client, tender_reference, key, key_source, paths, project_id=project_id)
        write_metadata(target, _metadata_from_entry(entry, registry_path=root_path / INDEX_FILENAME))
        _replace_entry(index, entry)
        save_index(root_path, index)
    return _resolution(entry, root_path, created=True, reused=False, reason="forced_new_project" if force_new else "anonymous_project")


def _resolution(entry: dict[str, Any], root: Path, *, created: bool, reused: bool, reason: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "biaoshu_project_workspace_resolution",
        "project_id": entry["project_id"],
        "project_key": entry.get("project_key"),
        "project_name": entry.get("project_name", ""),
        "client": entry.get("client", ""),
        "project_dir": entry["workspace_path"],
        "workspace_root": str(root),
        "metadata_path": str(Path(entry["workspace_path"]) / METADATA_FILENAME),
        "registry_path": str(root / INDEX_FILENAME),
        "created": created,
        "reused": reused,
        "reason": reason,
        "background_fingerprint": entry.get("background_fingerprint", ""),
    }


def record_run(project_dir: Path, run_id: str, project_id: str | None = None) -> bool:
    """Record the latest intake run without changing the project identity."""

    target = project_dir.expanduser().resolve()
    metadata = load_metadata(target)
    if not metadata:
        return False
    if project_id and metadata.get("project_id") != project_id:
        raise WorkspaceResolutionError("入口 project_id 与项目工作区身份不一致")
    timestamp = now()
    metadata["last_run_id"] = run_id
    metadata["last_run_at"] = timestamp
    metadata["updated_at"] = timestamp
    write_metadata(target, metadata)
    registry = metadata.get("registry_path")
    if isinstance(registry, str) and registry.strip():
        registry_path = Path(registry).expanduser().resolve()
        root = registry_path.parent
        with index_lock(root):
            index = load_index(root)
            entry = _path_entry(index, target)
            if entry:
                entry.update({"last_run_id": run_id, "last_run_at": timestamp, "updated_at": timestamp})
                _replace_entry(index, entry)
                save_index(root, index)
    return True


def list_workspaces(root: Path | None = None) -> dict[str, Any]:
    root_path = (root or default_root()).expanduser().resolve()
    with index_lock(root_path):
        index = load_index(root_path)
    projects = sorted(index["projects"], key=lambda item: str(item.get("updated_at", "")), reverse=True)
    views = []
    for item in projects:
        view = candidate_view(item)
        view["project_key"] = item.get("project_key")
        views.append(view)
    return {"schema_version": SCHEMA_VERSION, "kind": "biaoshu_project_workspace_list", "workspace_root": str(root_path), "projects": views}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="管理 biaoshu-master 的持久化项目工作区")
    commands = parser.add_subparsers(dest="command", required=True)

    resolve_parser = commands.add_parser("resolve", help="查找或创建项目工作区")
    resolve_parser.add_argument("--project-name", default="", help="项目名称")
    resolve_parser.add_argument("--client", default="", help="招标人/客户名称")
    resolve_parser.add_argument("--tender-reference", default="", help="招标编号或其他稳定项目标识")
    resolve_parser.add_argument("--project-key", default="", help="用户自定义的稳定项目标识")
    resolve_parser.add_argument("--background-path", action="append", default=[], help="背景资料路径，仅用于项目识别，不复制资料")
    resolve_parser.add_argument("--root", type=Path, help="项目工作区根目录；默认 ~/Documents/biaoshu-master-projects")
    resolve_parser.add_argument("--project-dir", type=Path, help="显式指定已有或待注册的项目目录")
    resolve_parser.add_argument("--new", action="store_true", help="忽略已有匹配并创建一个新的项目工作区")

    list_parser = commands.add_parser("list", help="列出已登记项目")
    list_parser.add_argument("--root", type=Path, help="项目工作区根目录")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "resolve":
            result = resolve_workspace(
                project_name=args.project_name,
                client=args.client,
                tender_reference=args.tender_reference,
                explicit_key=args.project_key,
                background_paths=args.background_path,
                root=args.root,
                project_dir=args.project_dir,
                force_new=args.new,
            )
        else:
            result = list_workspaces(args.root)
    except WorkspaceResolutionError as exc:
        result = {"schema_version": SCHEMA_VERSION, "kind": "biaoshu_project_workspace_error", "ok": False, "error": str(exc), "candidates": exc.candidates}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result = {"schema_version": SCHEMA_VERSION, "kind": "biaoshu_project_workspace_error", "ok": False, "error": str(exc), "candidates": []}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2
    result["ok"] = True
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
