"""日报终端文本转 GitHub Markdown，以及 reports/ 索引刷新。"""

import re
from datetime import datetime
from pathlib import Path

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"
REPORT_DIR = OUTPUT_DIR / "reports"


def _cells(line: str) -> list:
    return [p.strip() for p in re.split(r"\s{2,}", line.strip()) if p.strip()]


def _is_sep(line: str) -> bool:
    s = line.strip()
    return bool(s) and all(ch in "-─ " for ch in s)


def to_markdown(report_text: str) -> str:
    """把 daily_update 的终端样式日报转成 GitHub 友好的 Markdown。"""
    raw = [ln for ln in report_text.splitlines() if ln.strip() != "```"]
    out = []
    i = 0
    while i < len(raw):
        ln = raw[i]
        s = ln.strip()

        # ═══ 分区标题 → ##
        if s.startswith("═══") and s.endswith("═══"):
            out.append(f"## {s.strip('═ ').strip()}")
            out.append("")
            i += 1
            continue

        # 对齐文本表 → 标准 Markdown 表格
        if i + 1 < len(raw) and len(_cells(ln)) >= 3 and _is_sep(raw[i + 1]):
            header = _cells(ln)
            out.append("| " + " | ".join(header) + " |")
            out.append("|" + "---|" * len(header))
            i += 2
            while i < len(raw) and raw[i].strip() and not _is_sep(raw[i]):
                row = _cells(raw[i])
                if len(row) == len(header):
                    out.append("| " + " | ".join(row) + " |")
                else:
                    out.append("- " + raw[i].strip())
                i += 1
            out.append("")
            continue

        # [组名] N只 → ### 组名（N只）
        gm = re.match(r"^\[(.+?)\]\s*(\d+)只", s)
        if gm:
            name, count = gm.groups()
            out.append(f"### {name}（{count}只）")
            if name == "待分析":
                out.append("")
                out.append("> 缺少研究笔记或结论不明，等待 CC 深度分析。")
            out.append("")
            i += 1
            continue

        if s.startswith("[候选池]"):
            out.append("### 候选池")
            out.append("")
            out.append("- " + s[len("[候选池]") :].strip())
            i += 1
            continue

        if s.startswith("[深价候选] 新票"):
            out.append("- 深价候选新票：" + s.split(":", 1)[1].strip())
            i += 1
            continue
        if s.startswith("[趋势候选] 新票"):
            out.append("- 趋势候选新票：" + s.split(":", 1)[1].strip())
            i += 1
            continue
        if s.startswith("[趋势交叉]"):
            out.append("- 趋势交叉（已持有）：" + s.split(":", 1)[1].strip())
            i += 1
            continue

        if s in ("[深价候选]", "[趋势候选]"):
            out.append(f"### {s[1:-1]}")
            out.append("")
            i += 1
            continue

        if s.startswith("───") and s.endswith("───"):
            out.append(f"### {s.strip('─ ').strip()}")
            out.append("")
            i += 1
            continue

        # 研究结论/待分析行：4 空格起、6 位代码 → 项目符号
        rm = re.match(r"^\s{4,}(\d{6})\s+(.+?)\s*$", ln)
        if rm:
            code, rest = rm.group(1), rm.group(2)
            parts = [p.strip() for p in re.split(r"\s{2,}", rest) if p.strip()]
            name = parts[0] if parts else code
            tail = " ".join(parts[1:])
            row = f"- {code} {name}"
            if tail:
                row += f" {tail}"
            out.append(row)
            i += 1
            continue

        if s.startswith("汇总:"):
            out.append("- " + s)
            out.append("")
            i += 1
            continue

        if ln.startswith("  ") and s:
            out.append("- " + s)
            i += 1
            continue

        out.append(ln)
        i += 1

    return "\n".join(out).strip() + "\n"


def refresh_reports_index() -> None:
    """扫描 reports/ 下报告文件，重写 README.md 索引。"""
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    groups = {
        "日报": "daily_",
        "周报": "weekly_",
        "趋势周报": "trend_weekly_",
        "月报": "monthly_",
        "趋势月报": "trend_monthly_",
    }

    def date_label(prefix: str, stem: str) -> str:
        suffix = stem[len(prefix) :]
        if len(suffix) == 8 and suffix.isdigit():
            return f"{suffix[:4]}-{suffix[4:6]}-{suffix[6:]}"
        if len(suffix) == 6 and suffix.isdigit():
            return f"{suffix[:4]}-{suffix[4:]}"
        return stem

    latest = []
    buckets = {name: [] for name in groups}
    for name, prefix in groups.items():
        files = sorted(REPORT_DIR.glob(prefix + "*.md"), reverse=True)
        for f in files:
            buckets[name].append((f.name, date_label(prefix, f.stem)))
        if files:
            f = files[0]
            latest.append((name, f.name, date_label(prefix, f.stem)))

    lines = [
        "# 复盘报告索引",
        "",
        f"自动生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')}。日报按天、周报/月报按周期归档；最新报告置顶。",
        "",
        "## 最新报告",
        "",
        "| 类型 | 报告 | 周期 |",
        "|------|------|------|",
    ]
    for name, fname, label in latest:
        lines.append(f"| {name} | [{fname}]({fname}) | {label} |")
    lines.append("")
    for name in groups:
        lines.append(f"## {name}")
        lines.append("")
        for fname, label in buckets[name]:
            lines.append(f"- {label} [{fname}]({fname})")
        lines.append("")

    (REPORT_DIR / "README.md").write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
