# Changelog

本项目所有值得注意的变更都会记录在此文件中。

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/) 与
[Semantic Versioning](https://semver.org/lang/zh-CN/)。

版本号约定：发布分支采用 `V主.次.修订`（如 `V2.1.4`），Git tag 采用小写 `v主.次.修订`
（本版本为 `v2.1.6`）；桌面安装包（`package.json` / `src-tauri/tauri.conf.json`）另有独立的
bundle 版本号（当前 `1.1.6`）；分析引擎的内部评分里程碑（v1 → v25 → m1）在文末附录单独标注，
不与发布版本混用。

## [v2.1.6] - 2026-10-01

统一操作计划版本（tag：`v2.1.6`，对应发布分支 `v2.1.6`，桌面 bundle：`1.1.6`）。
变更范围自上一个发布 tag `v2.1.5`（`21bda5a`，2026-09-29）起算。

### 新增
- **统一操作计划**（`analysis/trading_plan.py`、`webui/services/trading_plan_service.py`）：
  打通「大盘 → 板块 → 个股」三层门控，覆盖自选股、挖掘机会与模拟持仓；输出买入区、
  试仓与加仓额度、锁定止损、分档止盈和减仓条件，替换页面中无条件的「越跌越加仓」方案；
  数据缺失或过期时暂停新增仓位，锁定计划在刷新行情后不会下移止损线。
- **操作模型与板块轮动**（`analysis/operation_models.py`、`analysis/sector_opportunities.py`）：
  在 K 线原位展示三类操作模型的确认条件与板块轮动机会；板块面板同时展示持续相对强度、
  广度、资金状态与已验证拐点，走势由成分股聚合收益绘制，不合成虚假 K 线。
- **前端面板**：新增 `webui/static/lumo_operations.css` / `.js` 与
  `webui/static/lumo_trading_plan.css` / `.js`，并在 `webui/templates/desktop.html` 接入新页签。
- **自选股路径解析**（`webui/services/paths.py`）：新增自选股存储路径解析，支持升级迁移与备份恢复。
- **测试**：新增 `test_trading_plan.py`、`test_trading_plan_api.py`、`test_trading_plan_service.py`、
  `test_operation_models.py`、`test_sector_opportunities.py`、`test_watchlist_paths.py`；并扩充
  `test_watchlist_service.py`、`test_market_pulse_service.py`、`test_robyn_app.py`。

### 变更
- `webui/core.py`、`webui/robyn_app.py`、`webui/services/market_pulse_service.py`、
  `webui/services/kline_service.py`、`analysis/sector_series.py` 适配新增服务与路由。
- README 截图顺序校正（个股工作台、龙虎榜、量化活跃度），并补充自选股 / K 线与操作计划说明。

### 修复
- **修复升级后自选股清空**：新增迁移与备份恢复链路，保证历史自选不因升级丢失。

### 文档
- README 新增「AI 软件定制 · 智能体开发 · 业务自动化」板块与微信联系二维码。

## [v2.1.5] - 2026-09-29

评分优化版本（tag：`v2.1.5`，对应发布分支 `v2.1.5`，桌面 bundle：`1.1.6`）。
本节为事后补记 —— `v2.1.5` 发布时已打 tag，但当时未同步更新本文件。

### 新增
- **评分与机会审计脚本**：新增 `scripts/optimize_factor_scores.py`、
  `scripts/analyze_installed_opportunities.py`、`scripts/benchmark_installed_opportunities.py`。
- **测试**：新增 `test_factor_score_optimization.py`、`test_installed_opportunity_audit.py`。

### 变更
- **综合分算法**：调整为「原综合分 + 当日涨幅分段校准 + 板块分数与近 3 日涨幅的交互分」。

### 修复
- 修复 README 中的图片引用错误。

### 文档
- README 补充 LINUX DO 社区友链。

## [v2.1.4] - 2026-09-12

首个在 `main` 分支上正式打 tag 发布的版本（tag：`v2.1.4`，对应发布分支 `V2.1.4`，
桌面 bundle：`1.1.6`）。

### 新增
- `CHANGELOG.md`：首次纳入版本控制，回溯项目初始化与完整演进。
- 跨平台桌面打包与发布流水线（`.github/workflows/build.yml`）：
  - 任意分支 push 产出测试包（`-test` 后缀，保留 7 天）；
  - `main` push 产出正式包（`-release` 后缀，保留 14 天）；
  - 推送 `v*` / `V*` tag 构建正式包并附到 GitHub Release。
- macOS Intel（x64，`macos-15-intel`）原生构建，与 Apple Silicon（arm64）各出独立安装包。
- 首页总览优化：总览与功能总览合并为单页，新增三热榜与个股快搜。

### 变更
- 品牌统一：Kronos → **Lumo**；仓库自引用链接同步 `kronos_ultra` → `lumo_trade`。
- 关闭桌面端设备验证门禁。
- 重写 README 并补充界面截图、macOS 安装安全提示与 TuShare 积分说明。
- 补全 `.gitignore`，忽略本地工具状态与可重新获取的数据产物。
- 恢复维护 CHANGELOG（历史上曾于 V2.1.3 按「完整代码即开源」策略移除）。

### 修复
- 修复 CI 产物收集从未生效：多行 glob 被换行拆成两条命令、`for` 循环语法错误、macOS 重复收集
  `.app`（改为只收 `.dmg`）。
- 修复 macOS codesign 失败：`APPLE_SIGNING_IDENTITY` 由空串改为 `-`（ad-hoc 签名）。
- 修复 Windows MSI 打包失败：非 ASCII 文件名导致的 `LGHT0311`、补齐 VBSCRIPT 可选功能、
  打开 `-vv` 诊断日志。
- 修正 workflow 级 concurrency 的 `matrix` 上下文错误。
- 补全 tag 触发说明，并让 `on.push.tags` 同时匹配大写 `V`。

### 文档
- 修正过时的启动 / 打包说明；补充「捐赠一点 token」赞赏入口。
- License 徽章改为静态并补全 license 元数据。
- 移除误提交的个人收款码图片。

## [V2.1.3] - 2026-09-03

开源整备版本。

### 新增
- 按开源标准补充社区文档：`CONTRIBUTING.md`、`CODE_OF_CONDUCT.md`、`SECURITY.md`。
- 独立撰写 `docs/Lumo_Trade_桌面端完整指南.md`。
- 设备授权相关控制。

### 变更
- README 重心迁移到 Lumo Trade（底层基于 Kronos 模型），LICENSE 版权归 Lumonote，采用 MIT 授权。
- 统一归集 Markdown 文档到 `docs/` 并校正引用。
- 将 `CLAUDE.md` 纳入版本控制并移除过期文档。
- 移除 `CHANGELOG.md`（当时按「完整代码即开源」策略发布，自 v2.1.4 起恢复维护）。

## [V2.1.2] - 2026-07-23

### 变更
- 页面功能优化；设备授权相关控制。

## [V2.1.1] - 2026-07-01

### 新增
- 风险·机遇统筹作战大屏：机会分 × 风险分四象限、KPI / 榜单 / 行情台、Plotly 矩阵与板块热力。
- 风险分层：个股、板块、市场、组合四层风险叠加与融合。
- 持仓·热点关联面板；行情台直达个股 / 板块。
- 关系画布：芯片点击定位、按日切换、节点及邻居自动缩放；历史 / 板块表格化与术语中文化。

### 变更
- 页面功能与个股分析优化。

## [V2.0.1] - 2026-06-04

### 新增
- 多空评审团（Tab #16）：51 位人物 / 7 流派 YAML 注册表、26 项标准化特征、12 条旗舰规则、
  流派 × 风格权重矩阵、裁决 / 共识 / 大分歧引擎、16 指标聚合器。
- 评审团 LLM 深度点评覆盖层：lite / medium / deep 分档，以及质量门（占位符 / 金句 / 风险 / FACTCHECK）。
- 个股深度挖掘：migration v6 新增 6 张机构 / 龙虎榜 / 控盘表 + `sync_log`，AkshareAdapter 统一取数
  （fallback / 限流 / 熔断），机构 provider 真实抓取与市场级回填。
- 个股弹窗升级为 11 Tab 工作台；StockAnalysisSuite 编排器与 `stock-analysis-suite` 路由。
- 批量分析完成卡直达个股分析；板块动量改用东财真实行业板块。

### 变更
- Web 层由 Flask 迁移到 Robyn：删除 `webui/app.py` 与 Flask 兼容层，94 个 helper 迁入
  `webui/core`，移除 flask / flask-cors 依赖。
- 基本面、资金流复用已入库数据；OHLCV / 技术 / 风控做门槛分级降级，短历史不再整页「数据不足」。
- 桌面端跨平台兼容控制；评审团与桌面交互优化。

### 修复
- 修复回测反馈闭环（sqlite-native 取数 + 增强自动优化器）。
- 修复打包后机构 / 资金等「数据源暂不可用」（hook-akshare 收集随包数据）。
- 修复 top10 流通股东 `holder_rank` NaN 导致的偶发 NOT NULL 崩溃。
- 端口冲突快速失败，并扩宽 Tauri 残留进程清理匹配。

## [V1.9.1] - 2026-05-20

### 新增
- 单个股票投资机会挖掘生成；分析页功能添加。

### 变更
- 页面功能升级。

## [V1.9] - 2026-03-11

### 新增
- 自动化回测控制；投资机会挖掘；热度榜优化。

### 变更
- 算法优化升级；配置功能优化。

### 修复
- 榜单错误解决；格式优化。

## [V1.8] - 2026-02-10

### 修复
- 龙虎榜时间获取问题；时间处理问题。

## [V1.7 ~ V1.2] - 2025-12-06 ~ 2026-01-29

### 变更
- 持续进行 LLM 分析与页面功能优化（V1.7 / V1.6 / V1.5 / V1.4 / V1.3 / V1.2）。

## [V1.1] - 2025-11-23

### 新增
- LLM 智能分析集成。

### 修复
- 修复投资机会挖掘卡顿问题。

## [V1.0] - 2025-11-07

首个桌面发布分支。

### 新增
- 桌面版应用与 Tauri 2 桌面壳。
- top100 分析功能。
- LLM 功能。

---

## 附录 A · 分析引擎里程碑（内部评分体系）

### v25 + m1 - 2026-07

### 新增
- `analysis/scoring_rules.py`：sim / live 统一规则源（`RULESET`），杜绝规则漂移；新增主力资金、
  期指门控 2 类因子。
- `analysis/outcome_markers.py`：m1 标记体系（入选后表现回溯、暴涨暴跌共性、体质分层）。

### 变更
- 数据驱动奖惩规则（RSI / 短期涨幅 / 追高 / 量化极端等），停用无区分度的旧规则。

### v5.0 - 2026-02

### 新增
- `retry_utils.py` 指数退避 + 多源回退 + 数据质量验证。
- `async_data_collector.py` / `async_opportunity_scorer.py` / `batch_processor.py` 异步三段流水线。
- `market_env_analyzer.py` 市场环境感知 + 6 种动态权重。

### 性能
- 100 只股票处理耗时约 450s → 90s（约 5 倍提升）。

### v4.0 - 2025-12

### 新增
- 非官方渠道深度挖掘（社交 / 供应链 / 招聘 / 资金 / 政策）。
- 信息可信度评估算法。

### v3.0 - 2025-12

### 新增
- 一体化发现引擎（关键词 / 论坛 / 新闻三模式融合）。
- 六阶段分析流程、五维度深度钻取、多轮验证。

### v2.0 - 2025-12

### 新增
- 超前瞻性深度事件发现系统（6 维前瞻信号）。
- 并购重组关联分析、关键词反向挖掘。

### v1.0 - 2025-11

### 新增
- 批量分析 + 30 个量化模型 + 五阶段筛选。
- Kronos 金融 K 线基础模型接入。

## 附录 B · 项目初始化与基础能力 - 2025-07 ~ 2025-10

### 新增
- **Kronos 金融 K 线基础模型**：`KronosTokenizer`（二值球面量化 BSQ）、`Kronos` Transformer
  预测器、`KronosPredictor` 推理接口；Kronos-mini / small / base 三个规格
  （4.1M / 24.7M / 102.3M 参数，上下文长度 2048 / 512 / 512）。
- **微调管线**（`finetune/`）：Qlib 数据预处理、tokenizer 训练、predictor 微调、回测评估。
- **Web UI**：模型选择与加载、实时行情可视化、预测生成与绘图、预测结果保存与批量预测。
- **多源数据采集**（`scripts/`）：Tushare / 东方财富 / 同花顺 / 雪球，多爬虫自动切换与反爬机制。
- **分析引擎**：30 个量化交易模型、20+ 技术指标、多空情绪与基本面因子。
- **SQLite 数据仓库**（`kronos_data.sqlite`，30+ topic 表）。
- **桌面版应用**起步（2025-10），并加入 top100 分析与 LLM 智能分析集成。

### 修复
- 修复 Kronos 模型预测连续性问题：相对变化率转绝对价格改用 `cumprod` 累积计算。
- 推理时增加 `torch.cuda.empty_cache()`；补充缺失依赖 `safetensors`。

---

Copyright (c) 2025 Lumonote. See [LICENSE](LICENSE) for full text.
