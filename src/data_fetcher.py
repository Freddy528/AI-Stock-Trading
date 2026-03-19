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
                def _sf(val):
                    try:
                        return float(val) if val else 0
                    except (ValueError, TypeError):
                        return 0

                results[symbol] = {
                    "名称": fields[0],
                    "今开": _sf(fields[1]),
                    "昨收": _sf(fields[2]),
                    "最新价": _sf(fields[3]),
                    "最高": _sf(fields[4]),
                    "最低": _sf(fields[5]),
                    "竞买价": _sf(fields[6]),
                    "竞卖价": _sf(fields[7]),
                    "成交量": int(_sf(fields[8])),
                    "成交额": _sf(fields[9]),
                    # 五档买卖盘
                    "买一量": int(_sf(fields[10])), "买一价": _sf(fields[11]),
                    "买二量": int(_sf(fields[12])), "买二价": _sf(fields[13]),
                    "买三量": int(_sf(fields[14])), "买三价": _sf(fields[15]),
                    "买四量": int(_sf(fields[16])), "买四价": _sf(fields[17]),
                    "买五量": int(_sf(fields[18])), "买五价": _sf(fields[19]),
                    "卖一量": int(_sf(fields[20])), "卖一价": _sf(fields[21]),
                    "卖二量": int(_sf(fields[22])), "卖二价": _sf(fields[23]),
                    "卖三量": int(_sf(fields[24])), "卖三价": _sf(fields[25]),
                    "卖四量": int(_sf(fields[26])), "卖四价": _sf(fields[27]),
                    "卖五量": int(_sf(fields[28])), "卖五价": _sf(fields[29]),
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
# 指数行情（主+备，午休直接用新浪避免akshare卡死）
# ============================================================
def _is_lunch_break():
    """判断是否在午休时段（11:30-13:00），此时akshare(东方财富)不稳定"""
    now = datetime.now()
    minutes = now.hour * 60 + now.minute
    return 11 * 60 + 25 <= minutes <= 13 * 60 + 5


def _get_index_sina():
    """新浪获取指数（最稳定）"""
    sina = _sina_quote(["sh000001", "sz399001", "sz399006"])
    result = {}
    mapping = {"sh000001": "上证指数", "sz399001": "深证成指", "sz399006": "创业板指"}
    for k, name in mapping.items():
        if k in sina:
            result[name] = {"price": sina[k]["最新价"], "change_pct": sina[k]["涨跌幅"]}
    return result if result else None


@retry(max_retries=2, delay=1)
def _get_index_akshare():
    """akshare获取指数"""
    return ak.stock_zh_index_spot_em()


def get_index_quotes():
    """获取主要指数行情，午休优先新浪，正常时段优先akshare"""
    # 午休直接走新浪，避免akshare卡100+秒
    if _is_lunch_break():
        try:
            result = _get_index_sina()
            if result:
                return result
        except Exception:
            pass

    # 正常时段：akshare优先
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

    # 最终备用：新浪
    try:
        result = _get_index_sina()
        if result:
            return result
    except Exception:
        pass
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
# 盘前集合竞价分时数据
# ============================================================
@retry(max_retries=2, delay=1)
def get_pre_market_minutes(code):
    """获取盘前集合竞价分时数据（09:15-09:25），来源东方财富"""
    df = ak.stock_zh_a_hist_pre_min_em(
        symbol=code, start_time="09:00:00", end_time="09:30:00"
    )
    if df is not None and not df.empty:
        return df
    return None


# ============================================================
# 个股历史K线
# ============================================================
def get_stock_history(code, period="daily", days=120):
    """获取个股历史K线（akshare 主 + 腾讯备份）"""
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    end_date = datetime.now().strftime("%Y%m%d")
    # 主：akshare
    for attempt in range(3):
        try:
            df = ak.stock_zh_a_hist(symbol=code, period=period,
                                    start_date=start_date, end_date=end_date, adjust="qfq")
            if df is not None and len(df) > 0:
                return df
        except Exception:
            if attempt < 2:
                time.sleep(2)

    # 备用：腾讯日K线 API
    try:
        df = _get_history_tencent(code, days)
        if df is not None and len(df) > 0:
            return df
    except Exception:
        pass
    return None


def _get_history_tencent(code, days=120):
    """腾讯日K接口获取历史K线"""
    market = "sh" if code.startswith("6") else "sz"
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={market}{code},day,,,{days},qfq"
    r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    data = r.json()
    klines = data.get("data", {}).get(f"{market}{code}", {}).get("qfqday", [])
    if not klines:
        klines = data.get("data", {}).get(f"{market}{code}", {}).get("day", [])
    if not klines:
        return None
    rows = []
    for k in klines:
        if len(k) >= 6:
            rows.append({
                "日期": k[0],
                "开盘": float(k[1]),
                "收盘": float(k[2]),
                "最高": float(k[3]),
                "最低": float(k[4]),
                "成交量": float(k[5]) if len(k) > 5 else 0,
            })
    if rows:
        return pd.DataFrame(rows)
    return None


# ============================================================
# 市场情绪（主+备）
# ============================================================
def _market_breadth_from_sina():
    """从新浪全市场数据计算涨跌家数（午休/盘前最稳定）"""
    all_stocks = _fetch_all_stocks_sina()
    if not all_stocks:
        return None
    mainboard = _filter_mainboard(all_stocks)
    total = len(mainboard)
    if total == 0:
        return None
    up = len([s for s in mainboard if float(s.get("changepercent", 0)) > 0])
    down = len([s for s in mainboard if float(s.get("changepercent", 0)) < 0])
    flat = total - up - down
    limit_up = len([s for s in mainboard if float(s.get("changepercent", 0)) >= 9.9])
    limit_down = len([s for s in mainboard if float(s.get("changepercent", 0)) <= -9.9])
    return {
        "source": "新浪",
        "total": total, "up": up, "down": down, "flat": flat,
        "limit_up": limit_up, "limit_down": limit_down,
        "up_ratio": round(up / total * 100, 1),
    }


def get_market_sentiment():
    """获取市场情绪指标，优先新浪（最稳定），akshare备用"""
    result = {}

    # 涨跌家数统计：优先新浪（午休/盘前不卡），失败再 akshare
    breadth = None
    try:
        breadth = _market_breadth_from_sina()
    except Exception:
        pass

    if breadth is None:
        try:
            df = _get_spot_akshare()
            total = len(df)
            up = len(df[df["涨跌幅"] > 0])
            down = len(df[df["涨跌幅"] < 0])
            flat = total - up - down
            limit_up = len(df[df["涨跌幅"] >= 9.9])
            limit_down = len(df[df["涨跌幅"] <= -9.9])
            breadth = {
                "source": "东方财富",
                "total": total, "up": up, "down": down, "flat": flat,
                "limit_up": limit_up, "limit_down": limit_down,
                "up_ratio": round(up / total * 100, 1),
            }
        except Exception:
            pass

    if breadth is None:
        # 最后备用：北向汇总（部分数据）
        try:
            df = ak.stock_hsgt_fund_flow_summary_em()
            row = df[df["板块"] == "沪股通"].iloc[0]
            breadth = {
                "source": "北向汇总(仅沪股通)",
                "up": int(row.get("上涨数", 0)),
                "down": int(row.get("下跌数", 0)),
                "flat": int(row.get("持平数", 0)),
            }
        except Exception:
            pass

    if breadth:
        result["market_breadth"] = breadth
        # 超跌信号检测
        oversold = detect_oversold_signal(breadth)
        if oversold["is_oversold"]:
            result["oversold_signal"] = oversold
    else:
        result["market_breadth_error"] = "所有数据源均失败"

    # 主要指数
    index_data = get_index_quotes()
    result.update(index_data)

    return result


def detect_oversold_signal(breadth: dict) -> dict:
    """
    超跌反弹信号检测。
    条件：上涨家数 < 1000 且 跌停数(不含ST) < 20
    注：breadth 来自 _market_breadth_from_sina/_filter_mainboard，已排除 ST。
    """
    up = breadth.get("up", 9999)
    limit_down = breadth.get("limit_down", 9999)

    is_oversold = (up < 1000 and limit_down < 20)

    return {
        "is_oversold": is_oversold,
        "up_count": up,
        "limit_down_ex_st": limit_down,
        "signal": "超跌反弹" if is_oversold else "正常",
        "description": (
            f"上涨家数仅{up}家(<1000)，跌停{limit_down}家(<20，不含ST)，"
            "市场阴跌非恐慌崩盘，适合介入强势股博次日反弹"
        ) if is_oversold else "",
    }


# ============================================================
# 北向资金（已修复废弃接口）
# ============================================================
def get_north_flow():
    """获取北向资金流向（akshare 主 + 东财直连备份）"""
    # 主：akshare 汇总
    try:
        df = ak.stock_hsgt_fund_flow_summary_em()
        north = df[df["资金方向"] == "北向"]
        if north is not None and len(north) > 0:
            return north
    except Exception:
        pass
    # 备用1：akshare 历史
    try:
        df = ak.stock_hsgt_hist_em(symbol="沪股通")
        if df is not None and len(df) > 0:
            return df.tail(10)
    except Exception:
        pass
    # 备用2：东财直连 API
    try:
        url = "https://push2.eastmoney.com/api/qt/kamtbs.wss?fields1=f1,f2,f3,f4&fields2=f51,f52,f53,f54,f55,f56"
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        data = r.json()
        items = data.get("data", {}).get("s2n", [])
        if items:
            rows = []
            for item in items[-5:]:  # 最近5天
                parts = item.split(",")
                if len(parts) >= 4:
                    rows.append({
                        "日期": parts[0],
                        "沪股通净流入": parts[1],
                        "深股通净流入": parts[2],
                        "北向合计": parts[3],
                    })
            if rows:
                return pd.DataFrame(rows)
    except Exception:
        pass
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


def _get_sector_flow_sina():
    """新浪行业板块涨跌排行（午休/盘前均可用）"""
    url = "http://money.finance.sina.com.cn/q/view/newSinaHy.php"
    headers = {"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"}
    r = requests.get(url, timeout=10, headers=headers)
    r.encoding = "gbk"
    # 解析 JS 变量: var S_Finance_bankuai_sinaindustry = {...}
    import json as _json
    text = r.text.strip()
    eq_pos = text.find("=")
    if eq_pos < 0:
        return None
    json_str = text[eq_pos + 1:].strip().rstrip(";")
    data = _json.loads(json_str)
    # data: {node: "node,name,count,avg_price,change_pct,change_rate,volume,amount,leader_code,leader_chg,leader_price,leader_change,leader_name"}
    rows = []
    for node, val in data.items():
        parts = val.split(",")
        if len(parts) >= 13:
            rows.append({
                "板块名称": parts[1],
                "涨跌幅": float(parts[4]) if parts[4] else 0,
                "成交量": int(float(parts[6])) if parts[6] else 0,
                "成交额": float(parts[7]) if parts[7] else 0,
                "领涨股": f"{parts[12]}({parts[8]}) {parts[9]}%",
            })
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df = df.sort_values("涨跌幅", ascending=False)
    return df.head(20)


def get_sector_flow():
    """获取板块资金流向，午休优先新浪，正常时段三级 fallback"""
    # 午休直接走新浪（akshare在午休全线不稳定）
    if _is_lunch_break():
        try:
            result = _get_sector_flow_sina()
            if result is not None:
                return result
        except Exception:
            pass

    # 正常时段：akshare资金流向排名
    try:
        df = ak.stock_sector_fund_flow_rank(indicator="今日", sector_type="行业资金流")
        if df is not None and not df.empty:
            return df.head(20)
    except Exception:
        pass
    # 备用1：akshare行业板块涨跌排名
    try:
        df = ak.stock_board_industry_name_em()
        if df is not None and not df.empty:
            cols = [c for c in ["板块名称", "涨跌幅", "总市值", "换手率", "领涨股票"] if c in df.columns]
            return df[cols].head(20) if cols else df.head(20)
    except Exception:
        pass
    # 最终备用：新浪行业板块
    try:
        return _get_sector_flow_sina()
    except Exception:
        pass
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
def get_limit_up_pool(date=None):
    """获取涨停股池（akshare 主 + 新浪全市场自算备份）"""
    if date is None:
        date = datetime.now().strftime("%Y%m%d")
    # 主：akshare
    for attempt in range(2):
        try:
            df = ak.stock_zt_pool_em(date=date)
            if df is not None and len(df) > 0:
                return df
        except Exception:
            if attempt == 0:
                time.sleep(1)

    # 备份：从新浪全市场数据自算涨停
    try:
        all_stocks = _fetch_all_stocks_sina()
        if all_stocks:
            mainboard = _filter_mainboard(all_stocks)
            zt_list = []
            for s in mainboard:
                chg = float(s.get("changepercent", 0))
                if chg >= 9.9:
                    zt_list.append({
                        "代码": s.get("code", ""),
                        "名称": s.get("name", ""),
                        "涨跌幅": chg,
                        "最新价": float(s.get("trade", 0)),
                        "成交额": float(s.get("amount", 0)),
                        "连板数": 1,  # 新浪数据无连板信息，默认1
                    })
            if zt_list:
                return pd.DataFrame(zt_list)
    except Exception:
        pass
    return None


def get_limit_down_pool(date=None):
    """获取跌停股池（akshare 主 + 新浪自算备份）"""
    if date is None:
        date = datetime.now().strftime("%Y%m%d")
    for attempt in range(2):
        try:
            df = ak.stock_zt_pool_dtgc_em(date=date)
            if df is not None and len(df) > 0:
                return df
        except Exception:
            if attempt == 0:
                time.sleep(1)

    # 备份：从新浪全市场数据自算跌停
    try:
        all_stocks = _fetch_all_stocks_sina()
        if all_stocks:
            mainboard = _filter_mainboard(all_stocks)
            dt_list = []
            for s in mainboard:
                chg = float(s.get("changepercent", 0))
                if chg <= -9.9:
                    dt_list.append({
                        "代码": s.get("code", ""),
                        "名称": s.get("name", ""),
                        "涨跌幅": chg,
                        "最新价": float(s.get("trade", 0)),
                    })
            if dt_list:
                return pd.DataFrame(dt_list)
    except Exception:
        pass
    return None


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
_sina_all_stocks_cache = {"data": [], "ts": 0}


def _fetch_all_stocks_sina():
    """通过新浪批量接口获取全市场A股行情（含单页重试，连续失败容忍，120秒缓存）"""
    # 120秒内复用缓存，避免 generate_signal 中重复拉取
    if _sina_all_stocks_cache["data"] and (time.time() - _sina_all_stocks_cache["ts"]) < 120:
        return _sina_all_stocks_cache["data"]
    import json as _json
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://finance.sina.com.cn",
        "Connection": "close",
    })
    url = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"

    all_stocks = []
    consecutive_failures = 0
    for page in range(1, 80):
        params = {
            "page": page, "num": 80,
            "sort": "changepercent", "asc": 0,
            "node": "hs_a", "symbol": "", "_s_r_a": "ssd",
        }
        success = False
        for attempt in range(3):  # 单页最多重试3次
            try:
                r = session.get(url, params=params, timeout=15)
                data = _json.loads(r.text)
                if not data:
                    # 空数据说明已到最后一页
                    _sina_all_stocks_cache.update({"data": all_stocks, "ts": time.time()})
                    return all_stocks
                all_stocks.extend(data)
                consecutive_failures = 0
                success = True
                break
            except Exception:
                if attempt < 2:
                    time.sleep(1)
        if not success:
            consecutive_failures += 1
            # 连续3页失败才放弃，否则跳过继续
            if consecutive_failures >= 3:
                break
        if page % 20 == 0:
            time.sleep(0.5)

    _sina_all_stocks_cache.update({"data": all_stocks, "ts": time.time()})
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


