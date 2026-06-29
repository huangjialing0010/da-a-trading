"""行业逻辑分析 — 选股前过滤结构性衰退行业、加分结构性支撑行业。

衰退行业用代码黑名单（config.structural_decline_codes），不依赖API。
支撑行业用名称关键词匹配（config.structural_support_kw）。
"""

from pathlib import Path
import yaml

BASE_DIR = Path(__file__).parent.parent
CONFIG_PATH = BASE_DIR / "config.yaml"

_config_cache = None


def _load_config() -> dict:
    global _config_cache
    if _config_cache is None:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            _config_cache = yaml.safe_load(f)
    return _config_cache


def is_enabled() -> bool:
    return _load_config().get("industry_filter", {}).get("enabled", True)


def _get_cfg() -> dict:
    return _load_config().get("industry_filter", {})


def classify_stock(code: str, name: str = "") -> str:
    """
    返回: 'decline' | 'support' | 'neutral'
    """
    if not is_enabled():
        return "neutral"

    cfg = _get_cfg()

    # 1. 代码黑名单（结构性衰退行业，直接淘汰）
    blacklist = [str(c).zfill(6) for c in cfg.get("structural_decline_codes", [])]
    if str(code).zfill(6) in blacklist:
        return "decline"

    # 2. 名称关键词（结构性支撑行业，加分）
    support_kw = cfg.get("structural_support_kw", [])
    for kw in support_kw:
        if kw and kw in name:
            return "support"

    return "neutral"


def get_sector_score(code: str, name: str = "") -> int:
    state = classify_stock(code, name)
    if state == "decline":
        return -999
    if state == "support":
        return 5
    return 0
