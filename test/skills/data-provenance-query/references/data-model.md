# 数据模型：来源 / 同步 / 缓存 / 新鲜度

## sync_log（同步日志）
| 列 | 说明 |
|---|---|
| source | 来源，如 `lhb / jgdy / cyq / capital_moneyflow` |
| ts_code | 对应代码（可为空） |
| ran_at | ISO 时间 `YYYY-MM-DDTHH:MM:SS` |
| status | `ok` / `failed` |
| rows | 写入行数 |
| error | 失败原因 |

本库 260 行，范围 `2026-06-01 ~ 2026-06-05`。可见失败统计：cyq 失败 169、lhb 失败 45、jgdy 失败 14。

## kv_cache（键值缓存）
| 列 | 说明 |
|---|---|
| namespace / key | 命名空间 / 键 |
| payload | JSON 文本 |
| updated_at | 更新时间 |
| ttl_seconds | 有效期（0=不过期） |

本库命名空间：`futures_rank`(652)、`quant_radar`(1)。

## sentiment_cache（情绪/快照缓存）
结构同 kv_cache（cache_type + identifier）。本库 2040 行，更新时间 `2026-05-31 ~ 2026-10-14`，类型含 sector / sector_name / dragon_tiger / capital_flow 等。

`cache_status` 只返回 updated_at/ttl 等元信息，不转储大 payload。

## data_freshness（数据新鲜度）
对每个日期数据表：
- latest_date = 表内最新日期（ISO）；
- lag_days_vs_requested = 请求日期 - 最新日期（自然日，正值=表内最新早于请求日期）；
- is_empty = 行数是否为 0。

## source_diagnostics（数据源诊断）
- empty_tables：行数为 0 的表；
- recent_sync_failures：按来源汇总的非 ok 次数；
- inventory：全量表行数/范围。

## 口径
- 元数据与历史记录，不是行情本身；过期缓存不代表当前值。
