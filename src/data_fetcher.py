"""数据获取模块 - 基于 AKShare + 备用数据源（新浪/东方财富直连）"""
import akshare as ak
import pandas as pd
import requests
import time
import re
from datetime import datetime, timedelta


# ============================================================
# 通用重试装饰器
# ============================================================
def retry(max_retries=3, delay=2):
    """重试装饰器，失败后等待delay秒再试"""
    def decorator(func):
        def wrapper(*args, **kwargs):
            last_err = None
            for i in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_err = e
                    if i < max_retries - 1:
                        time.sleep(delay)
            raise last_err
        wrapper.__name__ = func.__name__
        return wrapper
    return decorator


# ============================================================
# 备用数据源：新浪行情（轻量、稳定、不限频）
# ============================================================
def _sina_quote(codes):
    """通过新浪接口获取实时行情，codes为列表如['sh600258','sz000001']"""
    url = f"https://hq.sinajs.cn/list={','.join(codes)}"
    headers = {"Referer": "https://finance.sina.com.cn"}
    r = requests.get(url, headers=headers, timeout=10)
    r.encoding = "gbk"
    results = {}
    for line in r.text.strip().split("\n"):
        match = re.match(r'var hq_str_(\w+)="(.*)";', line)
        if match and match.group(2):
            symbol = match.group(1)
            fields = match.group(2).split(",")
            if len(fields) >= 32:
                results[symbol] = {
                    "名称": fields[0],
                    "今开": float(fields[1]) if fields[1] else 0,
                    "昨收": float(fields[2]) if fields[2] else 0,
                    "最新价": float(fields[3]) if fields[3] else 0,
                    "最高": float(fields[4]) if fields[4] else 0,
                    "最低": float(fields[5]) if fields[5] else 0,
                    "成交量": int(float(fields[8])) if fields[8] else 0,
                    "成交额": float(fields[9]) if fields[9] else 0,
                    "日期": fields[30],
                    "时间": fields[31],
                }
                price = results[symbol]["最新价"]
                prev = results[symbol]["昨收"]
                high = results[symbol]["最高"]
                low = results[symbol]["最低"]
                if prev > 0:
                    results[symbol]["涨跌幅"] = round((price / prev - 1) * 100, 2)
                    results[symbol]["振幅"] = round((high - low) / prev * 100, 2)
                else:
                    results[symbol]["涨跌幅"] = 0
                    results[symbol]["振幅"] = 0
    return results


def _code_to_sina(code):
    """股票代码转新浪格式：600258 -> sh600258"""
    if code.startswith("6"):
        return f"sh{code}"
    else:
        return f"sz{code}"


# ============================================================
# 备用数据源：腾讯行情
# ============================================================
def _tencent_quote(codes):
    """通过腾讯接口获取实时行情，codes为列表如['sh600258','sz000001']"""
    url = f"https://qt.gtimg.cn/q={','.join(codes)}"
    r = requests.get(url, timeout=10)
    r.encoding = "gbk"
    results = {}
    for line in r.text.strip().split("\n"):
        match = re.match(r'v_(\w+)="(.*)";', line)
        if match and match.group(2):
            symbol = match.group(1)
            f = match.group(2).split("~")
            if len(f) >= 50:

                def _safe_float(val, default=0):
                    try:
                        return float(val) if val else default
                    except (ValueError, TypeError):
                        return default

                price = _safe_float(f[3])
                prev = _safe_float(f[4])
                results[symbol] = {
                    "名称": f[1],
                    "今开": _safe_float(f[5]),
                    "昨收": prev,
                    "最新价": price,
                    "最高": _safe_float(f[33]),
                    "最低": _safe_float(f[34]),
                    "成交量": int(_safe_float(f[6]) * 100),
                    "成交额": _safe_float(f[37]) * 10000,
                    "日期": f[30][:8] if len(f[30]) >= 8 else "",
                    "时间": f[30][8:] if len(f[30]) > 8 else "",
                    "涨跌幅": round((price / prev - 1) * 100, 2) if prev > 0 else 0,
                    "换手率": _safe_float(f[38]),
                    "市盈率-动态": _safe_float(f[39]),
                    "振幅": _safe_float(f[43]),
                    "总市值": _safe_float(f[45]) * 1e8,
                    "流通市值": _safe_float(f[44]) * 1e8,
                    "市净率": _safe_float(f[46]),
                    "量比": _safe_float(f[49]),
                }
    return results


