#!/usr/bin/env python3
"""根据当前指标、枢纽点和学习状态生成 4 小时交易计划。"""

from __future__ import annotations

import argparse
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from arena_utils import (
    PROJECT_ROOT,
    append_jsonl_unique,
    compact_time,
    discover_latest_kline_file,
    format_beijing_time,
    format_decimal,
    format_percent,
    format_price,
    latest_market_close_time,
    load_config,
    parse_beijing_time,
    parse_percent,
    read_csv_dicts,
    read_json,
    read_market_candles,
    to_decimal,
    write_json,
)


def norm_bar(bar: str) -> str:
    return bar.strip().lower()


def quant_price(value: Decimal, precision: int) -> Decimal:
    quant = Decimal("1").scaleb(-precision)
    return value.quantize(quant, rounding=ROUND_HALF_UP)


def load_summary_by_bar(path: Path) -> dict[str, dict[str, str]]:
    rows = read_csv_dicts(path)
    return {norm_bar(row.get("bar", "")): row for row in rows}


def default_learning_state(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "strategy_version": config["strategy_version"],
        "cycle_hours": config["cycle_hours"],
        "take_profit_mode": config["take_profit_mode"],
        "long_short_independent": config["long_short_independent"],
        "adaptive_parameters": dict(config["adaptive_defaults"]),
        "recent_lessons": ["暂无历史复盘样本，使用默认策略参数。"],
    }


def get_adaptive_decimal(learning_state: dict[str, Any], config: dict[str, Any], key: str) -> Decimal:
    adaptive = learning_state.get("adaptive_parameters") or {}
    defaults = config.get("adaptive_defaults") or {}
    return to_decimal(adaptive.get(key, defaults.get(key, "1.0")), default=Decimal("1.0"))


def long_target_from_ma50(latest_ma50: Decimal, ma50_row: dict[str, str], factor: Decimal, precision: int) -> Decimal:
    # 这里用“当前最新 MA50”结合历史 long_avg_deviation 反推单层止盈。
    # long_avg_deviation 形如 +0.505%。
    avg_dev_pct = parse_percent(ma50_row["long_avg_deviation"])
    target = latest_ma50 * (Decimal("1") + (avg_dev_pct * factor) / Decimal("100"))
    return quant_price(target, precision)


def short_target_from_support(tech15: dict[str, str], tech30: dict[str, str], precision: int) -> Decimal:
    # 单层空头止盈使用最近的共振支撑，即 15m/30m S1 附近。
    support = max(to_decimal(tech15["pivot_s1"]), to_decimal(tech30["pivot_s1"]))
    return quant_price(support, precision)


def risk_reward(side: str, entry: Decimal, take_profit: Decimal, stop_loss: Decimal) -> Decimal | None:
    if side == "long":
        risk = entry - stop_loss
        reward = take_profit - entry
    else:
        risk = stop_loss - entry
        reward = entry - take_profit
    if risk <= 0:
        return None
    return reward / risk


def ensure_min_reward_risk(
    *,
    side: str,
    entry: Decimal,
    stop_loss: Decimal,
    take_profit: Decimal,
    candidates: list[Decimal],
    minimum_rr: Decimal,
    precision: int,
) -> tuple[Decimal, str]:
    """如果基础止盈盈亏比过低，则从支撑/压力候选位中选择满足最低盈亏比的目标。"""
    current_rr = risk_reward(side, entry, take_profit, stop_loss)
    if current_rr is not None and current_rr >= minimum_rr:
        return quant_price(take_profit, precision), "base_target"

    if side == "long":
        risk = entry - stop_loss
        required_target = entry + risk * minimum_rr
        valid_candidates = sorted({quant_price(c, precision) for c in candidates if c > entry})
        for candidate in valid_candidates:
            if candidate >= required_target:
                return candidate, "risk_reward_adjusted_resistance"
        return quant_price(required_target, precision), "risk_reward_minimum_target"

    risk = stop_loss - entry
    required_target = entry - risk * minimum_rr
    valid_candidates = sorted({quant_price(c, precision) for c in candidates if c < entry}, reverse=True)
    for candidate in valid_candidates:
        if candidate <= required_target:
            return candidate, "risk_reward_adjusted_support"
    return quant_price(required_target, precision), "risk_reward_minimum_target"


