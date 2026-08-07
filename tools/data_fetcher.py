"""数据获取 — akshare 封装 + 本地缓存"""

import os
import json
from datetime import datetime, date, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import akshare as ak

# 强行为所有 requests 调用设超时（15秒），防止 akshare 底层无限挂起
# socket.setdefaulttimeout 只影响 TCP 连接阶段，不影响 HTTP 读超时
import requests as _requests
_original_request = _requests.Session.request
def _patched_request(self, method, url, **kwargs):
    if "timeout" not in kwargs:
        kwargs["timeout"] = 15
    return _original_request(self, method, url, **kwargs)
_requests.Session.request = _patched_request

BASE_DIR = Path(__file__).parent.parent
CACHE_DIR = BASE_DIR / "data"
KLINE_DIR = CACHE_DIR / "daily_kline"
FIN_DIR = CACHE_DIR / "financials"
MARKET_DIR = CACHE_DIR / "market"
TRADE_CALENDAR_FILE = MARKET_DIR / "trade_calendar.csv"
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
MARKET_DATA_CUTOFF = time(16, 30)


def _cache_path(subdir: Path, key: str, suffix: str = ".csv") -> str:
    subdir.mkdir(parents=True, exist_ok=True)
    # key 中的 / 替换为 _
    safe_key = key.replace("/", "_").replace("\\", "_")
    return str(subdir / f"{safe_key}{suffix}")


def _cache_valid(filepath: str, ttl_days: int) -> bool:
    if not os.path.exists(filepath):
        return False
    mtime = datetime.fromtimestamp(os.path.getmtime(filepath))
    return (datetime.now() - mtime).days < ttl_days


def _normalize_trade_dates(values) -> list[str]:
    normalized = []
    for value in values:
        try:
            normalized.append(pd.Timestamp(value).date().isoformat())
        except (TypeError, ValueError):
            continue
    return sorted(set(normalized))


def resolve_expected_trade_date(trade_dates: list[str], now: datetime) -> dict:
    """用已覆盖的交易日历推导本次日更应看到的最新完整交易日。"""
    local_now = now.astimezone(SHANGHAI_TZ) if now.tzinfo else now.replace(tzinfo=SHANGHAI_TZ)
    dates = [date.fromisoformat(value) for value in _normalize_trade_dates(trade_dates)]
    today = local_now.date()
    previous = max((value for value in dates if value < today), default=None)
    calendar_max = max(dates, default=None)

    result = {
        "status": "unknown",
        "expected_date": "",
        "calendar_max": calendar_max.isoformat() if calendar_max else "",
        "reason": "交易日历为空",
    }
    if not dates:
        return result

    if today.weekday() >= 5:
        if previous:
            result.update(status="ready", expected_date=previous.isoformat(), reason="周末")
        return result

    if local_now.time() < MARKET_DATA_CUTOFF:
        if previous:
            result.update(status="ready", expected_date=previous.isoformat(), reason="收盘数据截止时间前")
        return result

    if today in dates:
        result.update(status="ready", expected_date=today.isoformat(), reason="当日交易日已覆盖")
        return result

    if calendar_max and calendar_max > today and previous:
        result.update(status="ready", expected_date=previous.isoformat(), reason="日历证明当日休市")
        return result

    result["reason"] = "工作日日历覆盖不足"
    return result


