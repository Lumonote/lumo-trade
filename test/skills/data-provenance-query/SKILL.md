---
name: data-provenance-query
description: 查询数据来源、同步记录、缓存时效与各表新鲜度，只读、不荐股。功能点包括同步日志、缓存状态（kv/sentiment）、指定日期的数据新鲜度（各表最新日期与滞后天数）、数据源诊断（空表与近期失败统计）。用于在研究前确认数据时点与可用性，是可追溯投研链路的来源核验入口。
---

# Data Provenance Query（数据来源与时效查询）

## 用途

为可追溯投研链路提供“来源核验”。回答**数据从哪来、何时同步、是否过期、哪些表为空、哪些源近期失败**。本技能只做查询与汇总，不做任何推荐。

## 何时使用

- 研究前确认各表最新日期与相对请求日期的滞后天数；
- 排查某数据源为何无数据（查看同步日志与失败原因）；
- 查看缓存的更新时间与 TTL，判断是否过期；
- 生成一份数据源诊断（空表清单 + 近期失败统计）。

## 功能点（4 个）

| 功能点 | 必需参数 | 说明 |
|---|---|---|
| `sync_log` | 可选 `--source --start --end --limit` | 同步日志（来源/时间/状态/行数/错误）。 |
| `cache_status` | 可选 `--namespace --cache-type` | kv_cache 与 sentiment_cache 的更新时间/TTL（不转储大 payload）。 |
| `data_freshness` | `--date` | 各表最新日期、相对请求日期的滞后天数、是否为空。 |
| `source_diagnostics` | 无 | 空表清单 + 近期同步失败统计 + 全量 inventory。 |

## 运行入口

```bash
# 数据新鲜度（以请求日期为基准）
python run.py query --function data_freshness --date 2026-10-29 --pretty

# 数据源诊断
python run.py query --function source_diagnostics

# 同步日志（仅看某来源）
python run.py query --function sync_log --source lhb

# 缓存状态
python run.py query --function cache_status
```

## 数据性质与限制（重要）

- 本技能呈现的是**元数据与历史同步记录**，不是行情本身。
- `lag_days_vs_requested` 为各表最新日期相对请求日期的自然日差；负值表示表内最新早于请求日期（数据滞后）。
- 缓存时效以 `updated_at/ttl_seconds` 为准；过期缓存不代表当前值。
- 空表表示对应功能在本库尚未产生数据，不代表功能不存在。

## 参考资料（按需深入）

- `references/data-model.md`：同步日志/缓存/新鲜度的计算口径与字段含义。
