"""数据获取模块 - 基于 AKShare"""
import akshare as ak
import pandas as pd
from datetime import datetime, timedelta


def get_stock_list():
    """获取沪深主板股票列表（排除ST、科创板、创业板）"""
    df = ak.stock_zh_a_spot_em()
    df = df[~df["名称"].str.contains("ST|退")]
    df = df[~df["代码"].str.startswith("688")]   # 排除科创板
    df = df[~df["代码"].str.startswith("300")]   # 排除创业板
    df = df[~df["代码"].str.startswith("301")]   # 排除创业板
    return df


def get_realtime_quotes():
    """获取全部A股实时行情（已过滤）"""
    df = get_stock_list()
    cols = ["代码", "名称", "最新价", "涨跌幅", "涨跌额", "成交量", "成交额",
            "振幅", "最高", "最低", "今开", "昨收", "量比", "换手率", "市盈率-动态",
            "市净率", "总市值", "流通市值", "60日涨跌幅"]
    available_cols = [c for c in cols if c in df.columns]
    return df[available_cols]


def get_stock_history(code, period="daily", days=120):
    """获取个股历史K线"""
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    end_date = datetime.now().strftime("%Y%m%d")
    df = ak.stock_zh_a_hist(symbol=code, period=period,
                            start_date=start_date, end_date=end_date, adjust="qfq")
    return df


def get_stock_realtime(code):
    """获取单只股票实时行情"""
    df = ak.stock_zh_a_spot_em()
    row = df[df["代码"] == code]
    if row.empty:
        return None
    return row.iloc[0].to_dict()


def get_market_sentiment():
    """获取市场情绪指标"""
    result = {}

    # 涨跌家数统计
    try:
        df = ak.stock_zh_a_spot_em()
        total = len(df)
        up = len(df[df["涨跌幅"] > 0])
        down = len(df[df["涨跌幅"] < 0])
        flat = total - up - down
        limit_up = len(df[df["涨跌幅"] >= 9.9])
        limit_down = len(df[df["涨跌幅"] <= -9.9])
        result["market_breadth"] = {
            "total": total, "up": up, "down": down, "flat": flat,
            "limit_up": limit_up, "limit_down": limit_down,
            "up_ratio": round(up / total * 100, 1),
        }
    except Exception as e:
        result["market_breadth_error"] = str(e)

    # 主要指数
    try:
        indices = ak.stock_zh_index_spot_em()
        for idx_code, idx_name in [("000001", "上证指数"), ("399001", "深证成指"), ("399006", "创业板指")]:
            row = indices[indices["代码"] == idx_code]
            if not row.empty:
                r = row.iloc[0]
                result[idx_name] = {
                    "price": r.get("最新价"),
                    "change_pct": r.get("涨跌幅"),
                }
    except Exception as e:
        result["index_error"] = str(e)

    return result


def get_sector_flow():
    """获取板块资金流向"""
    try:
        df = ak.stock_sector_fund_flow_rank(indicator="今日", sector_type="行业资金流")
        return df.head(20)
    except Exception:
        return None


def get_north_flow():
    """获取北向资金流向"""
    try:
        df = ak.stock_hsgt_north_net_flow_in_em(symbol="北向")
        return df.tail(10)
    except Exception:
        return None


def get_stock_individual_fund_flow(code):
    """获取个股资金流向"""
    try:
        df = ak.stock_individual_fund_flow(stock=code, market="sh" if code.startswith("6") else "sz")
        return df.tail(10)
    except Exception:
        return None


def get_limit_up_pool(date=None):
    """获取涨停股池"""
    try:
        if date is None:
            date = datetime.now().strftime("%Y%m%d")
        return ak.stock_zt_pool_em(date=date)
    except Exception:
        return None


def get_limit_down_pool(date=None):
    """获取跌停股池"""
    try:
        if date is None:
            date = datetime.now().strftime("%Y%m%d")
        return ak.stock_zt_pool_dtgc_em(date=date)
    except Exception:
        return None


def get_board_concept_flow():
    """获取概念板块资金流向排名"""
    try:
        return ak.stock_board_concept_name_em()
    except Exception:
        return None


def get_board_industry_flow():
    """获取行业板块资金流向排名"""
    try:
        return ak.stock_board_industry_name_em()
    except Exception:
        return None


def calculate_technical_indicators(df):
    """计算技术指标"""
    if df is None or df.empty:
        return df

    df = df.copy()

    # MA均线
    for period in [5, 10, 20, 60]:
        df[f"MA{period}"] = df["收盘"].rolling(window=period).mean().round(3)

    # MACD
    ema12 = df["收盘"].ewm(span=12).mean()
    ema26 = df["收盘"].ewm(span=26).mean()
    df["DIF"] = (ema12 - ema26).round(3)
    df["DEA"] = df["DIF"].ewm(span=9).mean().round(3)
    df["MACD"] = ((df["DIF"] - df["DEA"]) * 2).round(3)

    # KDJ
    low_min = df["最低"].rolling(window=9).min()
    high_max = df["最高"].rolling(window=9).max()
    rsv = ((df["收盘"] - low_min) / (high_max - low_min) * 100).fillna(50)
    df["K"] = rsv.ewm(com=2).mean().round(2)
    df["D"] = df["K"].ewm(com=2).mean().round(2)
    df["J"] = (3 * df["K"] - 2 * df["D"]).round(2)

    # RSI
    delta = df["收盘"].diff()
    gain = delta.where(delta > 0, 0).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df["RSI"] = (100 - 100 / (1 + rs)).round(2)

    # 布林带
    df["BOLL_MID"] = df["收盘"].rolling(window=20).mean().round(3)
    std = df["收盘"].rolling(window=20).std()
    df["BOLL_UP"] = (df["BOLL_MID"] + 2 * std).round(3)
    df["BOLL_DN"] = (df["BOLL_MID"] - 2 * std).round(3)

    # 成交量MA
    df["VOL_MA5"] = df["成交量"].rolling(window=5).mean().round(0)
    df["VOL_MA10"] = df["成交量"].rolling(window=10).mean().round(0)

    return df
