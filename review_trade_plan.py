#!/usr/bin/env python3
"""复盘/跟踪所有仍在 4 小时有效期内的交易计划。

核心规则：
- 项目可以每半小时运行一次；
- 每条交易指令从发出起最多有效 4 小时；
- 多头和空头独立复盘，互不排斥；
- 未成交、成交但未止盈止损、已止盈、已止损都会被记录或跟踪；
- 只有终态结果写入 logs/trade_reviews.jsonl；仍在有效期内的中间状态写入 state/latest_review_batch.json。
"""

from __future__ import annotations

import argparse
from decimal import Decimal
from typing import Any

from arena_utils import (
    PROJECT_ROOT,
    MarketCandle,
    append_jsonl_records_unique,
    compact_time,
    discover_latest_kline_file,
    format_beijing_time,
    format_decimal,
    format_percent,
    format_price,
    latest_market_close_time,
    load_config,
    parse_beijing_time,
    pct_change_for_side,
    r_multiple_for_side,
    read_json,
    read_market_candles,
    to_decimal,
    write_json,
)


TERMINAL_RESULTS = {
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
    "no_market_data_expired",
}


ACTIVE_RESULTS = {
    "active_waiting_market_data",
    "active_pending_entry",
    "active_entered_waiting_exit",
}


def is_terminal_result(result: str) -> bool:
    return result in TERMINAL_RESULTS


def candle_touches(candle: MarketCandle, price: Decimal) -> bool:
    return candle.low <= price <= candle.high


def side_take_profit_hit(side: str, candle: MarketCandle, take_profit: Decimal) -> bool:
    return candle.high >= take_profit if side == "long" else candle.low <= take_profit


def side_stop_loss_hit(side: str, candle: MarketCandle, stop_loss: Decimal) -> bool:
    return candle.low <= stop_loss if side == "long" else candle.high >= stop_loss


def favorable_price(side: str, candles: list[MarketCandle]) -> Decimal:
    return max(c.high for c in candles) if side == "long" else min(c.low for c in candles)


def adverse_price(side: str, candles: list[MarketCandle]) -> Decimal:
    return min(c.low for c in candles) if side == "long" else max(c.high for c in candles)


def nearest_price_to_entry(side: str, candles: list[MarketCandle], entry: Decimal) -> Decimal:
    return min(c.low for c in candles) if side == "long" else max(c.high for c in candles)


def not_triggered_result(side: str, candles: list[MarketCandle], take_profit: Decimal, stop_loss: Decimal) -> tuple[str, str]:
    """4小时到期仍未成交时，对未成交原因分类。"""
    if side == "long":
        favorable_hit = max(c.high for c in candles) >= take_profit
        adverse_hit = min(c.low for c in candles) <= stop_loss
    else:
        favorable_hit = min(c.low for c in candles) <= take_profit
        adverse_hit = max(c.high for c in candles) >= stop_loss

    if favorable_hit and adverse_hit:
        return "not_triggered_mixed_outcome", "4小时内未成交，但行情同时到达目标区和风险区，说明计划未触发且波动较大。"
    if favorable_hit:
        return "not_triggered_missed_profit", "4小时内未成交，但价格随后到达止盈价，方向判断有效，建仓价偏保守。"
    if adverse_hit:
        return "not_triggered_avoided_loss", "4小时内未成交，且价格触及止损方向区域，保守入场避免亏损。"
    return "not_triggered_no_signal", "4小时内未成交，且行情未到达止盈或止损区域，本轮计划没有得到充分验证。"


def build_base_record(
    *,
    plan: dict[str, Any],
    item: dict[str, Any],
    review_time: str,
    window: list[MarketCandle],
) -> dict[str, Any]:
    side = item["side"]
    entry = to_decimal(item["entry_price"])
    take_profit = to_decimal(item["take_profit"])
    stop_loss = to_decimal(item["stop_loss"])
    record = {
        "review_id": f"review_{item['plan_item_id']}_{compact_time(parse_beijing_time(plan['valid_until_beijing']))}",
        "plan_id": plan["plan_id"],
        "plan_item_id": item["plan_item_id"],
        "symbol": plan["symbol"],
        "side": side,
        "strategy_version": plan.get("strategy_version", ""),
        "created_at_beijing": plan["created_at_beijing"],
        "valid_until_beijing": plan["valid_until_beijing"],
        "review_time_beijing": review_time,
        "entry_zone": item.get("entry_zone", []),
        "entry_price": format_price(entry, 2),
        "take_profit": format_price(take_profit, 2),
        "stop_loss": format_price(stop_loss, 2),
        "logic_tags": item.get("logic_tags", []),
        "window_candle_count": len(window),
        "window_start": window[0].timestamp_beijing if window else "",
        "window_end": window[-1].close_time_beijing if window else "",
    }
    if window:
        record.update(
            {
                "window_high": format_price(max(c.high for c in window), 8),
                "window_low": format_price(min(c.low for c in window), 8),
                "window_final_close": format_price(window[-1].close, 8),
            }
        )
    return record


