# 大盘体检：涨跌停

入口为“大盘体检 → 涨跌停”。支持起止日期、行业/概念、涨停/跌停/炸板/跌停开板、形态、连续次数和关键词筛选。板块、股票和事件表按50条分页，分页不截断统计与导出；点击名称查看完整区间档案，可在股票与板块之间继续跳转。

## 数据口径

- `daily` 未复权OHLC与同日 `stk_limit` 实际价格按分核验，不使用固定涨跌幅估算。沪深北A股及ST均可核验；无有效限价或价格越界的日记录列为未核验，不推算事件。
- 收盘封板与盘中触及后打开分列，同一股票可同时有不同方向事件。`limit_list_d`只补充封板时间、开板次数、封单、换手率、市值等字段，不承担全市场覆盖；其不包含ST。`daily.amount`千元换算为元，其余补充金额与`dc_daily.amount`采用源接口的元。
- 交易日历来自`trade_cal`，当天16:10前不纳入当日收盘回补。连板与连跌停额外衔接区间前60个交易日；普通日或停牌打断，缺日和回看左端未断板时标记下界。普通连续下跌天数另外计算。
- `dc_index`按月读取行业与概念分类，`dc_member`按股票和区间读取历史成分，每次事件只使用当天归属。多板块分别统计，全市场按股票/日期/事件类型去重。
- 全部18种形态标签直接复用条件选股的`tech_features`与`preset_pass`默认参数。股票采用截止日锚定的前复权价格，板块采用指数日线。标签是区间最后有效收盘日的状态，非每次事件日的历史标签；不足21根、缺复权因子、请求失败与无命中分别展示。板块不套用个股20cm制度标签。

## 接口与缓存

- `GET /api/market-health/limits?start=YYYY-MM-DD&end=YYYY-MM-DD`：只读查询，缺快照时返回空态。
- `POST /api/market-health/limits/refresh`：JSON携带`start`、`end`，使用共享后台任务，重复请求复用运行中任务。
- `GET /api/market-health/limits/detail?kind=stock|sector&code=...&start=...&end=...`：完整对象、事件、关联股票与板块。

区间最多366天。使用现有SQLite `kv_cache`，namespace为`market_limits_v1`，分日事件、成分、日期锚定的形态与区间快照分别缓存。按接口控制请求速率，避免批量板块形态触发每分钟限额。失败保留可用缓存；区间缺日增加或数据日期回退时保留原成功快照。JSON导出包含全部研究，CSV导出当前筛选下全部事件。

## 验证

```sh
python -m pytest tests/test_market_limits.py tests/test_market_health_routes.py -q
```

浏览器测试需要已运行的隔离服务，合成测试数据仅允许写入`/private/tmp/`下的显式数据库：

```sh
KRONOS_USER_DIR=/private/tmp/kronos-limits-preview KRONOS_SQLITE_PATH=/private/tmp/kronos-limits-preview/limits.sqlite PYTHONPATH=. python tests/browser_market_limits.py http://127.0.0.1:7109
```
