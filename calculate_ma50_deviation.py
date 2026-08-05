#!/usr/bin/env python3
"""
读取项目根目录中的 SOL K 线 CSV 文件，输出两部分分析内容。

第一部分：MA50 偏离值统计，并按多空方向区分。
- MA50 = 当前 K 线及之前 49 根 K 线 close 的简单移动平均
- MA50 偏离值 = (close - MA50) / MA50 * 100%
- 多头偏离：偏离值 > 0，即 close 在 MA50 上方
- 空头偏离：偏离值 < 0，即 close 在 MA50 下方

第二部分：技术指标最新值。
- MA：10 / 20 / 50 / 100 / 200，使用 close 的简单移动平均 SMA
- RSI14：使用 Wilder 平滑
- MACD：EMA12 / EMA26 / Signal9，输出 DIF、DEA、柱；柱 = DIF - DEA
- ATR12：使用 Wilder 平滑
- 超级趋势：默认使用 ATR12，乘数默认 3，可通过命令行参数修改
- 枢纽点：经典 Pivot Points，基于最新一根已完成 K线的 high / low / close，输出 PP / R1 / R2 / R3 / S1 / S2 / S3

输出：
- 终端打印分析结果
- 在项目根目录生成 ma50_deviation_summary.csv
- 在项目根目录生成 technical_indicators_summary.csv

数值格式：
- MA50 偏离值输出为带正负号的百分比数值，并追加 % 符号
- 偏离值四舍五入保留小数点后 3 位，例如：+1.905%，-2.878%
- 价格类指标默认保留小数点后 8 位

说明：
- 第一部分前 49 根 K 线无法计算 MA50，会自动跳过。
- 使用 Decimal 做数值计算，避免浮点误差扩大。
- 不依赖第三方库，只使用 Python 标准库。
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, getcontext
from pathlib import Path
from typing import Sequence


getcontext().prec = 28

DEFAULT_INPUT_PATTERN = "SOL_*.csv"
DEFAULT_MA50_OUTPUT_FILE = "ma50_deviation_summary.csv"
DEFAULT_INDICATOR_OUTPUT_FILE = "technical_indicators_summary.csv"
MA50_WINDOW = 50
MA_PERIODS = (10, 20, 50, 100, 200)
RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
ATR_PERIOD = 12
DEFAULT_SUPERTREND_MULTIPLIER = Decimal("3")


@dataclass(frozen=True)
class KlineRow:
    row_number: int
    timestamp_beijing: str
    high: Decimal
    low: Decimal
    close: Decimal
    bar: str


@dataclass(frozen=True)
class DeviationPoint:
    timestamp_beijing: str
    close: Decimal
    ma50: Decimal
    deviation_pct: Decimal


@dataclass(frozen=True)
class SideStats:
    """单边偏离统计。"""

    count: int
    max_deviation_pct: Decimal | None
    max_deviation_time: str
    max_deviation_close: Decimal | None
    max_deviation_ma50: Decimal | None
    min_deviation_pct: Decimal | None
    min_deviation_time: str
    min_deviation_close: Decimal | None
    min_deviation_ma50: Decimal | None
    avg_deviation_pct: Decimal | None


@dataclass(frozen=True)
class FileSummary:
    file_name: str
    bar: str
    total_rows: int
    ma50_count: int
    first_ma50_time: str
    last_ma50_time: str
    long_stats: SideStats
    short_stats: SideStats
    zero_count: int


@dataclass(frozen=True)
class PivotPoints:
    """经典枢纽点价格。"""

    pivot: Decimal
    r1: Decimal
    r2: Decimal
    r3: Decimal
    s1: Decimal
    s2: Decimal
    s3: Decimal


@dataclass(frozen=True)
class IndicatorSummary:
    file_name: str
    bar: str
    total_rows: int
    latest_time: str
    latest_close: Decimal
    ma10: Decimal | None
    ma20: Decimal | None
    ma50: Decimal | None
    ma100: Decimal | None
    ma200: Decimal | None
    rsi14: Decimal | None
    macd_dif: Decimal | None
    macd_dea: Decimal | None
    macd_hist: Decimal | None
    atr12: Decimal | None
    supertrend_value: Decimal | None
    supertrend_direction: str
    supertrend_period: int
    supertrend_multiplier: Decimal
    pivot_source_time: str
    pivot_point: Decimal
    pivot_r1: Decimal
    pivot_r2: Decimal
    pivot_r3: Decimal
    pivot_s1: Decimal
    pivot_s2: Decimal
    pivot_s3: Decimal


def parse_decimal(value: str, *, file_path: Path, row_number: int, field_name: str) -> Decimal:
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{file_path.name} 第 {row_number} 行字段 {field_name} 不是合法数字：{value!r}") from exc
    if not number.is_finite():
        raise ValueError(f"{file_path.name} 第 {row_number} 行字段 {field_name} 不是有限数字：{value!r}")
    return number


def read_kline_csv(file_path: Path) -> list[KlineRow]:
    """读取单个 K 线 CSV，返回按文件顺序排列的数据。"""
    rows: list[KlineRow] = []

    with file_path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        required_fields = {"timestamp_beijing", "high", "low", "close", "bar"}
        fieldnames = set(reader.fieldnames or [])
        missing = required_fields - fieldnames
        if missing:
            raise ValueError(f"{file_path.name} 缺少必要字段：{', '.join(sorted(missing))}")

        for row_number, row in enumerate(reader, start=2):
            timestamp_beijing = (row.get("timestamp_beijing") or "").strip()
            bar = (row.get("bar") or "").strip()
            high = parse_decimal(row.get("high", ""), file_path=file_path, row_number=row_number, field_name="high")
            low = parse_decimal(row.get("low", ""), file_path=file_path, row_number=row_number, field_name="low")
            close = parse_decimal(row.get("close", ""), file_path=file_path, row_number=row_number, field_name="close")

            if not timestamp_beijing:
                raise ValueError(f"{file_path.name} 第 {row_number} 行 timestamp_beijing 为空")
            if not bar:
                raise ValueError(f"{file_path.name} 第 {row_number} 行 bar 为空")
            if min(high, low, close) <= 0:
                raise ValueError(f"{file_path.name} 第 {row_number} 行 high/low/close 必须为正数")
            if high < low:
                raise ValueError(f"{file_path.name} 第 {row_number} 行 high 小于 low：high={high} low={low}")
            if high < close or low > close:
                raise ValueError(f"{file_path.name} 第 {row_number} 行 close 不在 high/low 范围内")

            rows.append(
                KlineRow(
                    row_number=row_number,
                    timestamp_beijing=timestamp_beijing,
                    high=high,
                    low=low,
                    close=close,
                    bar=bar,
                )
            )

    if len(rows) < MA50_WINDOW:
        raise ValueError(f"{file_path.name} 数据不足 {MA50_WINDOW} 行，无法计算 MA50；当前 {len(rows)} 行")

    return rows


def infer_bar_from_file_name(file_name: str) -> str:
    """从新命名格式 SOL_15m_... / SOL_30m_... / SOL_1h_... 中解析周期。"""
    match = re.match(r"^[A-Za-z0-9]+_([^_]+)_\d{12}_\d{12}\.csv$", file_name)
    return match.group(1) if match else ""


def normalized_bar_for_sort(file_name: str) -> str:
    return infer_bar_from_file_name(file_name).lower()


def calculate_deviation_points(rows: Sequence[KlineRow]) -> list[DeviationPoint]:
    """计算每一根可用 K 线的 MA50 偏离百分比。"""
    points: list[DeviationPoint] = []
    rolling_sum = Decimal("0")

    for idx, row in enumerate(rows):
        rolling_sum += row.close
        if idx >= MA50_WINDOW:
            rolling_sum -= rows[idx - MA50_WINDOW].close

        if idx < MA50_WINDOW - 1:
            continue

        ma50 = rolling_sum / Decimal(MA50_WINDOW)
        if ma50 <= 0:
            raise ValueError(f"{row.timestamp_beijing} MA50 非法：{ma50}")
        deviation_pct = (row.close - ma50) / ma50 * Decimal("100")
        points.append(
            DeviationPoint(
                timestamp_beijing=row.timestamp_beijing,
                close=row.close,
                ma50=ma50,
                deviation_pct=deviation_pct,
            )
        )

    return points


def empty_side_stats() -> SideStats:
    return SideStats(
        count=0,
        max_deviation_pct=None,
        max_deviation_time="",
        max_deviation_close=None,
        max_deviation_ma50=None,
        min_deviation_pct=None,
        min_deviation_time="",
        min_deviation_close=None,
        min_deviation_ma50=None,
        avg_deviation_pct=None,
    )


def summarize_side(points: Sequence[DeviationPoint], *, side: str) -> SideStats:
    """
    统计单边偏离。

    side='long'：
      - 最大偏离值 = 正偏离中数值最大的点
      - 最小偏离值 = 正偏离中数值最小的点

    side='short'：
      - 最大偏离值 = 负偏离中绝对幅度最大的点，也就是数值最小/最负的点
      - 最小偏离值 = 负偏离中绝对幅度最小的点，也就是最接近 0 的点
      - 输出仍保留负号
    """
    if not points:
        return empty_side_stats()

    if side == "long":
        max_point = max(points, key=lambda p: p.deviation_pct)
        min_point = min(points, key=lambda p: p.deviation_pct)
    elif side == "short":
        max_point = min(points, key=lambda p: p.deviation_pct)  # 绝对偏离最大，保留负号
        min_point = max(points, key=lambda p: p.deviation_pct)  # 绝对偏离最小，最接近 0
    else:
        raise ValueError(f"未知 side：{side!r}")

    avg_deviation = sum((p.deviation_pct for p in points), Decimal("0")) / Decimal(len(points))

    return SideStats(
        count=len(points),
        max_deviation_pct=max_point.deviation_pct,
        max_deviation_time=max_point.timestamp_beijing,
        max_deviation_close=max_point.close,
        max_deviation_ma50=max_point.ma50,
        min_deviation_pct=min_point.deviation_pct,
        min_deviation_time=min_point.timestamp_beijing,
        min_deviation_close=min_point.close,
        min_deviation_ma50=min_point.ma50,
        avg_deviation_pct=avg_deviation,
    )


def summarize_ma50_deviation(file_path: Path, rows: Sequence[KlineRow]) -> FileSummary:
    points = calculate_deviation_points(rows)
    if not points:
        raise ValueError(f"{file_path.name} 没有可统计的 MA50 偏离值")

    long_points = [p for p in points if p.deviation_pct > 0]
    short_points = [p for p in points if p.deviation_pct < 0]
    zero_count = len(points) - len(long_points) - len(short_points)

    bars = {row.bar for row in rows}
    bar = rows[0].bar if len(bars) == 1 else infer_bar_from_file_name(file_path.name) or "MIXED"

    return FileSummary(
        file_name=file_path.name,
        bar=bar,
        total_rows=len(rows),
        ma50_count=len(points),
        first_ma50_time=points[0].timestamp_beijing,
        last_ma50_time=points[-1].timestamp_beijing,
        long_stats=summarize_side(long_points, side="long"),
        short_stats=summarize_side(short_points, side="short"),
        zero_count=zero_count,
    )


def latest_sma(rows: Sequence[KlineRow], period: int) -> Decimal | None:
    if len(rows) < period:
        return None
    return sum((row.close for row in rows[-period:]), Decimal("0")) / Decimal(period)


def calculate_rsi_latest(rows: Sequence[KlineRow], period: int = RSI_PERIOD) -> Decimal | None:
    """计算最新 RSI，使用 Wilder 平滑。"""
    if len(rows) <= period:
        return None

    gains: list[Decimal] = []
    losses: list[Decimal] = []
    for idx in range(1, period + 1):
        change = rows[idx].close - rows[idx - 1].close
        gains.append(max(change, Decimal("0")))
        losses.append(max(-change, Decimal("0")))

    avg_gain = sum(gains, Decimal("0")) / Decimal(period)
    avg_loss = sum(losses, Decimal("0")) / Decimal(period)

    for idx in range(period + 1, len(rows)):
        change = rows[idx].close - rows[idx - 1].close
        gain = max(change, Decimal("0"))
        loss = max(-change, Decimal("0"))
        avg_gain = (avg_gain * Decimal(period - 1) + gain) / Decimal(period)
        avg_loss = (avg_loss * Decimal(period - 1) + loss) / Decimal(period)

    if avg_loss == 0:
        if avg_gain == 0:
            return Decimal("50")
        return Decimal("100")

    rs = avg_gain / avg_loss
    return Decimal("100") - (Decimal("100") / (Decimal("1") + rs))


def ema_series(values: Sequence[Decimal], period: int) -> list[Decimal]:
    if not values:
        return []
    alpha = Decimal("2") / Decimal(period + 1)
    ema_values: list[Decimal] = [values[0]]
    ema = values[0]
    for value in values[1:]:
        ema = value * alpha + ema * (Decimal("1") - alpha)
        ema_values.append(ema)
    return ema_values


def calculate_macd_latest(
    rows: Sequence[KlineRow],
    *,
    fast: int = MACD_FAST,
    slow: int = MACD_SLOW,
    signal: int = MACD_SIGNAL,
) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    """计算最新 MACD：DIF=EMA12-EMA26，DEA=EMA9(DIF)，柱=DIF-DEA。"""
    if len(rows) < slow:
        return None, None, None

    closes = [row.close for row in rows]
    fast_ema = ema_series(closes, fast)
    slow_ema = ema_series(closes, slow)
    dif_series = [fast_value - slow_value for fast_value, slow_value in zip(fast_ema, slow_ema)]
    dea_series = ema_series(dif_series, signal)
    dif = dif_series[-1]
    dea = dea_series[-1]
    hist = dif - dea
    return dif, dea, hist


def true_ranges(rows: Sequence[KlineRow]) -> list[Decimal]:
    trs: list[Decimal] = []
    for idx, row in enumerate(rows):
        high_low = row.high - row.low
        if idx == 0:
            trs.append(high_low)
            continue
        prev_close = rows[idx - 1].close
        trs.append(max(high_low, abs(row.high - prev_close), abs(row.low - prev_close)))
    return trs


def atr_series(rows: Sequence[KlineRow], period: int = ATR_PERIOD) -> list[Decimal | None]:
    """计算 ATR 序列，使用 Wilder 平滑。ATR 第一个有效值在 period-1 位置。"""
    if len(rows) < period:
        return [None for _ in rows]

    trs = true_ranges(rows)
    atr_values: list[Decimal | None] = [None for _ in rows]
    atr = sum(trs[:period], Decimal("0")) / Decimal(period)
    atr_values[period - 1] = atr

    for idx in range(period, len(rows)):
        atr = (atr * Decimal(period - 1) + trs[idx]) / Decimal(period)
        atr_values[idx] = atr

    return atr_values


def calculate_atr_latest(rows: Sequence[KlineRow], period: int = ATR_PERIOD) -> Decimal | None:
    values = atr_series(rows, period)
    return values[-1] if values else None


def calculate_supertrend_latest(
    rows: Sequence[KlineRow],
    *,
    period: int = ATR_PERIOD,
    multiplier: Decimal = DEFAULT_SUPERTREND_MULTIPLIER,
) -> tuple[Decimal | None, str]:
    """计算最新超级趋势值和方向。方向：多头趋势 / 空头趋势。"""
    if len(rows) < period:
        return None, ""

    atr_values = atr_series(rows, period)
    final_upper: list[Decimal | None] = [None for _ in rows]
    final_lower: list[Decimal | None] = [None for _ in rows]
    supertrend: list[Decimal | None] = [None for _ in rows]
    direction: list[str] = ["" for _ in rows]

    for idx, row in enumerate(rows):
        atr = atr_values[idx]
        if atr is None:
            continue

        hl2 = (row.high + row.low) / Decimal("2")
        basic_upper = hl2 + multiplier * atr
        basic_lower = hl2 - multiplier * atr

        if idx == 0 or final_upper[idx - 1] is None or final_lower[idx - 1] is None or supertrend[idx - 1] is None:
            final_upper[idx] = basic_upper
            final_lower[idx] = basic_lower
            if row.close >= hl2:
                supertrend[idx] = basic_lower
                direction[idx] = "多头趋势"
            else:
                supertrend[idx] = basic_upper
                direction[idx] = "空头趋势"
            continue

        prev_final_upper = final_upper[idx - 1]
        prev_final_lower = final_lower[idx - 1]
        prev_supertrend = supertrend[idx - 1]
        prev_close = rows[idx - 1].close

        assert prev_final_upper is not None
        assert prev_final_lower is not None
        assert prev_supertrend is not None

        if basic_upper < prev_final_upper or prev_close > prev_final_upper:
            current_final_upper = basic_upper
        else:
            current_final_upper = prev_final_upper

        if basic_lower > prev_final_lower or prev_close < prev_final_lower:
            current_final_lower = basic_lower
        else:
            current_final_lower = prev_final_lower

        final_upper[idx] = current_final_upper
        final_lower[idx] = current_final_lower

        if prev_supertrend == prev_final_upper:
            if row.close <= current_final_upper:
                supertrend[idx] = current_final_upper
                direction[idx] = "空头趋势"
            else:
                supertrend[idx] = current_final_lower
                direction[idx] = "多头趋势"
        elif prev_supertrend == prev_final_lower:
            if row.close >= current_final_lower:
                supertrend[idx] = current_final_lower
                direction[idx] = "多头趋势"
            else:
                supertrend[idx] = current_final_upper
                direction[idx] = "空头趋势"
        else:
            # 兜底逻辑，正常情况下不会走到这里。
            if row.close >= current_final_lower:
                supertrend[idx] = current_final_lower
                direction[idx] = "多头趋势"
            else:
                supertrend[idx] = current_final_upper
                direction[idx] = "空头趋势"

    return supertrend[-1], direction[-1]


def calculate_classic_pivot_points(row: KlineRow) -> PivotPoints:
    """
    计算经典枢纽点价格。

    基于指定 K 线的 high / low / close：
    PP = (H + L + C) / 3
    R1 = 2 * PP - L
    S1 = 2 * PP - H
    R2 = PP + (H - L)
    S2 = PP - (H - L)
    R3 = H + 2 * (PP - L)
    S3 = L - 2 * (H - PP)
    """
    high = row.high
    low = row.low
    close = row.close
    pivot = (high + low + close) / Decimal("3")
    price_range = high - low

    return PivotPoints(
        pivot=pivot,
        r1=Decimal("2") * pivot - low,
        r2=pivot + price_range,
        r3=high + Decimal("2") * (pivot - low),
        s1=Decimal("2") * pivot - high,
        s2=pivot - price_range,
        s3=low - Decimal("2") * (high - pivot),
    )


def summarize_indicators(
    file_path: Path,
    rows: Sequence[KlineRow],
    *,
    supertrend_period: int,
    supertrend_multiplier: Decimal,
) -> IndicatorSummary:
    bars = {row.bar for row in rows}
    bar = rows[0].bar if len(bars) == 1 else infer_bar_from_file_name(file_path.name) or "MIXED"

    ma_values = {period: latest_sma(rows, period) for period in MA_PERIODS}
    macd_dif, macd_dea, macd_hist = calculate_macd_latest(rows)
    supertrend_value, supertrend_direction = calculate_supertrend_latest(
        rows,
        period=supertrend_period,
        multiplier=supertrend_multiplier,
    )
    pivot_points = calculate_classic_pivot_points(rows[-1])

    return IndicatorSummary(
        file_name=file_path.name,
        bar=bar,
        total_rows=len(rows),
        latest_time=rows[-1].timestamp_beijing,
        latest_close=rows[-1].close,
        ma10=ma_values[10],
        ma20=ma_values[20],
        ma50=ma_values[50],
        ma100=ma_values[100],
        ma200=ma_values[200],
        rsi14=calculate_rsi_latest(rows, RSI_PERIOD),
        macd_dif=macd_dif,
        macd_dea=macd_dea,
        macd_hist=macd_hist,
        atr12=calculate_atr_latest(rows, ATR_PERIOD),
        supertrend_value=supertrend_value,
        supertrend_direction=supertrend_direction,
        supertrend_period=supertrend_period,
        supertrend_multiplier=supertrend_multiplier,
        pivot_source_time=rows[-1].timestamp_beijing,
        pivot_point=pivot_points.pivot,
        pivot_r1=pivot_points.r1,
        pivot_r2=pivot_points.r2,
        pivot_r3=pivot_points.r3,
        pivot_s1=pivot_points.s1,
        pivot_s2=pivot_points.s2,
        pivot_s3=pivot_points.s3,
    )


def decimal_to_str(value: Decimal | None, places: int = 8) -> str:
    """把 Decimal 格式化为固定小数位字符串；None 输出空字符串。"""
    if value is None:
        return ""
    quant = Decimal("1").scaleb(-places)
    return str(value.quantize(quant, rounding=ROUND_HALF_UP))


def signed_decimal_to_str(value: Decimal | None, places: int = 8) -> str:
    """带正负号输出 Decimal；None 输出空字符串。"""
    if value is None:
        return ""
    quant = Decimal("1").scaleb(-places)
    rounded = value.quantize(quant, rounding=ROUND_HALF_UP)
    sign = "+" if value >= 0 else ""
    return f"{sign}{rounded}"


def signed_percent_to_str(value: Decimal | None, places: int = 3) -> str:
    """
    偏离值格式：带正负号，保留 3 位小数，并追加 % 符号。

    注意：value 本身已经是百分比数值，例如 Decimal('1.905') 表示 1.905%。
    """
    if value is None:
        return ""
    quant = Decimal("1").scaleb(-places)
    rounded = value.quantize(quant, rounding=ROUND_HALF_UP)

    # 使用原始值判断正负方向。这样负偏离即使四舍五入为 0.000，也会显示为 -0.000%，
    # 便于保留多空方向信息。
    sign = "+" if value >= 0 else ""
    return f"{sign}{rounded}%"


def parse_decimal_arg(raw: str) -> Decimal:
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(f"不是合法数字：{raw!r}") from exc
    if not value.is_finite() or value <= 0:
        raise argparse.ArgumentTypeError(f"必须是大于 0 的有限数字：{raw!r}")
    return value


def discover_input_files(input_dir: Path, pattern: str, excluded_file_names: set[str]) -> list[Path]:
    files = []
    for path in input_dir.glob(pattern):
        if not path.is_file():
            continue
        if path.name in excluded_file_names:
            continue
        # 只处理 K 线数据文件，避免误读其它 SOL_*.csv。
        if not re.match(r"^[A-Za-z0-9]+_(15m|30m|1h)_\d{12}_\d{12}\.csv$", path.name, flags=re.IGNORECASE):
            continue
        files.append(path)

    order = {"15m": 0, "30m": 1, "1h": 2}
    return sorted(files, key=lambda p: (order.get(normalized_bar_for_sort(p.name), 99), p.name))


def side_row_values(stats: SideStats) -> list[str | int]:
    return [
        stats.count,
        signed_percent_to_str(stats.max_deviation_pct),
        stats.max_deviation_time,
        decimal_to_str(stats.max_deviation_close),
        decimal_to_str(stats.max_deviation_ma50),
        signed_percent_to_str(stats.min_deviation_pct),
        stats.min_deviation_time,
        decimal_to_str(stats.min_deviation_close),
        decimal_to_str(stats.min_deviation_ma50),
        signed_percent_to_str(stats.avg_deviation_pct),
    ]


def write_ma50_summary_csv(output_path: Path, summaries: Sequence[FileSummary]) -> None:
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "file_name",
                "bar",
                "total_rows",
                "ma50_count",
                "first_ma50_time",
                "last_ma50_time",
                "long_count",
                "long_max_deviation",
                "long_max_deviation_time",
                "long_max_deviation_close",
                "long_max_deviation_ma50",
                "long_min_deviation",
                "long_min_deviation_time",
                "long_min_deviation_close",
                "long_min_deviation_ma50",
                "long_avg_deviation",
                "short_count",
                "short_max_deviation",
                "short_max_deviation_time",
                "short_max_deviation_close",
                "short_max_deviation_ma50",
                "short_min_deviation",
                "short_min_deviation_time",
                "short_min_deviation_close",
                "short_min_deviation_ma50",
                "short_avg_deviation",
                "zero_count",
            ]
        )
        for summary in summaries:
            writer.writerow(
                [
                    summary.file_name,
                    summary.bar,
                    summary.total_rows,
                    summary.ma50_count,
                    summary.first_ma50_time,
                    summary.last_ma50_time,
                    *side_row_values(summary.long_stats),
                    *side_row_values(summary.short_stats),
                    summary.zero_count,
                ]
            )


def write_indicator_summary_csv(output_path: Path, summaries: Sequence[IndicatorSummary]) -> None:
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "file_name",
                "bar",
                "total_rows",
                "latest_time",
                "latest_close",
                "ma10",
                "ma20",
                "ma50",
                "ma100",
                "ma200",
                "rsi14",
                "macd_dif",
                "macd_dea",
                "macd_hist",
                "atr12",
                "supertrend_value",
                "supertrend_direction",
                "supertrend_period",
                "supertrend_multiplier",
                "pivot_source_time",
                "pivot_point",
                "pivot_r1",
                "pivot_r2",
                "pivot_r3",
                "pivot_s1",
                "pivot_s2",
                "pivot_s3",
            ]
        )
        for summary in summaries:
            writer.writerow(
                [
                    summary.file_name,
                    summary.bar,
                    summary.total_rows,
                    summary.latest_time,
                    decimal_to_str(summary.latest_close),
                    decimal_to_str(summary.ma10),
                    decimal_to_str(summary.ma20),
                    decimal_to_str(summary.ma50),
                    decimal_to_str(summary.ma100),
                    decimal_to_str(summary.ma200),
                    decimal_to_str(summary.rsi14, places=3),
                    signed_decimal_to_str(summary.macd_dif),
                    signed_decimal_to_str(summary.macd_dea),
                    signed_decimal_to_str(summary.macd_hist),
                    decimal_to_str(summary.atr12),
                    decimal_to_str(summary.supertrend_value),
                    summary.supertrend_direction,
                    summary.supertrend_period,
                    decimal_to_str(summary.supertrend_multiplier, places=3),
                    summary.pivot_source_time,
                    decimal_to_str(summary.pivot_point),
                    decimal_to_str(summary.pivot_r1),
                    decimal_to_str(summary.pivot_r2),
                    decimal_to_str(summary.pivot_r3),
                    decimal_to_str(summary.pivot_s1),
                    decimal_to_str(summary.pivot_s2),
                    decimal_to_str(summary.pivot_s3),
                ]
            )


def print_side_stats(title: str, stats: SideStats) -> None:
    print(f"{title}样本数：{stats.count}")
    if stats.count == 0:
        print(f"{title}最大偏离值：无")
        print(f"{title}最小偏离值：无")
        print(f"{title}平均偏离值：无")
        return

    print(
        f"{title}最大偏离值：{signed_percent_to_str(stats.max_deviation_pct)} "
        f"@ {stats.max_deviation_time} "
        f"close={decimal_to_str(stats.max_deviation_close)} "
        f"MA50={decimal_to_str(stats.max_deviation_ma50)}"
    )
    print(
        f"{title}最小偏离值：{signed_percent_to_str(stats.min_deviation_pct)} "
        f"@ {stats.min_deviation_time} "
        f"close={decimal_to_str(stats.min_deviation_close)} "
        f"MA50={decimal_to_str(stats.min_deviation_ma50)}"
    )
    print(f"{title}平均偏离值：{signed_percent_to_str(stats.avg_deviation_pct)}")


def print_ma50_summary(summaries: Sequence[FileSummary], output_path: Path) -> None:
    print("第一部分：MA50 偏离值统计")
    print("公式：MA50 = 最近 50 根 close 的简单平均；偏离值 = (close - MA50) / MA50 * 100%")
    print("输出：带正负号，保留 3 位小数，并追加 % 符号；例如 +1.905%。")
    print("说明：前 49 根 K 线无法计算 MA50，已跳过。")
    print("空头最大/最小偏离值按绝对偏离幅度判断，输出仍保留负号。")
    print()

    for summary in summaries:
        print(f"文件：{summary.file_name}")
        print(f"周期：{summary.bar}，总行数：{summary.total_rows}，参与统计：{summary.ma50_count}，零偏离：{summary.zero_count}")
        print(f"统计区间：{summary.first_ma50_time} -> {summary.last_ma50_time}")
        print_side_stats("多头", summary.long_stats)
        print_side_stats("空头", summary.short_stats)
        print()

    print(f"第一部分统计结果 CSV 已保存：{output_path}")
    print()


def print_indicator_summary(summaries: Sequence[IndicatorSummary], output_path: Path) -> None:
    print("第二部分：技术指标最新值")
    print("MA：SMA10 / SMA20 / SMA50 / SMA100 / SMA200，基于 close。")
    print("RSI14：Wilder 平滑。")
    print("MACD：DIF=EMA12-EMA26，DEA=EMA9(DIF)，柱=DIF-DEA。")
    print("ATR12：Wilder 平滑。")
    if summaries:
        print(
            "超级趋势："
            f"ATR周期={summaries[0].supertrend_period}，"
            f"乘数={decimal_to_str(summaries[0].supertrend_multiplier, places=3)}。"
        )
    print("枢纽点：经典 Pivot Points，基于最新一根已完成 K线的 high / low / close，输出 PP / R1 / R2 / R3 / S1 / S2 / S3。")
    print()

    for summary in summaries:
        print(f"文件：{summary.file_name}")
        print(f"周期：{summary.bar}，最新K线：{summary.latest_time}，close={decimal_to_str(summary.latest_close)}")
        print(
            "MA："
            f"MA10={decimal_to_str(summary.ma10)}，"
            f"MA20={decimal_to_str(summary.ma20)}，"
            f"MA50={decimal_to_str(summary.ma50)}，"
            f"MA100={decimal_to_str(summary.ma100)}，"
            f"MA200={decimal_to_str(summary.ma200)}"
        )
        print(f"RSI14：{decimal_to_str(summary.rsi14, places=3)}")
        print(
            "MACD："
            f"DIF={signed_decimal_to_str(summary.macd_dif)}，"
            f"DEA={signed_decimal_to_str(summary.macd_dea)}，"
            f"柱={signed_decimal_to_str(summary.macd_hist)}"
        )
        print(f"ATR12：{decimal_to_str(summary.atr12)}")
        print(f"超级趋势：{decimal_to_str(summary.supertrend_value)}，方向={summary.supertrend_direction}")
        print(
            "枢纽点："
            f"PP={decimal_to_str(summary.pivot_point)}，"
            f"R1={decimal_to_str(summary.pivot_r1)}，"
            f"R2={decimal_to_str(summary.pivot_r2)}，"
            f"R3={decimal_to_str(summary.pivot_r3)}，"
            f"S1={decimal_to_str(summary.pivot_s1)}，"
            f"S2={decimal_to_str(summary.pivot_s2)}，"
            f"S3={decimal_to_str(summary.pivot_s3)}"
        )
        print()

    print(f"第二部分统计结果 CSV 已保存：{output_path}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="统计目录内 SOL K 线 CSV，输出 MA50 偏离和技术指标。")
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="输入 CSV 所在目录，默认脚本所在目录，即项目根目录。",
    )
    parser.add_argument(
        "--pattern",
        default=DEFAULT_INPUT_PATTERN,
        help=f"输入文件 glob 匹配规则，默认 {DEFAULT_INPUT_PATTERN!r}。",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help=f"第一部分 MA50 偏离统计输出 CSV，默认 input-dir/{DEFAULT_MA50_OUTPUT_FILE}。",
    )
    parser.add_argument(
        "--indicator-output",
        type=Path,
        default=None,
        help=f"第二部分技术指标输出 CSV，默认 input-dir/{DEFAULT_INDICATOR_OUTPUT_FILE}。",
    )
    parser.add_argument(
        "--supertrend-period",
        type=int,
        default=ATR_PERIOD,
        help=f"超级趋势 ATR 周期，默认 {ATR_PERIOD}。",
    )
    parser.add_argument(
        "--supertrend-multiplier",
        type=parse_decimal_arg,
        default=DEFAULT_SUPERTREND_MULTIPLIER,
        help=f"超级趋势 ATR 乘数，默认 {DEFAULT_SUPERTREND_MULTIPLIER}。",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    input_dir = args.input_dir.resolve()
    ma50_output_path = args.output.resolve() if args.output else input_dir / DEFAULT_MA50_OUTPUT_FILE
    indicator_output_path = (
        args.indicator_output.resolve() if args.indicator_output else input_dir / DEFAULT_INDICATOR_OUTPUT_FILE
    )

    if args.supertrend_period <= 0:
        print("--supertrend-period 必须大于 0", file=sys.stderr)
        return 1

    files = discover_input_files(
        input_dir,
        args.pattern,
        {ma50_output_path.name, indicator_output_path.name},
    )
    if not files:
        print(f"未找到可处理的 K 线 CSV 文件：目录={input_dir}，pattern={args.pattern}", file=sys.stderr)
        return 1

    rows_by_file = [(path, read_kline_csv(path)) for path in files]

    ma50_summaries = [summarize_ma50_deviation(path, rows) for path, rows in rows_by_file]
    indicator_summaries = [
        summarize_indicators(
            path,
            rows,
            supertrend_period=args.supertrend_period,
            supertrend_multiplier=args.supertrend_multiplier,
        )
        for path, rows in rows_by_file
    ]

    write_ma50_summary_csv(ma50_output_path, ma50_summaries)
    write_indicator_summary_csv(indicator_output_path, indicator_summaries)

    print_ma50_summary(ma50_summaries, ma50_output_path)
    print_indicator_summary(indicator_summaries, indicator_output_path)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n用户中断。", file=sys.stderr)
        raise SystemExit(130)
