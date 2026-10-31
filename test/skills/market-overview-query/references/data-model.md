# 数据模型：市场观察（指数 / 宽度 / 估值）

## market_daily（指数日线）
| 列 | 说明 |
|---|---|
| ts_code | 指数代码，本库为 `000001.SH`(上证)、`000300.SH`(沪深300)、`000852.SH`(中证1000)、`399303.SZ`(国证2000) |
| trade_date | 紧凑 `YYYYMMDD` |
| open/high/low/close/pre_close | 点位 |
| change/pct_chg | 涨跌点 / 涨跌幅 |
| vol/amount | 成交量 / 成交额 |

本库实际情况：每个交易日仅 2–4 行指数行，范围 `20250924 ~ 20260729`。**不是全市场个股日线**。

## market_breadth（市场宽度）
- 数据来源：`moneyflow_dc` 中 `top_n=0` 的全市场快照（约 5957 行/日）。
- 统计字段：`pct_change`（当日涨跌幅 %）。
- 计算：
  - up = `pct_change > 0` 的数量；down = `pct_change < 0`；flat = 其余；
  - up_ratio = up / 有效样本；
  - mean_pct_change = 等权平均涨跌幅。
- 口径覆盖：约 `2025-11-05 ~ 2026-07-08`。

## daily_basic（估值 / 换手快照）
| 列 | 说明 |
|---|---|
| ts_code | `XXXXXX.SH/SZ` |
| trade_date | 紧凑 `YYYYMMDD` |
| pe / pe_ttm / pb / ps / ps_ttm | 估值倍数 |
| turnover_rate / turnover_rate_f / volume_ratio | 换手 / 量比 |
| dv_ratio / dv_ttm | 股息率 |
| total_share / float_share / free_share | 股本（万股） |
| total_mv / circ_mv | 总市值 / 流通市值（万元） |

本库：30 个交易日、约 5465 行/日，范围 `20251226 ~ 20260209`。
快照返回各字段**中位数**（中位数对极端值更稳健）+ 前 5 行样本。

## 字段单位
- 价格/点位：元 / 点；股本：万股；市值：万元；成交额：万元（依 Tushare 口径）。
- 涨跌幅、换手率、股息率：%。

## 只读与时效
- 全部为历史快照，`data_nature=historical_snapshot`，`as_of.date` 为实际日期。
- 缺失日期不构造结果，返回 `no_data` 与最近可参考日期。