def review_plan_item(
    *,
    plan: dict[str, Any],
    item: dict[str, Any],
    window: list[MarketCandle],
    review_time: str,
    flat_threshold_pct: Decimal,
    finalize_due: bool,
) -> dict[str, Any]:
    side = item["side"]
    entry = to_decimal(item["entry_price"])
    take_profit = to_decimal(item["take_profit"])
    stop_loss = to_decimal(item["stop_loss"])
    record = build_base_record(plan=plan, item=item, review_time=review_time, window=window)

    if not window:
        if finalize_due:
            record.update(
                {
                    "was_triggered": False,
                    "result": "no_market_data_expired",
                    "is_terminal": True,
                    "notes": "交易指令4小时有效期已结束，但有效期内没有可用于复盘的K线数据。",
                }
            )
        else:
            record.update(
                {
                    "was_triggered": False,
                    "result": "active_waiting_market_data",
                    "is_terminal": False,
                    "notes": "交易指令已生成，但尚无后续K线可用于判断是否成交。",
                }
            )
        return record

    entry_index: int | None = None
    entry_candle: MarketCandle | None = None
    for idx, candle in enumerate(window):
        if candle_touches(candle, entry):
            entry_index = idx
            entry_candle = candle
            break

    if entry_index is None or entry_candle is None:
        nearest = nearest_price_to_entry(side, window, entry)
        distance = abs(nearest - entry)
        distance_pct = distance / entry * Decimal("100")

        if not finalize_due:
            record.update(
                {
                    "was_triggered": False,
                    "entry_time": "",
                    "exit_time": "",
                    "exit_price": "",
                    "result": "active_pending_entry",
                    "is_terminal": False,
                    "pnl_pct": "",
                    "r_multiple": "",
                    "nearest_price_to_entry": format_price(nearest, 8),
                    "distance_to_entry": format_price(distance, 8),
                    "distance_to_entry_pct": format_percent(distance_pct),
                    "max_favorable_price": format_price(favorable_price(side, window), 8),
                    "max_adverse_price": format_price(adverse_price(side, window), 8),
                    "notes": "交易指令仍在4小时有效期内，当前尚未成交，继续挂单跟踪。",
                }
            )
            return record

        result, notes = not_triggered_result(side, window, take_profit, stop_loss)
        record.update(
            {
                "was_triggered": False,
                "entry_time": "",
                "exit_time": window[-1].close_time_beijing,
                "exit_price": "",
                "result": result,
                "is_terminal": True,
                "pnl_pct": "",
                "r_multiple": "",
                "nearest_price_to_entry": format_price(nearest, 8),
                "distance_to_entry": format_price(distance, 8),
                "distance_to_entry_pct": format_percent(distance_pct),
                "max_favorable_price": format_price(favorable_price(side, window), 8),
                "max_adverse_price": format_price(adverse_price(side, window), 8),
                "notes": notes,
            }
        )
        return record

    post_entry = window[entry_index:]
    max_fav = favorable_price(side, post_entry)
    max_adv = adverse_price(side, post_entry)

    for idx, candle in enumerate(post_entry, start=entry_index):
        tp_hit = side_take_profit_hit(side, candle, take_profit)
        sl_hit = side_stop_loss_hit(side, candle, stop_loss)
        entry_and_exit_same_candle = idx == entry_index and (tp_hit or sl_hit)

        if tp_hit and sl_hit:
            conservative_exit = stop_loss
            optimistic_exit = take_profit
            conservative_pnl = pct_change_for_side(side, entry, conservative_exit)
            optimistic_pnl = pct_change_for_side(side, entry, optimistic_exit)
            record.update(
                {
                    "was_triggered": True,
                    "entry_time": entry_candle.timestamp_beijing,
                    "exit_time": candle.timestamp_beijing,
                    "exit_price": "",
                    "result": "ambiguous_same_candle",
                    "is_terminal": True,
                    "pnl_pct": "",
                    "r_multiple": "",
                    "conservative_result": "entered_stop_loss",
                    "conservative_pnl_pct": format_percent(conservative_pnl),
                    "optimistic_result": "entered_take_profit",
                    "optimistic_pnl_pct": format_percent(optimistic_pnl),
                    "entry_and_exit_same_candle": entry_and_exit_same_candle,
                    "max_favorable_price": format_price(max_fav, 8),
                    "max_adverse_price": format_price(max_adv, 8),
                    "notes": "同一根K线同时触发止盈和止损，仅凭当前周期OHLC无法判断先后顺序，记录为模糊样本。",
                }
            )
            return record

        if tp_hit:
            pnl = pct_change_for_side(side, entry, take_profit)
            r_mult = r_multiple_for_side(side, entry, take_profit, stop_loss)
            record.update(
                {
                    "was_triggered": True,
                    "entry_time": entry_candle.timestamp_beijing,
                    "exit_time": candle.timestamp_beijing,
                    "exit_price": format_price(take_profit, 2),
                    "result": "entered_take_profit",
                    "is_terminal": True,
                    "pnl_pct": format_percent(pnl),
                    "r_multiple": format_decimal(r_mult, 3),
                    "entry_and_exit_same_candle": entry_and_exit_same_candle,
                    "max_favorable_price": format_price(max_fav, 8),
                    "max_adverse_price": format_price(max_adv, 8),
                    "notes": "交易指令成交后命中单层止盈。" + (" 止盈发生在入场同一根K线内。" if entry_and_exit_same_candle else ""),
                }
            )
            return record

        if sl_hit:
            pnl = pct_change_for_side(side, entry, stop_loss)
            r_mult = r_multiple_for_side(side, entry, stop_loss, stop_loss)
            record.update(
                {
                    "was_triggered": True,
                    "entry_time": entry_candle.timestamp_beijing,
                    "exit_time": candle.timestamp_beijing,
                    "exit_price": format_price(stop_loss, 2),
                    "result": "entered_stop_loss",
                    "is_terminal": True,
                    "pnl_pct": format_percent(pnl),
                    "r_multiple": format_decimal(r_mult, 3),
                    "entry_and_exit_same_candle": entry_and_exit_same_candle,
                    "max_favorable_price": format_price(max_fav, 8),
                    "max_adverse_price": format_price(max_adv, 8),
                    "notes": "交易指令成交后触发止损。" + (" 止损发生在入场同一根K线内。" if entry_and_exit_same_candle else ""),
                }
            )
            return record

    final_close = window[-1].close
    pnl = pct_change_for_side(side, entry, final_close)
    r_mult = r_multiple_for_side(side, entry, final_close, stop_loss)

    if not finalize_due:
        record.update(
            {
                "was_triggered": True,
                "entry_time": entry_candle.timestamp_beijing,
                "exit_time": "",
                "exit_price": "",
                "result": "active_entered_waiting_exit",
                "is_terminal": False,
                "floating_pnl_pct": format_percent(pnl),
                "floating_r_multiple": format_decimal(r_mult, 3),
                "max_favorable_price": format_price(max_fav, 8),
                "max_adverse_price": format_price(max_adv, 8),
                "notes": "交易指令已成交，但尚未触发止盈或止损，且仍在4小时有效期内，继续跟踪。",
            }
        )
        return record

    if abs(pnl) <= flat_threshold_pct:
        result = "entered_expired_flat"
        notes = "交易指令成交后，4小时周期结束时未触发止盈或止损，最终基本持平。"
    elif pnl > 0:
        result = "entered_expired_profit"
        notes = "交易指令成交后，4小时周期结束时未触发止盈或止损，但仍处于浮盈。"
    else:
        result = "entered_expired_loss"
        notes = "交易指令成交后，4小时周期结束时未触发止盈或止损，最终处于浮亏。"

    record.update(
        {
            "was_triggered": True,
            "entry_time": entry_candle.timestamp_beijing,
            "exit_time": window[-1].close_time_beijing,
            "exit_price": format_price(final_close, 8),
            "result": result,
            "is_terminal": True,
            "pnl_pct": format_percent(pnl),
            "r_multiple": format_decimal(r_mult, 3),
            "entry_and_exit_same_candle": False,
            "max_favorable_price": format_price(max_fav, 8),
            "max_adverse_price": format_price(max_adv, 8),
            "notes": notes,
        }
    )
    return record


