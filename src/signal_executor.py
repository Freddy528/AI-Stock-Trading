"""信号执行器 - 解析 LLM 交易指令 → 规则验证 → 自动执行"""
import json
import os
import re
import logging
from datetime import datetime

from config import (
    STOP_LOSS_PCT, DRAWDOWN_PAUSE_THRESHOLD,
    MAX_POSITIONS, MAX_SINGLE_POSITION_PCT, MAX_DAILY_TRADES,
)
from account import load_account, buy, sell

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")
LOGS_DIR = os.path.join(PROJECT_ROOT, "logs")

# 配置日志（防止重复挂载 handler）
os.makedirs(LOGS_DIR, exist_ok=True)

exec_logger = logging.getLogger("execution")
if not exec_logger.handlers:
    exec_handler = logging.FileHandler(os.path.join(LOGS_DIR, "execution.log"), encoding="utf-8")
    exec_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    exec_logger.addHandler(exec_handler)
    exec_logger.setLevel(logging.INFO)

err_logger = logging.getLogger("errors")
if not err_logger.handlers:
    err_handler = logging.FileHandler(os.path.join(LOGS_DIR, "errors.log"), encoding="utf-8")
    err_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    err_logger.addHandler(err_handler)
    err_logger.setLevel(logging.WARNING)


def execute(signal_content: str, signal_type: str) -> dict:
    """主入口：止损检查 → 解析 LLM 动作 → 验证 → 执行 → 返回报告"""
    report = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "signal_type": signal_type,
        "circuit_breaker": False,
        "stop_loss_actions": [],
        "parsed_actions": [],
        "executed": [],
        "rejected": [],
        "errors": [],
    }

    try:
        account = load_account()
        current_prices = _get_current_prices(account)
        total_assets = _calc_total_assets(account, current_prices)

        # 1. 熔断检查
        if total_assets < DRAWDOWN_PAUSE_THRESHOLD:
            report["circuit_breaker"] = True
            report["total_assets"] = round(total_assets, 2)
            exec_logger.info(f"[熔断] 总资产 ¥{total_assets:,.2f} < ¥{DRAWDOWN_PAUSE_THRESHOLD:,.2f}，禁止买入")
            print(f"  ⚠️ 熔断状态：总资产 ¥{total_assets:,.2f} < ¥{DRAWDOWN_PAUSE_THRESHOLD:,.2f}，禁止买入")

        # 2. 止损检查（优先于 LLM 决策）
        stop_loss_results = _check_stop_loss(account, current_prices)
        report["stop_loss_actions"] = stop_loss_results

        # 3. 解析 LLM 动作
        actions = _parse_actions(signal_content)
        report["parsed_actions"] = actions

        if not actions:
            exec_logger.info(f"[{signal_type}] 无交易动作（hold/不操作）")
            _log_execution(report)
            return report

        # 4. 逐个验证并执行
        for action in actions:
            # 重新加载账户（前一个操作可能已修改）
            account = load_account()
            # 复用已有价格，仅为新出现的持仓补充获取
            for code in account.get("positions", {}):
                if code not in current_prices:
                    current_prices.update(_get_current_prices(account))
                    break
            total_assets = _calc_total_assets(account, current_prices)

            # 熔断：禁买允卖
            if report["circuit_breaker"] and action["action"] == "buy":
                report["rejected"].append({
                    **action, "reason": f"熔断状态，总资产 ¥{total_assets:,.2f} < ¥{DRAWDOWN_PAUSE_THRESHOLD:,.2f}"
                })
                continue

            valid, reject_reason = _validate_action(action, account, current_prices, total_assets)
            if not valid:
                report["rejected"].append({**action, "reject_reason": reject_reason})
                exec_logger.info(f"[拒绝] {action['action']} {action.get('code','')} {action.get('name','')}: {reject_reason}")
                print(f"  ❌ 规则拒绝 {action['action']} {action.get('name','')}: {reject_reason}")
                continue

            result = _execute_action(action, account, total_assets)
            report["executed"].append(result)

    except Exception as e:
        report["errors"].append(str(e))
        _log_error("execute_error", str(e))

    _log_execution(report)
    return report