def build_logic_tags(tech15: dict[str, str], tech30: dict[str, str], tech1h: dict[str, str], side: str) -> list[str]:
    tags: list[str] = []
    for label, row in (("15m", tech15), ("30m", tech30), ("1h", tech1h)):
        direction = row.get("supertrend_direction", "")
        if "多" in direction:
            tags.append(f"{label}_supertrend_long")
        elif "空" in direction:
            tags.append(f"{label}_supertrend_short")

    close15 = to_decimal(tech15["latest_close"])
    ma50_15 = to_decimal(tech15["ma50"])
    if close15 >= ma50_15:
        tags.append("15m_close_above_ma50")
    else:
        tags.append("15m_close_below_ma50")

    if side == "long":
        tags.extend(["pivot_support_entry", "ma50_long_avg_deviation_take_profit", "single_take_profit"])
    else:
        tags.extend(["pivot_resistance_entry", "pivot_support_take_profit", "single_take_profit"])
    return tags


def market_bias_text(tech15: dict[str, str], tech30: dict[str, str], tech1h: dict[str, str]) -> str:
    directions = [
        ("15m", tech15.get("supertrend_direction", "")),
        ("30m", tech30.get("supertrend_direction", "")),
        ("1H", tech1h.get("supertrend_direction", "")),
    ]
    long_count = sum(1 for _, direction in directions if "多" in direction)
    short_count = sum(1 for _, direction in directions if "空" in direction)
    if long_count >= 2 and short_count >= 1:
        return "大周期偏多，但短线承压，优先等待回踩支撑做多；空头只在压力区失败时独立执行。"
    if long_count == 3:
        return "多周期共振偏多，优先寻找回踩做多机会，空头仅作为压力区短线计划。"
    if short_count >= 2 and long_count >= 1:
        return "大周期偏弱，优先等待反弹压力区做空；多头仅在强支撑区独立观察。"
    if short_count == 3:
        return "多周期共振偏空，空头计划优先；多头只在极端支撑区作为反弹计划。"
    return "多空结构混合，按支撑和压力区分别制定独立交易计划。"


def plan_already_exists(config: dict[str, Any], plan_id: str) -> bool:
    active_path = PROJECT_ROOT / config["paths"].get("active_trade_plans", "state/active_trade_plans.json")
    active = read_json(active_path, default=[]) or []
    if any(plan.get("plan_id") == plan_id for plan in active):
        return True

    latest_path = PROJECT_ROOT / config["paths"]["latest_trade_plan"]
    latest = read_json(latest_path, default=None)
    return bool(latest and latest.get("plan_id") == plan_id)