# ============================================================
# 备用数据源：东方财富直连
# ============================================================
def _eastmoney_quote(codes):
    """通过东方财富接口获取实时行情，codes为原始代码列表如['600258','000001']"""
    secids = []
    for code in codes:
        market = "1" if code.startswith("6") else "0"
        secids.append(f"{market}.{code}")
    fields = "f12,f14,f2,f3,f4,f5,f6,f15,f16,f17,f18"
    url = f"https://push2.eastmoney.com/api/qt/ulist.np/get?fields={fields}&secids={','.join(secids)}"
    r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
    data = r.json()
    results = {}
    for item in data.get("data", {}).get("diff", []) or []:
        code = item.get("f12", "")

        def _em_price(val):
            """东方财富价格字段统一处理（可能是整数需要/100）"""
            if val is None or val == "-":
                return 0
            val = float(val)
            return val / 100 if val > 1000 else val

        results[code] = {
            "名称": item.get("f14", ""),
            "最新价": _em_price(item.get("f2")),
            "涨跌幅": _em_price(item.get("f3")),
            "成交量": item.get("f5", 0),
            "成交额": item.get("f6", 0),
            "最高": _em_price(item.get("f15")),
            "最低": _em_price(item.get("f16")),
            "今开": _em_price(item.get("f17")),
            "昨收": _em_price(item.get("f18")),
            "代码": code,
        }
    return results


# ============================================================
# 指数行情（主+备）
# ============================================================
@retry(max_retries=2, delay=1)
def _get_index_akshare():
    """akshare获取指数"""
    return ak.stock_zh_index_spot_em()


def get_index_quotes():
    """获取主要指数行情，akshare失败自动切新浪"""
    try:
        df = _get_index_akshare()
        result = {}
        for idx_code, idx_name in [("000001", "上证指数"), ("399001", "深证成指"), ("399006", "创业板指")]:
            row = df[df["代码"] == idx_code]
            if not row.empty:
                r = row.iloc[0]
                result[idx_name] = {"price": r.get("最新价"), "change_pct": r.get("涨跌幅")}
        if result:
            return result
    except Exception:
        pass

    # 备用：新浪
    try:
        sina = _sina_quote(["sh000001", "sz399001", "sz399006"])
        result = {}
        mapping = {"sh000001": "上证指数", "sz399001": "深证成指", "sz399006": "创业板指"}
        for k, name in mapping.items():
            if k in sina:
                result[name] = {"price": sina[k]["最新价"], "change_pct": sina[k]["涨跌幅"]}
        return result
    except Exception:
        return {}


# ============================================================
# 全市场行情（主+备）
# ============================================================
@retry(max_retries=2, delay=3)
def _get_spot_akshare():
    """akshare获取全市场行情"""
    return ak.stock_zh_a_spot_em()


def get_stock_list():
    """获取沪深主板股票列表（排除ST、科创板、创业板），akshare失败返回None"""
    try:
        df = _get_spot_akshare()
    except Exception:
        return None
    df = df[~df["名称"].str.contains("ST|退")]
    df = df[~df["代码"].str.startswith("688")]
    df = df[~df["代码"].str.startswith("300")]
    df = df[~df["代码"].str.startswith("301")]
    return df


def get_realtime_quotes():
    """获取全部A股实时行情（已过滤）"""
    df = get_stock_list()
    if df is None:
        return None
    cols = ["代码", "名称", "最新价", "涨跌幅", "涨跌额", "成交量", "成交额",
            "振幅", "最高", "最低", "今开", "昨收", "量比", "换手率", "市盈率-动态",
            "市净率", "总市值", "流通市值", "60日涨跌幅"]
    available_cols = [c for c in cols if c in df.columns]
    return df[available_cols]


# ============================================================
# 单只股票实时行情（主+备）
# ============================================================
def get_stock_realtime(code):
    """获取单只股票实时行情，多数据源轮询+重试"""
    sources = [
        ("新浪", lambda: _sina_single(code)),
        ("腾讯", lambda: _tencent_single(code)),
        ("东方财富", lambda: _eastmoney_single(code)),
        ("akshare", lambda: _akshare_single(code)),
    ]
    for attempt in range(2):
        for name, fetcher in sources:
            try:
                data = fetcher()
                if data and data.get("最新价", 0) > 0:
                    data["代码"] = code
                    return data
            except Exception:
                pass
        if attempt == 0:
            time.sleep(1)
    return None


def _sina_single(code):
    """新浪获取单只"""
    key = _code_to_sina(code)
    sina = _sina_quote([key])
    return sina.get(key)


def _tencent_single(code):
    """腾讯获取单只"""
    key = _code_to_sina(code)
    result = _tencent_quote([key])
    return result.get(key)


def _eastmoney_single(code):
    """东方财富获取单只"""
    result = _eastmoney_quote([code])
    return result.get(code)


def _akshare_single(code):
    """akshare获取单只"""
    df = _get_spot_akshare()
    row = df[df["代码"] == code]
    if not row.empty:
        return row.iloc[0].to_dict()
    return None