def get_expected_trade_date(
        now: datetime | None = None,
        cache_file: str | Path = TRADE_CALENDAR_FILE,
        fetcher=None) -> dict:
    """刷新交易日历并返回期望交易日；网络失败时仅使用覆盖充分的缓存。"""
    now = now or datetime.now(SHANGHAI_TZ)
    cache_path = Path(cache_file)
    fetcher = fetcher or ak.tool_trade_date_hist_sina
    source = "none"
    trade_dates = []

    try:
        raw = fetcher()
        if raw is None or raw.empty:
            raise ValueError("交易日历为空")
        column = "trade_date" if "trade_date" in raw.columns else raw.columns[0]
        trade_dates = _normalize_trade_dates(raw[column].tolist())
        if not trade_dates:
            raise ValueError("交易日历无有效日期")
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
        pd.DataFrame({"trade_date": trade_dates}).to_csv(tmp_path, index=False, encoding="utf-8")
        os.replace(tmp_path, cache_path)
        source = "network"
    except Exception:
        if cache_path.exists():
            try:
                cached = pd.read_csv(cache_path, dtype=str)
                column = "trade_date" if "trade_date" in cached.columns else cached.columns[0]
                trade_dates = _normalize_trade_dates(cached[column].tolist())
                source = "cache"
            except Exception:
                trade_dates = []

    result = resolve_expected_trade_date(trade_dates, now)
    result["source"] = source
    result["checked_at"] = now.astimezone(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M") if now.tzinfo else now.strftime("%Y-%m-%d %H:%M")
    return result


# === 日K线 ===

def _to_symbol(code: str) -> str:
    """000001 -> sz000001, 600519 -> sh600519"""
    code = str(code).zfill(6)
    if code.startswith(("0", "3")):
        return "sz" + code
    return "sh" + code


def fetch_daily_kline(code: str, start_date: str = "20100101",
                      end_date: str | None = None, ttl_days: int = 1) -> pd.DataFrame:
    """获取A股日K线，自动缓存。优先使用腾讯数据源，回退到东方财富"""
    if end_date is None:
        end_date = date.today().strftime("%Y%m%d")

    cache_file = _cache_path(KLINE_DIR, f"{code}_{end_date}")
    if _cache_valid(cache_file, ttl_days):
        return pd.read_csv(cache_file, index_col=0, parse_dates=True)

    symbol = _to_symbol(code)
    df = pd.DataFrame()

    # 主数据源：stock_zh_a_daily（腾讯，不易被封）
    try:
        df = ak.stock_zh_a_daily(symbol=symbol, start_date=start_date, end_date=end_date)
        if df is not None and not df.empty:
            df = df.rename(columns={
                "date": "日期", "open": "开盘", "high": "最高",
                "low": "最低", "close": "收盘", "volume": "成交量",
            })
            df["日期"] = pd.to_datetime(df["日期"])
            df = df.set_index("日期").sort_index()
            df.to_csv(cache_file, encoding="utf-8")
            return df
    except Exception:
        pass

    # 回退：stock_zh_a_hist（东方财富）
    try:
        df = ak.stock_zh_a_hist(symbol=code, period="daily",
                                start_date=start_date, end_date=end_date, adjust="qfq")
        if df is not None and not df.empty:
            df.columns = [c.lower() for c in df.columns]
            df["日期"] = pd.to_datetime(df["日期"])
            df = df.set_index("日期").sort_index()
            df.to_csv(cache_file, encoding="utf-8")
            return df
    except Exception:
        pass

    # 降级到缓存
    if os.path.exists(cache_file):
        return pd.read_csv(cache_file, index_col=0, parse_dates=True)
    print(f"[data_fetcher] 获取 {code} K线失败（所有数据源）")
    return pd.DataFrame()


def fetch_current_price(code: str) -> float | None:
    """获取个股最新价（从日K线缓存取收盘价，避免全市场下载）"""
    kline = fetch_daily_kline(code)
    if kline.empty:
        return None
    return float(kline["收盘"].iloc[-1])


def fetch_all_spot() -> pd.DataFrame:
    """获取全市场实时行情，用于筛选排序"""
    cache_file = _cache_path(MARKET_DIR, "all_spot")
    if _cache_valid(cache_file, ttl_days=1):
        return pd.read_csv(cache_file, dtype={"代码": str})

    try:
        df = ak.stock_zh_a_spot_em()
        df.to_csv(cache_file, index=False, encoding="utf-8")
        return df
    except Exception as e:
        if os.path.exists(cache_file):
            return pd.read_csv(cache_file, dtype={"代码": str})
        print(f"[data_fetcher] 获取全市场行情失败: {e}")
    return pd.DataFrame()


# === 财务数据（同花顺THS） ===

def _parse_pct(val) -> float | None:
    """'41.17%' -> 0.4117, '145.23亿' -> 14523000000"""
    if val is None or val == "" or val is False:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if s.endswith("%"):
        try:
            return float(s[:-1]) / 100.0
        except ValueError:
            return None
    if s.endswith("亿"):
        try:
            return float(s[:-1]) * 1e8
        except ValueError:
            return None
    if s.endswith("万"):
        try:
            return float(s[:-1]) * 1e4
        except ValueError:
            return None
    try:
        return float(s)
    except ValueError:
        return None


def fetch_financial_data(code: str, ttl_days: int = 30) -> dict:
    """使用同花顺摘要获取关键财务指标，缓存30天"""
    cache_file = _cache_path(FIN_DIR, f"{code}_ths", ".json")
    if _cache_valid(cache_file, ttl_days):
        with open(cache_file, "r", encoding="utf-8") as f:
            return json.load(f)

    result = {}
    try:
        df = ak.stock_financial_abstract_ths(symbol=code, indicator="按报告期")
        if df is None or df.empty:
            return result

        # 取最新年报（12-31行），ROE/EPS/CF等损益指标用年度数据才有可比性
        df["_rpt_str"] = df["报告期"].astype(str)
        annual_mask = df["_rpt_str"].str.endswith("12-31")
        annual = df[annual_mask]
        if not annual.empty:
            anne = annual.iloc[-1]  # 最新年报
        else:
            anne = df.iloc[-1]  # 降级

        latest = df.iloc[-1]  # 最新一期（资产负债表项目用）

        # ROE（年度）
        roe = _parse_pct(anne.get("净资产收益率"))
        if roe is not None:
            result["roe"] = roe

        # 资产负债率（时点指标，取最新）
        debt = _parse_pct(latest.get("资产负债率"))
        if debt is not None:
            result["debt_ratio"] = debt

        # 净利润（年度）
        net_profit = _parse_pct(anne.get("净利润"))
        if net_profit is not None:
            result["net_profit"] = net_profit

        # 营收同比增长（最新一期，捕捉拐点趋势）
        rev_yoy = _parse_pct(latest.get("营业总收入同比增长率"))
        if rev_yoy is not None:
            result["revenue_yoy"] = rev_yoy

        # 利润同比增长（最新一期，捕捉拐点趋势）
        ni_yoy = _parse_pct(latest.get("净利润同比增长率"))
        if ni_yoy is not None:
            result["profit_yoy"] = ni_yoy

        # 扣非利润同比增长（最新一期）
        deducted_yoy = _parse_pct(latest.get("扣非净利润同比增长率"))
        if deducted_yoy is not None:
            result["deducted_profit_yoy"] = deducted_yoy

        # 每股经营现金流（年度累计）
        ocf_ps = _parse_pct(anne.get("每股经营现金流"))
        if ocf_ps is not None:
            result["ocf_per_share"] = ocf_ps

        # 每股净资产（时点，取最新）
        bv_ps = _parse_pct(latest.get("每股净资产"))
        if bv_ps is not None:
            result["book_value_per_share"] = bv_ps

        # 基本每股收益（年度）
        eps = _parse_pct(anne.get("基本每股收益"))
        if eps is not None:
            result["eps"] = eps

        # 销售净利率（年度）
        npm = _parse_pct(anne.get("销售净利率"))
        if npm is not None:
            result["net_profit_margin"] = npm

        # 报告期：混合口径 — 累计指标用年报，趋势用最新期
        result["report_date"] = str(anne.get("报告期", ""))
        result["yoy_period"] = str(latest.get("报告期", ""))

        if result:
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False)

    except Exception as e:
        if os.path.exists(cache_file):
            with open(cache_file, "r", encoding="utf-8") as f:
                return json.load(f)
        print(f"[data_fetcher] THS {code} 财务数据失败: {e}")

    return result