def _score_candidate(code, name, price, change_pct, turnover, amount, high, low,
                     prev_close, pe=0, pb=0, market_cap=0, source="全市场扫描"):
    """对单只候选股计算综合评分，返回 dict 或 None（不符合条件）"""
    # 基础过滤
    if not (1 <= change_pct <= 8):
        return None
    if turnover < 2:
        return None
    if amount < 50000000:
        return None
    if price < 3 or price > 100:
        return None

    # 振幅（盘中波动）
    amplitude = round((high - low) / prev_close * 100, 2) if prev_close > 0 else 0

    # 综合评分
    score = 0
    if 3 <= change_pct <= 6:
        score += 30
    elif 2 <= change_pct < 3:
        score += 20
    else:
        score += 15
    if 3 <= turnover <= 10:
        score += 25
    elif turnover > 10:
        score += 15
    else:
        score += 10
    amount_yi = amount / 1e8
    if amount_yi >= 5:
        score += 25
    elif amount_yi >= 2:
        score += 20
    elif amount_yi >= 1:
        score += 15
    else:
        score += 10
    if 3 <= amplitude <= 8:
        score += 10
    if 0 < pe < 30:
        score += 10
    elif 0 < pe < 50:
        score += 5

    return {
        "code": code, "name": name, "price": price,
        "change_pct": change_pct, "turnover_rate": turnover,
        "amount": amount, "amount_yi": round(amount_yi, 2),
        "amplitude": amplitude, "high": high, "low": low,
        "open": 0, "prev_close": prev_close,
        "pe": pe, "pb": pb, "market_cap": market_cap,
        "score": score, "source": source,
    }


