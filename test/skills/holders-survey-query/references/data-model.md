# 数据模型：股东 / 机构调研 / 基金持仓

## top10_floatholders（十大流通股东）
| 列 | 说明 |
|---|---|
| ts_code | 裸码或带后缀（本库混合） |
| end_date | 报告期 |
| holder_rank | 排名 |
| holder_name | 股东名称 |
| hold_amount / hold_ratio | 持股数量 / 占比 |
| change_type / change_amount | 变动类型 / 变动数量 |

本库 1829 行，范围 `2003-12-31 ~ 2026-03-31`。

## stk_holdernumber（股东户数）
| 列 | 说明 |
|---|---|
| ts_code | 裸码或带后缀 |
| end_date | 报告期 |
| holder_num / avg_hold | 股东户数 / 户均持股 |
| pct_change | 户数变化 |

本库 254 行，范围 `1995-12-31 ~ 2026-03-31`。

## jgdy_detail（机构调研）
| 列 | 说明 |
|---|---|
| ts_code | 带后缀 |
| survey_date | 调研日期 |
| inst_name | 机构名称 |
| reception / topic | 接待 / 调研主题 |

本库 2336 行，范围 `2023-10-31 ~ 2026-06-01`。

## fund_hold_detail（基金持仓）
| 列 | 说明 |
|---|---|
| ts_code / end_date | 本库为裸 6 位代码（无后缀）/ 报告期 |
| fund_code / fund_name | 基金代码 / 名称 |
| hold_shares / market_value | 持股 / 市值 |
| nv_ratio | 占基金净值比（%） |

本库 100 行，均为 `2026-03-31`，仅覆盖 `000001`、`600519` 两只个股。

## 口径说明
- 均为报告期低频快照，不是实时数据；机构调研为历史事实，不代表机构当前观点。
