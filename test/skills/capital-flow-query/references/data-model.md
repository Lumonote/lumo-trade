# 数据模型：资金面（资金流 / 龙虎榜 / 沪深港通）

## moneyflow_dc（资金流）
| 列 | 说明 |
|---|---|
| trade_date | 以 ISO 为主、少量紧凑 |
| ts_code | `XXXXXX.SH/SZ` |
| top_n | 快照口径：`0`=全市场（约5957行）、`32`=32行榜单、另有31 |
| net_amount / net_amount_rate | 主力净额 / 净额占比 |
| buy_elg/lg/md/sm_amount(_rate) | 超大/大/中/小单买入额（及占比） |
| pct_change / close | 涨跌幅 / 收盘价 |
| amount_unit | 单位（万元） |

- `moneyflow_ranking`：优先取 `top_n=32` 榜单，按 `net_amount DESC`；缺失时回退 `top_n=0` 全市场截取 `--limit`。
- 覆盖约 `2025-11-05 ~ 2026-07-08`。

## dragon_tiger_inst（龙虎榜机构席位）
| 列 | 说明 |
|---|---|
| ts_code / trade_date | 带后缀代码 / 日期 |
| inst_name / side | 机构名称 / 买卖方向 |
| net_amount / buy_amount / sell_amount | 净额 / 买入 / 卖出 |
| is_quant / quant_confidence | 是否量化席位 / 置信度 |
| reason | 上榜原因 |

本库 16165 行，范围 `2025-06-03 ~ 2026-06-01`。

## dragon_tiger_list（游资净额口径）
本库为空（0 行）。应用对应 Tushare `top_list`，含游资净买入额；当前无数据。

## hsgt_individual（沪深港通个股持股）
| 列 | 说明 |
|---|---|
| ts_code | 裸 6 位代码 |
| trade_date | 日期 |
| hold_vol / hold_ratio | 持股量 / 占比 |
| market_cap | 持股市值 |

本库 3406 行，范围 `2017-03-16 ~ 2024-08-16`，**已过期**，不能表述为当前北向持股。

## 合规口径
- 按净额排序是事实排序，不是推荐、不是买入信号。
- 龙虎榜/沪深港通为历史事实，不代表机构当前观点。
