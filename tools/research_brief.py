"""研究数据简报：为 output/research/ 7 维度研究提供唯一数字来源。

所有数字来自本地缓存与既有数据模块，缺数据时显式声明 MISSING，
绝不编造、绝不使用模型记忆填充。用法：

    python tools/research_brief.py 601600 [--out FILE]
"""
import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.commodity_fetcher import check_commodity_cycle  # noqa: E402
from tools.data_fetcher import fetch_financial_data  # noqa: E402

KL_DIR = ROOT / "data" / "daily_kline"
UNIVERSE_FILE = ROOT / "data" / "market" / "stock_universe.csv"
INDUSTRY_FILE = ROOT / "data" / "market" / "stock_industry_map_v2.json"
OUTPUT_DIR = ROOT / "output"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _fmt(v, suffix=""):
    if v is None:
        return "MISSING"
    return f"{v:,.2f}{suffix}"


def _load_name(code: str) -> str:
    try:
        uni = pd.read_csv(UNIVERSE_FILE, dtype={"code": str})
        row = uni[uni["code"] == code]
        if not row.empty:
            return str(row.iloc[0]["name"])
    except Exception as exc:
        print(f"[brief] 股票池读取失败: {exc}")
    return "MISSING"


def _load_industry(code: str) -> dict:
    try:
        m = json.loads(INDUSTRY_FILE.read_text(encoding="utf-8"))
        return m.get(code) or {}
    except Exception as exc:
        print(f"[brief] 行业映射读取失败: {exc}")
        return {}


def _latest_qfq_kline(code: str):
    files = sorted(KL_DIR.glob(f"{code}_*_qfq.csv"))
    if not files:
        return None, None
    # 以数据列内最新日期为准，而非文件名
    best, best_date = None, ""
    for f in files:
        try:
            df = pd.read_csv(f)
            d = str(df["日期"].max())
            if d > best_date:
                best, best_date = (f, df), d
        except Exception:
            continue
    return best


def _price_section(code: str, name: str) -> tuple[list[str], list[tuple[str, str]]]:
    lines: list[str] = []
    src = [f"`data/daily_kline/{code}_*_qfq.csv`（前复权口径）"]
    result = _latest_qfq_kline(code)
    if result is None:
        return ["- **MISSING**：无 qfq 日K缓存。按项目规则禁止用未复权数据计算价格指标，"
                "请先运行日更刷新数据。"], src
    f, df = result
    src[0] = f"`{f.relative_to(ROOT)}`（前复权口径）"
    df = df.sort_values("日期")
    close = float(df["收盘"].iloc[-1])
    last_date = str(df["日期"].iloc[-1])
    lines.append(f"- 最新收盘：**{close:.2f}**（{last_date}）")

    win = df.tail(250)
    if len(win) >= 200:
        hi52 = float(win["最高"].max())
        lo52 = float(win["最低"].min())
        drop52 = close / hi52 - 1
        pos52 = (close - lo52) / (hi52 - lo52) if hi52 > lo52 else None
        lines.append(f"- 52周最高/最低：{hi52:.2f} / {lo52:.2f}"
                     f"（近250个交易日）")
        lines.append(f"- 距52周高点跌幅：**{drop52 * 100:.1f}%**"
                     f"（深价初选线 ≥40%）")
        if pos52 is not None:
            lines.append(f"- 当前价在52周区间的位置：**{pos52 * 100:.1f}%**"
                         f"（0=最低，100=最高；此为区间位置，非排名分位）")
    else:
        lines.append("- 52周指标：**MISSING**（历史不足250个交易日）")

    all_hi = float(df["最高"].max())
    lines.append(f"- 距全样本（{df['日期'].iloc[0]} 起）高点跌幅："
                 f"{(close / all_hi - 1) * 100:.1f}%（全样本高点 {all_hi:.2f}）")

    if len(df) >= 200:
        ma200 = float(df["收盘"].tail(200).mean())
        lines.append(f"- MA200：{ma200:.2f}，收盘价{'高于' if close >= ma200 else '低于'}MA200 "
                     f"({(close / ma200 - 1) * 100:+.1f}%)")
    else:
        lines.append("- MA200：**MISSING**（历史不足200个交易日）")
    if len(df) >= 60:
        ma60 = float(df["收盘"].tail(60).mean())
        lines.append(f"- MA60：{ma60:.2f}（偏离 {(close / ma60 - 1) * 100:+.1f}%）")

    vol5 = float(df["成交量"].tail(5).mean())
    vol60 = float(df["成交量"].tail(60).mean()) if len(df) >= 60 else None
    if vol60:
        lines.append(f"- 量能：近5日均量为近60日均量的 **{vol5 / vol60:.2f} 倍**"
                     f"（<0.5 视为明显缩量）")

    if len(df) >= 20:
        peak20 = float(df["收盘"].tail(20).max())
        lines.append(f"- 近20日收盘峰值：{peak20:.2f}"
                     f"（当前 {(close / peak20 - 1) * 100:+.1f}%）")
    return lines, src


