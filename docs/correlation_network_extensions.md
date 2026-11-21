# 大盘关联网络扩展方案

## 已实现的六大维度

1. **趋势维度（均线）** - 多头/空头排列
2. **动量维度（RSI）** - 强势/超买/弱势/超卖
3. **量能维度（成交量）** - 放量上涨/下跌
4. **位置维度（60日相对位置）** - 高位/低位
5. **波动率维度** - 高低波动
6. **技术形态维度** - 创新高/新低

## 🚀 可扩展的新维度

### 维度7：市场情绪（已在文档中预留）

**数据来源**：
- 恐惧贪婪指数（Fear & Greed Index）
- 投资者情绪指标
- 社交媒体情绪

**判断逻辑**：
```python
def _add_sentiment_dimension(bull_root, bear_root, metrics):
    fear_greed = metrics.get("fear_greed_index")
    
    if fear_greed >= 75:
        # 极度贪婪 → 看空信号
        bear_root.children.append(NetworkNode(
            id="sentiment_extreme_greed",
            type=NodeType.CATEGORY,
            label="😱 极度贪婪",
            description=f"恐惧贪婪指数 {fear_greed}，市场过热",
            color="#ef4444"
        ))
    elif fear_greed <= 25:
        # 极度恐惧 → 看多信号
        bull_root.children.append(NetworkNode(
            id="sentiment_extreme_fear",
            type=NodeType.CATEGORY,
            label="🤔 极度恐惧",
            description=f"恐惧贪婪指数 {fear_greed}，反向机会",
            color="#10b981"
        ))
```

### 维度8：资金流向

**数据来源**：
- 北向资金流入/流出
- 两融余额变化
- 大单净流入

**判断逻辑**：
- 北向资金连续5日净流入 → 看多
- 两融余额创新高 + 融资买入占比 > 10% → 看空（杠杆风险）
- 大单净流出连续3日 → 看空

### 维度9：期指对冲

**数据来源**：
- IF/IH/IC/IM 期现基差
- 期指多空持仓比

**判断逻辑**：
- 基差大幅贴水（<-2%）→ 看多（机构悲观，反向指标）
- 基差升水 + 多头持仓激增 → 看空（过度乐观）

### 维度10：板块轮动

**数据来源**：
- 热门板块涨幅
- 板块资金流入强度
- 概念板块活跃度

**判断逻辑**：
- 3个以上板块净流入 > 10亿 → 看多（普涨）
- 所有板块净流出 → 看空（全面撤离）
- 板块轮动加速（前5板块日度换手 > 80%）→ 震荡

### 维度11：龙虎榜

**数据来源**：
- 机构席位买入/卖出
- 游资活跃度
- 上榜股票数量

**判断逻辑**：
- 机构席位净买入占比 > 60% → 看多
- 游资活跃 + 炸板率 > 50% → 看空（投机过热）

### 维度12：估值水平

**数据来源**：
- 沪深300 PE/PB 分位数
- 破净股数量占比
- 估值历史分位

**判断逻辑**：
- PE/PB 同时 < 30% 历史分位 → 看多（低估）
- 破净率 > 15% → 看多（极度低估）
- PE > 90% 历史分位 → 看空（高估）

## 📋 实现步骤

### 第一阶段：单维度扩展（1-2天）

1. 选择一个新维度（建议从"市场情绪"或"资金流向"开始）
2. 在 `market_correlation_network.py` 中添加 `_add_xxx_dimension()` 函数
3. 在 `build_correlation_network()` 中调用新函数
4. 更新测试用例，验证新维度正常工作

### 第二阶段：批量扩展（1周）

1. 实现所有6个新维度
2. 添加维度开关（通过 `metrics` 中的可用字段自动启用）
3. 更新文档和前端说明

### 第三阶段：权重优化（可选）

1. 为每个维度分配权重
2. 根据历史回测调整权重
3. 支持自定义维度组合

## 🔧 代码模板

```python
def _add_new_dimension(bull_root: NetworkNode, bear_root: NetworkNode, 
                       metrics: Dict[str, Any]):
    """维度X：XXX（新增）"""
    
    # 1. 从 metrics 中提取数据
    indicator_value = metrics.get("indicator_key")
    
    if indicator_value is None:
        return  # 数据缺失，跳过此维度
    
    # 2. 判断看多信号
    if indicator_value > threshold_bullish:
        category_node = NetworkNode(
            id="dimension_x_bullish",
            type=NodeType.CATEGORY,
            label="📈 维度X看多",
            description="描述为什么看多",
            color="#10b981"
        )
        
        # 添加子节点（具体指标）
        indicator_node = NetworkNode(
            id="dimension_x_indicator",
            type=NodeType.INDICATOR,
            label="具体指标名称",
            value=indicator_value,
            strength=SignalStrength.STRONG_BULLISH,
            description=f"当前值 {indicator_value}，阈值 {threshold_bullish}",
            color="#10b981"
        )
        category_node.children.append(indicator_node)
        bull_root.children.append(category_node)
    
    # 3. 判断看空信号
    elif indicator_value < threshold_bearish:
        category_node = NetworkNode(
            id="dimension_x_bearish",
            type=NodeType.CATEGORY,
            label="📉 维度X看空",
            description="描述为什么看空",
            color="#ef4444"
        )
        
        indicator_node = NetworkNode(
            id="dimension_x_indicator",
            type=NodeType.INDICATOR,
            label="具体指标名称",
            value=indicator_value,
            strength=SignalStrength.STRONG_BEARISH,
            description=f"当前值 {indicator_value}，阈值 {threshold_bearish}",
            color="#ef4444"
        )
        category_node.children.append(indicator_node)
        bear_root.children.append(category_node)
```

## 📊 数据接入点

当前系统已有的数据模块可直接复用：

- **市场情绪**：`analysis/investor_sentiment.py`
- **资金流向**：`webui/services/market_capital_service.py`
- **期指对冲**：`webui/services/futures_service.py`
- **板块轮动**：`webui/services/sector_depth_service.py`
- **龙虎榜**：`data_store/dragon_tiger_list_repo.py`
- **估值水平**：`analysis/market_health.py` 中的 `market_snapshot()`

## ⚠️ 注意事项

1. **数据一致性**：新维度的数据必须与现有指标同日期（`as_of` 字段一致）
2. **缺失处理**：数据缺失时静默跳过，不影响其他维度
3. **信号独立性**：每个维度独立判断，不依赖其他维度结果
4. **避免过拟合**：不要为了"好看"而强行添加信号，保持客观
5. **前向验证**：新维度上线前必须回测至少6个月历史数据

## 🎯 优先级建议

**高优先级**（立即实现）：
1. 维度7：市场情绪（数据易得，效果明显）
2. 维度8：资金流向（已有数据接口）

**中优先级**（1-2周内）：
3. 维度9：期指对冲（专业度高）
4. 维度10：板块轮动（复杂度中等）

**低优先级**（按需实现）：
5. 维度11：龙虎榜（数据覆盖面窄）
6. 维度12：估值水平（与现有"位置维度"部分重叠）
