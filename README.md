# ARENA

ARENA 是一个 SOL-USDT 自动化行情分析、交易计划、复盘和学习优化项目。

## 当前功能

1. 从 OKX 下载 SOL-USDT K线数据：
   - 15m：1440 根
   - 30m：1440 根
   - 1H：1440 根
2. 计算 MA50 多空偏离统计。
3. 计算技术指标：
   - MA10 / MA20 / MA50 / MA100 / MA200
   - RSI14
   - MACD
   - ATR12
   - 超级趋势
   - 经典枢纽点 PP / R1 / R2 / R3 / S1 / S2 / S3
4. 每半小时生成一轮新的独立多空交易计划，每条交易指令有效期为 4 小时。
5. 复盘上一轮交易计划，成交和未成交都会记录。
6. 根据复盘日志更新学习状态。
7. 自动生成 Markdown 分析报告。
8. 支持 GitHub Actions 每半小时定时运行。

## 核心规则

- 多头和空头独立分析，互不排斥，互不取消。
- 每个方向只使用单层止盈，不做分批止盈。
- 项目每半小时执行一次；每条交易指令从发出起最多有效 4 小时。
- 成交、未成交、错过盈利、避免亏损、周期结束浮盈/浮亏都必须记录。
- 学习模块只更新 JSON 参数和记录，不自动修改 Python 策略代码。

## 一键运行

```bash
python run_pipeline.py
```

如果只想使用已有 CSV，不重新下载行情：

```bash
python run_pipeline.py --skip-download
```

## 主要文件

```text
download_okx_sol_klines.py        下载并清洗 OKX K线数据
calculate_ma50_deviation.py       计算 MA50 偏离和技术指标
generate_trade_plan.py            生成结构化交易计划 JSON
review_trade_plan.py              复盘上一轮交易计划
update_learning_state.py          更新学习状态
generate_report.py                生成最终 Markdown 报告
run_pipeline.py                   总控脚本
```

## 状态和日志

```text
state/latest_trade_plan.json      最新结构化交易计划
state/active_trade_plans.json     仍在4小时有效期内、需要继续跟踪的交易计划
state/latest_review_batch.json    最近一次复盘/跟踪批次结果
state/learning_state.json         学习状态和自适应参数
logs/trade_plans.jsonl            历史交易计划日志
logs/trade_reviews.jsonl          历史复盘日志
logs/learning_updates.jsonl       学习更新日志
reports/SOL_trade_analysis_report.md 最新分析报告
reports/history/                  历史报告归档
```

## GitHub Actions

工作流文件：

```text
.github/workflows/arena_pipeline.yml
```

默认每半小时运行一次，在整点和半点后的第 5 分钟执行：

```text
cron: '5,35 * * * *'
```