def _financial_section(code: str) -> tuple[list[str], list[tuple[str, str]]]:
    lines: list[str] = []
    src = ["`data/financials/{code}_ths.json`（同花顺摘要，缓存30天）"]
    fin = {}
    try:
        fin = fetch_financial_data(code)
    except Exception as exc:
        lines.append(f"- **MISSING**：财务数据获取失败（{exc}）")
        return lines, src
    if not fin:
        lines.append("- **MISSING**：财务数据为空（接口失败或无数据）")
        return lines, src

    annual_p = fin.get("report_date") or "最新年报"
    latest_p = fin.get("yoy_period") or "最新报告期"
    roe = fin.get("roe")
    if roe is not None:
        lines.append(f"- ROE：**{roe * 100:.1f}%**（口径：{annual_p} 年报；深价硬过滤 ≥6%）")
    np_ = fin.get("net_profit")
    if np_ is not None:
        lines.append(f"- 净利润：{_fmt(np_ / 1e8, ' 亿元')}（口径：{annual_p} 年报）")
    ocf = fin.get("ocf_per_share")
    if ocf is not None:
        lines.append(f"- 每股经营现金流：{_fmt(ocf, ' 元')}（口径：{annual_p} 年报累计）")
    rev = fin.get("revenue_yoy")
    if rev is not None:
        lines.append(f"- 营收同比：**{rev * 100:+.1f}%**（口径：{latest_p} 报告期；硬过滤 ≥0）")
    ni = fin.get("profit_yoy")
    if ni is not None:
        lines.append(f"- 净利润同比：**{ni * 100:+.1f}%**（口径：{latest_p} 报告期；硬过滤 ≥-20%）")
    dk = fin.get("deducted_profit_yoy")
    if dk is not None:
        lines.append(f"- 扣非净利润同比：{dk * 100:+.1f}%（口径：{latest_p} 报告期）")
    debt = fin.get("debt_ratio")
    if debt is not None:
        lines.append(f"- 资产负债率：{debt * 100:.1f}%（口径：{latest_p} 时点；趋势仓不过滤，深价参考）")
    bv = fin.get("book_value_per_share")
    if bv is not None:
        lines.append(f"- 每股净资产：{_fmt(bv, ' 元')}（口径：{latest_p} 时点）")
    eps = fin.get("eps")
    if eps is not None:
        lines.append(f"- 基本每股收益：{_fmt(eps, ' 元')}（口径：{annual_p} 年报）")
    npm = fin.get("net_profit_margin")
    if npm is not None:
        lines.append(f"- 销售净利率：{npm * 100:.1f}%（口径：{annual_p} 年报）")
    if ocf is not None and eps:
        lines.append(f"- 参考：每股经营现金流/每股收益 = {ocf / eps:.2f}"
                     f"（>0.8 为健康，对应深价现金流过滤思想）")
    return lines, src


# 已知强周期但商品映射表未覆盖的行业：简报不得静默跳过，须显式 MISSING（2026-09-30 600026 研究发现）
CYCLICAL_UNMAPPED = {
    "航运港口": "运价指数（BDTI/BCTI）——akshare 已有 macro_china_bdti_index / macro_shipping_bcti 接口，待接入后自动计算分位",
    "保险Ⅱ": "权益市场beta（利润由投资收益驱动，非商品周期）",
    "证券Ⅱ": "权益市场beta（利润由投资收益驱动，非商品周期）",
}


