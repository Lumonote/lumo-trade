# Kronos 数据查询技能包（只读）

面向本地智能投研工作站的**数据查询**技能集合。技能包把“观察市场 → 研究个股 → 识别资金/风险 → 来源核验”所需的数据访问，以**渐进式披露**组织为多个可发现技能，每个技能包含多个功能点。

**合规边界：所有技能只做数据查询与事实呈现，不做荐股、不做买卖建议、不做目标价、不代客理财。** 每条结果都附数据性质、来源与时间，历史快照不会被表述为实时行情。

## 技能清单（6 个，共 24 个功能点）

| 技能 | 功能点 | 对应链路 |
|---|---|---|
| [market-calendar-query](market-calendar-query/SKILL.md) | is_trading_day · list_open_days · nearest_trading_day · data_availability | 确认日期 |
| [market-overview-query](market-overview-query/SKILL.md) | index_daily · market_breadth · daily_basic_snapshot · market_daily_snapshot | 观察市场 |
| [stock-research-query](stock-research-query/SKILL.md) | daily_kline · valuation · financial_statements · stock_context | 研究个股 |
| [capital-flow-query](capital-flow-query/SKILL.md) | moneyflow_ranking · moneyflow_stock · dragon_tiger · hsgt_hold | 资金面 |
| [holders-survey-query](holders-survey-query/SKILL.md) | top_floatholders · holder_number · institution_survey · fund_holdings | 股东/机构 |
| [data-provenance-query](data-provenance-query/SKILL.md) | sync_log · cache_status · data_freshness · source_diagnostics | 来源核验 |

机器可读清单：[`skills.manifest.json`](skills.manifest.json)。

## 快速开始

```bash
# 1) 发现：列出全部技能与功能点
python3 market-calendar-query/run.py list

# 2) 先确认日期是否交易日、本地是否有数据
python3 market-calendar-query/run.py query \
  --function data_availability --date 2026-06-01 --pretty

# 3) 取当日市场宽度（历史快照）
python3 market-overview-query/run.py query \
  --function market_breadth --date 2026-06-01 --pretty
```

## 运行入口

- 每个技能目录下的 `run.py` 是该技能的运行入口（`query` / `list` 两个子命令）。
- 所有 `run.py` 共享 `_shared/` 运行时：
  - `_shared/db_gateway.py`：只读 SQLite 网关（`?mode=ro`），含日期/代码归一化；
  - `_shared/envelope.py`：标准响应信封与日期/代码工具；
  - `_shared/handlers.py`：功能点处理器与注册表。
- 输出为标准 JSON（stdout，单个对象），退出码：`0`=ok/limited，`3`=no_data/error，便于脚本分支。

## 统一响应信封

```json
{
  "skill": "...",
  "function": "...",
  "request": { "回显的查询参数（不含任何密钥）" },
  "status": "ok | limited | no_data | error",
  "data_nature": "historical_snapshot | reference_data | not_available",
  "as_of": { "date": "数据实际日期" },
  "source": "来源表",
  "coverage": { "rows": 0 },
  "data": [ ... ],
  "limitations": [ "限制说明" ],
  "disclaimer": "仅为数据查询……不构成投资建议……",
  "generated_at": "生成时间"
}
```

## 数据时效原则

1. 本地库数据均为**历史快照**，不是实时行情；`data_nature` 与 `as_of` 明确标注。
2. 缺失、过期或不可用数据：保留原始状态与可取得的时间信息，不补造、不掩盖。
3. 当查询参数或数据源本身不支持指定日期时，明确说明限制，不构造当日结果。
4. 实时行情需由正在运行的应用/自选服务提供，不在本日期查询技能范围内。

## 依赖

- 核心只读查询**仅依赖 Python 标准库**（sqlite3），任何 CPython 3.9+ 均可运行。
- 无需 pandas/requests/akshare/tushare；这些仅用于应用侧联网更新，技能不依赖。
- 默认数据库路径 `data/lumo_data.sqlite`；可用 `--db` 或 `KRONOS_SQLITE_PATH` 覆盖。

## 验证

可复现验证脚本：[`verify.py`](verify.py)。覆盖正常返回、历史日期、无数据、数据源失败四类场景。验证不会读取密钥到日志，也不会删除或重置任何用户数据。

```bash
python3 verify.py
```

详见 `test/docs/`：评估报告、使用示例、数据新鲜度清单、项目问题总结与验证报告。
