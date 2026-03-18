"""交易信号生成器 - 调用 LLM API 生成操作建议"""
import json
import os
import requests
from datetime import datetime

from config import (
    LLM_API_URL, LLM_API_KEY, LLM_MODEL,
    MAX_POSITIONS, MAX_SINGLE_POSITION_PCT, STYLE,
)
from account import load_account
from data_fetcher import (
    get_realtime_quotes, get_market_sentiment,
    get_sector_flow, get_north_flow,
    get_limit_up_pool, get_stock_realtime,
    get_stocks_realtime_batch,
    get_stock_history, calculate_technical_indicators,
    scan_market_candidates,
)

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")
SIGNALS_DIR = os.path.join(PROJECT_ROOT, "signals")
WATCHLIST_FILE = os.path.join(PROJECT_ROOT, "memory", "watchlist.md")
STRATEGIES_FILE = os.path.join(PROJECT_ROOT, "memory", "strategies.md")
LESSONS_FILE = os.path.join(PROJECT_ROOT, "memory", "lessons.md")
DAILY_DIR = os.path.join(PROJECT_ROOT, "docs", "daily")
TOKEN_USAGE_DIR = os.path.join(PROJECT_ROOT, "token-usage")


def _load_watchlist():
    """加载关注列表"""
    try:
        with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "暂无关注列表"


def _load_latest_review():
    """加载最近一次每日复盘（找最新的 docs/daily/YYYY-MM-DD.md）"""
    try:
        if not os.path.isdir(DAILY_DIR):
            return None, None
        files = sorted([f for f in os.listdir(DAILY_DIR) if f.endswith(".md")], reverse=True)
        if not files:
            return None, None
        latest = files[0]
        date_str = latest.replace(".md", "")
        with open(os.path.join(DAILY_DIR, latest), "r", encoding="utf-8") as f:
            content = f.read()
        # 截取前2000字符避免 prompt 过长
        if len(content) > 2000:
            content = content[:2000] + "\n...(截断)"
        return date_str, content
    except Exception:
        return None, None


def _load_lessons():
    """加载经验教训"""
    try:
        with open(LESSONS_FILE, "r", encoding="utf-8") as f:
            content = f.read()
        # 截取前1500字符
        if len(content) > 1500:
            content = content[:1500] + "\n...(截断)"
        return content
    except FileNotFoundError:
        return None


def _load_strategies():
    """加载完整策略文件，提取选股流程、风险管理、形态筛选、止盈止损等关键章节"""
    try:
        with open(STRATEGIES_FILE, "r", encoding="utf-8") as f:
            content = f.read()
        # 提取所有关键章节
        target_headers = [
            "综合选股流程", "风险管理规则", "形态好", "市场情绪指标",
            "情绪周期四阶段", "技术分析策略工具箱",
        ]
        sections = []
        in_section = False
        for line in content.split("\n"):
            # 遇到目标章节标题，开始收集
            if any(h in line for h in target_headers):
                in_section = True
            # 遇到同级或更高级标题（## 开头），且不是目标章节，停止
            elif line.startswith("## ") and in_section:
                if not any(h in line for h in target_headers):
                    in_section = False
            if in_section:
                sections.append(line)
        return "\n".join(sections) if sections else content[:3000]
    except FileNotFoundError:
        return "暂无策略文件"


def _parse_watchlist_codes():
    """从 watchlist.md 动态解析关注股票代码"""
    import re
    content = _load_watchlist()
    # 匹配6位数字股票代码
    codes = re.findall(r'\b([036]\d{5})\b', content)
    # 去重并保持顺序
    seen = set()
    unique = []
    for c in codes:
        if c not in seen:
            seen.add(c)
            unique.append(c)
    return unique