# === 以下保留旧函数签名以兼容 ===

def fetch_financial_summary(code: str, ttl_days: int = 30) -> dict:
    return fetch_financial_data(code, ttl_days)


def fetch_financial_indicators(code: str, ttl_days: int = 30) -> dict:
    return fetch_financial_data(code, ttl_days)


# === 市场水位数据 ===

def fetch_margin_balance(ttl_days: int = 1) -> dict:
    """获取两融余额"""
    cache_file = _cache_path(MARKET_DIR, "margin", ".json")
    if _cache_valid(cache_file, ttl_days):
        with open(cache_file, "r", encoding="utf-8") as f:
            return json.load(f)

    result = {}
    try:
        margin_total = 0
        short_total = 0
        last_date = ""

        # 沪市
        df_sh = ak.macro_china_market_margin_sh()
        if df_sh is not None and not df_sh.empty:
            last = df_sh.iloc[-1]
            last_date = str(last.get("日期", ""))
            margin_total += float(last.get("融资余额", 0))
            short_total += float(last.get("融券余额", 0))

        # 深市
        df_sz = ak.macro_china_market_margin_sz()
        if df_sz is not None and not df_sz.empty:
            last = df_sz.iloc[-1]
            if not last_date:
                last_date = str(last.get("日期", ""))
            margin_total += float(last.get("融资余额", 0))
            short_total += float(last.get("融券余额", 0))

        if margin_total > 0:
            result = {
                "date": last_date,
                "margin_balance": round(margin_total, 2),
                "short_balance": round(short_total, 2),
            }
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False)
    except Exception as e:
        if os.path.exists(cache_file):
            with open(cache_file, "r", encoding="utf-8") as f:
                return json.load(f)
        print(f"[data_fetcher] 获取两融数据失败: {e}")

    return result