def _scan_via_sina(exclude_codes, top_n):
    """新浪数据源全市场扫描。返回 None=数据获取失败，[]=有数据但无候选"""
    all_stocks = _fetch_all_stocks_sina()
    if not all_stocks:
        return None
    mainboard = _filter_mainboard(all_stocks)
    candidates = []
    for s in mainboard:
        code = s.get("code", "")
        if code in exclude_codes:
            continue
        c = _score_candidate(
            code=code, name=s.get("name", ""),
            price=float(s.get("trade", 0)),
            change_pct=float(s.get("changepercent", 0)),
            turnover=float(s.get("turnoverratio", 0)),
            amount=float(s.get("amount", 0)),
            high=float(s.get("high", 0)),
            low=float(s.get("low", 0)),
            prev_close=float(s.get("settlement", 0)),
            pe=float(s.get("per", 0)),
            pb=float(s.get("pb", 0)),
            market_cap=float(s.get("nmc", 0)) * 10000,
            source="全市场扫描(新浪)",
        )
        if c:
            c["open"] = float(s.get("open", 0))
            candidates.append(c)
    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:top_n]


def _scan_via_akshare(exclude_codes, top_n):
    """akshare(东方财富)数据源全市场扫描。返回 None=数据获取失败，[]=有数据但无候选"""
    df = get_stock_list()  # 已含 retry + 过滤 ST/科创/创业板
    if df is None or df.empty:
        return None
    candidates = []
    for _, row in df.iterrows():
        code = row.get("代码", "")
        if code in exclude_codes:
            continue

        def _safe(val, default=0):
            try:
                v = float(val)
                return v if pd.notna(v) else default
            except (ValueError, TypeError):
                return default

        c = _score_candidate(
            code=code, name=row.get("名称", ""),
            price=_safe(row.get("最新价")),
            change_pct=_safe(row.get("涨跌幅")),
            turnover=_safe(row.get("换手率")),
            amount=_safe(row.get("成交额")),
            high=_safe(row.get("最高")),
            low=_safe(row.get("最低")),
            prev_close=_safe(row.get("昨收")),
            pe=_safe(row.get("市盈率-动态")),
            pb=_safe(row.get("市净率")),
            market_cap=_safe(row.get("总市值")),
            source="全市场扫描(东方财富)",
        )
        if c:
            c["open"] = _safe(row.get("今开"))
            candidates.append(c)
    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:top_n]


