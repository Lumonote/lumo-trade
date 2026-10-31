---
name: capital-flow-query
description: 查询指定日期/个股的资金面事实数据，只读、不荐股。功能点包括资金流净额事实排序、个股资金流、龙虎榜机构席位、沪深港通个股持股。结果均为历史快照并附来源与时间；按净额的排序只是事实排序而非买入建议，过期数据（如沪深港通）明确标注，不表述为当前值。
---

# Capital Flow Query（资金面查询）

## 用途

对应投研链路中“资金面观察”。返回**资金流、龙虎榜、沪深港通**等事实数据。本技能只做查询：按净额的排序是对数据字段的事实排序，**不是推荐、不是买入信号**。

## 何时使用

- 复盘某交易日主力资金净额的事实分布；
- 查询某只个股当日资金流；
- 查看某交易日或个股的龙虎榜机构席位；
- 查询沪深港通个股历史持股（注意本库该表已过期）。

## 功能点（4 个）

| 功能点 | 必需参数 | 说明 |
|---|---|---|
| `moneyflow_ranking` | `--date`（`--limit`，默认 32） | 当日按 `net_amount` 的事实排序（取 top_n=32 榜单，回退 top_n=0）。 |
| `moneyflow_stock` | `--code --date` | 当日个股资金流（超大/大/中/小单净额）。 |
| `dragon_tiger` | `--date`（可叠加 `--code`） | 龙虎榜机构席位；带 code 时查个股，否则查当日全部。 |
| `hsgt_hold` | `--code` | 沪深港通个股持股历史（本库已过期，明确标注）。 |

## 运行入口

```bash
# 资金流事实排序
python run.py query --function moneyflow_ranking --date 2026-06-01 --pretty

# 个股资金流
python run.py query --function moneyflow_stock --code 000001 --date 2026-06-01

# 当日龙虎榜
python run.py query --function dragon_tiger --date 2026-06-01

# 沪深港通个股持股（过期）
python run.py query --function hsgt_hold --code 600519
```

## 数据性质与限制（重要）

- **全部为历史快照，不是实时行情，也不是资金动向建议。**
- `moneyflow_dc` 覆盖约 `2025-11-05 ~ 2026-07-08`；区间外日期无数据。
- `dragon_tiger_list`（游资净额口径）在本库为空；`dragon_tiger_inst` 为机构席位事实，不代表机构当前观点。
- `hsgt_individual` 本库最新仅到 `2024-08-16`，明显过期，**不能表述为当前北向持股**。
- 缺失日期返回 `no_data` 与最近可参考日期，不构造当日结果。

## 参考资料（按需深入）

- `references/data-model.md`：资金流/龙虎榜/沪深港通表结构、字段单位与口径差异。