def fetch_index_pe(ttl_days: int = 1) -> dict:
    """获取主要指数PE，用于算ERP"""
    cache_file = _cache_path(MARKET_DIR, "index_pe", ".json")
    if _cache_valid(cache_file, ttl_days):
        with open(cache_file, "r", encoding="utf-8") as f:
            return json.load(f)

    result = {}
    try:
        # 沪深300 PE
        df300 = ak.stock_zh_index_value_csindex(symbol="000300")
        if df300 is not None and not df300.empty:
            last = df300.iloc[-1]
            result["hs300_pe"] = float(last.get("市盈率1", 0))

        # 中证500 PE
        df500 = ak.stock_zh_index_value_csindex(symbol="000905")
        if df500 is not None and not df500.empty:
            last = df500.iloc[-1]
            result["zz500_pe"] = float(last.get("市盈率1", 0))

        if result:
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False)
    except Exception as e:
        if os.path.exists(cache_file):
            with open(cache_file, "r", encoding="utf-8") as f:
                return json.load(f)
        print(f"[data_fetcher] 获取指数PE失败: {e}")

    return result


def fetch_bond_yield(ttl_days: int = 1) -> float:
    """获取中国10年期国债收益率"""
    cache_file = _cache_path(MARKET_DIR, "bond_yield", ".json")
    if _cache_valid(cache_file, ttl_days):
        with open(cache_file, "r", encoding="utf-8") as f:
            return json.load(f).get("yield_10y", 0.025)

    result = 0.025  # 默认2.5%
    try:
        df = ak.bond_china_yield()
        if df is not None and not df.empty:
            # 中债国债收益率曲线 的 '10年' 列
            gov_row = df[df["曲线名称"] == "中债国债收益率曲线"]
            if not gov_row.empty:
                val = gov_row.iloc[0]["10年"]
                result = float(val) / 100.0
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump({"yield_10y": result}, f)
    except Exception as e:
        print(f"[data_fetcher] 获取国债收益率失败: {e}")

    return result


def calculate_erp() -> float:
    """计算沪深300风险溢价 ERP = 1/PE - 10年期国债收益率"""
    pe_data = fetch_index_pe()
    bond_yield = fetch_bond_yield()

    hs300_pe = pe_data.get("hs300_pe", 0)
    if hs300_pe <= 0:
        return 0.0

    earnings_yield = 1.0 / hs300_pe
    erp = earnings_yield - bond_yield
    return erp


