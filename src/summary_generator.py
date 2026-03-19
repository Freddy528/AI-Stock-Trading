"""每日经验总结生成器 - 收盘后自动复盘并回写策略"""
import json
import os
import re
import requests
from datetime import datetime

from config import LLM_API_URL, LLM_API_KEY, LLM_MODEL
from account import load_account
from data_fetcher import (
    get_market_sentiment, get_stocks_realtime_batch,
    get_stock_realtime, scan_market_candidates,
)

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")
SIGNALS_DIR = os.path.join(PROJECT_ROOT, "signals")
SUMMARY_DIR = os.path.join(PROJECT_ROOT, "docs", "daily-summary")
WATCHLIST_FILE = os.path.join(PROJECT_ROOT, "memory", "watchlist.md")
STRATEGIES_FILE = os.path.join(PROJECT_ROOT, "memory", "strategies.md")
LESSONS_FILE = os.path.join(PROJECT_ROOT, "memory", "lessons.md")
TOKEN_USAGE_DIR = os.path.join(PROJECT_ROOT, "token-usage")


def _load_file(path, max_chars=None):
    """加载文件内容，可选截断"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        if max_chars and len(content) > max_chars:
            content = content[:max_chars] + "\n...(截断)"
        return content
    except FileNotFoundError:
        return None


def _parse_watchlist_codes():
    """从 watchlist.md 提取股票代码"""
    content = _load_file(WATCHLIST_FILE) or ""
    codes = re.findall(r'\b([036]\d{5})\b', content)
    seen = set()
    unique = []
    for c in codes:
        if c not in seen:
            seen.add(c)
            unique.append(c)
    return unique


def _load_today_signals():
    """加载当日所有信号文件"""
    today = datetime.now().strftime("%Y-%m-%d")
    signal_dir = os.path.join(SIGNALS_DIR, today)
    if not os.path.isdir(signal_dir):
        return "今日无信号文件"
    files = sorted(os.listdir(signal_dir))
    contents = []
    for f in files:
        if f.endswith(".md"):
            path = os.path.join(signal_dir, f)
            text = _load_file(path, max_chars=2000)
            if text:
                contents.append(f"### {f}\n{text}")
    return "\n\n".join(contents) if contents else "今日无信号文件"


def _get_account_summary():
    """获取账户摘要"""
    account = load_account()
    positions_info = []
    for code, pos in account.get("positions", {}).items():
        positions_info.append(
            f"{pos['name']}({code}): {pos['shares']}股, 成本{pos['avg_cost']}, 买入日{pos['buy_date']}"
        )
    pending = account.get("pending_orders", [])
    pending_info = []
    for o in pending:
        if o.get("status") == "pending":
            pending_info.append(
                f"挂单 {o['name']}({o['code']}) {o['shares']}股 限价{o['limit_price']}"
            )
    snapshots = account.get("snapshots", [])
    recent_snapshots = snapshots[-5:] if snapshots else []
    return {
        "cash": account["cash"],
        "initial_capital": account["initial_capital"],
        "positions": positions_info if positions_info else ["空仓"],
        "pending_orders": pending_info,
        "total_trades": account["total_trades"],
        "recent_snapshots": recent_snapshots,
    }


def _gather_watchlist_performance(codes):
    """获取关注列表今日表现"""
    batch_data = get_stocks_realtime_batch(codes)
    for code in codes:
        if code not in batch_data:
            single = get_stock_realtime(code)
            if single:
                batch_data[code] = single
    results = []
    for code in codes:
        info = batch_data.get(code)
        if info:
            results.append({
                "code": code,
                "name": info.get("名称", ""),
                "price": info.get("最新价", ""),
                "change_pct": info.get("涨跌幅", ""),
                "volume_ratio": info.get("量比", ""),
                "turnover_rate": info.get("换手率", ""),
            })
    return results


def _load_today_logs():
    """加载今日的执行日志和错误日志"""
    today = datetime.now().strftime("%Y-%m-%d")
    logs_dir = os.path.join(PROJECT_ROOT, "logs")
    exec_log = ""
    error_log = ""

    exec_path = os.path.join(logs_dir, "execution.log")
    if os.path.exists(exec_path):
        try:
            with open(exec_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            exec_log = "".join(l for l in lines if today in l)
        except Exception:
            pass

    error_path = os.path.join(logs_dir, "errors.log")
    if os.path.exists(error_path):
        try:
            with open(error_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            error_log = "".join(l for l in lines if today in l)
        except Exception:
            pass

    return exec_log or "今日无执行日志", error_log or "今日无错误日志"


def _get_today_trades(account):
    """获取今日交易记录"""
    today = datetime.now().strftime("%Y-%m-%d")
    trades = [t for t in account.get("trade_history", []) if t.get("time", "")[:10] == today]
    if not trades:
        return "今日无交易记录"
    lines = []
    for t in trades:
        if t["type"] == "buy":
            lines.append(f"- 买入 {t['name']}({t['code']}) {t['shares']}股 × ¥{t['price']} = ¥{t['total']} ({t['time'][11:]})")
        else:
            profit = t.get("profit", 0)
            pct = t.get("profit_pct", 0)
            lines.append(f"- 卖出 {t['name']}({t['code']}) {t['shares']}股 × ¥{t['price']} 盈亏¥{profit:+.2f}({pct:+.2f}%) ({t['time'][11:]})")
    return "\n".join(lines)


def _build_summary_prompt(market_sentiment, today_signals, watchlist_perf,
                          account_summary, scan_top20, strategies, lessons,
                          today_trades_text, exec_log, error_log, snapshots_text):
    """构建总结 prompt"""
    now = datetime.now()

    return f"""你是一名A股交易复盘分析师。请根据以下数据，生成今日（{now.strftime('%Y-%m-%d')}）的结构化每日总结。