def load_active_plans(config: dict[str, Any]) -> list[dict[str, Any]]:
    active_path = PROJECT_ROOT / config["paths"].get("active_trade_plans", "state/active_trade_plans.json")
    active = read_json(active_path, default=None)
    if isinstance(active, list):
        return active

    # 兼容旧状态：如果 active_trade_plans 不存在，则尝试把 latest_trade_plan 作为待跟踪计划。
    latest = read_json(PROJECT_ROOT / config["paths"]["latest_trade_plan"], default=None)
    if latest and latest.get("reviewed") is not True:
        return [latest]
    return []


def save_active_plans(config: dict[str, Any], active_plans: list[dict[str, Any]]) -> None:
    active_path = PROJECT_ROOT / config["paths"].get("active_trade_plans", "state/active_trade_plans.json")
    write_json(active_path, active_plans)


def update_latest_plan_if_needed(config: dict[str, Any], active_plans: list[dict[str, Any]]) -> None:
    latest_path = PROJECT_ROOT / config["paths"]["latest_trade_plan"]
    latest = read_json(latest_path, default=None)
    if not latest:
        return
    latest_id = latest.get("plan_id")
    for plan in active_plans:
        if plan.get("plan_id") == latest_id:
            write_json(latest_path, plan)
            return
    if latest.get("reviewed") is not True:
        latest["reviewed"] = True
        latest["status"] = "all_items_terminal_or_expired"
        write_json(latest_path, latest)