def _save_erp_history(erp: float):
    """追加当日ERP到历史文件"""
    erp_file = MARKET_DIR / "erp_history.csv"
    today_str = date.today().isoformat()
    new_row = pd.DataFrame([{"date": today_str, "erp": round(erp, 4)}])
    if erp_file.exists():
        df = pd.read_csv(erp_file)
        # 今天已有则跳过
        if today_str in df["date"].values:
            return
        df = pd.concat([df, new_row], ignore_index=True)
    else:
        df = new_row
    df.to_csv(erp_file, index=False, encoding="utf-8")
    global _erp_history_cache
    _erp_history_cache = None  # 清除缓存，下次重新加载


_erp_history_cache = None


def get_erp_position_cap(erp: float | None = None) -> dict:
    """根据ERP水平返回建议仓位上限。
    优先用分位（历史>60天），历史不足时用阈值启发。
    返回 {pct, level, method, cap}"""
    if erp is None:
        erp = calculate_erp()
    if erp <= 0:
        return {"pct": 50, "level": "数据不可用", "method": "fallback", "cap": 0.30}

    erp_file = MARKET_DIR / "erp_history.csv"
    global _erp_history_cache
    if _erp_history_cache is None and erp_file.exists():
        try:
            df = pd.read_csv(erp_file)
            if len(df) >= 60:
                _erp_history_cache = list(df["erp"])
        except Exception:
            pass

    if _erp_history_cache and len(_erp_history_cache) >= 60:
        # 分位法
        pct = sum(1 for v in _erp_history_cache if v < erp) / len(_erp_history_cache) * 100
        method = "percentile"
    else:
        # 阈值启发（A股经验值：ERP均值~4%，>6%极度便宜，<3%极度贵）
        if erp >= 0.06:
            pct = 90
        elif erp >= 0.05:
            pct = 70
        elif erp >= 0.04:
            pct = 50
        elif erp >= 0.03:
            pct = 30
        else:
            pct = 10
        method = "heuristic"

    # 仓位上限映射
    if pct >= 80:
        cap, level = 0.80, "极度便宜"
    elif pct >= 50:
        cap, level = 0.50, "偏便宜"
    elif pct >= 20:
        cap, level = 0.30, "偏贵"
    else:
        cap, level = 0.20, "极度贵"

    return {"pct": round(pct, 1), "level": level, "method": method, "cap": cap}


def fetch_market_water_level() -> dict:
    """获取综合市场水位"""
    margin = fetch_margin_balance()
    index_pe = fetch_index_pe()
    bond_yield = fetch_bond_yield()
    erp = calculate_erp()

    # 保存ERP历史，用于计算分位
    if erp > 0:
        try:
            _save_erp_history(erp)
        except Exception:
            pass

    return {
        "date": date.today().isoformat(),
        "erp": round(erp, 4),
        "hs300_pe": index_pe.get("hs300_pe", 0),
        "zz500_pe": index_pe.get("zz500_pe", 0),
        "bond_10y": round(bond_yield, 4),
        "margin_balance": margin.get("margin_balance", 0),
    }


# === 股票池 ===

def _validated_hs300_universe(df: pd.DataFrame | None) -> pd.DataFrame | None:
    """返回规范化的沪深300成分表；来源、列或数量不可信时返回 None。"""
    required = {"code", "name", "index"}
    if df is None or df.empty or not required.issubset(df.columns):
        return None
    normalized = df[["code", "name", "index"]].copy()
    normalized["code"] = normalized["code"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True).str.zfill(6)
    normalized["name"] = normalized["name"].astype(str).str.strip()
    normalized["index"] = normalized["index"].astype(str).str.strip()
    normalized = normalized.drop_duplicates(subset=["code"]).reset_index(drop=True)
    if not 250 <= len(normalized) <= 350:
        return None
    if set(normalized["index"]) != {"沪深300"}:
        return None
    if not normalized["code"].str.fullmatch(r"\d{6}").all():
        return None
    return normalized


