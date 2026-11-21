# v26 优化快速上手指南

> 🎯 **目标**: 在 2 周内完成 v26 优化验证,提升选股胜率 2-3 个百分点

---

## 📋 前置检查清单

在开始之前,确保以下条件满足:

- [ ] **Python 环境**: Python 3.11+ (当前使用 3.13.11)
- [ ] **依赖包**: `pip install -r requirements.txt` 已执行
- [ ] **Tushare Token**: `config/tushare_config.json` 已配置有效 token
- [ ] **数据库**: `kronos_data.sqlite` 存在且包含基础数据
- [ ] **磁盘空间**: 至少 5GB 可用 (用于历史数据补充)

验证环境:
```bash
python3 scripts/check_environment.py
```

---

## 🚀 三种使用模式

### 模式 1: 完整流程 (首次使用,~2小时)

从零开始,包含数据补全:

```bash
./scripts/run_v26_pipeline.sh --full --start 2025-11-01 --end 2026-07-31
```

**流程**:
1. ✅ 补充流通股本数据 (daily_basic)
2. ✅ 补充北向资金持仓 (hk_hold)  
3. ✅ 补充 QFII 持股 (qfii_hold)
4. ✅ 验证新因子 IC 值
5. ✅ 生成对比报告

---

### 模式 2: 快速验证 (数据已有,~30分钟)

跳过数据补全,直接验证因子:

```bash
./scripts/run_v26_pipeline.sh --quick
```

适用场景:
- 已经运行过完整流程
- 只想测试因子计算逻辑
- 调整参数后快速复测

---

### 模式 3: 单因子测试 (开发调试,~5分钟)

针对单个因子的快速测试:

```bash
# 只测试筹码因子
python3 scripts/validate_v26_factors.py \
  --factor chip_locked \
  --start 2025-11-01 \
  --end 2026-07-31

# 只测试量价因子
python3 scripts/validate_v26_factors.py \
  --factor volume_quality \
  --start 2025-11-01 \
  --end 2026-07-31
```

---

## 📊 结果解读

### 1. 因子验证报告位置

```
results/factor_validation_v26/
├── validation_chip_locked_20261121_143022.json
├── validation_volume_quality_20261121_143845.json
└── ...
```

### 2. 关键指标含义

| 指标 | 合格标准 | 优秀标准 | 说明 |
|------|---------|---------|------|
| **IC 均值** | > 0.03 | > 0.05 | 因子预测能力 |
| **IC_IR** | > 1.0 | > 1.5 | IC 稳定性 |
| **正IC占比** | > 60% | > 70% | 时序一致性 |
| **单调性得分** | > 0.5 | > 0.75 | 分层收益单调性 |
| **双半窗一致** | ✓ | ✓ | 前后半窗同号 (必须) |

### 3. 判定标准

**✅ 通过** (综合得分 ≥ 80):
- IC > 0.03 且双半窗一致
- 建议纳入 v26 正式规则

**⚠️ 观察** (60-79 分):
- 部分指标达标但有瑕疵
- 需要调整参数或增加样本

**❌ 不通过** (< 60 分):
- 核心指标不达标
- 暂不采用,需重新设计

---

## 🔧 常见问题处理

### Q1: Tushare 接口报错 "权限不足"

**原因**: Token 未配置或积分不足

**解决**:
```bash
# 检查 token
cat config/tushare_config.json

# 测试连接
python3 -c "
import tushare as ts
from config.tushare_config import get_tushare_token
pro = ts.pro_api(get_tushare_token())
print(pro.trade_cal(exchange='SSE', start_date='20261101', end_date='20261130'))
"
```

如果积分不足,考虑:
- 方案 A: 升级 Tushare 积分套餐
- 方案 B: 使用已有数据降级运行 (部分因子会缺失)

---

### Q2: 数据补全很慢,能加速吗?

**正常耗时**:
- daily_basic: ~30 分钟 (1000 只股票 × 200 天)
- hk_hold: ~10 分钟 (每天 ~200 只)
- qfii_hold: ~5 分钟 (季度数据)

**加速技巧**:
1. 缩小日期范围 (最少 3 个月,约 60 个交易日)
2. 使用 `--codes` 参数只补充重点股票
3. 分批执行 (每次 500 只股票)

不建议:
- ❌ 并行多线程 (会触发 Tushare 限频,反而更慢)
- ❌ 去掉 sleep (同上)

---

### Q3: 因子 IC 值为负,是不是算错了?

**不一定是错误**!

负 IC 的可能原因:
1. **反向因子**: 如 `volume_surge_risk` 本身就是风险指标,IC 为负说明预测准确
2. **市场环境**: 某些因子在特定市场环境下会反转
3. **计算错误**: 如果绝对值很小 (|IC| < 0.01),可能是噪声

**处理方式**:
- 反向因子: 在评分规则中用惩罚项 (如 `-10 分`)
- 市场相关: 加入市场门控 (如只在震荡市生效)
- 噪声信号: 放弃该因子

---

### Q4: 双半窗检验不一致怎么办?

**原因分析**:
1. **样本量不足**: < 60 个交易日,随机性大
2. **过拟合**: 因子在训练窗口有效,测试窗口失效
3. **市场环境变化**: 2025-11 ~ 2026-07 经历了小盘崩盘

