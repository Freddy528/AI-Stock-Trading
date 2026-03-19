# AI-Stock-Trading — AI 自主交易系统

> LLM 驱动的 A 股模拟交易系统，自动采集数据 → 生成信号 → 执行交易 → 每日复盘 → 策略自迭代

## 系统架构

```
定时调度器 (scheduler.py)
  │
  ├─ 09:23  盘前信号 ─── 数据采集 → LLM 分析 → 输出建议（不自动执行）
  ├─ 09:58  盘中信号 ─┐
  ├─ 10:58  盘中信号  │  数据采集 → LLM 分析 → 结构化指令
  ├─ 12:58  盘中信号  ├─ → 规则引擎验证 → 模拟执行 → 写入账户
  ├─ 13:58  盘中信号 ─┘
  ├─ 14:48  尾盘信号 ─── 同上 + 超跌反弹策略检测
  └─ 16:56  每日总结 ─── 收盘快照 → LLM 复盘 → 策略自动回写
```

## 核心特性

- **LLM + 规则引擎双重决策**：LLM 负责选股和时机判断，代码级规则引擎进行 9 项硬性验证（不可被 LLM 覆盖）
- **多源数据容灾**：每类数据至少 2-3 个备用源（新浪/腾讯/东方财富/AKShare），单源故障自动切换
- **自迭代闭环**：每日复盘 LLM 输出策略优化建议 → 自动回写 `strategies.md` → 次日信号自动应用新策略
- **风险管理**：单笔止损 -8%（代码强制）、账户回撤熔断（总资产 < ¥8,500 禁止买入）、T+1 合规
- **超跌反弹检测**：市场上涨家数 < 1000 且非 ST 跌停 < 20 时，自动触发反弹买入策略

## 项目结构

```
AI-Stock-Trading/
├── src/
│   ├── scheduler.py          # 定时调度器（APScheduler，7 个定时任务）
│   ├── signal_generator.py   # 信号生成（数据采集 → LLM prompt → 解析输出）
│   ├── signal_executor.py    # 信号执行（JSON 解析 → 规则验证 → 模拟交易）
│   ├── summary_generator.py  # 每日复盘（交易分析 + 策略回写）
│   ├── data_fetcher.py       # 多源数据采集（行情/情绪/北向/K线）
│   ├── account.py            # 模拟账户（买卖/持仓/快照/佣金计算）
│   └── config.py             # 全局配置
├── memory/
│   ├── strategies.md         # 策略文件（唯一策略源，LLM 每次读取）
│   └── lessons.md            # 经验教训
├── data/
│   └── account.json          # 模拟账户状态持久化
├── signals/                  # 每日信号文件（含执行报告）
│   └── YYYY-MM-DD/
├── docs/
│   ├── daily-summary/        # 每日复盘报告
│   └── changelog.md          # 更新日志
├── logs/                     # 执行日志 + 错误日志
└── token-usage/              # LLM API 用量统计
```

## 快速开始

### 1. 安装依赖

```bash
pip install akshare apscheduler pandas requests
```

### 2. 配置 LLM API

编辑 `src/config.py`，设置你的 LLM API：

```python
LLM_API_URL = "你的 OpenAI 兼容 API 地址"
LLM_API_KEY = "你的 API Key"
LLM_MODEL = "模型名称"
```

系统使用 OpenAI 兼容格式（`/v1/chat/completions`），支持任何兼容的 LLM 服务。

### 3. 初始化账户

首次运行时，系统会自动创建 `data/account.json`，初始资金 ¥10,000。

### 4. 启动

**守护模式（推荐）**：自动按时间表执行

```bash
python src/scheduler.py daemon
```

**手动触发单次信号**：

```bash
# 盘中信号（会自动执行交易）
python src/scheduler.py now --type intraday

# 盘前信号（仅分析，不自动执行）
python src/scheduler.py now --type pre_market

# 尾盘信号
python src/scheduler.py now --type pre_close

# 每日复盘
python src/scheduler.py now --type daily_summary
```