def review_active_plans(config: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    batch_path = PROJECT_ROOT / config["paths"]["latest_review_batch"]
    review_log_path = PROJECT_ROOT / config["paths"]["trade_reviews_log"]

    active_plans = load_active_plans(config)
    if not active_plans:
        batch = {"status": "no_active_plans", "message": "当前没有需要复盘或跟踪的交易计划。", "results": []}
        write_json(batch_path, batch)
        return batch

    file_symbol = config["file_symbol"]
    review_bar = config["review_rules"]["review_timeframe"]
    latest_close = latest_market_close_time(file_symbol, review_bar, PROJECT_ROOT)
    review_time = format_beijing_time(latest_close)
    candles = read_market_candles(discover_latest_kline_file(file_symbol, review_bar, PROJECT_ROOT))
    flat_threshold = to_decimal(config["review_rules"]["expired_flat_threshold_pct"])

    terminal_results: list[dict[str, Any]] = []
    active_results: list[dict[str, Any]] = []
    remaining_active_plans: list[dict[str, Any]] = []

    for plan in active_plans:
        created_at = parse_beijing_time(plan["created_at_beijing"])
        valid_until = parse_beijing_time(plan["valid_until_beijing"])
        review_end = min(latest_close, valid_until)
        finalize_due = latest_close >= valid_until or force
        window = [c for c in candles if c.timestamp >= created_at and c.close_time <= review_end]

        completed_item_ids = set(plan.get("completed_item_ids", []))
        new_completed_ids = set(completed_item_ids)
        plan_active_results: list[dict[str, Any]] = []

        for item in plan.get("plans", []):
            if item.get("plan_item_id") in completed_item_ids:
                continue
            result = review_plan_item(
                plan=plan,
                item=item,
                window=window,
                review_time=review_time,
                flat_threshold_pct=flat_threshold,
                finalize_due=finalize_due,
            )
            if result.get("is_terminal") is True:
                terminal_results.append(result)
                new_completed_ids.add(item["plan_item_id"])
            else:
                active_results.append(result)
                plan_active_results.append(result)

        plan["completed_item_ids"] = sorted(new_completed_ids)
        plan["last_tracked_at_beijing"] = review_time
        plan["last_active_results"] = plan_active_results

        all_items_completed = len(new_completed_ids) >= len(plan.get("plans", []))
        if all_items_completed:
            plan["reviewed"] = True
            plan["reviewed_at_beijing"] = review_time
            plan["status"] = "completed"
        else:
            remaining_active_plans.append(plan)

    written_count = append_jsonl_records_unique(review_log_path, terminal_results, "review_id")
    save_active_plans(config, remaining_active_plans)
    update_latest_plan_if_needed(config, remaining_active_plans)

    batch = {
        "status": "reviewed_active_plans",
        "review_time_beijing": review_time,
        "review_timeframe": review_bar,
        "active_plan_count_before": len(active_plans),
        "active_plan_count_after": len(remaining_active_plans),
        "terminal_count": len(terminal_results),
        "active_count": len(active_results),
        "written_count": written_count,
        "terminal_results": terminal_results,
        "active_results": active_results,
        # 兼容旧报告读取逻辑：results 放终态结果。
        "results": terminal_results,
    }
    write_json(batch_path, batch)
    return batch


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="复盘/跟踪所有仍在 4 小时有效期内的交易计划。")
    parser.add_argument("--force", action="store_true", help="强制把当前活跃计划按到期状态复盘。")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    config = load_config()
    batch = review_active_plans(config, force=args.force)
    status = batch.get("status")
    if status == "reviewed_active_plans":
        print(
            f"已跟踪/复盘活跃计划：终态 {batch['terminal_count']} 条，"
            f"仍活跃 {batch['active_count']} 条，新增写入 {batch['written_count']} 条。"
        )
        for result in batch.get("terminal_results", []):
            print(f"终态 {result['side']}: {result['result']}，{result.get('notes', '')}")
        for result in batch.get("active_results", []):
            print(f"跟踪 {result['side']}: {result['result']}，{result.get('notes', '')}")
    else:
        print(batch.get("message", f"复盘状态：{status}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
