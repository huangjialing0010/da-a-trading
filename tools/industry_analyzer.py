"""行业逻辑分析 — 双层体系：手工规则（最高优先）+ 申万一级行业量化评分。

Layer 1: 手工覆盖 — structural_decline_codes 黑名单 + structural_support_kw 关键词
Layer 2: 数据驱动 — 申万一级行业 PE分位/价格分位/近期表现 → 健康分 0-100

保留所有现有接口签名不变。
"""

from pathlib import Path
import yaml

import pandas as pd

BASE_DIR = Path(__file__).parent.parent
CONFIG_PATH = BASE_DIR / "config.yaml"

_config_cache = None

# 31个申万一级行业 → index_hist_sw 指数代码映射
# 指数代码来自 sw_index_first_info() 的行业代码列（去掉.SI后缀）
LEVEL1_INDEX_CODES = {
    "农林牧渔": "801010",
    "基础化工": "801030",
    "钢铁": "801040",
    "有色金属": "801050",
    "电子": "801080",
    "汽车": "801880",
    "家用电器": "801110",
    "食品饮料": "801120",
    "纺织服饰": "801130",
    "轻工制造": "801140",
    "医药生物": "801150",
    "公用事业": "801160",
    "交通运输": "801170",
    "房地产": "801180",
    "商贸零售": "801200",
    "社会服务": "801210",
    "银行": "801780",
    "非银金融": "801790",
    "综合": "801230",
    "建筑材料": "801710",
    "建筑装饰": "801720",
    "电力设备": "801730",
    "机械设备": "801890",
    "国防军工": "801740",
    "计算机": "801750",
    "传媒": "801760",
    "通信": "801770",
    "煤炭": "801950",
    "石油石化": "801960",
    "环保": "801970",
    "美容护理": "801980",
}

# 模块级缓存
_stock_industry_cache: dict | None = None
_level1_industries_cache: pd.DataFrame | None = None
_industry_scores_cache: dict | None = None


def _load_config() -> dict:
    global _config_cache
    if _config_cache is None:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            _config_cache = yaml.safe_load(f)
    return _config_cache


def _get_ia_cfg() -> dict:
    """获取 industry_analysis 配置节"""
    return _load_config().get("industry_analysis", {})


def _get_if_cfg() -> dict:
    """获取 industry_filter 配置节"""
    return _load_config().get("industry_filter", {})


def is_enabled() -> bool:
    return _get_if_cfg().get("enabled", True)


# === Layer 1: 手工规则（现有逻辑，不变） ===

def _manual_classify(code: str, name: str = "") -> str:
    """手工规则判断，返回 'decline' | 'support' | 'neutral'"""
    cfg = _get_if_cfg()

    # 代码黑名单
    blacklist = [str(c).zfill(6) for c in cfg.get("structural_decline_codes", [])]
    if str(code).zfill(6) in blacklist:
        return "decline"

    # 名称关键词
    support_kw = cfg.get("structural_support_kw", [])
    for kw in support_kw:
        if kw and kw in name:
            return "support"

    return "neutral"


# === Layer 2: 数据驱动（新增） ===

def _load_stock_industry_map() -> dict:
    """懒加载股票→行业映射"""
    global _stock_industry_cache
    if _stock_industry_cache is None:
        from .industry_data import fetch_stock_industry_map
        cfg = _get_ia_cfg()
        ttl = cfg.get("classification_ttl_days", 30)
        _stock_industry_cache = fetch_stock_industry_map(ttl_days=ttl)
    return _stock_industry_cache


def _load_level1_industries() -> pd.DataFrame:
    """懒加载31个一级行业PE/PB数据"""
    global _level1_industries_cache
    if _level1_industries_cache is None:
        from .industry_data import fetch_level1_industries
        _level1_industries_cache = fetch_level1_industries()
    return _level1_industries_cache


