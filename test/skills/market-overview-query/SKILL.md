---
name: market-overview-query
description: 查询指定日期的市场整体观察数据，只读、不荐股。功能点包括指数日线、市场涨跌家数宽度、全市场估值/换手快照、全市场日线快照。结果均为历史快照，附来源与数据时点；缺失日期返回无数据并给出最近可参考日期，不把历史快照表述为实时行情。
---

# Market Overview Query（市场观察查询）

## 用途

对应投研链路的“观察市场”。返回某交易日的**指数、市场宽度、估值/换手**等整体事实，供研究复核。本技能只做数据查询，不判断方向、不推荐任何标的。

## 何时使用

- 需要复盘某交易日大盘整体表现（指数点位、涨跌家数、估值中位数）；
- 需要为一个个股研究提供当日市场环境背景；
- 想确认某日期是否有全市场数据，而不是把零星指数行误当成全市场。

## 功能点（4 个）

| 功能点 | 必需参数 | 说明 |
|---|---|---|
| `index_daily` | `--date` | 当日指数日线（上证/沪深300/中证1000/国证2000）。 |
| `market_breadth` | `--date` | 当日全市场上涨/下跌/平盘家数、上涨占比与等权平均涨跌幅。 |
| `daily_basic_snapshot` | `--date` | 当日全市场估值/换手汇总（PE/PB/换手率/总市值中位数）+ 少量样本行。 |
| `market_daily_snapshot` | `--date` | 当日全市场日线快照（与 `index_daily` 同表，明确暴露其稀疏性）。 |

## 运行入口

```bash
# 市场宽度
python run.py query --function market_breadth --date 2026-06-01 --pretty

# 估值/换手快照
python run.py query --function daily_basic_snapshot --date 2026-02-06

# 指数日线
python run.py query --function index_daily --date 2026-07-28
```

## 数据性质与限制（重要）

- **全部为历史快照，不是实时行情**。`data_nature=historical_snapshot`，`as_of.date` 为实际数据日期。
- `market_daily` 在本库**仅有 4 条指数行**（每个交易日 2–4 行），并非全市场个股日线；全市场个股日线需由应用在联网时按交易日补齐。本技能会明确标注这一点，避免把指数行误当全市场。
- `market_breadth` 由 `moneyflow_dc(top_n=0)` 的 `pct_change` 逐只统计，该表覆盖约 `2025-11-05 ~ 2026-07-08`，区间外日期无宽度数据。
- `daily_basic_snapshot` 覆盖约 `2025-12-26 ~ 2026-02-09`（全市场约 5400 行/日）。
- 缺失日期返回 `no_data` 与最近可参考日期，不构造当日结果。

## 参考资料（按需深入）

- `references/data-model.md`：指数/宽度/估值的取数口径、字段单位与统计方法。
