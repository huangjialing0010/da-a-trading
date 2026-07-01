"""申万行业分类数据获取 — akshare 封装 + 本地缓存

数据源:
- sw_index_first_info: 31个一级行业 PE/PB/股息率
- stock_industry_clf_hist_sw: 全部A股→申万行业代码映射 (swsresearch.com, SSL证书问题)
- stock_industry_category_cninfo: 行业代码→名称树
- index_hist_sw: 行业指数历史日线
"""

import os
import json
import warnings
from contextlib import contextmanager
from datetime import datetime, date
from pathlib import Path

import pandas as pd
import akshare as ak
import requests

BASE_DIR = Path(__file__).parent.parent
CACHE_DIR = BASE_DIR / "data"
MARKET_DIR = CACHE_DIR / "market"
SW_INDEX_DIR = CACHE_DIR / "sw_index"


@contextmanager
def _insecure_ssl():
    """临时禁用SSL证书验证，仅用于swsresearch.com的过期证书。

    用完自动恢复，不影响其他模块的网络请求。
    """
    original = requests.get
    def _get(*args, **kwargs):
        kwargs["verify"] = False
        return original(*args, **kwargs)
    requests.get = _get
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            yield
        finally:
            requests.get = original


def _cache_path(subdir: Path, key: str, suffix: str = ".csv") -> str:
    subdir.mkdir(parents=True, exist_ok=True)
    safe_key = key.replace("/", "_").replace("\\", "_")
    return str(subdir / f"{safe_key}{suffix}")


def _cache_valid(filepath: str, ttl_days: int) -> bool:
    if not os.path.exists(filepath):
        return False
    mtime = datetime.fromtimestamp(os.path.getmtime(filepath))
    return (datetime.now() - mtime).days < ttl_days


# === 申万一级行业 ===

def fetch_level1_industries(ttl_days: int = 1) -> pd.DataFrame:
    """获取31个申万一级行业的PE/PB/股息率/成分股数。缓存1天。"""
    cache_file = _cache_path(MARKET_DIR, "sw_level1")
    if _cache_valid(cache_file, ttl_days):
        return pd.read_csv(cache_file, dtype={"行业代码": str, "行业名称": str})

    try:
        df = ak.sw_index_first_info()
        if df is not None and not df.empty:
            df.to_csv(cache_file, index=False, encoding="utf-8")
            return df
    except Exception:
        pass

    if os.path.exists(cache_file):
        return pd.read_csv(cache_file, dtype={"行业代码": str, "行业名称": str})
    return pd.DataFrame()


def fetch_level1_history(code: str, start_date: str = "20200101",
                         ttl_days: int = 1) -> pd.DataFrame:
    """获取申万一级行业指数历史日线。缓存1天。"""
    cache_file = _cache_path(SW_INDEX_DIR, code)
    if _cache_valid(cache_file, ttl_days):
        df = pd.read_csv(cache_file, index_col=0, parse_dates=True)
        if not df.empty:
            return df

    try:
        df = ak.index_hist_sw(symbol=code)
        if df is not None and not df.empty:
            col_map = {}
            for c in df.columns:
                if c in ("日期", "date"):
                    col_map[c] = "date"
                elif c in ("开盘", "open"):
                    col_map[c] = "open"
                elif c in ("最高", "high"):
                    col_map[c] = "high"
                elif c in ("最低", "low"):
                    col_map[c] = "low"
                elif c in ("收盘", "close"):
                    col_map[c] = "close"
                elif c in ("成交量", "volume"):
                    col_map[c] = "volume"
            df = df.rename(columns=col_map)
            if "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"])
                df = df.set_index("date").sort_index()
            df.to_csv(cache_file, encoding="utf-8")
            return df
    except Exception:
        pass

    if os.path.exists(cache_file):
        return pd.read_csv(cache_file, index_col=0, parse_dates=True)
    return pd.DataFrame()


# === 股票→行业映射 ===

def _build_industry_code_map() -> dict:
    """构建 industry_code前2位 → 申万一级行业名称 的映射"""
    try:
        tree = ak.stock_industry_category_cninfo(symbol="申银万国行业分类标准")
        if tree is None or tree.empty:
            return {}
    except Exception:
        return {}

    level1 = tree[tree.iloc[:, -1] == 1]
    code_map = {}
    for _, row in level1.iterrows():
        vals = list(row)
        tree_code = str(vals[0])
        name = str(vals[1])
        if tree_code.startswith("S") and len(tree_code) >= 3:
            code_map[tree_code[1:3]] = name
    return code_map


def fetch_stock_industry_map(ttl_days: int = 30) -> dict:
    """获取 股票代码 → 申万一级行业名称 的映射。缓存30天。

    数据源: stock_industry_clf_hist_sw (申万研究所官方Excel)
    每只股票取最新一条分类记录。
    swsresearch.com 的 SSL 证书过期，临时绕过验证（仅此函数）。
    """
    cache_file = _cache_path(MARKET_DIR, "stock_industry_map", ".json")
    if _cache_valid(cache_file, ttl_days):
        with open(cache_file, "r", encoding="utf-8") as f:
            return json.load(f)

    code_name_map = _build_industry_code_map()

    with _insecure_ssl():
        try:
            df = ak.stock_industry_clf_hist_sw()
        except Exception:
            if os.path.exists(cache_file):
                with open(cache_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            return {}

    if df is None or df.empty:
        return {}

    df = df.sort_values("update_time", ascending=False)
    df = df.drop_duplicates(subset="symbol", keep="first")

    result = {}
    for _, row in df.iterrows():
        code = str(row["symbol"]).zfill(6)
        industry_code = str(row["industry_code"])
        level1_name = code_name_map.get(industry_code[:2])
        if level1_name:
            result[code] = level1_name

    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)

    return result
