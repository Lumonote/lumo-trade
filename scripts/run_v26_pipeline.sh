#!/bin/bash
# v26 完整优化流程一键执行脚本
#
# 使用方法:
#   ./scripts/run_v26_pipeline.sh --full    # 完整流程(数据补全+因子验证+规则优化)
#   ./scripts/run_v26_pipeline.sh --quick   # 快速验证(跳过数据补全)
#   ./scripts/run_v26_pipeline.sh --validate-only  # 只验证不上线

set -e  # 遇到错误立即退出

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 日志函数
log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# 检查Python环境
check_environment() {
    log_info "检查Python环境..."

    if ! command -v python3 &> /dev/null; then
        log_error "Python3 未安装"
        exit 1
    fi

    # 检查必要的包
    python3 -c "import pandas, numpy, tushare" 2>/dev/null || {
        log_warn "依赖包缺失,尝试安装..."
        pip install -r requirements.txt
    }

    log_info "✓ 环境检查通过"
}

# 参数解析
MODE="full"
START_DATE="2025-11-01"
END_DATE="2026-07-31"

while [[ $# -gt 0 ]]; do
    case $1 in
        --full)
            MODE="full"
            shift
            ;;
        --quick)
            MODE="quick"
            shift
            ;;
        --validate-only)
            MODE="validate"
            shift
            ;;
        --start)
            START_DATE="$2"
            shift 2
            ;;
        --end)
            END_DATE="$2"
            shift 2
            ;;
        *)
            log_error "未知参数: $1"
            exit 1
            ;;
    esac
done

log_info "执行模式: $MODE"
log_info "日期范围: $START_DATE ~ $END_DATE"
echo ""

# ============================================================
# Phase 1: 数据补全 (full 模式)
# ============================================================

if [ "$MODE" == "full" ]; then
    log_info "Phase 1/4: 数据补全"
    echo "==========================================="

    log_info "1.1 检查数据覆盖率..."
    python3 scripts/backfill_v26_data.py \
        --start "$START_DATE" \
        --end "$END_DATE" \
        --verify-only

    read -p "是否需要补充数据? (y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        log_info "1.2 开始补充数据 (预计耗时 30-60 分钟)..."

        log_info "  补充 daily_basic (流通股本)..."
        python3 scripts/backfill_v26_data.py \
            --start "$START_DATE" \
            --end "$END_DATE" \
            --table daily_basic

        log_info "  补充 hk_hold (北向资金)..."
        python3 scripts/backfill_v26_data.py \
            --start "$START_DATE" \
            --end "$END_DATE" \
            --table hk_hold

        log_info "  补充 qfii_hold (QFII)..."
        python3 scripts/backfill_v26_data.py \
            --start "$START_DATE" \
            --end "$END_DATE" \
            --table qfii_hold

        log_info "✓ 数据补全完成"
    else
        log_warn "跳过数据补充 (因子计算可能不完整)"
    fi

    echo ""
fi

# ============================================================
# Phase 2: 因子验证
# ============================================================

log_info "Phase 2/4: 因子验证"
echo "==========================================="

log_info "2.1 验证筹码因子..."
python3 scripts/validate_v26_factors.py \
    --factor chip_locked \
    --start "$START_DATE" \
    --end "$END_DATE" \
    --output results/factor_validation_v26

log_info "2.2 验证量价因子..."
python3 scripts/validate_v26_factors.py \
    --factor volume_quality \
    --start "$START_DATE" \
    --end "$END_DATE" \
    --output results/factor_validation_v26

log_info "✓ 因子验证完成"
log_info "  查看详细报告: results/factor_validation_v26/"
echo ""

# ============================================================
# Phase 3: 回测对比
# ============================================================

log_info "Phase 3/4: v25 vs v26 回测对比"
echo "==========================================="

log_info "3.1 运行 v25 baseline..."
python3 scripts/simulate_v5_backtest.py \
    --start "$START_DATE" \
    --end "$END_DATE" \
    --output results/backtest_v25_baseline.csv

log_info "3.2 运行 v26 候选规则..."
# 这里需要临时修改 scoring_rules.py 为 v26 参数
# 实际实施时应该用配置文件切换

log_info "⚠️  v26 规则集成需要手动完成"
log_warn "  请根据 docs/optimization_roadmap_v26.md 更新 scoring_rules.py"
log_warn "  然后运行: python3 scripts/simulate_v5_backtest.py --version v26"

echo ""

# ============================================================
# Phase 4: 生成验证报告
# ============================================================

log_info "Phase 4/4: 生成验证报告"
echo "==========================================="

REPORT_FILE="docs/superpowers/specs/2026-11-v26-validation-report.md"

cat > "$REPORT_FILE" << EOF
# v26 优化验证报告

> 生成时间: $(date +"%Y-%m-%d %H:%M:%S")
> 数据范围: $START_DATE ~ $END_DATE
> 执行模式: $MODE

## 一、因子验证结果

### 1.1 筹码集中度因子 (chip_locked)

$(cat results/factor_validation_v26/validation_chip_locked_*.json 2>/dev/null | python3 -m json.tool | head -50 || echo "未完成")

### 1.2 量价配合因子 (volume_quality)

$(cat results/factor_validation_v26/validation_volume_quality_*.json 2>/dev/null | python3 -m json.tool | head -50 || echo "未完成")

## 二、回测对比

### 2.1 v25 Baseline

- 数据: results/backtest_v25_baseline.csv
- 关键指标: (待补充)

### 2.2 v26 候选

- 数据: results/backtest_v26_candidate.csv
- 关键指标: (待补充)

### 2.3 增量分析

| 指标 | v25 | v26 | 增量 |
|------|-----|-----|------|
| B+ 胜率 | 55.7% | - | - |
| 平均收益 | +2.30% | - | - |
| S级占比 | 91% | - | - |

## 三、上线决策

### 3.1 通过标准

- [ ] 新因子 IC > 0.03 且双半窗一致
- [ ] B+ 胜率提升 > 2 pct
- [ ] 无新增过拟合风险

### 3.2 建议

(待人工评估后填写)

---

**下一步**: 根据本报告决定是否将 v26 规则合并到主分支
EOF

log_info "✓ 报告已生成: $REPORT_FILE"
echo ""

# ============================================================
# 总结
# ============================================================

echo ""
echo "=========================================="
log_info "v26 优化流程执行完毕"
echo "=========================================="
echo ""
echo "📊 查看结果:"
echo "  - 因子验证: results/factor_validation_v26/"
echo "  - 回测数据: results/backtest_v26_*.csv"
echo "  - 验证报告: $REPORT_FILE"
echo ""
echo "📋 下一步:"
echo "  1. 审阅验证报告,确认因子有效性"
echo "  2. 调优 scoring_rules.py 参数"
echo "  3. 运行 A/B 测试 (v25 vs v26 并行)"
echo "  4. 观察 2 周实盘表现后决定是否上线"
echo ""
echo "⚠️  注意事项:"
echo "  - v26 规则集成需手动完成 (见 docs/optimization_roadmap_v26.md)"
echo "  - 新因子上线前必须通过双半窗检验"
echo "  - 保留 v25 作为 fallback"
echo ""
