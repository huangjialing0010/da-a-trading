"""商品期货周期检测 — 防止筛选器被周期顶部低PE价值陷阱欺骗

用股票名称关键词匹配商品，查商品期货5年价格分位。
不做硬过滤，做标记+分类扣分。
"""

import json
import os
from datetime import datetime, date
from pathlib import Path

import akshare as ak

BASE_DIR = Path(__file__).parent.parent
CACHE_DIR = BASE_DIR / "data" / "market"


def _cache_valid(filepath: str, ttl_days: int) -> bool:
    if not os.path.exists(filepath):
        return False
    mtime = datetime.fromtimestamp(os.path.getmtime(filepath))
    return (datetime.now() - mtime).days < ttl_days


# 关键词 → 商品期货（按优先级匹配，命中即止）
# False-positives通过 _KEYWORD_BLACKLIST 排除
# type: industrial(硬周期/扣15分) | energy(能源/扣10分) | precious(贵金属/不扣分)
COMMODITY_MAP = {
    "铝": {"symbol": "AL0", "type": "industrial", "name": "沪铝"},
    "铜": {"symbol": "CU0", "type": "industrial", "name": "沪铜"},
    "钼": {"symbol": None, "type": "industrial", "name": "钼（无期货）"},
    "钢": {"symbol": "RB0", "type": "industrial", "name": "螺纹钢"},
    "金": {"symbol": "AU0", "type": "precious", "name": "沪金"},
    "煤": {"symbol": "ZC0", "type": "energy", "name": "动力煤"},
    "石化": {"symbol": "SC0", "type": "energy", "name": "原油"},
    "石油": {"symbol": "SC0", "type": "energy", "name": "原油"},
    "油服": {"symbol": "SC0", "type": "energy", "name": "原油"},
    "神华": {"symbol": "ZC0", "type": "energy", "name": "动力煤"},
}

# 名称含"金"但不是黄金股的公司（避免标记金风科技、金龙鱼等）
_KEYWORD_BLACKLIST = {
    "金": {"金风科技", "金龙鱼", "中金公司", "金山办公", "金地集团",
           "金螳螂", "金隅集团", "金发科技", "金域医学", "金诚信"},
}

# 分位阈值
THRESHOLDS = {
    "industrial": 0.60,  # >60%分位扣15分
    "energy": 0.60,      # >60%分位扣10分
    "precious": 0.80,    # >80%分位仅标记
}

PENALTIES = {
    "industrial": 15,
    "energy": 10,
    "precious": 0,  # 不扣分
}


def _match_commodity(name: str) -> dict | None:
    """根据股票名称匹配商品，按优先级返回第一个匹配。

    对"金"等宽泛关键词做黑名单排除，避免非周期股误匹配。
    """
    for keyword, info in COMMODITY_MAP.items():
        if keyword in name:
            blacklist = _KEYWORD_BLACKLIST.get(keyword, set())
            if name in blacklist:
                continue
            return info
    return None


def fetch_commodity_percentile(symbol: str, ttl_days: int = 1) -> dict | None:
    """获取商品期货当前价格在5年历史中的分位。缓存1天。"""
    cache_file = str(CACHE_DIR / f"commodity_{symbol}.json")
    if _cache_valid(cache_file, ttl_days):
        with open(cache_file, "r", encoding="utf-8") as f:
            return json.load(f)

    try:
        df = ak.futures_main_sina(symbol=symbol)
        if df is None or df.empty:
            return _load_cache_or_none(cache_file)

        close_col = "close" if "close" in df.columns else df.columns[4]
        close = df[close_col].dropna()
        if len(close) < 250:
            return None

        current = float(close.iloc[-1])
        recent = close.tail(min(len(close), 1250))  # ~5年
        low = float(recent.min())
        high = float(recent.max())
        pct = (current - low) / (high - low) if high > low else 0.5

        chg_1y = (current / float(close.iloc[-250]) - 1) if len(close) >= 250 else 0
        chg_1m = (current / float(close.iloc[-20]) - 1) if len(close) >= 20 else 0

        result = {
            "symbol": symbol,
            "price": round(current, 2),
            "pct": round(pct, 4),
            "chg_1y": round(chg_1y, 4),
            "chg_1m": round(chg_1m, 4),
            "high_5y": round(high, 2),
            "low_5y": round(low, 2),
            "date": date.today().isoformat(),
        }

        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
        return result

    except Exception:
        return _load_cache_or_none(cache_file)


def _load_cache_or_none(path: str) -> dict | None:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def check_commodity_cycle(name: str) -> dict | None:
    """输入股票名称，匹配商品→周期分析。

    返回 None 表示不是商品相关公司，或数据不可用。

    返回 dict:
        {commodity_name, pct, commodity_type, penalty, warning}
    """
    info = _match_commodity(name)
    if not info:
        return None

    # 无期货合约的商品（如钼），无法计算分位，跳过
    if not info["symbol"]:
        return None

    data = fetch_commodity_percentile(info["symbol"])
    if not data:
        return None

    pct = data["pct"]
    ctype = info["type"]
    threshold = THRESHOLDS.get(ctype, 0.60)
    penalty = PENALTIES.get(ctype, 0) if pct > threshold else 0

    if ctype == "precious":
        # 黄金只标记不扣分，阈值也更高(80%)
        if pct > threshold:
            return {
                "commodity": info["name"],
                "pct": pct,
                "type": ctype,
                "penalty": 0,
                "price": data["price"],
                "warning": f"[{info['name']}]处5年{pct:.0%}分位，金价高位 注意风险",
            }
        return None  # 黄金不到80%分位不提示

    # 工业和能源：超过阈值才告警
    if pct > threshold:
        return {
            "commodity": info["name"],
            "pct": pct,
            "type": ctype,
            "penalty": penalty,
            "price": data["price"],
            "warning": f"[{info['name']}]处5年{pct:.0%}分位，利润或为周期顶部(-{penalty}分)",
        }

    # 低于阈值但接近的给提示不扣分
    if pct > threshold - 0.15:
        return {
            "commodity": info["name"],
            "pct": pct,
            "type": ctype,
            "penalty": 0,
            "price": data["price"],
            "warning": f"[{info['name']}]处5年{pct:.0%}分位，注意跟踪",
        }

    return None  # 低位：不提示