**补救措施**:
- 增加样本: 扩展到 2024-01 ~ 2026-07 (18 个月)
- 三窗口验证: 前 1/3 / 中 1/3 / 后 1/3 都要一致
- 市场门控: 分别在牛市/熊市/震荡市验证

**无法补救时**:
- ❌ 不要强行上线
- ✅ 记录为"待观察因子",下一季度重新验证

---

### Q5: v26 回测胜率反而下降了?

**排查步骤**:

1. **确认数据完整性**
   ```bash
   python3 scripts/backfill_v26_data.py --verify-only
   ```

2. **对比规则差异**
   ```bash
   # 查看 v25 vs v26 的评分差异
   python3 -c "
   from analysis.scoring_rules import RULESET, RULESET_VERSION
   print(f'当前版本: {RULESET_VERSION}')
   print(f'规则数量: {len(RULESET)}')
   "
   ```

3. **检查降级模式**
   - v26 新因子缺失时会触发 `degraded=True`
   - 降级模式下评分会被压制
   - 查看降级比例: `grep "degraded.*True" results/opportunity_*.csv | wc -l`

4. **分层分析**
   - 不要只看总体胜率
   - 分别看 S/A/B 级的表现
   - 可能是 v26 更保守,S 级少了但更准

---

## 🎓 进阶使用

### 自定义因子开发

1. **在 `analysis/enhanced_factors.py` 中添加函数**

```python
def compute_my_factor(df: pd.DataFrame) -> pd.Series:
    """自定义因子"""
    # 你的逻辑
    return df['my_score']
```

2. **在 `scripts/validate_v26_factors.py` 中注册**

```python
# 在 compute_chip_factor() 之后添加
def compute_my_factor_wrapper(df: pd.DataFrame) -> pd.DataFrame:
    df['my_factor'] = compute_my_factor(df)
    return df
```

3. **运行验证**

```bash
python3 scripts/validate_v26_factors.py --factor my_factor
```

---

### 网格搜索最优参数

当你有多个候选参数组合时:

```bash
# 使用现有的网格搜索工具 (需要先集成到 scoring_rules.py)
python3 scripts/grid_v25_variants.py \
  --params chip_locked_bonus:3,5,8 \
  --params volume_quality_bonus:4,6,8 \
  --output results/grid_search_v26.csv
```

---

### A/B 测试框架

并行运行 v25 和 v26,收集对比数据:

```bash
# 每天自动运行两个版本
crontab -e

# 添加定时任务 (每天 15:30 收盘后)
30 15 * * 1-5 cd /path/to/Kronos && python3 scripts/run_opportunity_discovery.py --version v25 --output results/daily_v25/
40 15 * * 1-5 cd /path/to/Kronos && python3 scripts/run_opportunity_discovery.py --version v26 --output results/daily_v26/
```

2 周后对比:
```bash
python3 scripts/compare_ab_test.py \
  --baseline results/daily_v25/ \
  --candidate results/daily_v26/ \
  --start 2026-11-21 \
  --end 2026-12-05
```

---

## 📞 获取帮助

### 文档资源

- **设计文档**: `docs/optimization_roadmap_v26.md`
- **历史决策**: `.claude/projects/.../memory/MEMORY.md`
- **API 参考**: `analysis/enhanced_factors.py` 的 docstring

### 日志调试

```bash
# 开启详细日志
export KRONOS_LOG_LEVEL=DEBUG

# 运行并保存日志
python3 scripts/validate_v26_factors.py --all 2>&1 | tee logs/v26_debug.log

# 查看错误
grep "ERROR\|Exception" logs/v26_debug.log
```

### 常用诊断命令

```bash
# 检查数据库完整性
sqlite3 kronos_data.sqlite "PRAGMA integrity_check;"

# 统计因子覆盖率
python3 -c "
import pandas as pd
df = pd.read_csv('results/backtest_rebuilt_20260324_220014.csv')
print('因子缺失率:')
for col in ['rsi', 'chase_risk', 'quant_score', 'sector_score']:
    missing = df[col].isna().sum() / len(df) * 100
    print(f'  {col:20s} {missing:.1f}%')
"

# 查看最近的机会挖掘运行
ls -lht results/opportunity_top10_*.md | head -5
```

---

## ✅ 成功标准

在正式上线 v26 之前,确保:

- [ ] **所有核心因子** IC > 0.03 且双半窗一致
- [ ] **B+ 级胜率** > 58% (相比 v25 的 55.7%)
- [ ] **平均收益** > +3.0% (相比 v25 的 +2.30%)
- [ ] **S 级占比** < 30% (相比 v25 的 91%)
- [ ] **降级率** < 15% (新因子数据完整性)
- [ ] **无新增过拟合** (三窗口验证都通过)
- [ ] **A/B 测试** 至少 10 个交易日实盘数据支持

只有全部勾选后,才建议将 v26 合并到主分支!

---

## 🎉 下一步

完成 v26 验证后:

1. **更新主配置**: 将验证通过的规则合并到 `scoring_rules.py`
2. **文档归档**: 验证报告存入 `docs/superpowers/specs/`
3. **版本标记**: 在 MEMORY.md 中记录 v26 上线时间和关键参数
4. **监控看板**: 设置因子 IC 监控,每月复盘一次

**v27 展望**:
- 深度学习因子 (LSTM 预测短期波动)
- 宏观因子 (利率、汇率、大宗商品)
- 行业轮动模型

---

**祝顺利! 🚀**
