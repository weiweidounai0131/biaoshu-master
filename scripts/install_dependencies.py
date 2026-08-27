#!/usr/bin/env python3
"""Install the skill's open-source Python runtime from a mainland-China mirror."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


DEFAULT_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"
REQUIREMENTS = Path(__file__).resolve().parents[1] / "requirements.txt"


def main() -> int:
    parser = argparse.ArgumentParser(description="安装标书技能运行依赖")
    parser.add_argument("--index-url", default=DEFAULT_INDEX, help="默认使用清华 PyPI 镜像，无需代理")
    parser.add_argument("--check", action="store_true", help="仅检查依赖，不执行安装")
    args = parser.parse_args()
    modules = {"docx": "python-docx", "openpyxl": "openpyxl", "xlrd": "xlrd"}
    missing: list[str] = []
    for module, package in modules.items():
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    if not missing:
        print("依赖已就绪：python-docx、openpyxl、xlrd")
        return 0
    if args.check:
        print("缺少依赖：" + "、".join(missing), file=sys.stderr)
        return 2
    command = [sys.executable, "-m", "pip", "install", "--user", "--disable-pip-version-check", "--index-url", args.index_url, "-r", str(REQUIREMENTS)]
    print("正在从国内镜像安装：" + "、".join(missing), flush=True)
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode == 0:
        if result.stdout:
            print(result.stdout, end="")
        return 0
    combined = (result.stdout or "") + (result.stderr or "")
    if "externally-managed-environment" not in combined:
        print(combined, file=sys.stderr, end="")
        return result.returncode
    retry = command[:4] + ["--break-system-packages"] + command[4:]
    print("检测到受管理的Python环境，改用用户级兼容模式重试。", flush=True)
    return subprocess.call(retry)


if __name__ == "__main__":
    raise SystemExit(main())