def _commodity_section(name: str, industry: str = "") -> tuple[list[str], list[tuple[str, str]]]:
    src = ["`data/market/commodity_*.json`（商品期货缓存）"]
    try:
        cycle = check_commodity_cycle(name)
    except Exception as exc:
        return [f"- 商品周期：**MISSING**（检测异常：{exc}）"], src
    if not cycle:
        cyc = CYCLICAL_UNMAPPED.get(industry)
        if cyc:
            return ([f"- 商品周期：**MISSING**——{name} 属强周期行业（{industry}），"
                     f"商品映射表未覆盖（{cyc}）。禁止静默跳过：外部核实周期分位前，"
                     f"利润高增一律按周期顶风险对待"], src)
        return [f"- 商品周期：{name} 所属行业无对应商品映射，跳过检测"], src
    pct = cycle.get("pct")
    sym = cycle.get("commodity", "?")
    price = cycle.get("price")
    warning = cycle.get("warning", "")
    penalty = cycle.get("penalty", 0)
    if isinstance(pct, (int, float)):
        verdict = "高位（周期风险）" if pct > 0.6 else "非高位"
        pct_txt = f"{pct * 100:.0f}%"
    else:
        verdict = "分位缺失，不判定"
        pct_txt = "MISSING"
    price_txt = f"，现价 {price:,.0f}" if isinstance(price, (int, float)) else ""
    return ([f"- 商品 {sym} 价格3年分位：**{pct_txt}** → {verdict}"
             f"（>60% 高位不买是趋势仓规则，深价仅告警，罚分 {penalty}）"
             f"{price_txt}{('，' + str(warning)) if warning else ''}"],
            src)


def _candidate_section(code: str) -> list[str]:
    lines = []
    checks = [
        ("output/candidates.csv", "深价候选"),
        ("output/candidates_quick.csv", "深价快筛"),
        ("output/trend_candidates.csv", "趋势候选"),
        (f"output/research/{code}.md", "研究文件"),
    ]
    for rel, label in checks:
        p = ROOT / rel
        if rel.endswith(".csv"):
            found = False
            if p.exists():
                try:
                    df = pd.read_csv(p, dtype=str)
                    code_col = next((c for c in df.columns if "code" in c.lower()
                                     or c == "代码"), None)
                    if code_col and code in set(df[code_col].astype(str)):
                        found = True
                except Exception:
                    found = False
            lines.append(f"- {label}：{'在列' if found else '不在列'}（`{rel}`）")
        else:
            lines.append(f"- {label}：{'已存在' if p.exists() else '不存在'}（`{rel}`）")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description="生成研究数据简报")
    ap.add_argument("code", help="六位股票代码")
    ap.add_argument("--out", default=None, help="同时写出 markdown 文件路径")
    args = ap.parse_args()
    code = args.code.strip().replace("sh", "").replace("sz", "").zfill(6)

    name = _load_name(code)
    ind = _load_industry(code)

    out = [f"# 研究数据简报 — {code} {name}", "",
           "> 本简报由 `tools/research_brief.py` 生成，是研究稿唯一数字来源。"
           "所有数字必须带口径引用；缺失项显式标 MISSING，禁止推测填充。", ""]

    out.append("## 身份与行业")
    out.append(f"- 代码/名称：{code} / {name}（来源：`data/market/stock_universe.csv`）")
    out.append(f"- 行业：{ind.get('level1_name', 'MISSING')} / "
               f"{ind.get('level2_name', 'MISSING')}"
               f"（来源：`data/market/stock_industry_map_v2.json`）")
    out.append("")

    price_lines, price_src = _price_section(code, name)
    out.append("## 价格结构（前复权）")
    out.extend(price_lines)
    out.append("")

    fin_lines, fin_src = _financial_section(code)
    out.append("## 财务快照（混合口径）")
    out.extend(fin_lines)
    out.append("")

    if name != "MISSING":
        com_lines, com_src = _commodity_section(name, ind.get("level2_name", ""))
    else:
        com_lines, com_src = ["- 商品周期：**MISSING**（股票名缺失，无法匹配商品）"], []
    out.append("## 商品周期")
    out.extend(com_lines)
    out.append("")

    out.append("## 候选与研究状态")
    out.extend(_candidate_section(code))
    out.append("")

    out.append("## 数据来源")
    all_src = price_src + fin_src + com_src + [
        "`data/market/stock_universe.csv`、`data/market/stock_industry_map_v2.json`"]
    for s in dict.fromkeys(all_src):
        out.append(f"- {s}")

    text = "\n".join(out) + "\n"
    print(text)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"[brief] 已写出 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