def scan_market_candidates(exclude_codes=None, top_n=15):
    """
    全市场扫描，筛选激进型短线候选股。
    数据源优先级：新浪批量接口 > akshare(东方财富)，自动 fallback + 重试。
    """
    if exclude_codes is None:
        exclude_codes = set()
    else:
        exclude_codes = set(exclude_codes)

    for attempt in range(2):
        if attempt > 0:
            print(f"    全市场扫描: 第{attempt+1}轮重试（等待3秒避免限频）...")
            time.sleep(3)

        # 主：新浪（分页批量，速度快）
        try:
            result = _scan_via_sina(exclude_codes, top_n)
            if result is not None and len(result) > 0:
                print(f"    全市场扫描: 新浪源成功, {len(result)}只候选")
                return result
            elif result is not None:
                print(f"    全市场扫描: 新浪有数据但无符合条件候选（可能非交易时段）")
            else:
                print(f"    全市场扫描: 新浪源数据获取失败, 切换备用源...")
        except Exception as e:
            print(f"    全市场扫描: 新浪源异常({e}), 切换备用源...")

        # 备用：akshare（东方财富 API，稍慢但稳定）
        try:
            result = _scan_via_akshare(exclude_codes, top_n)
            if result is not None and len(result) > 0:
                print(f"    全市场扫描: 东方财富源成功, {len(result)}只候选")
                return result
            elif result is not None:
                print(f"    全市场扫描: 东方财富有数据但无符合条件候选")
            else:
                print(f"    全市场扫描: 东方财富源数据获取失败")
        except Exception as e:
            print(f"    全市场扫描: 东方财富源异常({e})")

    print("    全市场扫描: 所有数据源+重试均失败")
    return []