def get_stock_industry(code: str) -> dict | None:
    """返回某只股票的申万一级行业信息，无数据返回None"""
    stock_map = _load_stock_industry_map()
    level1_name = stock_map.get(str(code).zfill(6))
    if not level1_name:
        return None

    idx_code = LEVEL1_INDEX_CODES.get(level1_name)

    df = _load_level1_industries()
    pe_static = None
    pe_ttm = None
    pb = None
    if not df.empty and idx_code:
        match = df[df.iloc[:, 0].str.replace(".SI", "") == idx_code]
        if not match.empty:
            row = match.iloc[0]
            cols = list(df.columns)
            pe_static = float(row[cols[3]]) if len(cols) > 3 else None
            pe_ttm = float(row[cols[4]]) if len(cols) > 4 else None
            pb = float(row[cols[5]]) if len(cols) > 5 else None

    return {
        "level1_name": level1_name,
        "level1_code": idx_code,
        "pe_static": pe_static,
        "pe_ttm": pe_ttm,
        "pb": pb,
    }


def _calc_price_percentile(code: str) -> float | None:
    """计算申万一级行业指数当前价格在5年历史中的分位 (0=最低, 1=最高)"""
    from .industry_data import fetch_level1_history
    cfg = _get_ia_cfg()
    years = cfg.get("pe_lookback_years", 5)
    start = f"{pd.Timestamp.now().year - years}0101"

    hist = fetch_level1_history(code, start_date=start)
    if hist.empty or "close" not in hist.columns:
        return None

    close = hist["close"]
    current = close.iloc[-1]
    low = close.min()
    high = close.max()
    if high == low:
        return 0.5
    return (current - low) / (high - low)


def _calc_recent_performance(code: str) -> float | None:
    """计算20日涨跌幅"""
    from .industry_data import fetch_level1_history
    hist = fetch_level1_history(code)
    if hist.empty or "close" not in hist.columns or len(hist) < 21:
        return None

    close = hist["close"]
    return (close.iloc[-1] / close.iloc[-21] - 1)


def get_industry_health(level1_name: str) -> dict:
    """计算单个申万一级行业的健康度评分 0-100

    评分逻辑：低PE + 低价格分位 + 近期跌幅大 = 高分（价值投资者的视角）
    """
    idx_code = LEVEL1_INDEX_CODES.get(level1_name)
    if not idx_code:
        return {"score": 50, "level1_name": level1_name, "error": "无指数代码"}

    cfg = _get_ia_cfg()
    weights = cfg.get("score_weights", {
        "pe_percentile": 0.50, "recent_performance": 0.30, "price_percentile": 0.20,
    })

    df = _load_level1_industries()
    score = 50  # 默认中性
    pe_pct = None
    price_pct = None
    perf_20d = None
    pe_static = None
    pe_ttm = None
    pb = None

    # --- PE 分位（横截面对比，50%权重）---
    if not df.empty:
        cols = list(df.columns)
        pe_col_idx = 3  # 静态市盈率
        valid_pes = []
        current_pe = None

        for _, row in df.iterrows():
            vals = list(row)
            pe_val = float(vals[pe_col_idx]) if len(vals) > pe_col_idx and vals[pe_col_idx] else None
            if pe_val and pe_val > 0:
                name = str(vals[1])
                pe_code = str(vals[0]).replace(".SI", "")
                valid_pes.append(pe_val)
                if name == level1_name or pe_code == idx_code:
                    current_pe = pe_val

        if current_pe is not None and valid_pes:
            pe_static = current_pe
            pe_pct = sum(1 for p in valid_pes if p < current_pe) / len(valid_pes)
            score_pe = (1 - pe_pct) * 100  # PE越低分越高
        else:
            score_pe = 50
    else:
        score_pe = 50

    # --- 价格分位（5年历史，20%权重）---
    price_pct = _calc_price_percentile(idx_code)
    if price_pct is not None:
        score_price = (1 - price_pct) * 100
    else:
        score_price = 50

    # --- 近期表现（20日，30%权重）---
    perf_20d = _calc_recent_performance(idx_code)
    if perf_20d is not None:
        score_perf = max(0, min(100, 50 - perf_20d * 200))
    else:
        score_perf = 50

    score = (
        score_pe * weights.get("pe_percentile", 0.50)
        + score_perf * weights.get("recent_performance", 0.30)
        + score_price * weights.get("price_percentile", 0.20)
    )

    # 补充PE/PB
    if not df.empty:
        match = df[df.iloc[:, 0].str.replace(".SI", "") == idx_code]
        if not match.empty:
            row = match.iloc[0]
            cols_list = list(df.columns)
            pe_static = float(row[cols_list[3]]) if pe_static is None and len(cols_list) > 3 else pe_static
            pe_ttm = float(row[cols_list[4]]) if len(cols_list) > 4 else None
            pb = float(row[cols_list[5]]) if len(cols_list) > 5 else None

    return {
        "level1_name": level1_name,
        "score": round(score, 1),
        "pe_static": pe_static,
        "pe_ttm": pe_ttm,
        "pb": pb,
        "pe_pct": round(pe_pct, 3) if pe_pct is not None else None,
        "price_pct": round(price_pct, 3) if price_pct is not None else None,
        "perf_20d": round(perf_20d, 4) if perf_20d is not None else None,
    }


