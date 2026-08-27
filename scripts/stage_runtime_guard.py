#!/usr/bin/env python3
"""Print and validate the minimal hard rule before generating a workflow stage."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from bid_confirm_ui import server


GENERATION_RULES = {
    "stage1": "读取已确认入口与本地资料，完整生成项目口径；不得直接写阶段2。阶段1完成后只能以前台等待。",
    "stage2": "读取阶段1确认回执，完整生成目录、评分映射和页数预算；不得写阶段3。阶段2完成后只能以前台等待；收到章节页数重新调整或整体调整事件时按请求重生成并重新等待。",
    "stage3": "读取阶段2确认回执，完整生成统一视觉方向和图片规划；不得写正文。收到整体调整事件时按请求重生成当前阶段并重新等待。",
    "stage4": "读取阶段3确认回执，完整生成最终交付方案；不得初始化正文生产。",
}


def prerequisite_valid(data_dir: Path, stage: str) -> bool:
    if stage == "stage1":
        return bool(server.intake_receipt_valid(data_dir))
    return server.wait_prerequisite_valid(data_dir, stage)


def main() -> int:
    parser = argparse.ArgumentParser(description="biaoshu-master 阶段运行守卫")
    parser.add_argument("project_dir", type=Path)
    parser.add_argument("--stage", choices=sorted(GENERATION_RULES), required=True)
    args = parser.parse_args()
    project_dir = args.project_dir.expanduser().resolve()
    data_dir = project_dir / server.DATA_DIR_NAME
    if not prerequisite_valid(data_dir, args.stage):
        raise ValueError("前序确认回执无效，不能生成当前阶段")
    wait_command = (
        f"python3 scripts/bid_confirm_ui/server.py <project_dir> "
        f"--wait-only --wait-stage {args.stage} --wait-timeout 0"
    )
    print(
        f"[stage-guard:{args.stage}] {GENERATION_RULES[args.stage]} "
        f"必须原子写入 generation_status=complete 后，立即执行：{wait_command}；"
        "该命令必须以前台阻塞方式运行。禁止工具参数 run_in_background=true、background=true、"
        "异步/分离任务，也禁止在命令启动后结束当前对话。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