## 要求
输出必须包含以下章节（严格按此顺序）：

### 一、大盘概况
简要分析今日大盘走势、涨跌家数、情绪判断。

### 二、关注列表表现分析
分析关注列表中每只票今日表现，哪些涨了哪些跌了，原因分析。

### 三、交易执行分析
分析今日每笔实际交易的入场/出场时机是否合理，盈亏情况，执行系统是否正常工作。
特别关注：
- 止损是否及时执行
- 规则引擎是否正确拦截了不合规交易
- 信号解析是否成功

### 四、错误日志分析
分析今日错误日志中的问题，给出修复建议。如无错误则说明系统运行正常。

### 五、操作记录回顾
回顾今日所有信号和实际操作，分析操作是否正确。

### 六、教训与发现
今日交易中的重要教训和新发现。

### 七、策略迭代建议
基于今日复盘，提出对现有策略的具体修改建议。
**必须以 JSON 格式输出**，放在 ```strategy_update``` 代码块中，格式如下：
```strategy_update
{{
  "date": "{now.strftime('%Y-%m-%d')}",
  "version": "下一版本号",
  "changes": "变更内容的一句话描述",
  "reason": "变更原因",
  "details": [
    {{
      "section": "要修改的策略章节名",
      "action": "append/modify/note",
      "content": "具体要追加或修改的内容"
    }}
  ]
}}
```
如果今日没有需要迭代的策略，输出空的 details 数组即可。

### 八、明日关注方向
明日应关注的板块、题材、个股方向。

---

## 输入数据

### 市场情绪
{json.dumps(market_sentiment, ensure_ascii=False, default=str) if market_sentiment else '获取失败'}

### 今日信号记录
{today_signals}

### 今日实际交易记录
{today_trades_text}

### 执行日志（今日）
{exec_log[:2000]}

### 错误日志（今日）
{error_log[:1000]}

### 关注列表今日表现
{json.dumps(watchlist_perf, ensure_ascii=False, indent=2, default=str)}

### 账户状态
- 可用资金: {account_summary['cash']:,.2f}
- 初始资金: {account_summary['initial_capital']:,.2f}
- 持仓: {'; '.join(account_summary['positions'])}
- 挂单: {'; '.join(account_summary['pending_orders']) if account_summary['pending_orders'] else '无'}
- 累计交易: {account_summary['total_trades']} 次
- 近期净值快照: {json.dumps(account_summary['recent_snapshots'], ensure_ascii=False, default=str)}

### 净值曲线
{snapshots_text}

### 全市场扫描 TOP20（今日资金方向）
{json.dumps(scan_top20, ensure_ascii=False, indent=2, default=str)}

