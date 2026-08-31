#!/usr/bin/env python3
"""Manage the local Stage4 bid-writing rule profiles.

Profiles are local, small Markdown overlays.  The immutable default rules in
``references/stage4-writing-rules.md`` remain the base for every profile;
selected overlays are combined with that base when a delivery workspace is
initialized.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path, PurePosixPath
from typing import Any


SCHEMA_VERSION = 1
INDEX_FILENAME = "rule-index.json"
BASE_RULE_PATH = "references/stage4-writing-rules.md"
PROFILE_KINDS = {"default", "preset", "custom"}
INDEX_KEYS = {"schema_version", "kind", "default_profile_id", "profiles"}
PROFILE_KEYS = {"id", "name", "kind", "description", "path", "base_id", "read_only"}
SELECTION_KEYS = {
    "id", "name", "kind", "description", "path", "sha256", "base_id", "base_sha256", "effective_sha256",
}
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")


def skill_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _root(root: Path | None) -> Path:
    return (root or skill_root()).expanduser().resolve()


def _index_path(root: Path) -> Path:
    return root / "rules" / INDEX_FILENAME


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_relative_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}不能为空")
    raw = value.strip()
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or raw.startswith("./"):
        raise ValueError(f"{label}必须是安全的相对路径")
    return raw


def _safe_id(value: Any, label: str = "规则ID") -> str:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value.strip()):
        raise ValueError(f"{label}必须是2至64位小写字母、数字或连字符")
    return value.strip()


def _nonempty(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}不能为空")
    return value.strip()


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, path)


def _read_index(root: Path) -> dict[str, Any]:
    path = _index_path(root)
    if not path.is_file():
        raise ValueError(f"规则索引不存在：{path}")
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ValueError("规则索引不是有效JSON") from exc
    if not isinstance(data, dict) or set(data) != INDEX_KEYS:
        raise ValueError("规则索引字段不完整或包含不支持的字段")
    if data.get("schema_version") != SCHEMA_VERSION or data.get("kind") != "biaoshu_rule_index":
        raise ValueError("规则索引版本不支持")
    default_id = _safe_id(data.get("default_profile_id"), "默认规则ID")
    profiles = data.get("profiles")
    if not isinstance(profiles, list) or not profiles:
        raise ValueError("规则索引至少需要一个规则")
    seen: set[str] = set()
    for profile in profiles:
        if not isinstance(profile, dict) or set(profile) != PROFILE_KEYS:
            raise ValueError("规则索引中的规则字段不完整")
        profile_id = _safe_id(profile.get("id"))
        if profile_id in seen:
            raise ValueError("规则ID重复")
        seen.add(profile_id)
        _nonempty(profile.get("name"), "规则名称")
        kind = profile.get("kind")
        if kind not in PROFILE_KINDS:
            raise ValueError("规则类型不支持")
        _nonempty(profile.get("description"), "规则说明")
        profile_path = _safe_relative_path(profile.get("path"), "规则文件路径")
        if profile.get("base_id") != "default":
            raise ValueError("当前规则只能继承默认规则")
        if not isinstance(profile.get("read_only"), bool):
            raise ValueError("规则只读状态无效")
        if profile_id == "default" and kind != "default":
            raise ValueError("default规则类型无效")
        if profile_id == "default" and profile_path != BASE_RULE_PATH:
            raise ValueError("default规则路径无效")
        if kind == "preset" and not profile_path.startswith("rules/presets/"):
            raise ValueError("预设规则必须位于rules/presets目录")
        if kind == "custom" and not profile_path.startswith("rules/custom/"):
            raise ValueError("自定义规则必须位于rules/custom目录")
    if default_id not in seen:
        raise ValueError("默认规则不存在")
    return data


def _profile_record(root: Path, profile_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    index = _read_index(root)
    profile_id = _safe_id(profile_id)
    profile = next((item for item in index["profiles"] if item["id"] == profile_id), None)
    if profile is None:
        raise ValueError(f"规则不存在：{profile_id}")
    return index, profile


def _resolve_profile_file(root: Path, profile: dict[str, Any]) -> Path:
    relative = _safe_relative_path(profile["path"], "规则文件路径")
    base = root.resolve()
    path = (base / PurePosixPath(relative)).resolve()
    if path != base and base not in path.parents:
        raise ValueError("规则文件路径越出Skill目录")
    if not path.is_file():
        raise ValueError(f"规则文件不存在：{path}")
    return path


def _base_file(root: Path) -> Path:
    path = (root / PurePosixPath(BASE_RULE_PATH)).resolve()
    if not path.is_file():
        raise ValueError(f"默认规则文件不存在：{path}")
    return path


def _selection_from_descriptor(descriptor: dict[str, Any]) -> dict[str, Any]:
    return {key: descriptor[key] for key in SELECTION_KEYS}


def profile_descriptor(profile_id: str, root: Path | None = None) -> dict[str, Any]:
    root = _root(root)
    index, profile = _profile_record(root, profile_id)
    profile_file = _resolve_profile_file(root, profile)
    profile_data = profile_file.read_bytes()
    base_data = _base_file(root).read_bytes()
    profile_sha256 = _sha256_bytes(profile_data)
    base_sha256 = _sha256_bytes(base_data)
    if profile["id"] == "default":
        effective = base_data
    else:
        effective = (
            base_data.rstrip() + b"\n\n---\n\n"
            + f"# 已选择的领域规则覆盖层：{profile['name']}\n\n".encode("utf-8")
            + profile_data.strip() + b"\n"
        )
    return {
        "id": profile["id"],
        "name": profile["name"],
        "kind": profile["kind"],
        "description": profile["description"],
        "path": profile["path"],
        "sha256": profile_sha256,
        "base_id": profile["base_id"],
        "base_sha256": base_sha256,
        "effective_sha256": _sha256_bytes(effective),
        "is_default": index["default_profile_id"] == profile["id"],
        "read_only": profile["read_only"],
    }


def selection_descriptor(profile_id: str, root: Path | None = None) -> dict[str, Any]:
    return _selection_from_descriptor(profile_descriptor(profile_id, root))


def validate_selection(selection: Any, root: Path | None = None) -> dict[str, Any]:
    if not isinstance(selection, dict) or set(selection) != SELECTION_KEYS:
        raise ValueError("生成规则选择字段不完整或包含不支持的字段")
    current = selection_descriptor(selection.get("id"), root)
    for key in SELECTION_KEYS:
        if selection.get(key) != current[key]:
            raise ValueError("生成规则已变化，请刷新Stage4页面后重新选择")
    return current


def default_selection(root: Path | None = None) -> dict[str, Any]:
    root = _root(root)
    index = _read_index(root)
    return selection_descriptor(index["default_profile_id"], root)


def list_profiles(root: Path | None = None) -> dict[str, Any]:
    root = _root(root)
    index = _read_index(root)
    profiles = [profile_descriptor(profile["id"], root) for profile in index["profiles"]]
    return {"schema_version": SCHEMA_VERSION, "kind": "biaoshu_rule_profiles", "default_profile_id": index["default_profile_id"], "profiles": profiles}


def effective_rule_bytes(profile_id: str, root: Path | None = None) -> tuple[bytes, dict[str, Any]]:
    root = _root(root)
    index, profile = _profile_record(root, profile_id)
    descriptor = profile_descriptor(profile_id, root)
    base_data = _base_file(root).read_bytes()
    if profile["id"] == "default":
        effective = base_data
    else:
        overlay = _resolve_profile_file(root, profile).read_bytes()
        effective = (
            base_data.rstrip() + b"\n\n---\n\n"
            + f"# 已选择的领域规则覆盖层：{profile['name']}\n\n".encode("utf-8")
            + overlay.strip() + b"\n"
        )
    if _sha256_bytes(effective) != descriptor["effective_sha256"]:
        raise ValueError("规则有效内容摘要计算不一致")
    return effective, descriptor


def set_default(profile_id: str, root: Path | None = None) -> dict[str, Any]:
    root = _root(root)
    index, _profile = _profile_record(root, profile_id)
    descriptor = profile_descriptor(profile_id, root)
    index["default_profile_id"] = _safe_id(profile_id)
    _atomic_write_json(_index_path(root), index)
    return profile_descriptor(profile_id, root)


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if len(slug) < 2:
        slug = f"custom-rule-{int(time.time())}"
    return slug[:64].rstrip("-")


def register_profile(source: Path, name: str, description: str, profile_id: str | None = None, root: Path | None = None) -> dict[str, Any]:
    root = _root(root)
    source = source.expanduser().resolve()
    if not source.is_file():
        raise ValueError("自定义规则来源文件不存在或为空")
    source_data = source.read_bytes()
    if not source_data.strip():
        raise ValueError("自定义规则来源文件不存在或为空")
    index = _read_index(root)
    existing_ids = {profile["id"] for profile in index["profiles"]}
    candidate = _safe_id(profile_id, "规则ID") if profile_id else _slugify(_nonempty(name, "规则名称"))
    if candidate in existing_ids:
        raise ValueError(f"规则ID已存在：{candidate}")
    target = root / "rules" / "custom" / f"{candidate}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(source_data)
    os.replace(temporary, target)
    index["profiles"].append({
        "id": candidate, "name": _nonempty(name, "规则名称"), "kind": "custom",
        "description": _nonempty(description, "规则说明"),
        "path": f"rules/custom/{candidate}.md", "base_id": "default", "read_only": False,
    })
    _atomic_write_json(_index_path(root), index)
    return profile_descriptor(candidate, root)


def _print(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description="管理biaoshu-master的Stage4生成规则")
    subparsers = parser.add_subparsers(dest="command", required=True)
    list_parser = subparsers.add_parser("list", help="列出规则")
    list_parser.add_argument("--root", type=Path)
    default_parser = subparsers.add_parser("set-default", help="设置默认规则")
    default_parser.add_argument("profile_id")
    default_parser.add_argument("--root", type=Path)
    register_parser = subparsers.add_parser("register", help="登记自定义规则Markdown")
    register_parser.add_argument("--source", required=True, type=Path)
    register_parser.add_argument("--name", required=True)
    register_parser.add_argument("--description", required=True)
    register_parser.add_argument("--id")
    register_parser.add_argument("--root", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "list":
            _print(list_profiles(args.root))
        elif args.command == "set-default":
            _print(set_default(args.profile_id, args.root))
        else:
            _print(register_profile(args.source, args.name, args.description, args.id, args.root))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
