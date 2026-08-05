#!/usr/bin/env python3
"""
从 OKX 下载 SOL K 线数据，清洗校验后保存为 CSV。

默认任务：
- SOL-USDT 15m  1440 根
- SOL-USDT 30m  1440 根
- SOL-USDT 1H   1440 根

输出文件保存在项目根目录，命名格式：
SOL_K线级别_周期起点年月日时分_周期终点年月日时分.csv
例如：SOL_15m_202607212230_202608052215.csv

说明：
- CSV 中的 timestamp_beijing 是 K 线起始时间，使用东八区北京时间。
- 默认只保留已确认完成的 K 线，即会丢弃 OKX 返回的当前未收盘 K 线。
- 不依赖第三方库，只使用 Python 标准库。
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Sequence


OKX_BASE_URL = "https://www.okx.com"
BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

# OKX bar 名称是大小写敏感的：分钟用 m，小时用 H。
DEFAULT_TASKS: tuple[tuple[str, int], ...] = (
    ("15m", 1440),
    ("30m", 1440),
    ("1H", 1440),
)

BAR_MS: dict[str, int] = {
    "15m": 15 * 60 * 1000,
    "30m": 30 * 60 * 1000,
    "1H": 60 * 60 * 1000,
}

# OKX 当前 K 线接口 max limit=300；历史 K 线接口 max limit=100。
ENDPOINT_LIMITS = {
    "candles": 300,
    "history-candles": 100,
}


@dataclass(frozen=True)
class Candle:
    ts_ms: int
    open: str
    high: str
    low: str
    close: str
    volume: str
    volume_ccy: str
    volume_quote: str
    confirm: str


def beijing_dt_from_ms(ts_ms: int) -> datetime:
    """把 OKX 毫秒时间戳转换为北京时间 datetime。"""
    return datetime.fromtimestamp(ts_ms / 1000, tz=BEIJING_TZ)


def format_csv_time(ts_ms: int) -> str:
    """CSV 中使用的北京时间格式。"""
    return beijing_dt_from_ms(ts_ms).strftime("%Y-%m-%d %H:%M:%S+08:00")


def format_filename_time(ts_ms: int) -> str:
    """文件名中的北京时间格式：年月日时分。"""
    return beijing_dt_from_ms(ts_ms).strftime("%Y%m%d%H%M")


def format_bar_for_filename(bar: str) -> str:
    """文件名中的 K 线级别标识，按需求统一使用小写，例如 1H -> 1h。"""
    return bar.lower()


def parse_decimal(value: str, field_name: str, ts_ms: int) -> Decimal:
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{ts_ms} 字段 {field_name} 不是合法数字：{value!r}") from exc
    if not number.is_finite():
        raise ValueError(f"{ts_ms} 字段 {field_name} 不是有限数字：{value!r}")
    return number


def normalize_and_validate_row(row: Sequence[str], *, keep_unconfirmed: bool) -> Candle | None:
    """
    OKX 原始 K 线数组格式：
    [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
    """
    if len(row) != 9:
        raise ValueError(f"OKX 返回的 K 线字段数量异常，期望 9 个字段，实际 {len(row)} 个：{row!r}")

    ts_raw, o, h, l, c, vol, vol_ccy, vol_quote, confirm = [str(x).strip() for x in row]

    try:
        ts_ms = int(ts_raw)
    except ValueError as exc:
        raise ValueError(f"非法时间戳：{ts_raw!r}") from exc

    if confirm not in {"0", "1"}:
        raise ValueError(f"{ts_ms} confirm 字段异常：{confirm!r}")

    # 默认丢弃未收盘的 K 线，避免把实时变动中的数据写入训练/回测数据。
    if confirm != "1" and not keep_unconfirmed:
        return None

    open_d = parse_decimal(o, "open", ts_ms)
    high_d = parse_decimal(h, "high", ts_ms)
    low_d = parse_decimal(l, "low", ts_ms)
    close_d = parse_decimal(c, "close", ts_ms)
    volume_d = parse_decimal(vol, "volume", ts_ms)
    volume_ccy_d = parse_decimal(vol_ccy, "volume_ccy", ts_ms)
    volume_quote_d = parse_decimal(vol_quote, "volume_quote", ts_ms)

    if min(open_d, high_d, low_d, close_d) <= 0:
        raise ValueError(f"{ts_ms} OHLC 价格必须为正数：{row!r}")
    if high_d < max(open_d, low_d, close_d):
        raise ValueError(f"{ts_ms} high 小于 open/low/close 中的最大值：{row!r}")
    if low_d > min(open_d, high_d, close_d):
        raise ValueError(f"{ts_ms} low 大于 open/high/close 中的最小值：{row!r}")
    if min(volume_d, volume_ccy_d, volume_quote_d) < 0:
        raise ValueError(f"{ts_ms} 成交量字段不能为负数：{row!r}")

    return Candle(
        ts_ms=ts_ms,
        open=o,
        high=h,
        low=l,
        close=c,
        volume=vol,
        volume_ccy=vol_ccy,
        volume_quote=vol_quote,
        confirm=confirm,
    )


def okx_get(endpoint: str, params: dict[str, str], *, retries: int, timeout: int) -> dict:
    """调用 OKX 公共 REST API，带简单重试。"""
    query = urllib.parse.urlencode(params)
    url = f"{OKX_BASE_URL}/api/v5/market/{endpoint}?{query}"
    headers = {
        # OKX/Cloudflare 对默认 Python User-Agent 可能返回 403，因此显式设置浏览器 UA。
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
        ),
        "Accept": "application/json",
    }

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            if payload.get("code") != "0":
                raise RuntimeError(f"OKX API 错误：code={payload.get('code')} msg={payload.get('msg')}")
            return payload
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:500]
            last_error = RuntimeError(f"HTTP {exc.code}: {body}")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
            last_error = exc

        if attempt < retries:
            time.sleep(min(2 ** (attempt - 1), 8))

    raise RuntimeError(f"请求 OKX 失败，已重试 {retries} 次：{last_error}")


def fetch_page(
    *,
    inst_id: str,
    bar: str,
    endpoint: str,
    after: int | None,
    retries: int,
    timeout: int,
) -> list[list[str]]:
    params: dict[str, str] = {
        "instId": inst_id,
        "bar": bar,
        "limit": str(ENDPOINT_LIMITS[endpoint]),
    }
    if after is not None:
        # OKX after 表示返回早于该 timestamp 的数据。
        params["after"] = str(after)

    payload = okx_get(endpoint, params, retries=retries, timeout=timeout)
    data = payload.get("data", [])
    if not isinstance(data, list):
        raise ValueError(f"OKX data 字段异常：{data!r}")
    return data


def fetch_clean_candles(
    *,
    inst_id: str,
    bar: str,
    count: int,
    keep_unconfirmed: bool,
    request_sleep: float,
    retries: int,
    timeout: int,
) -> list[Candle]:
    """从 OKX 分页拉取、清洗，并返回最新 count 根 K 线，按时间升序排列。"""
    if bar not in BAR_MS:
        raise ValueError(f"暂不支持 bar={bar!r}，当前支持：{', '.join(BAR_MS)}")
    if count <= 0:
        raise ValueError("count 必须大于 0")

    by_ts: dict[int, Candle] = {}
    after: int | None = None
    endpoint = "candles"

    while len(by_ts) < count:
        rows = fetch_page(
            inst_id=inst_id,
            bar=bar,
            endpoint=endpoint,
            after=after,
            retries=retries,
            timeout=timeout,
        )
        time.sleep(request_sleep)

        if not rows:
            # 当前 K 线接口最多返回最近一段数据；不够时继续切到历史 K 线接口。
            if endpoint == "candles":
                endpoint = "history-candles"
                continue
            break

        # OKX 返回顺序为从新到旧；最后一行最旧，用它继续向更早分页。
        try:
            after = int(rows[-1][0])
        except (IndexError, ValueError) as exc:
            raise ValueError(f"OKX 分页游标异常：{rows[-1]!r}") from exc

        for row in rows:
            candle = normalize_and_validate_row(row, keep_unconfirmed=keep_unconfirmed)
            if candle is None:
                continue
            # 去重：同一时间戳只保留一根。
            by_ts[candle.ts_ms] = candle

    if len(by_ts) < count:
        raise RuntimeError(f"{inst_id} {bar} 数据不足：需要 {count} 根，实际只获取到 {len(by_ts)} 根")

    candles = sorted(by_ts.values(), key=lambda x: x.ts_ms)[-count:]
    validate_continuity(candles, bar)
    return candles


def validate_continuity(candles: Sequence[Candle], bar: str) -> None:
    """检查 K 线数量、时间升序、无缺口。"""
    if not candles:
        raise ValueError("K 线为空")

    step_ms = BAR_MS[bar]
    for prev, curr in zip(candles, candles[1:]):
        delta = curr.ts_ms - prev.ts_ms
        if delta != step_ms:
            prev_time = format_csv_time(prev.ts_ms)
            curr_time = format_csv_time(curr.ts_ms)
            raise ValueError(
                f"{bar} K 线时间不连续：{prev_time} -> {curr_time}，"
                f"实际间隔 {delta}ms，期望 {step_ms}ms"
            )


def delete_old_csv_files(*, output_dir: Path, file_symbol: str, bar: str) -> list[Path]:
    """
    删除同一币种、同一 K 线级别的旧 CSV。

    为兼容本脚本早期版本，也会删除旧命名格式：SOL_起点_终点.csv。
    删除动作放在新数据已成功拉取和校验之后、写入新文件之前，避免下载失败时误删旧数据。
    """
    bar_label = format_bar_for_filename(bar)
    same_bar_pattern = f"{file_symbol}_{bar_label}_*.csv"
    # 严格匹配旧命名格式，例如 SOL_202607211700_202608051645.csv。
    # 注意不能用 glob 的 SOL_[0-9]*_[0-9]*.csv，因为它会误匹配 SOL_15m_...。
    legacy_name_re = re.compile(rf"^{re.escape(file_symbol)}_\d{{12}}_\d{{12}}\.csv$")

    candidates: list[Path] = []
    candidates.extend(output_dir.glob(same_bar_pattern))
    candidates.extend(path for path in output_dir.glob(f"{file_symbol}_*.csv") if legacy_name_re.match(path.name))

    deleted: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        if path in seen or not path.is_file():
            continue
        path.unlink()
        deleted.append(path)
        seen.add(path)

    return sorted(deleted)


def write_csv(
    *,
    candles: Sequence[Candle],
    output_dir: Path,
    file_symbol: str,
    inst_id: str,
    bar: str,
) -> tuple[Path, list[Path]]:
    """删除对应旧文件，把清洗后的 K 线写入 CSV，返回新文件路径和已删除旧文件列表。"""
    output_dir.mkdir(parents=True, exist_ok=True)

    start_ts = candles[0].ts_ms
    end_ts = candles[-1].ts_ms
    bar_label = format_bar_for_filename(bar)
    file_name = f"{file_symbol}_{bar_label}_{format_filename_time(start_ts)}_{format_filename_time(end_ts)}.csv"
    output_path = output_dir / file_name
    deleted_old_files = delete_old_csv_files(output_dir=output_dir, file_symbol=file_symbol, bar=bar)

    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "timestamp_beijing",
                "close_time_beijing",
                "timestamp_ms",
                "inst_id",
                "bar",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "volume_ccy",
                "volume_quote",
                "confirm",
            ]
        )
        step_ms = BAR_MS[bar]
        for candle in candles:
            writer.writerow(
                [
                    format_csv_time(candle.ts_ms),
                    format_csv_time(candle.ts_ms + step_ms),
                    candle.ts_ms,
                    inst_id,
                    bar,
                    candle.open,
                    candle.high,
                    candle.low,
                    candle.close,
                    candle.volume,
                    candle.volume_ccy,
                    candle.volume_quote,
                    candle.confirm,
                ]
            )

    return output_path, deleted_old_files


def parse_tasks(raw_tasks: Iterable[str]) -> tuple[tuple[str, int], ...]:
    """解析 --task 参数，格式如 15m:1440。"""
    tasks: list[tuple[str, int]] = []
    for raw in raw_tasks:
        if ":" not in raw:
            raise argparse.ArgumentTypeError(f"任务格式错误：{raw!r}，应为 bar:count，例如 15m:1440")
        bar, count_raw = raw.split(":", 1)
        bar = bar.strip()
        try:
            count = int(count_raw)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"任务 count 不是整数：{raw!r}") from exc
        tasks.append((bar, count))
    return tuple(tasks)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从 OKX 下载 SOL K 线，清洗校验后按北京时间保存为 CSV。"
    )
    parser.add_argument(
        "--inst-id",
        default="SOL-USDT",
        help="OKX 产品 ID，默认 SOL-USDT。",
    )
    parser.add_argument(
        "--file-symbol",
        default="SOL",
        help="输出文件名前缀，默认 SOL。",
    )
    parser.add_argument(
        "--output-dir",
        default=Path(__file__).resolve().parent,
        type=Path,
        help="CSV 输出目录，默认脚本所在目录，即项目根目录。",
    )
    parser.add_argument(
        "--task",
        action="append",
        default=[],
        metavar="BAR:COUNT",
        help="下载任务，可重复传入，例如 --task 15m:1440。未传时使用默认三组任务。",
    )
    parser.add_argument(
        "--keep-unconfirmed",
        action="store_true",
        help="保留 OKX 返回的未确认/未收盘 K 线；默认不保留。",
    )
    parser.add_argument(
        "--request-sleep",
        type=float,
        default=0.15,
        help="每次 API 请求后的等待秒数，用于避免触发限流，默认 0.15。",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=5,
        help="单次请求失败后的最大重试次数，默认 5。",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=20,
        help="单次请求超时时间，单位秒，默认 20。",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    tasks = parse_tasks(args.task) if args.task else DEFAULT_TASKS

    print(f"开始下载：inst_id={args.inst_id}，输出目录={args.output_dir}")
    outputs: list[Path] = []

    for bar, count in tasks:
        print(f"\n拉取 {bar} K 线，共 {count} 根...")
        candles = fetch_clean_candles(
            inst_id=args.inst_id,
            bar=bar,
            count=count,
            keep_unconfirmed=args.keep_unconfirmed,
            request_sleep=args.request_sleep,
            retries=args.retries,
            timeout=args.timeout,
        )
        output_path, deleted_old_files = write_csv(
            candles=candles,
            output_dir=args.output_dir,
            file_symbol=args.file_symbol,
            inst_id=args.inst_id,
            bar=bar,
        )
        outputs.append(output_path)
        if deleted_old_files:
            print("已删除旧文件：" + ", ".join(path.name for path in deleted_old_files))
        print(
            f"完成 {bar}: {len(candles)} 根，"
            f"起点={format_csv_time(candles[0].ts_ms)}，"
            f"终点={format_csv_time(candles[-1].ts_ms)}，"
            f"文件={output_path.name}"
        )

    print("\n全部完成：")
    for path in outputs:
        print(f"- {path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n用户中断。", file=sys.stderr)
        raise SystemExit(130)
