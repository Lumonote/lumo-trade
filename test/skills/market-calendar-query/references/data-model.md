# 数据模型：交易日历与数据可用性

## 涉及表

### trade_calendar
| 列 | 类型 | 说明 |
|---|---|---|
| cal_date | TEXT PK | 日历日期，本库为紧凑格式 `YYYYMMDD` |
| is_open | INTEGER | 1=交易日 |

本库实际情况：共 536 行，**全部 is_open=1**，范围 `20240312 ~ 20260529`。即只存了交易日，未存周末/节假日。

### 数据可用性盘点覆盖的表
`ohlcv, daily_basic, market_daily, moneyflow_dc, dragon_tiger_inst, hsgt_individual, jgdy_detail`。

## 日期口径与取数方式

- 日期输入支持 `YYYY-MM-DD` / `YYYYMMDD` / ISO 时间，统一归一化为 `(iso, compact)`。
- 各表日期列存在两种存储格式：
  - `trade_calendar.cal_date`：紧凑 `YYYYMMDD`；
  - 其余多数表：以 ISO 为主、少量紧凑（如 moneyflow）。
- 为命中索引，日期等值匹配采用“同一日期的两种存储形式” OR 匹配：
  - 日期/混合列：`(col = iso OR col = compact)`；
  - 日期时间列（ohlcv.ts）：半开区间 `ts >= 'iso 00:00:00' AND ts < 'iso 23:59:59.999'`。

## 判定规则（不臆测）

- `is_trading_day`：
  - 命中且 is_open=1 → `true`；
  - 命中且 is_open=0 → `false`（本库无此情况）；
  - 未命中 → `is_open=null`，状态 `limited`，附日历范围与最近已知日期。未命中可能是周末、节假日或日历未覆盖的未来日期，不做武断推断。
- `data_availability`：对每个表给出当日行数、表范围与最近可参考日期（先向前找，再向后找）。

## 只读保证

连接以 `file:<path>?mode=ro` 打开，物理上只读，不写入、不迁移、不触发任何重置。
