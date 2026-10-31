---
name: holders-survey-query
description: 查询个股的股东结构、机构调研与基金持仓等低频事实数据，只读、不荐股。功能点包括十大流通股东、股东户数、机构调研记录、基金持仓。结果均为报告期历史快照并附来源与时间，不代表相关机构当前观点；无记录时明确返回无数据。
---

# Holders & Survey Query（股东与机构调研查询）

## 用途

对应投研链路中“个股基本面与机构行为”的低频事实查询。返回**十大流通股东、股东户数、机构调研、基金持仓**等报告期数据。本技能只做查询，不据此推断机构态度、不做任何推荐。

## 何时使用

- 需要查看某只个股的股东集中度（十大流通股东、股东户数变化）；
- 需要回看机构调研记录；
- 需要查询基金在报告期对个股的持仓。

## 功能点（4 个）

| 功能点 | 必需参数 | 说明 |
|---|---|---|
| `top_floatholders` | `--code`（`--end-date` 指定报告期，默认最新） | 十大流通股东（排名/名称/持股/比例/变动）。 |
| `holder_number` | `--code` | 股东户数历史（户数/户均持股/变化）。 |
| `institution_survey` | `--code`（`--start --end --limit`） | 机构调研记录（日期/机构/接待/主题）。 |
| `fund_holdings` | `--code`（`--end-date`） | 基金持仓（基金代码/名称/持股/市值/占净值比）。 |

## 运行入口

```bash
# 十大流通股东
python run.py query --function top_floatholders --code 600519 --pretty

# 股东户数
python run.py query --function holder_number --code 600519

# 机构调研
python run.py query --function institution_survey --code 000001 --limit 50

# 基金持仓
python run.py query --function fund_holdings --code 600519
```

## 数据性质与限制（重要）

- **全部为报告期历史快照，低频更新，不是实时数据。**
- 机构调研记录为历史事实，不代表机构当前观点；基金持仓为报告期口径（本库多为 `2026-03-31`）。
- 代码在部分表中可能以裸码或带后缀两种形式出现，网关自动匹配。
- 无记录时返回 `no_data`，不构造数据。

## 参考资料（按需深入）

- `references/data-model.md`：股东/调研/基金表结构、报告期口径与字段含义。
