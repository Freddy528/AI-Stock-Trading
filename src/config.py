"""项目全局配置"""

# === 账户配置 ===
INITIAL_CAPITAL = 10000  # 初始资金（模拟账户）
ACCOUNT_TYPE = "simulation"  # simulation / live

# === 交易规则 ===
MAX_DAILY_TRADES = 4          # 每日最大交易次数（买卖各算1次）
MAX_POSITIONS = 2             # 最大同时持仓数
MAX_SINGLE_POSITION_PCT = 0.6 # 单只股票最大仓位比例
ALLOW_EMPTY_POSITION = True   # 允许空仓

# === 选股范围 ===
EXCLUDE_ST = True             # 排除ST股
EXCLUDE_STAR = True           # 排除科创板（688开头）
EXCLUDE_GEM = True            # 排除创业板（300开头）
# 即只交易沪市主板(60开头) + 深市主板(000/001开头)

# === 交易时间 ===
REPORT_TIMES = {
    "morning": "09:25",       # 盘前报告
    "afternoon": "14:50",     # 尾盘报告
}

# === 风格 ===
STYLE = "aggressive"          # conservative / balanced / aggressive

# === 数据源 ===
DATA_SOURCE = "akshare"
