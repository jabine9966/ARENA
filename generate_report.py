#!/usr/bin/env python3
"""根据复盘、学习状态、当前指标和最新交易计划生成最终 Markdown 报告。"""

from __future__ import annotations

import argparse
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from arena_utils import (
    BEIJING_TZ,
    PROJECT_ROOT,
    compact_time,
    format_beijing_time,
    load_config,
    read_csv_dicts,
    read_json,
)


RESULT_CN = {
    "entered_take_profit": "已成交并止盈",
    "entered_stop_loss": "已成交并止损",
    "entered_expired_profit": "已成交，周期结束浮盈",
    "entered_expired_loss": "已成交，周期结束浮亏",
    "entered_expired_flat": "已成交，周期结束基本持平",
    "ambiguous_same_candle": "同K线触发止盈止损，结果模糊",
    "not_triggered_missed_profit": "未成交但方向正确，错过盈利",
    "not_triggered_avoided_loss": "未成交且避免亏损",
    "not_triggered_no_signal": "未成交且行情未验证",
    "not_triggered_mixed_outcome": "未成交但行情同时到达目标区和风险区",
    "no_market_data": "无可复盘数据",
    "no_market_data_expired": "到期但无可复盘数据",
    "active_waiting_market_data": "等待后续K线",
    "active_pending_entry": "有效期内等待成交",
    "active_entered_waiting_exit": "已成交，等待止盈/止损或到期",
}

SIDE_CN = {"long": "多头", "short": "空头"}
TP_BASIS_CN = {
    "base_target": "基础目标",
    "risk_reward_adjusted_resistance": "盈亏比不足，顺延至上方压力位",
    "risk_reward_adjusted_support": "盈亏比不足，顺延至下方支撑位",
    "risk_reward_minimum_target": "按最低盈亏比推算目标",
}


def norm_bar(bar: str) -> str:
    return bar.strip().lower()


def rows_by_bar(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    return {norm_bar(row.get("bar", "")): row for row in rows}


def md_table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(x) for x in row) + " |")
    return "\n".join(lines)


