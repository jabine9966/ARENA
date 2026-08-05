#!/usr/bin/env python3
"""根据复盘日志更新自主学习状态。学习模块只更新 JSON 参数，不修改策略代码。"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from decimal import Decimal
from typing import Any

from arena_utils import (
    PROJECT_ROOT,
    append_jsonl_unique,
    format_beijing_time,
    format_decimal,
    load_config,
    read_jsonl,
    to_decimal,
    write_json,
)
from datetime import datetime
from arena_utils import BEIJING_TZ


RESULT_KEYS = [
    "entered_take_profit",
    "entered_stop_loss",
    "entered_expired_profit",
    "entered_expired_loss",
    "entered_expired_flat",
    "ambiguous_same_candle",
    "not_triggered_missed_profit",
    "not_triggered_avoided_loss",
    "not_triggered_no_signal",
    "not_triggered_mixed_outcome",
    "no_market_data",
    "no_market_data_expired",
]


def clamp(value: Decimal, min_value: Decimal, max_value: Decimal) -> Decimal:
    return max(min_value, min(max_value, value))


def pct(num: int, den: int) -> Decimal:
    if den <= 0:
        return Decimal("0")
    return Decimal(num) / Decimal(den)


def summarize_side(records: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(record.get("result", "unknown") for record in records)
    triggered = sum(1 for record in records if record.get("was_triggered") is True)
    not_triggered = sum(1 for record in records if record.get("was_triggered") is False)
    total = len(records)
    triggered_den = max(triggered, 1)
    return {
        "total_plans": total,
        "triggered": triggered,
        "not_triggered": not_triggered,
        **{key: counts.get(key, 0) for key in RESULT_KEYS},
        "trigger_rate": format_decimal(pct(triggered, total), 4),
        "take_profit_rate_on_triggered": format_decimal(pct(counts.get("entered_take_profit", 0), triggered_den), 4),
        "stop_loss_rate_on_triggered": format_decimal(pct(counts.get("entered_stop_loss", 0), triggered_den), 4),
        "missed_profit_rate": format_decimal(pct(counts.get("not_triggered_missed_profit", 0), total), 4),
        "expired_profit_rate_on_triggered": format_decimal(pct(counts.get("entered_expired_profit", 0), triggered_den), 4),
        "ambiguous_rate": format_decimal(pct(counts.get("ambiguous_same_candle", 0), total), 4),
    }


def result_counter(records: list[dict[str, Any]], result: str) -> int:
    return sum(1 for record in records if record.get("result") == result)


def derive_adaptive_parameters(
    *,
    config: dict[str, Any],
    records_by_side: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, str], list[str]]:
    defaults = {key: to_decimal(value) for key, value in config["adaptive_defaults"].items()}
    rules = config["learning_rules"]
    recent_window = int(rules["recent_window"])
    missed_high = to_decimal(rules["missed_profit_rate_high"])
    stop_high = to_decimal(rules["stop_loss_rate_high"])
    expired_profit_high = to_decimal(rules["expired_profit_rate_high"])
    tp_good = to_decimal(rules["take_profit_rate_good"])
    step = to_decimal(rules["adjustment_step"])
    min_factor = to_decimal(rules["min_factor"])
    max_factor = to_decimal(rules["max_factor"])

    params = dict(defaults)
    lessons: list[str] = []

    stop_rates: list[Decimal] = []
    for side in ("long", "short"):
        side_records = records_by_side.get(side, [])[-recent_window:]
        total = len(side_records)
        triggered = sum(1 for r in side_records if r.get("was_triggered") is True)
        if total == 0:
            lessons.append(f"{side} 暂无复盘样本，保持默认参数。")
            continue

        missed_rate = pct(result_counter(side_records, "not_triggered_missed_profit"), total)
        stop_rate = pct(result_counter(side_records, "entered_stop_loss"), max(triggered, 1))
        tp_rate = pct(result_counter(side_records, "entered_take_profit"), max(triggered, 1))
        expired_profit_rate = pct(result_counter(side_records, "entered_expired_profit"), max(triggered, 1))
        stop_rates.append(stop_rate)

        entry_key = f"{side}_entry_aggressiveness"
        tp_key = f"{side}_take_profit_factor"

        if missed_rate > missed_high:
            params[entry_key] = params[entry_key] + step
            lessons.append(
                f"最近 {total} 条 {side} 计划中 missed_profit 比例为 {format_decimal(missed_rate * 100, 2)}%，"
                "说明入场偏保守，下一轮适度提高入场积极性。"
            )
        if stop_rate > stop_high:
            params[entry_key] = params[entry_key] - step
            lessons.append(
                f"最近 {triggered} 条已成交 {side} 计划中 stop_loss 比例为 {format_decimal(stop_rate * 100, 2)}%，"
                "说明入场或方向过滤偏激进，下一轮适度降低入场积极性。"
            )
        if expired_profit_rate > expired_profit_high and tp_rate < tp_good:
            params[tp_key] = params[tp_key] - step
            lessons.append(
                f"最近 {triggered} 条已成交 {side} 计划中周期结束浮盈但未止盈比例偏高，"
                "说明止盈可能偏远，下一轮适度收近止盈。"
            )
        elif tp_rate > tp_good and expired_profit_rate == 0:
            params[tp_key] = params[tp_key] + step
            lessons.append(
                f"最近 {triggered} 条已成交 {side} 计划中止盈率较好，"
                "可小幅放宽止盈目标以观察收益弹性。"
            )

    if stop_rates and sum(stop_rates, Decimal("0")) / Decimal(len(stop_rates)) > stop_high:
        params["stop_loss_atr_factor"] = params["stop_loss_atr_factor"] + step
        lessons.append("整体止损触发率偏高，下一轮小幅放宽结构止损缓冲。")

    for key in list(params):
        params[key] = clamp(params[key], min_factor, max_factor)

    if not lessons:
        lessons.append("暂无足够复盘证据触发参数调整，保持当前默认策略参数。")

    return {key: format_decimal(value, 3) for key, value in params.items()}, lessons[-10:]


def update_learning_state(config: dict[str, Any]) -> dict[str, Any]:
    review_log = PROJECT_ROOT / config["paths"]["trade_reviews_log"]
    learning_path = PROJECT_ROOT / config["paths"]["learning_state"]
    learning_log = PROJECT_ROOT / config["paths"]["learning_updates_log"]

    records = read_jsonl(review_log)
    records_by_side: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        side = record.get("side")
        if side in {"long", "short"}:
            records_by_side[side].append(record)

    performance = {
        "long": summarize_side(records_by_side.get("long", [])),
        "short": summarize_side(records_by_side.get("short", [])),
    }
    adaptive_parameters, lessons = derive_adaptive_parameters(config=config, records_by_side=records_by_side)

    now = datetime.now(BEIJING_TZ)
    last_review_id = records[-1].get("review_id") if records else "no_reviews"
    state = {
        "strategy_version": config["strategy_version"],
        "cycle_hours": config["cycle_hours"],
        "take_profit_mode": config["take_profit_mode"],
        "long_short_independent": config["long_short_independent"],
        "last_updated_beijing": format_beijing_time(now),
        "review_sample_size": len(records),
        "performance": performance,
        "adaptive_parameters": adaptive_parameters,
        "recent_lessons": lessons,
        "last_review_id": last_review_id,
    }
    write_json(learning_path, state)

    update_record = {
        "update_id": f"learning_{len(records)}_{last_review_id}",
        "updated_at_beijing": state["last_updated_beijing"],
        "review_sample_size": len(records),
        "adaptive_parameters": adaptive_parameters,
        "recent_lessons": lessons,
    }
    append_jsonl_unique(learning_log, update_record, "update_id")
    return state


def build_arg_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(description="根据复盘日志更新学习状态 JSON。")


def main() -> int:
    build_arg_parser().parse_args()
    config = load_config()
    state = update_learning_state(config)
    print(f"学习状态已更新：样本数 {state['review_sample_size']}")
    for lesson in state.get("recent_lessons", []):
        print(f"- {lesson}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