def _gather_market_data():
    """采集市场数据"""
    data = {}

    # 市场情绪
    try:
        sentiment = get_market_sentiment()
        data["market_sentiment"] = json.dumps(sentiment, ensure_ascii=False, default=str)
    except Exception as e:
        data["market_sentiment"] = f"获取失败: {e}"

    # 板块资金流向 top10
    try:
        sector = get_sector_flow()
        if sector is not None:
            data["sector_flow"] = sector.head(10).to_string(index=False)
        else:
            data["sector_flow"] = "获取失败"
    except Exception as e:
        data["sector_flow"] = f"获取失败: {e}"

    # 北向资金
    try:
        north = get_north_flow()
        if north is not None:
            data["north_flow"] = north.tail(5).to_string(index=False)
        else:
            data["north_flow"] = "获取失败"
    except Exception as e:
        data["north_flow"] = f"获取失败: {e}"

    # 涨停池
    try:
        limit_up = get_limit_up_pool()
        if limit_up is not None:
            data["limit_up_count"] = len(limit_up)
            # 取连板数最高的前10只
            if "连板数" in limit_up.columns:
                top = limit_up.sort_values("连板数", ascending=False).head(10)
                cols = [c for c in ["代码", "名称", "连板数", "涨跌幅", "最新价"] if c in top.columns]
                data["limit_up_top"] = top[cols].to_string(index=False)
            else:
                data["limit_up_top"] = limit_up.head(10).to_string(index=False)
        else:
            data["limit_up_count"] = "获取失败"
            data["limit_up_top"] = "获取失败"
    except Exception as e:
        data["limit_up_count"] = f"获取失败: {e}"
        data["limit_up_top"] = ""

    return data


def _gather_watchlist_realtime(watchlist_codes):
    """获取关注股票的实时行情和技术指标（批量获取+逐只回退）"""
    # 先批量获取实时行情
    batch_data = get_stocks_realtime_batch(watchlist_codes)

    # 批量失败的票逐只重试
    for code in watchlist_codes:
        if code not in batch_data:
            single = get_stock_realtime(code)
            if single:
                batch_data[code] = single

    results = []
    for code in watchlist_codes:
        realtime = batch_data.get(code)
        if realtime is None:
            continue
        try:
            hist = get_stock_history(code, days=60)
            if hist is not None and not hist.empty:
                hist = calculate_technical_indicators(hist)
                latest = hist.iloc[-1]
                tech = {
                    "MA5": latest.get("MA5"),
                    "MA10": latest.get("MA10"),
                    "MA20": latest.get("MA20"),
                    "MACD": latest.get("MACD"),
                    "DIF": latest.get("DIF"),
                    "DEA": latest.get("DEA"),
                    "RSI": latest.get("RSI"),
                    "K": latest.get("K"),
                    "D": latest.get("D"),
                    "J": latest.get("J"),
                }
            else:
                tech = {}
            results.append({
                "code": code,
                "name": realtime.get("名称", ""),
                "price": realtime.get("最新价", ""),
                "change_pct": realtime.get("涨跌幅", ""),
                "volume_ratio": realtime.get("量比", ""),
                "turnover_rate": realtime.get("换手率", ""),
                "technical": tech,
            })
        except Exception:
            results.append({
                "code": code,
                "name": realtime.get("名称", ""),
                "price": realtime.get("最新价", ""),
                "change_pct": realtime.get("涨跌幅", ""),
                "technical": {},
            })
    return results


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
                f"挂单买入 {o['name']}({o['code']}) {o['shares']}股 限价¥{o['limit_price']} (创建于{o['created_at'][:10]})"
            )
    return {
        "cash": account["cash"],
        "initial_capital": account["initial_capital"],
        "positions": positions_info if positions_info else ["空仓"],
        "pending_orders": pending_info if pending_info else [],
        "total_trades": account["total_trades"],
    }


