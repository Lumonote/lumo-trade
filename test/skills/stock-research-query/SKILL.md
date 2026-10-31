---
name: stock-research-query
description: 查询指定个股在指定日期的研究数据，只读、不荐股。功能点包括前复权日K窗口、个股估值（PE/PB/换手/市值）、财报三大表、个股事实卡片（汇总当日K线/估值/资金/龙虎榜事实）。结果均为历史快照并附来源与时间；未覆盖的个股或财报表为空时明确返回无数据，不编造。
---

# Stock Research Query（个股研究查询）

## 用途

对应投研链路的“研究个股”。围绕一个个股代码与一个日期，返回其**日K、估值、财报、当日事实**等原始数据，供研究复核。本技能只呈现事实，不做评分、不做盈利预测、不给出买卖建议。

## 何时使用

- 需要调取某只个股在某日期前后的K线与估值；
- 需要查看财报三大表（资产负债表/利润表/现金流量表）；
- 需要把个股当日的K线、估值、资金、龙虎榜事实汇总成一张可复核的事实卡片。

## 功能点（4 个）

| 功能点 | 必需参数 | 说明 |
|---|---|---|
| `daily_kline` | `--code --date` | 以日期为中心的前复权日K窗口（`--window` 天数，默认 30）。 |
| `valuation` | `--code --date` | 当日个股估值/换手/市值（PE/PB/PS/换手率/总市值/流通市值）。 |
| `financial_statements` | `--code`（`--statement-type balance\|income\|cashflow`，默认 income） | 财报三大表行项目。 |
| `stock_context` | `--code --date` | 事实卡片：当日日K、估值、资金流、龙虎榜机构席位（无数据的字段为空数组）。 |

## 运行入口

```bash
# 日K窗口
python run.py query --function daily_kline --code 000001 --date 2026-06-01 --pretty

# 个股估值
python run.py query --function valuation --code 000001 --date 2026-02-06

# 利润表（本库为空，将返回 no_data）
python run.py query --function financial_statements --code 600519 --statement-type income

# 事实卡片
python run.py query --function stock_context --code 000001 --date 2026-06-01
```

## 数据性质与限制（重要）

- **全部为历史快照，不是实时行情**。
- `ohlcv` 为前复权日K，仅覆盖被单独分析过的个股（本库约 202 只），并非全市场；未覆盖个股返回 `no_data`，不构造K线。
- `financial_statement` 表在本库为空，财报需由应用联网按报告期缓存；当前返回 `no_data`，不编造行项目。
- 个股代码在不同表中口径不同（`ohlcv` 为 6 位裸码，`daily_basic/moneyflow` 为 `XXXXXX.SH/SZ`），网关会自动匹配两种形式。
- `stock_context` 仅汇总已存在的事实，不做任何评级或建议。

## 参考资料（按需深入）

- `references/data-model.md`：个股相关表结构、复权口径、财报字段与事实卡片组装方式。