def render_review_section(batch: dict[str, Any] | None) -> str:
    lines: list[str] = ["# 第一部分：前次交易复盘", ""]
    if not batch:
        lines.append("暂无复盘状态文件。")
        return "\n".join(lines)

    status = batch.get("status")
    if status in {"no_plan", "no_active_plans"}:
        lines.append("当前没有需要复盘或跟踪的交易计划。首次运行或活跃计划全部完成后会出现该状态。")
        return "\n".join(lines)
    if status == "not_due":
        lines.append("上一轮交易计划尚未到期，本轮暂不复盘。")
        lines.append("")
        lines.append(f"- 计划 ID：{batch.get('plan_id', '')}")
        lines.append(f"- 当前最新市场时间：{batch.get('latest_market_close_time', '')}")
        lines.append(f"- 计划有效期至：{batch.get('valid_until_beijing', '')}")
        return "\n".join(lines)
    if status == "already_reviewed":
        lines.append("上一轮交易计划已经复盘，以下展示最近一次复盘结果。")
    elif status == "reviewed":
        lines.append(f"上一轮计划 ID：`{batch.get('plan_id', '')}`")
        lines.append(f"复盘时间：{batch.get('review_time_beijing', '')}")
        lines.append(f"复盘周期：{batch.get('created_at_beijing', '')} -> {batch.get('valid_until_beijing', '')}")
    elif status == "reviewed_active_plans":
        lines.append(f"本轮跟踪/复盘时间：{batch.get('review_time_beijing', '')}")
        lines.append(f"本轮开始前活跃计划数：{batch.get('active_plan_count_before', 0)}")
        lines.append(f"本轮结束后活跃计划数：{batch.get('active_plan_count_after', 0)}")
        lines.append(f"本轮新增终态复盘：{batch.get('terminal_count', 0)} 条")
        lines.append(f"本轮仍在跟踪：{batch.get('active_count', 0)} 条")
    else:
        lines.append(batch.get("message", f"复盘状态：{status}"))

    terminal_results = batch.get("terminal_results") or batch.get("results") or []
    active_results = batch.get("active_results") or []
    if not terminal_results and not active_results:
        lines.append("")
        lines.append("本轮暂无终态复盘结果，也没有仍在跟踪的交易指令。")
        return "\n".join(lines)

    if terminal_results:
        rows = []
        for result in terminal_results:
            rows.append(
                [
                    SIDE_CN.get(result.get("side", ""), result.get("side", "")),
                    result.get("entry_price", ""),
                    result.get("take_profit", ""),
                    result.get("stop_loss", ""),
                    "是" if result.get("was_triggered") is True else "否",
                    RESULT_CN.get(result.get("result", ""), result.get("result", "")),
                    result.get("pnl_pct", ""),
                    result.get("r_multiple", ""),
                ]
            )
        lines.append("")
        lines.append("## 本轮新增终态复盘")
        lines.append("")
        lines.append(md_table(["方向", "建仓价", "止盈", "止损", "是否成交", "复盘结果", "收益率", "R倍数"], rows))
        lines.append("")

        for result in terminal_results:
            side = SIDE_CN.get(result.get("side", ""), result.get("side", ""))
            lines.append(f"### {side}终态复盘说明")
            lines.append("")
            lines.append(f"- 结果：{RESULT_CN.get(result.get('result', ''), result.get('result', ''))}")
            if result.get("entry_time"):
                lines.append(f"- 入场时间：{result.get('entry_time')}")
            if result.get("exit_time"):
                lines.append(f"- 退出/评估时间：{result.get('exit_time')}")
            if result.get("max_favorable_price"):
                lines.append(f"- 最大有利价格：{result.get('max_favorable_price')}")
            if result.get("max_adverse_price"):
                lines.append(f"- 最大不利价格：{result.get('max_adverse_price')}")
            if result.get("nearest_price_to_entry"):
                lines.append(f"- 距离建仓价最近价格：{result.get('nearest_price_to_entry')}，距离：{result.get('distance_to_entry')} / {result.get('distance_to_entry_pct')}")
            if result.get("conservative_result"):
                lines.append(f"- 保守结果：{RESULT_CN.get(result.get('conservative_result'), result.get('conservative_result'))}，{result.get('conservative_pnl_pct', '')}")
                lines.append(f"- 乐观结果：{RESULT_CN.get(result.get('optimistic_result'), result.get('optimistic_result'))}，{result.get('optimistic_pnl_pct', '')}")
            lines.append(f"- 说明：{result.get('notes', '')}")
            lines.append("")

    if active_results:
        rows = []
        for result in active_results:
            rows.append(
                [
                    result.get("plan_id", ""),
                    SIDE_CN.get(result.get("side", ""), result.get("side", "")),
                    result.get("entry_price", ""),
                    result.get("take_profit", ""),
                    result.get("stop_loss", ""),
                    "是" if result.get("was_triggered") is True else "否",
                    RESULT_CN.get(result.get("result", ""), result.get("result", "")),
                    result.get("floating_pnl_pct", ""),
                    result.get("distance_to_entry_pct", ""),
                ]
            )
        lines.append("")
        lines.append("## 仍在4小时有效期内的交易指令")
        lines.append("")
        lines.append(md_table(["计划ID", "方向", "建仓价", "止盈", "止损", "是否成交", "当前状态", "浮动收益", "距建仓价"], rows))
        lines.append("")
        lines.append("以上活跃指令尚未形成终态结果，不写入复盘日志；后续每半小时运行时会继续跟踪，直到止盈、止损或4小时到期。")
        lines.append("")

    return "\n".join(lines)