def check_and_settle_pending_orders():
    """
    检查挂单是否触价成交。
    逻辑：获取挂单股票的实时行情，若当日最低价 <= 挂单价，则判定成交并执行买入。
    当日收盘后（15:00后）未成交的挂单自动作废。
    返回：(settled_list, expired_list, still_pending_list)
    """
    from account import load_account, save_account, buy
    account = load_account()
    pending_orders = account.get("pending_orders", [])
    if not pending_orders:
        return [], [], []

    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    market_closed = now.hour >= 15

    settled = []
    expired = []
    still_pending = []

    for order in pending_orders:
        if order.get("status") != "pending":
            continue

        # 判断是否过期（非今日创建的挂单，或收盘后）
        order_date = order.get("created_at", "")[:10]
        if order_date != today or market_closed:
            order["status"] = "expired"
            expired.append(order)
            print(f"  ⏰ 挂单过期: {order['name']}({order['code']}) 限价¥{order['limit_price']}")
            continue

        # 获取挂单时间之后的最低价（用分时逐笔数据，精确过滤挂单时间）
        try:
            import akshare as ak
            order_time = order.get("updated_at", order.get("created_at", ""))
            order_time_str = order_time[11:19] if len(order_time) >= 19 else "00:00:00"  # HH:MM:SS
            df_intraday = ak.stock_intraday_em(symbol=order["code"])
            # 只看挂单时间之后的成交
            df_after = df_intraday[df_intraday["时间"] >= order_time_str]
            if df_after.empty:
                still_pending.append(order)
                continue
            low_after_order = float(df_after["成交价"].min())
        except Exception as e:
            print(f"  ⚠️  获取{order['code']}分时数据失败: {e}，回退到实时最低价")
            try:
                realtime = get_stock_realtime(order["code"])
                if realtime is None:
                    still_pending.append(order)
                    continue
                low_after_order = float(realtime.get("最新价", 9999))
            except Exception:
                still_pending.append(order)
                continue

        limit_price = float(order["limit_price"])
        if low_after_order <= limit_price:
            # 触价，执行买入
            print(f"  ✅ 挂单触价成交: {order['name']}({order['code']}) 挂单后最低价¥{low_after_order} <= 限价¥{limit_price}")
            success = buy(order["code"], order["name"], limit_price, order["shares"])
            if success:
                order["status"] = "filled"
                order["filled_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
                order["filled_price"] = limit_price
                settled.append(order)
            else:
                still_pending.append(order)
        else:
            print(f"  📋 挂单未触价: {order['name']}({order['code']}) 今日最低¥{today_low} > 限价¥{limit_price}")
            still_pending.append(order)

    # 更新 account.json 中的挂单状态
    account = load_account()  # 重新加载（buy() 可能已修改）
    # 合并所有订单（已成交/过期保留记录，pending继续）
    all_orders = []
    for o in pending_orders:
        if o["status"] == "filled":
            for s in settled:
                if s["code"] == o["code"] and s.get("created_at") == o.get("created_at"):
                    all_orders.append(s)
                    break
            else:
                all_orders.append(o)
        elif o["status"] == "expired":
            all_orders.append(o)
        else:
            all_orders.append(o)
    account["pending_orders"] = all_orders
    save_account(account)

    return settled, expired, still_pending


def _build_prompt(signal_type, market_data, watchlist_data, account_summary, scan_candidates=None):
    """构建 LLM prompt"""
    now = datetime.now()

    type_instructions = {
        "pre_market": """你是盘前分析师。现在是开盘前，请基于以下数据给出今日操作计划：
1. 分析大盘情绪和主线方向
2. 从关注列表中筛选今日最值得操作的标的
3. 给出明确的【买入建议】（股票代码+名称+挂单价+仓位比例）或【不操作】
4. 如果有持仓，评估是否需要卖出（挂卖价）
5. 给出风险提示""",

        "intraday": """你是盘中分析师。现在是交易时段，请基于实时数据给出即时建议：
1. 检查当前持仓表现，是否触发止损/止盈
2. 检查关注列表是否有异动机会
3. 给出明确操作：【买入/卖出/不操作】+ 具体价格
4. 如果不操作，说明原因""",

        "pre_close": """你是盘尾分析师。现在是收盘前（14:50），请给出最终操作建议：
1. 回顾今日行情走势
2. A股T+1下，尾盘买入可缩短风险窗口，是否有值得尾盘介入的机会
3. 持仓是否需要尾盘调整
4. 给出明确操作：【买入/卖出/不操作】+ 具体价格
5. 展望明日""",
    }

    # 动态加载最新策略 + 历史经验
    strategies_content = _load_strategies()
    review_date, review_content = _load_latest_review()
    lessons_content = _load_lessons()

    prompt = f"""{type_instructions.get(signal_type, type_instructions["intraday"])}

## 当前时间
{now.strftime('%Y-%m-%d %H:%M')}

## 交易规则
- 模拟账户，初始资金 ¥10,000
- 风格：{STYLE}
- 最大持仓：{MAX_POSITIONS} 只
- 单股仓位上限：{int(MAX_SINGLE_POSITION_PCT * 100)}%
- 选股范围：沪市主板(60开头) + 深市主板(000/001开头)，排除ST/科创/创业板
- **T+1规则（严格遵守）**：当日买入的股票，当日不能卖出。对于**今日新买入**的持仓，不要输出止损/止盈价格，等次日盘前结合开盘情况再给出具体的止损止盈策略。

## 策略（严格遵守，这是最新版策略，所有选股和操作决策必须依据此策略）
{strategies_content}

## 最近一次复盘（{review_date or '无'}）
{review_content or '暂无复盘记录。'}

## 经验教训
{lessons_content or '暂无。'}

## 账户状态
- 可用资金：¥{account_summary['cash']:,.2f}
- 当前持仓：{'; '.join(account_summary['positions'])}
- 挂单中：{'; '.join(account_summary['pending_orders']) if account_summary['pending_orders'] else '无'}
- 累计交易：{account_summary['total_trades']} 次

## 市场数据
### 市场情绪
{market_data.get('market_sentiment', '暂无')}

### 板块资金流向 Top10
{market_data.get('sector_flow', '暂无')}

### 北向资金（近5日）
{market_data.get('north_flow', '暂无')}

### 涨停数据
涨停数量：{market_data.get('limit_up_count', '暂无')}
连板龙头：
{market_data.get('limit_up_top', '暂无')}

## 关注股票实时数据
{json.dumps(watchlist_data, ensure_ascii=False, indent=2, default=str)}

## 全市场扫描候选（量化初筛 Top 结果）
以下是从全市场 ~2500 只沪深主板股票中，按涨幅、换手率、成交额等量化指标筛选出的候选股。
请结合技术指标综合判断，从中挑选值得操作的标的（也可以不选，如果都不合适）。
{json.dumps(scan_candidates or [], ensure_ascii=False, indent=2, default=str)}

## 输出格式要求
请严格按以下格式输出：

### 市场概况
（简要分析大盘/情绪/资金，2-3句话）

### 操作建议

| 操作 | 股票 | 代码 | 挂单价 | 数量/仓位 | 理由 |
|------|------|------|--------|----------|------|
（如有买入/卖出建议填写；如不操作则写一行"不操作"并说明原因）

### 风险提示
（1-2条核心风险）
"""
    return prompt


def _call_llm(prompt):
    """调用 LLM API"""
    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": "你是一个专业的A股短线交易分析师，擅长技术分析和情绪分析。你的建议必须具体到个股代码、价格和仓位，不能模糊。如果没有好的机会就明确说不操作。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
    }

    resp = requests.post(LLM_API_URL, headers=headers, json=payload, timeout=60)
    if resp.status_code != 200:
        error_msg = f"LLM API 调用失败 (HTTP {resp.status_code}): {resp.text[:200]}"
        print(f"  WARNING: {error_msg}")
        # 返回错误提示作为内容，让信号文件仍然记录
        fallback = f"### LLM API 不可用\n\n{error_msg}\n\n请检查网络环境或 API Key 是否有效。"
        return fallback, {}

    result = resp.json()

    # 提取 token 用量
    usage = result.get("usage", {})

    # 提取回复内容
    content = result["choices"][0]["message"]["content"]
    return content, usage