def _parse_actions(content: str) -> list[dict]:
    """从 LLM 输出解析 trade_actions JSON 代码块，失败时回退到正则匹配表格"""
    # 优先：```trade_actions``` JSON 代码块
    pattern = r'```trade_actions\s*\n(.*?)\n```'
    match = re.search(pattern, content, re.DOTALL)
    if match:
        try:
            actions = json.loads(match.group(1))
            if isinstance(actions, list):
                # 过滤 hold 动作
                return [a for a in actions if a.get("action") in ("buy", "sell")]
        except json.JSONDecodeError as e:
            _log_error("json_parse_error", f"trade_actions JSON 解析失败: {e}", match.group(1)[:200])

    # 回退：正则匹配 markdown 表格
    actions = _parse_table_fallback(content)
    if actions:
        return actions

    # 都失败
    if "不操作" in content or "hold" in content.lower():
        return []  # 明确不操作

    # LLM API 失败返回的错误信息，不记为解析失败
    if "LLM API 不可用" in content or "API 调用失败" in content:
        _log_error("llm_api_error", "LLM API 不可用，本次不执行", content[:200])
        return []

    _log_error("parse_failure", "无法从 LLM 输出解析交易动作", content[:300])
    return []


def _parse_table_fallback(content: str) -> list[dict]:
    """从 markdown 表格回退解析交易动作"""
    actions = []
    # 匹配表格行：| 买入/卖出 | 股票名 | 代码 | 价格 | 仓位/数量 | 理由 |
    table_pattern = r'\|\s*(买入|卖出)\s*\|\s*(.+?)\s*\|\s*(\d{6})\s*\|\s*[¥￥]?([\d.]+)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|'
    for m in re.finditer(table_pattern, content):
        action_type = "buy" if m.group(1) == "买入" else "sell"
        name = m.group(2).strip()
        code = m.group(3).strip()
        price = float(m.group(4))
        position_str = m.group(5).strip()
        reason = m.group(6).strip()

        # 解析仓位比例
        position_pct = 0.5  # 默认 50%
        pct_match = re.search(r'(\d+)\s*%', position_str)
        if pct_match:
            position_pct = int(pct_match.group(1)) / 100

        actions.append({
            "action": action_type,
            "code": code,
            "name": name,
            "price": price,
            "position_pct": position_pct,
            "reason": reason,
        })
    return actions


def _validate_action(action: dict, account: dict, current_prices: dict, total_assets: float) -> tuple[bool, str]:
    """规则引擎验证（代码级，不可被 LLM 覆盖）"""
    act = action["action"]
    code = action.get("code", "")
    price = action.get("price", 0)
    position_pct = action.get("position_pct", 0.5)
    today = datetime.now().strftime("%Y-%m-%d")

    if act == "buy":
        # 1. 持仓数限制
        positions = account.get("positions", {})
        if len(positions) >= MAX_POSITIONS and code not in positions:
            return False, f"持仓数已达上限 {MAX_POSITIONS}，禁止买新股"

        # 2. 单股仓位限制
        if position_pct > MAX_SINGLE_POSITION_PCT:
            return False, f"单股仓位 {position_pct*100:.0f}% 超过上限 {MAX_SINGLE_POSITION_PCT*100:.0f}%"

        # 3. 每日交易次数限制
        today_trades = sum(1 for t in account.get("trade_history", [])
                          if t.get("time", "")[:10] == today)
        if today_trades >= MAX_DAILY_TRADES:
            return False, f"今日已交易 {today_trades} 次，达到上限 {MAX_DAILY_TRADES}"

        # 4. 选股范围：60/000/001，排除 ST/688/300
        if not (code.startswith("60") or code.startswith("000") or code.startswith("001")):
            return False, f"代码 {code} 不在交易范围（仅 60/000/001）"

        # 5. RSI >= 70 禁止买入（实时获取技术指标验证）
        # 6. 距 20 日低点涨幅 > 30% 禁止买入
        rsi, gain_from_low = _fetch_risk_indicators(code)
        if rsi is not None and rsi >= 70:
            return False, f"RSI={rsi:.1f} >= 70，超买禁止买入"
        if gain_from_low is not None and gain_from_low > 30:
            return False, f"距20日低点涨幅 {gain_from_low:.1f}% > 30%，位置偏高"

        # 7. 资金充足性
        shares = _calc_shares(total_assets, position_pct, price)
        if shares <= 0:
            return False, "资金不足，无法买入最少1手(100股)"
        cost = price * shares * 1.0001  # 含佣金
        if cost > account["cash"]:
            return False, f"资金不足：需 ¥{cost:,.2f}，可用 ¥{account['cash']:,.2f}"

    elif act == "sell":
        positions = account.get("positions", {})
        if code not in positions:
            return False, f"未持有 {code}，无法卖出"

        # T+1：今日买入不可卖出
        pos = positions[code]
        if pos.get("buy_date") == today:
            return False, f"T+1限制：{pos['name']}({code}) 今日买入，明日才能卖出"

    return True, ""