def render_plan_section(plan: dict[str, Any] | None) -> str:
    lines: list[str] = ["# 第二部分：本次交易指令", ""]
    if not plan:
        lines.append("暂无最新交易计划。")
        return "\n".join(lines)

    lines.append(f"计划 ID：`{plan.get('plan_id', '')}`")
    lines.append(f"交易标的：{plan.get('symbol', '')}")
    lines.append(f"项目执行间隔：每 {plan.get('execution_interval_minutes', 30)} 分钟")
    lines.append(f"指令最长有效期：{plan.get('cycle_hours', '')} 小时")
    lines.append(f"本轮指令有效期：{plan.get('created_at_beijing', '')} -> {plan.get('valid_until_beijing', '')}")
    lines.append(f"多空关系：{'独立，不互斥' if plan.get('long_short_independent') else '互斥'}")
    lines.append(f"止盈模式：{plan.get('take_profit_mode', '')}")
    lines.append(f"当前市场判断：{plan.get('market_bias', '')}")
    lines.append("")

    rows = []
    for item in plan.get("plans", []):
        rows.append(
            [
                SIDE_CN.get(item.get("side", ""), item.get("side", "")),
                " - ".join(item.get("entry_zone", [])),
                item.get("entry_price", ""),
                item.get("take_profit", ""),
                item.get("stop_loss", ""),
                item.get("risk_reward", ""),
                TP_BASIS_CN.get(item.get("take_profit_basis", ""), item.get("take_profit_basis", "")),
                item.get("status", ""),
            ]
        )
    lines.append(md_table(["方向", "建仓区", "建议建仓价", "止盈价", "止损价", "盈亏比", "止盈依据", "状态"], rows))
    lines.append("")

    for item in plan.get("plans", []):
        side = SIDE_CN.get(item.get("side", ""), item.get("side", ""))
        lines.append(f"## {side}交易指令")
        lines.append("")
        lines.append(f"- 建仓区：{ ' - '.join(item.get('entry_zone', [])) }")
        lines.append(f"- 建议建仓价：{item.get('entry_price', '')}")
        lines.append(f"- 止盈价：{item.get('take_profit', '')}")
        lines.append(f"- 止损价：{item.get('stop_loss', '')}")
        lines.append(f"- 止盈依据：{TP_BASIS_CN.get(item.get('take_profit_basis', ''), item.get('take_profit_basis', ''))}")
        lines.append(f"- 执行说明：{item.get('rationale', '')}")
        lines.append(f"- 逻辑标签：{', '.join(item.get('logic_tags', []))}")
        lines.append("")

    return "\n".join(lines)


def render_analysis_section(ma50_rows: dict[str, dict[str, str]], tech_rows: dict[str, dict[str, str]]) -> str:
    lines: list[str] = ["# 第三部分：本次分析逻辑", ""]

    lines.append("## 1. MA50 偏离值统计")
    lines.append("")
    rows = []
    for bar in ["15m", "30m", "1h"]:
        row = ma50_rows.get(bar)
        if not row:
            continue
        rows.append(
            [
                row.get("bar", bar),
                row.get("long_count", ""),
                row.get("long_max_deviation", ""),
                row.get("long_min_deviation", ""),
                row.get("long_avg_deviation", ""),
                row.get("short_count", ""),
                row.get("short_max_deviation", ""),
                row.get("short_min_deviation", ""),
                row.get("short_avg_deviation", ""),
            ]
        )
    lines.append(md_table(["周期", "多头样本", "多头最大", "多头最小", "多头平均", "空头样本", "空头最大", "空头最小", "空头平均"], rows))
    lines.append("")
    lines.append("MA50 偏离值用于估算价格围绕 MA50 运动时的平均目标空间。单层止盈优先采用更适合 4 小时周期的保守目标，而不是无限追求扩展目标。")
    lines.append("")

    lines.append("## 2. 技术指标最新值")
    lines.append("")
    rows = []
    for bar in ["15m", "30m", "1h"]:
        row = tech_rows.get(bar)
        if not row:
            continue
        rows.append(
            [
                row.get("bar", bar),
                row.get("latest_time", ""),
                row.get("latest_close", ""),
                row.get("ma10", ""),
                row.get("ma20", ""),
                row.get("ma50", ""),
                row.get("ma100", ""),
                row.get("ma200", ""),
                row.get("rsi14", ""),
                row.get("macd_dif", ""),
                row.get("macd_dea", ""),
                row.get("macd_hist", ""),
                row.get("atr12", ""),
                row.get("supertrend_value", ""),
                row.get("supertrend_direction", ""),
            ]
        )
    lines.append(md_table(["周期", "最新K线", "Close", "MA10", "MA20", "MA50", "MA100", "MA200", "RSI14", "MACD DIF", "MACD DEA", "MACD柱", "ATR12", "超级趋势", "方向"], rows))
    lines.append("")

    lines.append("## 3. 枢纽点价格")
    lines.append("")
    rows = []
    for bar in ["15m", "30m", "1h"]:
        row = tech_rows.get(bar)
        if not row:
            continue
        rows.append(
            [
                row.get("bar", bar),
                row.get("pivot_point", ""),
                row.get("pivot_r1", ""),
                row.get("pivot_r2", ""),
                row.get("pivot_r3", ""),
                row.get("pivot_s1", ""),
                row.get("pivot_s2", ""),
                row.get("pivot_s3", ""),
            ]
        )
    lines.append(md_table(["周期", "PP", "R1", "R2", "R3", "S1", "S2", "S3"], rows))
    lines.append("")

    lines.append("## 4. 逻辑归纳")
    lines.append("")
    lines.append("- 多头逻辑重点观察：价格回踩 15m/30m S1 与 1H PP 的支撑共振区后，是否能够重新站稳。")
    lines.append("- 空头逻辑重点观察：价格反弹至 15m/30m R2 与 1H R1 的压力共振区后，是否出现受阻。")
    lines.append("- 多空计划相互独立，复盘时分别判断是否成交、是否止盈、是否止损、是否错过机会。")
    lines.append("- 当前止盈为单层结构，复盘时不会进行分批收益拆分。")
    lines.append("")
    return "\n".join(lines)