def _normalize_hs300_provider_frame(df: pd.DataFrame | None) -> pd.DataFrame | None:
    if df is None or df.empty:
        return None
    if {"code", "name"}.issubset(df.columns):
        code_col, name_col = "code", "name"
    elif len(df.columns) >= 6:
        code_col, name_col = df.columns[4], df.columns[5]
    else:
        return None
    normalized = pd.DataFrame({
        "code": df[code_col],
        "name": df[name_col],
        "index": "沪深300",
    })
    return _validated_hs300_universe(normalized)


def fetch_stock_universe(
        cache_file: str | Path | None = None, fetcher=None, ttl_days: int = 7) -> pd.DataFrame:
    """获取并验证沪深300股票池；失败时只使用最后有效的沪深300缓存。"""
    cache_path = Path(cache_file) if cache_file is not None else Path(
        _cache_path(MARKET_DIR, "stock_universe")
    )
    fetcher = fetcher or ak.index_stock_cons_csindex
    cached = None
    if cache_path.exists():
        try:
            cached = _validated_hs300_universe(pd.read_csv(cache_path, dtype={"code": str}))
        except Exception:
            cached = None
    if cached is not None and _cache_valid(str(cache_path), ttl_days=ttl_days):
        return cached

    try:
        fresh = _normalize_hs300_provider_frame(fetcher(symbol="000300"))
        if fresh is None:
            raise ValueError("沪深300成分数据未通过数量/来源校验")
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
        fresh.to_csv(tmp_path, index=False, encoding="utf-8")
        os.replace(tmp_path, cache_path)
        print(f"[data_fetcher] 股票池: {len(fresh)} 只 (沪深300，网络)")
        return fresh
    except Exception as exc:
        if cached is not None:
            print(f"[data_fetcher] 成分股刷新失败，使用有效缓存: {exc}")
            return cached
        print(f"[data_fetcher] 沪深300股票池不可用: {exc}")
        return pd.DataFrame(columns=["code", "name", "index"])


def fetch_stock_quick_snapshot(code: str) -> dict | None:
    """快速获取单只股票的K线快照指标，不拉完整财务数据"""
    kline = fetch_daily_kline(code)
    if kline.empty or len(kline) < 250:
        return None

    close = kline["收盘"]
    volume = kline["成交量"]
    high = kline["最高"]
    low = kline["最低"]

    current_price = float(close.iloc[-1])
    high_52w = float(high.tail(250).max())
    low_52w = float(low.tail(250).min())
    drawdown_52w = (high_52w - current_price) / high_52w

    # 成交量萎缩
    vol_ma20 = volume.rolling(20).mean()
    vol_ratio = (float(volume.iloc[-1]) / float(vol_ma20.iloc[-1])
                 if vol_ma20.iloc[-1] > 0 else 1.0)

    # 连续缩量天数
    shrink_days = 0
    window = min(len(volume), 30)
    for i in range(1, window + 1):
        idx = -i
        ma20_val = float(vol_ma20.iloc[idx]) if idx >= -len(vol_ma20) else float(vol_ma20.iloc[-1])
        cur_vol = float(volume.iloc[idx])
        ratio = cur_vol / ma20_val if ma20_val > 0 else 1.0
        if ratio < 0.5:
            shrink_days += 1
        else:
            break

    # 价格分位（1年内）
    price_pct = float((close.tail(250) <= current_price).mean())

    return {
        "price": current_price,
        "high_52w": high_52w,
        "drawdown_52w": drawdown_52w,
        "vol_ratio": vol_ratio,
        "shrink_days": shrink_days,
        "price_percentile_1y": price_pct,
    }


def fetch_price_percentile(code: str, years: int = 5) -> float | None:
    """当前价格在N年历史中的分位 (0~1)，作为PE估值分位的近似代理"""
    kline = fetch_daily_kline(code)
    if kline.empty:
        return None
    close = kline["收盘"]
    n_days = years * 250
    if len(close) < min(250, n_days):
        return None
    recent = close.iloc[-min(len(close), n_days):]
    current = float(recent.iloc[-1])
    return float((recent <= current).mean())
