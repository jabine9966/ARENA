#!/usr/bin/env python3
"""ARENA 项目通用工具函数。"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parent
BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
CONFIG_PATH = PROJECT_ROOT / "config" / "strategy_config.json"


@dataclass(frozen=True)
class MarketCandle:
    timestamp_beijing: str
    close_time_beijing: str
    timestamp: datetime
    close_time: datetime
    high: Decimal
    low: Decimal
    close: Decimal
    bar: str


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def ensure_project_dirs(config: dict[str, Any]) -> None:
    dirs = [
        PROJECT_ROOT / "config",
        PROJECT_ROOT / "state",
        PROJECT_ROOT / "logs",
        PROJECT_ROOT / "reports",
        PROJECT_ROOT / config["paths"]["report_history_dir"],
    ]
    for path in dirs:
        path.mkdir(parents=True, exist_ok=True)


def to_decimal(value: Any, default: Decimal | None = None) -> Decimal:
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).replace("%", "").replace("+", "").strip())
    except (InvalidOperation, ValueError, AttributeError):
        if default is not None:
            return default
        raise


def parse_percent(value: Any) -> Decimal:
    """解析 +0.505% / -0.601% / 0.505 等百分比数值，返回 Decimal('0.505')。"""
    return to_decimal(value)


def format_decimal(value: Decimal | None, places: int = 8, signed: bool = False) -> str:
    if value is None:
        return ""
    quant = Decimal("1").scaleb(-places)
    rounded = value.quantize(quant, rounding=ROUND_HALF_UP)
    if signed:
        sign = "+" if value >= 0 else ""
        return f"{sign}{rounded}"
    return str(rounded)


def format_price(value: Decimal | None, precision: int = 2) -> str:
    return format_decimal(value, precision)


def format_percent(value: Decimal | None, places: int = 3, signed: bool = True) -> str:
    if value is None:
        return ""
    return f"{format_decimal(value, places, signed=signed)}%"


def parse_beijing_time(value: str) -> datetime:
    raw = value.strip()
    # datetime.fromisoformat 支持 'YYYY-mm-dd HH:MM:SS+08:00'
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BEIJING_TZ)
    return dt.astimezone(BEIJING_TZ)


def format_beijing_time(dt: datetime) -> str:
    return dt.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S+08:00")


def compact_time(dt: datetime) -> str:
    return dt.astimezone(BEIJING_TZ).strftime("%Y%m%d%H%M")


def json_default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return format_decimal(obj, 8).rstrip("0").rstrip(".") if "." in format_decimal(obj, 8) else format_decimal(obj, 8)
    if isinstance(obj, datetime):
        return format_beijing_time(obj)
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=json_default)
        f.write("\n")


def append_jsonl_unique(path: Path, record: dict[str, Any], unique_key: str) -> bool:
    """追加 JSONL；如果 unique_key 对应值已存在，则不重复追加。返回是否写入。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    key_value = record.get(unique_key)
    if key_value is not None and path.exists():
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    existing = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if existing.get(unique_key) == key_value:
                    return False
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=json_default) + "\n")
    return True


def append_jsonl_records_unique(path: Path, records: Iterable[dict[str, Any]], unique_key: str) -> int:
    count = 0
    for record in records:
        if append_jsonl_unique(path, record, unique_key):
            count += 1
    return count


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def bar_file_label(bar: str) -> str:
    return bar.lower()


def discover_latest_kline_file(file_symbol: str, bar: str, root: Path = PROJECT_ROOT) -> Path:
    label = bar_file_label(bar)
    pattern = re.compile(rf"^{re.escape(file_symbol)}_{re.escape(label)}_\d{{12}}_\d{{12}}\.csv$", re.IGNORECASE)
    candidates = [path for path in root.glob(f"{file_symbol}_{label}_*.csv") if path.is_file() and pattern.match(path.name)]
    if not candidates:
        raise FileNotFoundError(f"未找到 {file_symbol} {bar} K线 CSV 文件")
    # 文件名终点时间越大越新。
    return sorted(candidates, key=lambda p: p.name)[-1]


def read_market_candles(file_path: Path) -> list[MarketCandle]:
    candles: list[MarketCandle] = []
    with file_path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        required = {"timestamp_beijing", "close_time_beijing", "high", "low", "close", "bar"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{file_path.name} 缺少字段：{', '.join(sorted(missing))}")
        for row in reader:
            timestamp_raw = (row.get("timestamp_beijing") or "").strip()
            close_time_raw = (row.get("close_time_beijing") or "").strip()
            candles.append(
                MarketCandle(
                    timestamp_beijing=timestamp_raw,
                    close_time_beijing=close_time_raw,
                    timestamp=parse_beijing_time(timestamp_raw),
                    close_time=parse_beijing_time(close_time_raw),
                    high=to_decimal(row.get("high")),
                    low=to_decimal(row.get("low")),
                    close=to_decimal(row.get("close")),
                    bar=(row.get("bar") or "").strip(),
                )
            )
    return candles


def latest_market_close_time(file_symbol: str, bar: str, root: Path = PROJECT_ROOT) -> datetime:
    candles = read_market_candles(discover_latest_kline_file(file_symbol, bar, root))
    if not candles:
        raise ValueError(f"{file_symbol} {bar} K线数据为空")
    return candles[-1].close_time


def read_csv_dicts(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def pct_change_for_side(side: str, entry: Decimal, exit_price: Decimal) -> Decimal:
    if side == "long":
        return (exit_price - entry) / entry * Decimal("100")
    if side == "short":
        return (entry - exit_price) / entry * Decimal("100")
    raise ValueError(f"未知方向：{side}")


def r_multiple_for_side(side: str, entry: Decimal, exit_price: Decimal, stop_loss: Decimal) -> Decimal | None:
    if side == "long":
        risk = entry - stop_loss
        reward = exit_price - entry
    elif side == "short":
        risk = stop_loss - entry
        reward = entry - exit_price
    else:
        raise ValueError(f"未知方向：{side}")
    if risk <= 0:
        return None
    return reward / risk