**查看任务列表**：

```bash
python src/scheduler.py list
```

## 信号执行流程

每个信号（盘中/尾盘）的完整执行流程：

1. **数据采集**：市场行情、涨跌停池、北向资金、板块资金、市场情绪
2. **LLM 分析**：将数据 + 当前策略 + 持仓状态 → 构建 prompt → LLM 输出操作建议
3. **止损检查**：检查持仓是否触及 -8% 止损线，触发则强制卖出
4. **指令解析**：从 LLM 输出中提取 `trade_actions` JSON（回退到正则匹配表格）
5. **规则验证**（代码级，不可覆盖）：
   - 账户熔断：总资产 < ¥8,500 → 禁止买入
   - 持仓上限：≤ 2 只
   - 单股仓位：≤ 60%
   - 日交易次数：≤ 4
   - T+1 合规：今日买入不可当日卖出
   - 选股范围：仅 60/000/001 开头，排除 ST/688/300
   - RSI ≥ 70 禁止买入
   - 距 20 日低点涨幅 > 30% 禁止追高
   - 资金充足性检查
6. **模拟执行**：计算股数（100 整数倍）→ 写入账户 → 记录日志
7. **输出报告**：执行结果追加到信号 markdown 文件

## 策略自迭代机制

```
每日复盘 (16:56)
  ├─ 输入：今日所有信号 + 交易记录 + 执行日志 + 错误日志 + 净值曲线
  ├─ LLM 分析：胜率/盈亏比/策略有效性
  └─ 输出：
      ├─ 复盘报告 → docs/daily-summary/YYYY-MM-DD.md
      └─ strategy_update JSON → 自动追加到 memory/strategies.md
                                    ↓
                    次日信号生成时自动读取更新后的策略
```

`memory/strategies.md` 是**唯一策略源**，所有策略变更都通过修改此文件生效。

## 风险控制参数

| 参数 | 值 | 说明 |
|------|------|------|
| 初始资金 | ¥10,000 | 模拟账户 |
| 单笔止损 | -8% | 代码级强制执行 |
| 账户熔断 | ¥8,500 | 总资产低于此值禁止买入 |
| 最大持仓 | 2 只 | 分散风险 |
| 单股仓位上限 | 60% | 防止过度集中 |
| 日交易上限 | 4 次 | 避免频繁交易 |
| 买入佣金 | 万分之一 | 模拟券商费率 |
| 卖出费用 | 万分之一 + 万分之五印花税 | A 股卖出规则 |

## 数据源与容灾

| 数据类型 | 主源 | 备用源 |
|----------|------|--------|
| 实时行情 | 新浪财经 | 腾讯财经 |
| 市场广度 | 东方财富 | 新浪全市场自算 |
| 涨跌停池 | AKShare | 新浪全市场自算 |
| 北向资金 | AKShare | 东方财富网页 / 东财直连 API |
| 历史K线 | AKShare | 腾讯日K接口 |
| 板块资金 | 东方财富 | — |

## LLM 输出格式

LLM 输出同时包含人类可读的 markdown 表格和机器可解析的 JSON：

```markdown
### 操作建议

| 操作 | 股票 | 代码 | 挂单价 | 数量/仓位 | 理由 |
|------|------|------|--------|----------|------|
| 买入 | 贵州茅台 | 600519 | 1400.00 | 50% | 放量突破... |

```trade_actions
[{"action":"buy","code":"600519","name":"贵州茅台","price":1400.00,"position_pct":0.5,"reason":"放量突破..."}]
```
```

## 协作者

- [@Freddy528](https://github.com/Freddy528) — 项目发起人
- [@WeFunPOTION](https://github.com/WeFunPOTION) — 协作开发者

## 免责声明

本项目仅供学习研究使用，所有交易均为模拟盘操作。股市有风险，投资需谨慎。本系统的任何输出不构成投资建议。

## License

MIT