def get_stocks_realtime_batch(codes):
    """批量获取多只股票实时行情，多数据源轮询+重试"""
    if not codes:
        return {}

    sina_codes = [_code_to_sina(c) for c in codes]

    # 数据源优先级：新浪 > 腾讯 > 东方财富
    batch_sources = [
        ("新浪", lambda: _sina_quote(sina_codes), lambda r: {
            c: {**r[_code_to_sina(c)], "代码": c}
            for c in codes if _code_to_sina(c) in r
        }),
        ("腾讯", lambda: _tencent_quote(sina_codes), lambda r: {
            c: {**r[_code_to_sina(c)], "代码": c}
            for c in codes if _code_to_sina(c) in r
        }),
        ("东方财富", lambda: _eastmoney_quote(codes), lambda r: {
            c: r[c] for c in codes if c in r
        }),
    ]

    for attempt in range(2):
        for name, fetcher, mapper in batch_sources:
            try:
                raw = fetcher()
                if raw:
                    result = mapper(raw)
                    if result:
                        return result
            except Exception:
                pass
        if attempt == 0:
            time.sleep(1)
    return {}


# ============================================================
# 个股历史K线
# ============================================================
@retry(max_retries=3, delay=2)
def get_stock_history(code, period="daily", days=120):
    """获取个股历史K线"""
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    end_date = datetime.now().strftime("%Y%m%d")
    df = ak.stock_zh_a_hist(symbol=code, period=period,
                            start_date=start_date, end_date=end_date, adjust="qfq")
    return df


# ============================================================
# 市场情绪（主+备）
# ============================================================
def get_market_sentiment():
    """获取市场情绪指标，多数据源容错"""
    result = {}

    # 涨跌家数统计（akshare）
    try:
        df = _get_spot_akshare()
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
        # 备用1：新浪全市场数据
        try:
            all_stocks = _fetch_all_stocks_sina()
            mainboard = _filter_mainboard(all_stocks)
            total = len(mainboard)
            up = len([s for s in mainboard if float(s.get("changepercent", 0)) > 0])
            down = len([s for s in mainboard if float(s.get("changepercent", 0)) < 0])
            flat = total - up - down
            limit_up = len([s for s in mainboard if float(s.get("changepercent", 0)) >= 9.9])
            limit_down = len([s for s in mainboard if float(s.get("changepercent", 0)) <= -9.9])
            result["market_breadth"] = {
                "source": "sina_fallback",
                "total": total, "up": up, "down": down, "flat": flat,
                "limit_up": limit_up, "limit_down": limit_down,
                "up_ratio": round(up / total * 100, 1) if total > 0 else 0,
            }
        except Exception:
            # 备用2：北向汇总
            try:
                df = ak.stock_hsgt_fund_flow_summary_em()
                row = df[df["板块"] == "沪股通"].iloc[0]
                up = int(row.get("上涨数", 0))
                down = int(row.get("下跌数", 0))
                flat = int(row.get("持平数", 0))
                result["market_breadth_partial"] = {
                    "source": "hsgt_summary(仅沪股通)",
                    "up": up, "down": down, "flat": flat,
                }
            except Exception:
                pass

    # 主要指数
    index_data = get_index_quotes()
    result.update(index_data)

    return result


# ============================================================
# 北向资金（已修复废弃接口）
# ============================================================
@retry(max_retries=2, delay=1)
def get_north_flow():
    """获取北向资金流向（使用新版接口）"""
    try:
        df = ak.stock_hsgt_fund_flow_summary_em()
        north = df[df["资金方向"] == "北向"]
        return north
    except Exception:
        pass
    # 备用：历史数据接口
    try:
        df = ak.stock_hsgt_hist_em(symbol="沪股通")
        return df.tail(10)
    except Exception:
        return None


# ============================================================
# 板块行情（主+备）
# ============================================================
@retry(max_retries=2, delay=2)
def get_board_industry_flow():
    """获取行业板块行情"""
    return ak.stock_board_industry_name_em()


@retry(max_retries=2, delay=2)
def get_board_concept_flow():
    """获取概念板块行情"""
    return ak.stock_board_concept_name_em()


def get_sector_flow():
    """获取板块资金流向"""
    try:
        df = ak.stock_sector_fund_flow_rank(indicator="今日", sector_type="行业资金流")
        return df.head(20)
    except Exception:
        return None


# ============================================================
# 个股资金流向
# ============================================================
@retry(max_retries=2, delay=1)
def get_stock_individual_fund_flow(code):
    """获取个股资金流向"""
    df = ak.stock_individual_fund_flow(stock=code, market="sh" if code.startswith("6") else "sz")
    return df.tail(10)


# ============================================================
# 涨停/跌停/强势股池
# ============================================================
@retry(max_retries=2, delay=1)
def get_limit_up_pool(date=None):
    """获取涨停股池"""
    if date is None:
        date = datetime.now().strftime("%Y%m%d")
    return ak.stock_zt_pool_em(date=date)