def _execute_action(action: dict, account: dict, total_assets: float) -> dict:
    """执行交易：计算股数(100整数倍) → account.buy()/sell()"""
    act = action["action"]
    code = action["code"]
    name = action.get("name", "")
    price = action["price"]
    result = {"action": act, "code": code, "name": name, "price": price}

    try:
        if act == "buy":
            position_pct = action.get("position_pct", 0.5)
            shares = _calc_shares(total_assets, position_pct, price)
            if shares <= 0:
                result["success"] = False
                result["error"] = "计算股数为0"
                return result
            success = buy(code, name, price, shares)
            result["shares"] = shares
            result["success"] = success
            if success:
                exec_logger.info(f"[买入] {name}({code}) {shares}股 × ¥{price} = ¥{price*shares:,.2f} 仓位{position_pct*100:.0f}%")
            else:
                exec_logger.warning(f"[买入失败] {name}({code}) {shares}股 × ¥{price}")

        elif act == "sell":
            positions = account.get("positions", {})
            pos = positions.get(code, {})
            shares = action.get("shares", pos.get("shares", 0))
            if shares <= 0:
                result["success"] = False
                result["error"] = "无可卖股数"
                return result
            success = sell(code, price, shares)
            result["shares"] = shares
            result["success"] = success
            if success:
                profit_pct = (price / pos.get("avg_cost", price) - 1) * 100
                exec_logger.info(f"[卖出] {name}({code}) {shares}股 × ¥{price} 盈亏{profit_pct:+.2f}%")
            else:
                exec_logger.warning(f"[卖出失败] {name}({code}) {shares}股 × ¥{price}")

    except Exception as e:
        result["success"] = False
        result["error"] = str(e)
        _log_error("execute_action_error", str(e), {"action": action})

    return result


def _check_stop_loss(account: dict, current_prices: dict) -> list[dict]:
    """检查持仓止损：亏损 >= 8% → 强制卖出（T+1 限制的记录警告）"""
    results = []
    today = datetime.now().strftime("%Y-%m-%d")

    for code, pos in account.get("positions", {}).items():
        curr_price = current_prices.get(code)
        if curr_price is None or curr_price <= 0:
            continue

        avg_cost = pos.get("avg_cost", 0)
        if avg_cost <= 0:
            continue

        loss_pct = (curr_price / avg_cost - 1)

        if loss_pct <= STOP_LOSS_PCT:
            if pos.get("buy_date") == today:
                # T+1 限制，无法卖出，记录警告
                warning = f"[止损警告] {pos['name']}({code}) 亏损{loss_pct*100:.2f}%，但T+1限制无法卖出，次日优先处理"
                exec_logger.warning(warning)
                err_logger.warning(warning)
                print(f"  ⚠️ {warning}")
                results.append({
                    "code": code, "name": pos["name"],
                    "loss_pct": round(loss_pct * 100, 2),
                    "action": "warning_t1", "executed": False,
                })
            else:
                # 强制卖出
                shares = pos["shares"]
                print(f"  🚨 止损触发：{pos['name']}({code}) 亏损{loss_pct*100:.2f}%，强制卖出 {shares}股")
                success = sell(code, curr_price, shares)
                exec_logger.info(f"[止损卖出] {pos['name']}({code}) {shares}股 × ¥{curr_price} 亏损{loss_pct*100:.2f}%")
                results.append({
                    "code": code, "name": pos["name"],
                    "loss_pct": round(loss_pct * 100, 2),
                    "action": "stop_loss_sell", "executed": success,
                    "price": curr_price, "shares": shares,
                })

    return results


def _fetch_risk_indicators(code: str) -> tuple:
    """获取个股 RSI 和距 20 日低点涨幅，用于代码级硬性规则验证。
    返回 (rsi, gain_from_20d_low)，获取失败返回 (None, None)。
    """
    try:
        from data_fetcher import get_stock_history, calculate_technical_indicators, get_stock_realtime
        hist = get_stock_history(code, days=60)
        if hist is None or hist.empty or len(hist) < 5:
            return None, None

        hist = calculate_technical_indicators(hist)
        latest = hist.iloc[-1]
        rsi = latest.get("RSI")

        # 距 20 日低点涨幅
        gain_from_low = None
        low_20d = hist["最低"].tail(20).min() if "最低" in hist.columns else None
        if low_20d and low_20d > 0:
            realtime = get_stock_realtime(code)
            current_price = float(realtime.get("最新价", 0) or 0) if realtime else 0
            if current_price > 0:
                gain_from_low = (current_price - low_20d) / low_20d * 100

        return rsi, gain_from_low
    except Exception as e:
        _log_error("risk_indicator_error", f"获取{code}风控指标失败: {e}")
        return None, None


