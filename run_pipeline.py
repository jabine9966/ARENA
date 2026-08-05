#!/usr/bin/env python3
"""ARENA 自动化闭环总控脚本。

流程：
1. 下载最新 K线数据
2. 计算指标和 MA50 偏离统计
3. 复盘上一轮 4 小时交易计划
4. 根据复盘日志更新学习状态
5. 按半小时运行节奏生成本轮独立多空交易计划，单条指令最长有效 4 小时
6. 生成 Markdown 分析报告
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from arena_utils import PROJECT_ROOT, ensure_project_dirs, load_config


def run_step(name: str, args: list[str]) -> None:
    print(f"\n========== {name} ==========" , flush=True)
    print("$ " + " ".join(args), flush=True)
    subprocess.run(args, cwd=PROJECT_ROOT, check=True)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="运行 ARENA 自动交易分析闭环。")
    parser.add_argument("--skip-download", action="store_true", help="跳过 OKX 数据下载，使用现有 CSV。")
    parser.add_argument("--force-review", action="store_true", help="强制复盘上一轮计划。")
    parser.add_argument("--force-new-plan", action="store_true", help="即使当前最新K线已生成过计划，也强制生成新计划。")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    config = load_config()
    ensure_project_dirs(config)

    py = sys.executable
    if not args.skip_download:
        run_step("1. 下载最新 K线数据", [py, "download_okx_sol_klines.py"])
    else:
        print("\n========== 1. 下载最新 K线数据 ==========")
        print("已跳过，使用现有 CSV。")

    run_step("2. 计算指标和 MA50 偏离统计", [py, "calculate_ma50_deviation.py"])

    review_cmd = [py, "review_trade_plan.py"]
    if args.force_review:
        review_cmd.append("--force")
    run_step("3. 复盘上一轮交易计划", review_cmd)

    run_step("4. 更新学习状态", [py, "update_learning_state.py"])

    plan_cmd = [py, "generate_trade_plan.py"]
    if args.force_new_plan:
        plan_cmd.append("--force")
    run_step("5. 生成本轮交易计划", plan_cmd)

    run_step("6. 生成最终分析报告", [py, "generate_report.py"])

    print("\nARENA 自动化闭环运行完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
