# 数据模型：个股研究（K线 / 估值 / 财报 / 事实卡片）

## ohlcv（日K）
| 列 | 说明 |
|---|---|
| code | 裸 6 位代码 |
| frequency | `1d`（本库仅有日K） |
| ts | `YYYY-MM-DD HH:MM:SS`（日K为 `00:00:00`） |
| open/high/low/close | 前复权价 |
| volume / amount | 成交量 / 成交额 |

- 前复权（qfq），保证同一序列内买卖区间内部一致。
- 本库：202 只个股、5050 行，范围 `2024-01-02 ~ 2026-08-12`；仅覆盖被单独分析过的个股。
- `daily_kline` 以请求日期为中心，取 `[-window, date]`（默认 window=30）。

## daily_basic（个股估值）
- 代码口径 `XXXXXX.SH/SZ`，日期紧凑。
- 返回个股当日 PE/PB/PS/换手率/股本/总市值/流通市值等。

## financial_statement（财报三大表）
| 列 | 说明 |
|---|---|
| code / ts_code | 裸码 / 带后缀 |
| statement_type | `balance` / `income` / `cashflow` |
| report_date | 报告期 |
| period | 年报/中报/季报说明 |
| source | `akshare` / `tushare` |
| items_json | 行项目 `{字段:数值}` |

本库该表为空（0 行），返回 `no_data`，不编造行项目。

## stock_context（事实卡片）
对同一 code+date 汇总四个来源，各自独立、无数据即为空数组：
- daily_bar（ohlcv）
- valuation（daily_basic）
- moneyflow（moneyflow_dc，top_n=0）
- dragon_tiger_inst（龙虎榜机构席位）

`coverage.sections_with_data / sections_total` 标明有数据的来源数。

## 代码口径匹配
- 裸码表（ohlcv）：`code=?`；
- 带后缀表（daily_basic/moneyflow）：`ts_code LIKE 'bare.%'`；
- 混合表：两者 OR。

## 只读与时效
- 全部历史快照，不做评分、不做盈利预测、不输出买卖建议。