def render_learning_section(learning_state: dict[str, Any] | None) -> str:
    lines: list[str] = ["# 第四部分：学习优化记录", ""]
    if not learning_state:
        lines.append("暂无学习状态文件。")
        return "\n".join(lines)

    lines.append(f"学习状态更新时间：{learning_state.get('last_updated_beijing', '')}")
    lines.append(f"复盘样本数：{learning_state.get('review_sample_size', 0)}")
    lines.append("")

    performance = learning_state.get("performance", {})
    rows = []
    for side in ["long", "short"]:
        stats = performance.get(side, {})
        rows.append(
            [
                SIDE_CN.get(side, side),
                stats.get("total_plans", 0),
                stats.get("triggered", 0),
                stats.get("entered_take_profit", 0),
                stats.get("entered_stop_loss", 0),
                stats.get("not_triggered_missed_profit", 0),
                stats.get("not_triggered_avoided_loss", 0),
                stats.get("take_profit_rate_on_triggered", ""),
                stats.get("stop_loss_rate_on_triggered", ""),
            ]
        )
    lines.append(md_table(["方向", "总计划", "已成交", "止盈", "止损", "错过盈利", "避免亏损", "成交止盈率", "成交止损率"], rows))
    lines.append("")

    adaptive = learning_state.get("adaptive_parameters", {})
    lines.append("## 当前自适应参数")
    lines.append("")
    for key, value in adaptive.items():
        lines.append(f"- {key}: {value}")
    lines.append("")

    lines.append("## 最近学习结论")
    lines.append("")
    for lesson in learning_state.get("recent_lessons", []):
        lines.append(f"- {lesson}")
    lines.append("")

    return "\n".join(lines)


def generate_report(config: dict[str, Any]) -> Path:
    paths = config["paths"]
    batch = read_json(PROJECT_ROOT / paths["latest_review_batch"], default=None)
    plan = read_json(PROJECT_ROOT / paths["latest_trade_plan"], default=None)
    learning_state = read_json(PROJECT_ROOT / paths["learning_state"], default=None)

    ma50_path = PROJECT_ROOT / config["indicator_files"]["ma50_deviation"]
    tech_path = PROJECT_ROOT / config["indicator_files"]["technical_indicators"]
    ma50_rows = rows_by_bar(read_csv_dicts(ma50_path)) if ma50_path.exists() else {}
    tech_rows = rows_by_bar(read_csv_dicts(tech_path)) if tech_path.exists() else {}

    now = datetime.now(BEIJING_TZ)
    title = [
        "# SOL-USDT 自动交易分析报告",
        "",
        f"生成时间：{format_beijing_time(now)}",
        "",
        "> 本报告由 ARENA 自动化分析流程生成，用于交易计划、复盘和策略学习记录。内容不构成保证收益的投资建议，实盘需自行控制仓位、滑点、手续费和风险。",
        "",
        "---",
        "",
    ]
    content = "\n".join(title) + "\n".join(
        [
            render_review_section(batch),
            "\n---\n",
            render_plan_section(plan),
            "\n---\n",
            render_analysis_section(ma50_rows, tech_rows),
            "\n---\n",
            render_learning_section(learning_state),
        ]
    )

    report_path = PROJECT_ROOT / paths["latest_report"]
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(content, encoding="utf-8")

    # 兼容旧位置：根目录也保留一份最新报告。
    legacy_path = PROJECT_ROOT / paths["legacy_root_report"]
    legacy_path.write_text(content, encoding="utf-8")

    history_dir = PROJECT_ROOT / paths["report_history_dir"]
    history_dir.mkdir(parents=True, exist_ok=True)
    suffix = plan.get("plan_id") if plan else compact_time(now)
    history_path = history_dir / f"SOL_trade_analysis_report_{suffix}.md"
    shutil.copyfile(report_path, history_path)
    return report_path


def build_arg_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(description="生成自动交易分析 Markdown 报告。")


def main() -> int:
    build_arg_parser().parse_args()
    config = load_config()
    report_path = generate_report(config)
    print(f"报告已生成：{report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