def get_all_industry_scores() -> list[dict]:
    """获取所有31个申万一级行业的健康分排名"""
    global _industry_scores_cache
    if _industry_scores_cache is not None:
        return sorted(_industry_scores_cache.values(), key=lambda x: x["score"], reverse=True)

    results = {}
    for name, idx_code in LEVEL1_INDEX_CODES.items():
        results[name] = get_industry_health(name)

    _industry_scores_cache = results
    return sorted(results.values(), key=lambda x: x["score"], reverse=True)


def get_industry_distribution(codes: list[str]) -> dict:
    """给定股票代码列表，返回申万一级行业分布"""
    stock_map = _load_stock_industry_map()
    dist = {}
    for code in codes:
        name = stock_map.get(str(code).zfill(6))
        if name:
            dist[name] = dist.get(name, 0) + 1
    return dict(sorted(dist.items(), key=lambda x: x[1], reverse=True))


def _data_driven_classify(code: str, name: str = "") -> str:
    """数据驱动行业分类，仅在手工规则返回neutral后调用"""
    cfg = _get_ia_cfg()
    if not cfg.get("enabled", True):
        return "neutral"

    stock_map = _load_stock_industry_map()
    level1_name = stock_map.get(str(code).zfill(6))
    if not level1_name:
        return "neutral"

    # 获取全行业排名，找当前行业的分
    scores = get_all_industry_scores()
    current_score = None
    for s in scores:
        if s["level1_name"] == level1_name:
            current_score = s["score"]
            break

    if current_score is None:
        return "neutral"

    decline_threshold = cfg.get("decline_threshold", 25)
    support_threshold = cfg.get("support_threshold", 70)

    if current_score < decline_threshold:
        return "decline"
    if current_score > support_threshold:
        return "support"
    return "neutral"


# === 公开接口（保持向后兼容） ===

def classify_stock(code: str, name: str = "") -> str:
    """返回: 'decline' | 'support' | 'neutral'

    Layer 1: 手工规则（黑名单/关键词），最高优先级
    Layer 2: 数据驱动（申万行业健康分），仅在Layer1返回neutral时生效
    """
    if not is_enabled():
        return "neutral"

    manual_result = _manual_classify(code, name)
    if manual_result != "neutral":
        return manual_result

    return _data_driven_classify(code, name)


def get_sector_score(code: str, name: str = "") -> int:
    """返回行业评分加成。手工support固定+5，数据驱动support按健康分加成"""
    state = classify_stock(code, name)
    if state == "decline":
        return -999
    if state == "support":
        # 如果是数据驱动判定为support，按健康分额外加成
        manual_result = _manual_classify(code, name)
        if manual_result == "support":
            return 5  # 手工关键词匹配，固定+5
        # 数据驱动support，按健康分加成
        stock_map = _load_stock_industry_map()
        level1_name = stock_map.get(str(code).zfill(6))
        if level1_name:
            health = get_industry_health(level1_name)
            bonus = max(3, min(8, int(health["score"] / 20)))
            return bonus
        return 3
    return 0
