"""模拟账户管理"""
import json
import os
from datetime import datetime

ACCOUNT_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "account.json")


def _ensure_data_dir():
    os.makedirs(os.path.dirname(ACCOUNT_FILE), exist_ok=True)


def init_account(capital=10000):
    """初始化模拟账户"""
    _ensure_data_dir()
    account = {
        "initial_capital": capital,
        "cash": capital,
        "positions": {},  # {"000001": {"shares": 100, "avg_cost": 10.5, "buy_date": "2026-03-17"}}
        "total_trades": 0,
        "trade_history": [],
        "daily_snapshots": [],
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(ACCOUNT_FILE, "w", encoding="utf-8") as f:
        json.dump(account, f, ensure_ascii=False, indent=2)
    print(f"✅ 模拟账户已初始化，资金: ¥{capital:,.2f}")
    return account


def load_account():
    """加载账户"""
    if not os.path.exists(ACCOUNT_FILE):
        return init_account()
    with open(ACCOUNT_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_account(account):
    """保存账户"""
    _ensure_data_dir()
    with open(ACCOUNT_FILE, "w", encoding="utf-8") as f:
        json.dump(account, f, ensure_ascii=False, indent=2)


def buy(code, name, price, shares):
    """买入股票"""
    account = load_account()
    cost = price * shares
    commission = max(cost * 0.00025, 5)  # 佣金万2.5，最低5元
    total_cost = cost + commission

    if total_cost > account["cash"]:
        print(f"❌ 资金不足！需要 ¥{total_cost:,.2f}，可用 ¥{account['cash']:,.2f}")
        return False

    if len(account["positions"]) >= 2 and code not in account["positions"]:
        print(f"❌ 已持有 {len(account['positions'])} 只股票，超过最大持仓限制")
        return False

    account["cash"] -= total_cost

    if code in account["positions"]:
        pos = account["positions"][code]
        old_total = pos["shares"] * pos["avg_cost"]
        new_total = old_total + cost
        pos["shares"] += shares
        pos["avg_cost"] = round(new_total / pos["shares"], 3)
    else:
        account["positions"][code] = {
            "name": name,
            "shares": shares,
            "avg_cost": round(price, 3),
            "buy_date": datetime.now().strftime("%Y-%m-%d"),
        }

    trade = {
        "type": "buy",
        "code": code,
        "name": name,
        "price": price,
        "shares": shares,
        "commission": round(commission, 2),
        "total": round(total_cost, 2),
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    account["trade_history"].append(trade)
    account["total_trades"] += 1
    save_account(account)

    print(f"✅ 买入 {name}({code}) {shares}股 × ¥{price} = ¥{cost:,.2f} (佣金 ¥{commission:.2f})")
    print(f"   剩余资金: ¥{account['cash']:,.2f}")
    return True


def sell(code, price, shares):
    """卖出股票"""
    account = load_account()

    if code not in account["positions"]:
        print(f"❌ 未持有 {code}")
        return False

    pos = account["positions"][code]
    if shares > pos["shares"]:
        print(f"❌ 持有 {pos['shares']} 股，不足卖出 {shares} 股")
        return False

    revenue = price * shares
    commission = max(revenue * 0.00025, 5)  # 佣金
    stamp_tax = revenue * 0.0005  # 印花税万5（卖出收取）
    net_revenue = revenue - commission - stamp_tax

    account["cash"] += net_revenue

    profit = (price - pos["avg_cost"]) * shares
    profit_pct = (price / pos["avg_cost"] - 1) * 100

    if shares >= pos["shares"]:
        del account["positions"][code]
    else:
        pos["shares"] -= shares

    trade = {
        "type": "sell",
        "code": code,
        "name": pos["name"],
        "price": price,
        "shares": shares,
        "commission": round(commission, 2),
        "stamp_tax": round(stamp_tax, 2),
        "net_revenue": round(net_revenue, 2),
        "profit": round(profit, 2),
        "profit_pct": round(profit_pct, 2),
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    account["trade_history"].append(trade)
    account["total_trades"] += 1
    save_account(account)

    emoji = "📈" if profit > 0 else "📉"
    print(f"{emoji} 卖出 {pos['name']}({code}) {shares}股 × ¥{price}")
    print(f"   盈亏: ¥{profit:,.2f} ({profit_pct:+.2f}%)")
    print(f"   剩余资金: ¥{account['cash']:,.2f}")
    return True


def snapshot(current_prices=None):
    """每日快照 - 记录账户净值"""
    account = load_account()

    positions_value = 0
    position_details = {}
    for code, pos in account["positions"].items():
        if current_prices and code in current_prices:
            curr_price = current_prices[code]
        else:
            curr_price = pos["avg_cost"]  # 无实时价则用成本价
        value = curr_price * pos["shares"]
        positions_value += value
        profit_pct = (curr_price / pos["avg_cost"] - 1) * 100
        position_details[code] = {
            "name": pos["name"],
            "shares": pos["shares"],
            "avg_cost": pos["avg_cost"],
            "current_price": curr_price,
            "value": round(value, 2),
            "profit_pct": round(profit_pct, 2),
        }

    total_value = account["cash"] + positions_value
    total_return = (total_value / account["initial_capital"] - 1) * 100

    snap = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "cash": round(account["cash"], 2),
        "positions_value": round(positions_value, 2),
        "total_value": round(total_value, 2),
        "total_return_pct": round(total_return, 2),
        "positions": position_details,
    }
    account["daily_snapshots"].append(snap)
    save_account(account)

    print(f"\n{'='*50}")
    print(f"📊 账户快照 - {snap['date']}")
    print(f"{'='*50}")
    print(f"  💰 现金: ¥{snap['cash']:,.2f}")
    print(f"  📦 持仓市值: ¥{snap['positions_value']:,.2f}")
    print(f"  💎 总资产: ¥{snap['total_value']:,.2f}")
    print(f"  📈 总收益率: {snap['total_return_pct']:+.2f}%")
    if position_details:
        print(f"  --- 持仓明细 ---")
        for code, d in position_details.items():
            print(f"  {d['name']}({code}): {d['shares']}股 成本¥{d['avg_cost']} 现价¥{d['current_price']} ({d['profit_pct']:+.2f}%)")
    else:
        print(f"  🏖️  当前空仓")
    print(f"{'='*50}")
    return snap


def status():
    """打印当前账户状态"""
    account = load_account()
    print(f"\n💼 模拟账户状态")
    print(f"  初始资金: ¥{account['initial_capital']:,.2f}")
    print(f"  可用现金: ¥{account['cash']:,.2f}")
    print(f"  累计交易: {account['total_trades']} 次")
    if account["positions"]:
        print(f"  当前持仓:")
        for code, pos in account["positions"].items():
            print(f"    {pos['name']}({code}): {pos['shares']}股 × 成本¥{pos['avg_cost']} (买入日期: {pos['buy_date']})")
    else:
        print(f"  当前持仓: 空仓")
    return account