def _calc_shares(total_assets: float, position_pct: float, price: float) -> int:
    """计算买入股数（100股整数倍），最少1手"""
    if price <= 0:
        return 0
    target_amount = total_assets * position_pct
    shares = int(target_amount / price / 100) * 100
    return max(shares, 0)


def _calc_total_assets(account: dict, current_prices: dict) -> float:
    """计算总资产 = 现金 + 持仓市值"""
    total = account.get("cash", 0)
    for code, pos in account.get("positions", {}).items():
        price = current_prices.get(code, pos.get("avg_cost", 0))
        total += price * pos.get("shares", 0)
    return total


def _get_current_prices(account: dict) -> dict:
    """获取持仓股票的实时价格"""
    positions = account.get("positions", {})
    if not positions:
        return {}

    prices = {}
    try:
        from data_fetcher import get_stocks_realtime_batch, get_stock_realtime
        codes = list(positions.keys())
        batch = get_stocks_realtime_batch(codes)
        for code in codes:
            if code in batch:
                p = float(batch[code].get("最新价", 0) or 0)
                if p > 0:
                    prices[code] = p
            if code not in prices:
                single = get_stock_realtime(code)
                if single:
                    p = float(single.get("最新价", 0) or 0)
                    if p > 0:
                        prices[code] = p
    except Exception as e:
        _log_error("price_fetch_error", f"获取实时价格失败: {e}")

    # 未获取到价格的用成本价兜底
    for code, pos in positions.items():
        if code not in prices:
            prices[code] = pos.get("avg_cost", 0)

    return prices


def _log_execution(report: dict):
    """记录执行报告到 logs/execution.log"""
    exec_logger.info(f"--- 执行报告 [{report['signal_type']}] ---")
    if report["circuit_breaker"]:
        exec_logger.info(f"  熔断状态: 是")
    if report["stop_loss_actions"]:
        exec_logger.info(f"  止损动作: {json.dumps(report['stop_loss_actions'], ensure_ascii=False)}")
    exec_logger.info(f"  解析动作数: {len(report['parsed_actions'])}")
    exec_logger.info(f"  执行成功: {len(report['executed'])}")
    exec_logger.info(f"  被拒绝: {len(report['rejected'])}")
    if report["errors"]:
        exec_logger.error(f"  错误: {report['errors']}")


def _log_error(error_type: str, message: str, context=None):
    """记录到 logs/errors.log"""
    err_logger.warning(f"[{error_type}] {message}")
    if context:
        err_logger.warning(f"  context: {str(context)[:500]}")


def format_execution_report(report: dict) -> str:
    """将执行报告格式化为 markdown，用于追加到信号文件"""
    lines = ["\n\n---\n\n## 执行报告\n"]
    lines.append(f"> 执行时间: {report['time']}\n")

    if report.get("circuit_breaker"):
        lines.append(f"**⚠️ 熔断状态** — 总资产低于 ¥{DRAWDOWN_PAUSE_THRESHOLD:,.0f}，禁止买入\n")

    if report.get("stop_loss_actions"):
        lines.append("### 止损动作")
        for sl in report["stop_loss_actions"]:
            status = "已执行" if sl.get("executed") else ("T+1限制" if sl.get("action") == "warning_t1" else "失败")
            lines.append(f"- {sl['name']}({sl['code']}) 亏损 {sl['loss_pct']:.2f}% → {status}")
        lines.append("")

    if report.get("executed"):
        lines.append("### 已执行")
        for ex in report["executed"]:
            emoji = "✅" if ex.get("success") else "❌"
            lines.append(f"- {emoji} {ex['action']} {ex.get('name','')}({ex['code']}) {ex.get('shares','')}股 × ¥{ex['price']}")
        lines.append("")

    if report.get("rejected"):
        lines.append("### 被规则拒绝")
        for rj in report["rejected"]:
            reason = rj.get("reject_reason", rj.get("reason", ""))
            lines.append(f"- ❌ {rj['action']} {rj.get('name','')}({rj.get('code','')}): {reason}")
        lines.append("")

    if not report.get("executed") and not report.get("rejected") and not report.get("stop_loss_actions"):
        lines.append("*无交易动作*\n")

    return "\n".join(lines)