def generate_trade_plan(config: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    ensure_files = config["indicator_files"]
    ma50_path = PROJECT_ROOT / ensure_files["ma50_deviation"]
    indicator_path = PROJECT_ROOT / ensure_files["technical_indicators"]
    if not ma50_path.exists() or not indicator_path.exists():
        raise FileNotFoundError("缺少指标统计 CSV，请先运行 calculate_ma50_deviation.py")

    file_symbol = config["file_symbol"]
    review_bar = config["review_rules"]["review_timeframe"]
    latest_close_time = latest_market_close_time(file_symbol, review_bar, PROJECT_ROOT)

    ma50_rows = load_summary_by_bar(ma50_path)
    tech_rows = load_summary_by_bar(indicator_path)
    tech15 = tech_rows["15m"]
    tech30 = tech_rows["30m"]
    tech1h = tech_rows["1h"]
    ma15 = ma50_rows["15m"]

    learning_path = PROJECT_ROOT / config["paths"]["learning_state"]
    learning_state = read_json(learning_path, default=None) or default_learning_state(config)

    precision = int(config["price_precision"])
    rules = config["plan_rules"]
    cycle_hours = int(config["cycle_hours"])

    created_at = latest_close_time
    valid_until = created_at + timedelta(hours=cycle_hours)
    plan_id = f"{config['file_symbol']}_{compact_time(created_at)}_{config['strategy_version']}"
    if plan_already_exists(config, plan_id) and not force:
        return {
            "status": "skipped",
            "reason": f"当前市场数据对应的交易计划已存在：{plan_id}。半小时定时运行时，同一根最新K线不会重复生成计划。",
        }

    long_entry_aggr = get_adaptive_decimal(learning_state, config, "long_entry_aggressiveness")
    short_entry_aggr = get_adaptive_decimal(learning_state, config, "short_entry_aggressiveness")
    long_tp_factor = get_adaptive_decimal(learning_state, config, "long_take_profit_factor")
    short_tp_factor = get_adaptive_decimal(learning_state, config, "short_take_profit_factor")
    stop_factor = get_adaptive_decimal(learning_state, config, "stop_loss_atr_factor")

    # 多头：15m/30m S1 与 1H PP 构成回踩支撑区。
    long_zone_candidates = [
        to_decimal(tech15["pivot_s1"]),
        to_decimal(tech30["pivot_s1"]),
        to_decimal(tech1h["pivot_point"]),
    ]
    long_zone_low = quant_price(min(long_zone_candidates), precision)
    long_zone_high = quant_price(max(long_zone_candidates), precision)
    long_buffer = to_decimal(rules["long_entry_buffer"])
    long_entry_base = min(long_zone_candidates) + long_buffer
    if long_entry_aggr > Decimal("1"):
        long_entry_base += (max(long_zone_candidates) - min(long_zone_candidates)) * (long_entry_aggr - Decimal("1")) / Decimal("2")
    elif long_entry_aggr < Decimal("1"):
        long_entry_base -= (max(long_zone_candidates) - min(long_zone_candidates)) * (Decimal("1") - long_entry_aggr) / Decimal("2")
    long_entry = quant_price(long_entry_base, precision)

    long_stop_candidates = [
        to_decimal(tech15["pivot_s3"]),
        to_decimal(tech30["pivot_s3"]),
        to_decimal(tech30["ma200"]),
        to_decimal(tech1h["ma100"]),
    ]
    long_stop = quant_price(min(long_stop_candidates) - to_decimal(rules["long_stop_buffer"]) * stop_factor, precision)
    base_long_take_profit = long_target_from_ma50(to_decimal(tech15["ma50"]), ma15, long_tp_factor, precision)
    long_take_profit, long_take_profit_basis = ensure_min_reward_risk(
        side="long",
        entry=long_entry,
        stop_loss=long_stop,
        take_profit=base_long_take_profit,
        candidates=[
            base_long_take_profit,
            to_decimal(tech15["pivot_r1"]),
            to_decimal(tech15["pivot_r2"]),
            to_decimal(tech15["pivot_r3"]),
            to_decimal(tech30["pivot_r1"]),
            to_decimal(tech30["pivot_r2"]),
            to_decimal(tech30["pivot_r3"]),
            to_decimal(tech1h["pivot_r1"]),
            to_decimal(tech1h["pivot_r2"]),
            to_decimal(tech1h["pivot_r3"]),
        ],
        minimum_rr=to_decimal(rules["minimum_reward_risk"]),
        precision=precision,
    )

    # 空头：15m/30m R2 与 1H R1 构成反弹压力区。
    short_zone_candidates = [
        to_decimal(tech15["pivot_r2"]),
        to_decimal(tech30["pivot_r2"]),
        to_decimal(tech1h["pivot_r1"]),
    ]
    short_zone_low = quant_price(min(short_zone_candidates), precision)
    short_zone_high = quant_price(max(short_zone_candidates), precision)
    short_buffer = to_decimal(rules["short_entry_buffer"])
    short_entry_base = min(short_zone_candidates) + short_buffer
    if short_entry_aggr > Decimal("1"):
        short_entry_base -= (max(short_zone_candidates) - min(short_zone_candidates)) * (short_entry_aggr - Decimal("1")) / Decimal("2")
    elif short_entry_aggr < Decimal("1"):
        short_entry_base += (max(short_zone_candidates) - min(short_zone_candidates)) * (Decimal("1") - short_entry_aggr) / Decimal("2")
    short_entry = quant_price(short_entry_base, precision)

    short_stop_candidates = [
        to_decimal(tech15["pivot_r3"]),
        to_decimal(tech30["pivot_r3"]),
        to_decimal(tech1h["pivot_r2"]),
    ]
    short_stop = quant_price(max(short_stop_candidates) + to_decimal(rules["short_stop_buffer"]) * stop_factor, precision)
    base_short_take_profit = short_target_from_support(tech15, tech30, precision)
    adjusted_short_base = quant_price(short_entry - (short_entry - base_short_take_profit) * short_tp_factor, precision)
    short_take_profit, short_take_profit_basis = ensure_min_reward_risk(
        side="short",
        entry=short_entry,
        stop_loss=short_stop,
        take_profit=adjusted_short_base,
        candidates=[
            adjusted_short_base,
            to_decimal(tech15["pivot_s1"]),
            to_decimal(tech15["pivot_s2"]),
            to_decimal(tech15["pivot_s3"]),
            to_decimal(tech30["pivot_s1"]),
            to_decimal(tech30["pivot_s2"]),
            to_decimal(tech30["pivot_s3"]),
            to_decimal(tech1h["pivot_s1"]),
            to_decimal(tech1h["pivot_s2"]),
            to_decimal(tech1h["pivot_s3"]),
        ],
        minimum_rr=to_decimal(rules["minimum_reward_risk"]),
        precision=precision,
    )

    latest_15m_file = discover_latest_kline_file(file_symbol, "15m", PROJECT_ROOT)
    latest_30m_file = discover_latest_kline_file(file_symbol, "30m", PROJECT_ROOT)
    latest_1h_file = discover_latest_kline_file(file_symbol, "1h", PROJECT_ROOT)
    c15 = read_market_candles(latest_15m_file)[-1]
    c30 = read_market_candles(latest_30m_file)[-1]
    c1h = read_market_candles(latest_1h_file)[-1]

    plans = [
        {
            "plan_item_id": f"{plan_id}_LONG",
            "side": "long",
            "entry_zone": [format_price(long_zone_low, precision), format_price(long_zone_high, precision)],
            "entry_price": format_price(long_entry, precision),
            "take_profit": format_price(long_take_profit, precision),
            "stop_loss": format_price(long_stop, precision),
            "status": "pending",
            "logic_tags": build_logic_tags(tech15, tech30, tech1h, "long"),
            "risk_reward": format_decimal(risk_reward("long", long_entry, long_take_profit, long_stop), 3),
            "take_profit_basis": long_take_profit_basis,
            "rationale": "30m/1H 结构仍偏多，等待价格回踩 15m/30m S1 与 1H PP 共振支撑；止盈优先使用 15m MA50 多头平均偏离值，若盈亏比不足则顺延至上方关键压力位。",
        },
        {
            "plan_item_id": f"{plan_id}_SHORT",
            "side": "short",
            "entry_zone": [format_price(short_zone_low, precision), format_price(short_zone_high, precision)],
            "entry_price": format_price(short_entry, precision),
            "take_profit": format_price(short_take_profit, precision),
            "stop_loss": format_price(short_stop, precision),
            "status": "pending",
            "logic_tags": build_logic_tags(tech15, tech30, tech1h, "short"),
            "risk_reward": format_decimal(risk_reward("short", short_entry, short_take_profit, short_stop), 3),
            "take_profit_basis": short_take_profit_basis,
            "rationale": "若价格反弹至 15m/30m R2 与 1H R1 压力区失败，则执行独立空头计划；当多周期偏多时，空头仅作为压力区短线回落计划。止盈优先取最近共振支撑，若盈亏比不足则顺延至下方关键支撑位。",
        },
    ]

    plan = {
        "plan_id": plan_id,
        "symbol": config["symbol"],
        "file_symbol": file_symbol,
        "strategy_version": config["strategy_version"],
        "created_at_beijing": format_beijing_time(created_at),
        "valid_until_beijing": format_beijing_time(valid_until),
        "execution_interval_minutes": int(config.get("execution_interval_minutes", 30)),
        "cycle_hours": cycle_hours,
        "take_profit_mode": config["take_profit_mode"],
        "long_short_independent": config["long_short_independent"],
        "review_timeframe": review_bar,
        "reviewed": False,
        "market_bias": market_bias_text(tech15, tech30, tech1h),
        "data_snapshot": {
            "15m_file": latest_15m_file.name,
            "30m_file": latest_30m_file.name,
            "1h_file": latest_1h_file.name,
            "15m_last_start": c15.timestamp_beijing,
            "15m_last_close": c15.close_time_beijing,
            "30m_last_start": c30.timestamp_beijing,
            "30m_last_close": c30.close_time_beijing,
            "1h_last_start": c1h.timestamp_beijing,
            "1h_last_close": c1h.close_time_beijing,
        },
        "adaptive_parameters_used": {
            "long_entry_aggressiveness": format_decimal(long_entry_aggr, 3),
            "short_entry_aggressiveness": format_decimal(short_entry_aggr, 3),
            "long_take_profit_factor": format_decimal(long_tp_factor, 3),
            "short_take_profit_factor": format_decimal(short_tp_factor, 3),
            "stop_loss_atr_factor": format_decimal(stop_factor, 3),
        },
        "plans": plans,
    }
    return {"status": "created", "plan": plan}


def clean_plan_for_log(plan: dict[str, Any]) -> dict[str, Any]:
    """交易计划日志只记录生成时的原始计划，不记录后续跟踪状态。"""
    return {key: value for key, value in plan.items() if key not in {"completed_item_ids", "last_tracked_at_beijing", "last_active_results"}}


def save_trade_plan(config: dict[str, Any], plan: dict[str, Any]) -> None:
    plan_path = PROJECT_ROOT / config["paths"]["latest_trade_plan"]
    active_path = PROJECT_ROOT / config["paths"].get("active_trade_plans", "state/active_trade_plans.json")
    log_path = PROJECT_ROOT / config["paths"]["trade_plans_log"]

    write_json(plan_path, clean_plan_for_log(plan))

    active_plans = read_json(active_path, default=[]) or []
    replaced = False
    for index, existing in enumerate(active_plans):
        if existing.get("plan_id") == plan["plan_id"]:
            # 强制重算同一根K线计划时，更新活跃计划内容，但保留已完成的子指令记录。
            plan["completed_item_ids"] = existing.get("completed_item_ids", [])
            plan["last_tracked_at_beijing"] = existing.get("last_tracked_at_beijing", "")
            plan["last_active_results"] = existing.get("last_active_results", [])
            active_plans[index] = plan
            replaced = True
            break
    if not replaced:
        active_plans.append(plan)
    write_json(active_path, active_plans)

    append_jsonl_unique(log_path, clean_plan_for_log(plan), "plan_id")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成 4 小时 SOL 独立多空交易计划。")
    parser.add_argument("--force", action="store_true", help="即使当前最新K线已生成过计划，也强制生成/覆盖新计划。")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    config = load_config()
    result = generate_trade_plan(config, force=args.force)
    if result["status"] == "skipped":
        print(f"交易计划未更新：{result['reason']}")
        return 0
    plan = result["plan"]
    save_trade_plan(config, plan)
    print(f"已生成交易计划：{plan['plan_id']}")
    print(f"有效期：{plan['created_at_beijing']} -> {plan['valid_until_beijing']}")
    for item in plan["plans"]:
        print(
            f"{item['side']}: entry={item['entry_price']} "
            f"tp={item['take_profit']} sl={item['stop_loss']} rr={item['risk_reward']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