@retry(max_retries=2, delay=1)
def get_limit_down_pool(date=None):
    """获取跌停股池"""
    if date is None:
        date = datetime.now().strftime("%Y%m%d")
    return ak.stock_zt_pool_dtgc_em(date=date)


# ============================================================
# 技术指标计算
# ============================================================
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


# ============================================================
# 全市场扫描（新浪批量接口，稳定可靠）
# ============================================================
def _fetch_all_stocks_sina():
    """通过新浪批量接口获取全市场A股行情"""
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://finance.sina.com.cn",
        "Connection": "close",
    })
    url = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"

    all_stocks = []
    for page in range(1, 80):
        params = {
            "page": page, "num": 80,
            "sort": "changepercent", "asc": 0,
            "node": "hs_a", "symbol": "", "_s_r_a": "ssd",
        }
        try:
            r = session.get(url, params=params, timeout=15)
            import json
            data = json.loads(r.text)
            if not data:
                break
            all_stocks.extend(data)
        except Exception:
            break
        if page % 20 == 0:
            time.sleep(0.5)

    return all_stocks


def _filter_mainboard(stocks):
    """过滤出沪深主板非ST股票"""
    return [s for s in stocks if
            "ST" not in s.get("name", "") and
            "退" not in s.get("name", "") and
            not s["code"].startswith("688") and
            not s["code"].startswith("300") and
            not s["code"].startswith("301") and
            not s["code"].startswith("8") and
            not s["code"].startswith("9") and
            not s["code"].startswith("4") and
            float(s.get("trade", 0)) > 0]


def scan_market_candidates(exclude_codes=None, top_n=15):
    """
    全市场扫描，筛选激进型短线候选股。
    返回 list[dict]，每个 dict 包含股票基本数据 + 筛选得分。

    筛选条件（激进型）：
    1. 涨幅 1~8%（有动量但不追涨停）
    2. 换手率 > 2%（活跃交易）
    3. 成交额 > 5000万（流动性保障）
    4. 量比信息（新浪接口无量比，用换手率代替活跃度）
    排序：综合评分 = 涨幅得分 + 换手率得分 + 成交额得分
    """
    if exclude_codes is None:
        exclude_codes = set()
    else:
        exclude_codes = set(exclude_codes)

    # 1. 获取全市场数据
    all_stocks = _fetch_all_stocks_sina()
    if not all_stocks:
        return []

    # 2. 过滤主板
    mainboard = _filter_mainboard(all_stocks)

    # 3. 量化筛选
    candidates = []
    for s in mainboard:
        code = s.get("code", "")
        if code in exclude_codes:
            continue

        change_pct = float(s.get("changepercent", 0))
        turnover = float(s.get("turnoverratio", 0))
        amount = float(s.get("amount", 0))
        price = float(s.get("trade", 0))
        high = float(s.get("high", 0))
        low = float(s.get("low", 0))
        prev_close = float(s.get("settlement", 0))

        # 基础过滤
        if not (1 <= change_pct <= 8):
            continue
        if turnover < 2:
            continue
        if amount < 50000000:
            continue
        if price < 3 or price > 100:
            continue

        # 振幅（盘中波动）
        amplitude = round((high - low) / prev_close * 100, 2) if prev_close > 0 else 0

        # 综合评分
        score = 0
        # 涨幅得分：3-6% 最优
        if 3 <= change_pct <= 6:
            score += 30
        elif 2 <= change_pct < 3:
            score += 20
        else:
            score += 15
        # 换手率得分：3-10% 最优
        if 3 <= turnover <= 10:
            score += 25
        elif turnover > 10:
            score += 15
        else:
            score += 10
        # 成交额得分（亿级加分）
        amount_yi = amount / 1e8
        if amount_yi >= 5:
            score += 25
        elif amount_yi >= 2:
            score += 20
        elif amount_yi >= 1:
            score += 15
        else:
            score += 10
        # 振幅合理性（3-8%活跃不乱）
        if 3 <= amplitude <= 8:
            score += 10
        # 低市盈率加分
        pe = float(s.get("per", 0))
        if 0 < pe < 30:
            score += 10
        elif 0 < pe < 50:
            score += 5

        candidates.append({
            "code": code,
            "name": s.get("name", ""),
            "price": price,
            "change_pct": change_pct,
            "turnover_rate": turnover,
            "amount": amount,
            "amount_yi": round(amount_yi, 2),
            "amplitude": amplitude,
            "high": high,
            "low": low,
            "open": float(s.get("open", 0)),
            "prev_close": prev_close,
            "pe": pe,
            "pb": float(s.get("pb", 0)),
            "market_cap": float(s.get("nmc", 0)) * 10000,
            "score": score,
            "source": "全市场扫描",
        })

    # 4. 按得分排序，取 top_n
    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:top_n]