### 当前策略（memory/strategies.md）
{strategies[:3000] if strategies else '暂无'}

### 经验教训（memory/lessons.md）
{lessons[:1500] if lessons else '暂无'}
"""


def _call_llm(prompt):
    """调用 LLM API"""
    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": "你是一个专业的A股交易复盘分析师，擅长从每日交易中提炼经验和策略改进建议。输出结构化的每日总结。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
    }

    resp = requests.post(LLM_API_URL, headers=headers, json=payload, timeout=120)
    if resp.status_code != 200:
        error_msg = f"LLM API 调用失败 (HTTP {resp.status_code}): {resp.text[:200]}"
        print(f"  WARNING: {error_msg}")
        return f"### LLM API 不可用\n\n{error_msg}", {}

    result = resp.json()
    usage = result.get("usage", {})
    content = result["choices"][0]["message"]["content"]
    return content, usage


def _record_token_usage(usage):
    """记录 token 用量（复用 signal_generator 的格式）"""
    os.makedirs(TOKEN_USAGE_DIR, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    usage_file = os.path.join(TOKEN_USAGE_DIR, f"{today}-聚合api.json")

    if os.path.exists(usage_file):
        with open(usage_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {
            "date": today,
            "by_model": {},
            "summary": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "call_count": 0,
            },
        }

    model = LLM_MODEL
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    total_tokens = usage.get("total_tokens", prompt_tokens + completion_tokens)

    if model not in data["by_model"]:
        data["by_model"][model] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "call_count": 0,
        }
    data["by_model"][model]["prompt_tokens"] += prompt_tokens
    data["by_model"][model]["completion_tokens"] += completion_tokens
    data["by_model"][model]["total_tokens"] += total_tokens
    data["by_model"][model]["call_count"] += 1

    data["summary"]["prompt_tokens"] += prompt_tokens
    data["summary"]["completion_tokens"] += completion_tokens
    data["summary"]["total_tokens"] += total_tokens
    data["summary"]["call_count"] += 1

    with open(usage_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"  Token 用量已记录: prompt={prompt_tokens}, completion={completion_tokens}, total={total_tokens}")


def _parse_strategy_update(content):
    """从 LLM 输出中解析策略迭代 JSON"""
    pattern = r'```strategy_update\s*\n(.*?)\n```'
    match = re.search(pattern, content, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError as e:
        print(f"  WARNING: 策略迭代 JSON 解析失败: {e}")
        return None


def _write_back_strategies(update):
    """将策略迭代建议回写到 strategies.md"""
    if not update or not update.get("details"):
        print("  无策略迭代建议，跳过回写")
        return

    strategies_content = _load_file(STRATEGIES_FILE)
    if not strategies_content:
        print("  WARNING: strategies.md 不存在，跳过回写")
        return

    date = update.get("date", datetime.now().strftime("%Y-%m-%d"))
    version = update.get("version", "?")
    changes = update.get("changes", "")
    reason = update.get("reason", "")

    # 追加到策略迭代日志表
    log_entry = f"| {date} | {version} | {changes} | {reason} |"

    if "策略迭代日志" in strategies_content:
        # 找到表格最后一行，追加新记录
        lines = strategies_content.split("\n")
        insert_idx = None
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].startswith("|") and "日期" not in lines[i] and "---" not in lines[i]:
                insert_idx = i + 1
                break
        if insert_idx:
            lines.insert(insert_idx, log_entry)
            strategies_content = "\n".join(lines)
    else:
        strategies_content += f"\n\n## 策略迭代日志\n\n| 日期 | 版本 | 变更内容 | 原因 |\n|------|------|---------|------|\n{log_entry}\n"

    # 处理具体的策略修改（append 类型追加到对应章节末尾）
    for detail in update.get("details", []):
        action = detail.get("action", "")
        section = detail.get("section", "")
        detail_content = detail.get("content", "")
        if action == "append" and section and detail_content:
            # 在对应章节末尾追加内容（以注释形式标注来源）
            marker = f"\n\n> **[{date} 自动回写]** {detail_content}\n"
            # 尝试找到章节位置
            if section in strategies_content:
                # 找到章节后的下一个同级标题
                section_idx = strategies_content.index(section)
                rest = strategies_content[section_idx:]
                # 找下一个 ### 或 ## 标题
                next_header = re.search(r'\n(#{2,3}\s)', rest[len(section):])
                if next_header:
                    insert_pos = section_idx + len(section) + next_header.start()
                    strategies_content = strategies_content[:insert_pos] + marker + strategies_content[insert_pos:]
                else:
                    strategies_content += marker

    with open(STRATEGIES_FILE, "w", encoding="utf-8") as f:
        f.write(strategies_content)

    print(f"  策略已回写: {version} - {changes}")


def generate_daily_summary():
    """生成每日经验总结（主入口）"""
    from account import load_account as _load_acct, snapshot as account_snapshot
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    print(f"\n{'='*60}")
    print(f"[{now.strftime('%H:%M:%S')}] 开始生成每日经验总结")
    print(f"{'='*60}")

    # 0. 每日快照：获取持仓股收盘价并记录净值
    print("  记录每日快照...")
    try:
        acct = _load_acct()
        current_prices = {}
        for code in acct.get("positions", {}):
            try:
                rt = get_stock_realtime(code)
                if rt:
                    p = float(rt.get("最新价", 0) or 0)
                    if p > 0:
                        current_prices[code] = p
            except Exception:
                pass
        account_snapshot(current_prices)
    except Exception as e:
        print(f"  WARNING: 快照记录失败: {e}")

    # 1. 收集数据
    print("  收集市场情绪...")
    try:
        market_sentiment = get_market_sentiment()
    except Exception as e:
        print(f"  WARNING: 市场情绪获取失败: {e}")
        market_sentiment = None

    print("  加载今日信号...")
    today_signals = _load_today_signals()

    print("  获取关注列表表现...")
    watchlist_codes = _parse_watchlist_codes()
    watchlist_perf = _gather_watchlist_performance(watchlist_codes)

    print("  获取账户状态...")
    account_summary = _get_account_summary()

    print("  全市场扫描 TOP20...")
    try:
        scan_top20 = scan_market_candidates(top_n=20)
    except Exception as e:
        print(f"  WARNING: 全市场扫描失败: {e}")
        scan_top20 = []

    print("  加载策略和经验教训...")
    strategies = _load_file(STRATEGIES_FILE)
    lessons = _load_file(LESSONS_FILE)

    # 1.5 加载今日交易记录和日志
    print("  加载交易记录和日志...")
    acct = _load_acct()
    today_trades_text = _get_today_trades(acct)
    exec_log, error_log = _load_today_logs()

    # 净值曲线
    snapshots = acct.get("daily_snapshots", [])
    snapshots_text = "暂无"
    if snapshots:
        recent = snapshots[-10:]
        lines = [f"- {s['date']}: 总资产¥{s['total_value']:,.2f} 收益率{s['total_return_pct']:+.2f}%" for s in recent]
        snapshots_text = "\n".join(lines)

    # 2. 构建 prompt 并调用 LLM
    prompt = _build_summary_prompt(
        market_sentiment, today_signals, watchlist_perf,
        account_summary, scan_top20, strategies, lessons,
        today_trades_text, exec_log, error_log, snapshots_text,
    )

    print("  调用 LLM 生成总结...")
    content, usage = _call_llm(prompt)

    # 3. 记录 token 用量
    if usage:
        _record_token_usage(usage)

    # 4. 保存总结文件
    os.makedirs(SUMMARY_DIR, exist_ok=True)
    summary_file = os.path.join(SUMMARY_DIR, f"{today}.md")
    header = f"# 每日经验总结 - {today}\n\n> 自动生成于 {now.strftime('%H:%M:%S')}\n\n"
    with open(summary_file, "w", encoding="utf-8") as f:
        f.write(header + content)
    print(f"  总结已保存: {summary_file}")

    # 5. 解析并回写策略
    print("  解析策略迭代建议...")
    strategy_update = _parse_strategy_update(content)
    _write_back_strategies(strategy_update)

    print(f"  完成!\n")
    return summary_file, content


if __name__ == "__main__":
    filepath, content = generate_daily_summary()
    print("\n--- 生成内容 ---")
    print(content)