def _record_token_usage(usage):
    """记录 token 用量到 token-usage 目录"""
    os.makedirs(TOKEN_USAGE_DIR, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    usage_file = os.path.join(TOKEN_USAGE_DIR, f"{today}-聚合api.json")

    # 加载已有数据或初始化
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

    # 按模型累加
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

    # 总量累加
    data["summary"]["prompt_tokens"] += prompt_tokens
    data["summary"]["completion_tokens"] += completion_tokens
    data["summary"]["total_tokens"] += total_tokens
    data["summary"]["call_count"] += 1

    with open(usage_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"  Token 用量已记录: prompt={prompt_tokens}, completion={completion_tokens}, total={total_tokens}")


def _save_signal(signal_type, label, content):
    """保存信号文件到 signals/YYYY-MM-DD/ 目录"""
    now = datetime.now()
    date_dir = os.path.join(SIGNALS_DIR, now.strftime("%Y-%m-%d"))
    os.makedirs(date_dir, exist_ok=True)

    time_str = now.strftime("%H-%M")
    filename = f"{time_str}-{label}.md"
    filepath = os.path.join(date_dir, filename)

    header = f"# 交易信号 - {now.strftime('%Y-%m-%d %H:%M')} {label}\n\n"
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(header + content)

    print(f"  信号已保存: {filepath}")
    return filepath


def generate_signal(signal_type="intraday", label="盘中"):
    """生成一次交易信号（主入口）"""
    now = datetime.now()
    print(f"\n{'='*60}")
    print(f"[{now.strftime('%H:%M:%S')}] 开始生成交易信号 - {label}")
    print(f"{'='*60}")

    # 0. 检查挂单是否触价成交
    print("  检查挂单触价情况...")
    settled, expired, still_pending = check_and_settle_pending_orders()
    if settled:
        print(f"  🎯 本次成交挂单: {[o['name'] for o in settled]}")
    if expired:
        print(f"  ⏰ 过期挂单: {[o['name'] for o in expired]}")

    # 1. 采集数据
    print("  采集市场数据...")
    market_data = _gather_market_data()

    # 2. 全市场扫描（仅盘前/尾盘执行，盘中跳过）
    watchlist_codes = _parse_watchlist_codes()
    scan_detail = []
    if signal_type in ("pre_market", "pre_close"):
        print("  全市场扫描候选股...")
        scan_candidates = scan_market_candidates(exclude_codes=watchlist_codes, top_n=15)
        print(f"  扫描结果: {len(scan_candidates)} 只候选")
        # 候选股技术指标（取 top 8 做详细分析）
        for c in scan_candidates[:8]:
            try:
                hist = get_stock_history(c["code"], days=60)
                if hist is not None and not hist.empty:
                    hist = calculate_technical_indicators(hist)
                    latest = hist.iloc[-1]
                    c["technical"] = {
                        "MA5": latest.get("MA5"),
                        "MA10": latest.get("MA10"),
                        "MA20": latest.get("MA20"),
                        "MACD": latest.get("MACD"),
                        "RSI": latest.get("RSI"),
                    }
            except Exception:
                pass
            scan_detail.append(c)
    else:
        print("  盘中模式，跳过全市场扫描")

    # 3. 关注列表实时数据
    print(f"  获取关注股票实时行情({len(watchlist_codes)}只)...")
    watchlist_data = _gather_watchlist_realtime(watchlist_codes)

    # 5. 账户状态
    account_summary = _get_account_summary()

    # 6. 构建 prompt
    prompt = _build_prompt(signal_type, market_data, watchlist_data, account_summary,
                           scan_candidates=scan_detail)

    # 5. 调用 LLM
    print("  调用 LLM 生成分析...")
    content, usage = _call_llm(prompt)

    # 6. 记录 token 用量
    if usage:
        _record_token_usage(usage)

    # 7. 保存信号文件
    filepath = _save_signal(signal_type, label, content)

    print(f"  完成!\n")
    return filepath, content


# 支持直接运行测试
if __name__ == "__main__":
    import sys
    signal_type = sys.argv[1] if len(sys.argv) > 1 else "intraday"
    label = sys.argv[2] if len(sys.argv) > 2 else "测试"
    filepath, content = generate_signal(signal_type, label)
    print("\n--- 生成内容 ---")
    print(content)
