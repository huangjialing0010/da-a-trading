"""
大A 虚拟盘交易系统 — CLI 入口

用法:
  python main.py init [金额]    初始化账户（默认100万）
  python main.py status         查看账户状态
  python main.py daily          生成日报
  python main.py weekly         生成周报
  python main.py monitor        持仓监控（止损/止盈检查）
  python main.py screener       运行选股筛选
  python main.py trade          交互式交易
  python main.py log [N]        查看最近N笔交易
  python main.py report weekly|monthly  生成复盘报告
"""

import sys
import os
import socket

socket.setdefaulttimeout(30)  # 所有网络调用30秒超时，防止akshare卡死

def _configure_console_output(stream=None) -> bool:
    """保留控制台编码，只降级无法编码的字符，避免末尾打印中断。"""
    target = stream if stream is not None else sys.stdout
    reconfigure = getattr(target, "reconfigure", None)
    if reconfigure is None:
        return False
    try:
        reconfigure(errors="replace")
        return True
    except Exception:
        return False


if not _configure_console_output():
    try:
        sys.stderr.write("warning: console output encoding fallback unavailable\n")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(__file__))

import pandas as pd
from tools.account import VirtualAccount
from tools.signal_engine import generate_signals, check_monitor
from tools.screener import run_full_screening, load_candidates
from tools.reporter import weekly_report, show_trade_log
from tools.review import weekly_review, monthly_review
from tools.data_fetcher import fetch_current_price


def cmd_init(args: list[str]):
    amount = 1_000_000
    if args:
        try:
            amount = float(args[0])
        except ValueError:
            print(f"无效金额: {args[0]}")
            return

    acc = VirtualAccount.init_with_cash(amount)
    print(f"账户已初始化，初始资金: ¥{amount:,.0f}")
    print(acc)


def cmd_status(_args=None):
    acc = VirtualAccount()
    if acc.state.position_count == 0 and acc.state.cash == 0:
        print("账户未初始化，请先运行: python main.py init [金额]")
        return

    print(acc)
    print()

    positions = acc.get_holdings()
    if positions:
        print(f"{'代码':<8} {'名称':<10} {'数量':>6} {'成本':>8} {'现价':>8} {'市值':>10} {'盈亏':>10}")
        print("-" * 65)
        for p in positions:
            print(f"{p.code:<8} {p.name:<10} {p.quantity:>6} "
                  f"{p.avg_cost:>8.2f} {p.current_price:>8.2f} "
                  f"{p.market_value:>10,.0f} {p.pnl_pct:>+9.2%}")


def cmd_daily(_args=None):
    """日报：深价主仓 + 趋势虚拟仓 + 候选池 + 市场水位"""
    from tools.auto_trader import daily_update
    print(daily_update())


def cmd_weekly(_args=None):
    acc = VirtualAccount()
    if acc.state.cash == 0 and acc.state.position_count == 0:
        print("账户未初始化，请先运行: python main.py init [金额]")
        return

    for pos in acc.get_holdings():
        price = fetch_current_price(pos.code)
        if price:
            acc.update_price(pos.code, price)

    acc.record_snapshot()
    weekly_report(acc)


def cmd_monitor(_args=None):
    acc = VirtualAccount()
    if acc.state.cash == 0 and acc.state.position_count == 0:
        print("账户未初始化，请先运行: python main.py init [金额]")
        return

    for pos in acc.get_holdings():
        price = fetch_current_price(pos.code)
        if price:
            acc.update_price(pos.code, price)

    signals = check_monitor(acc)
    urgent = [s for s in signals if s.urgency == "urgent"]
    normal = [s for s in signals if s.urgency != "urgent"]

    if urgent:
        print(f"\n### {len(urgent)} 条紧急信号 ###")
        for s in urgent:
            print(f"  [{s.type}] {s.name}({s.code}) — {s.reason}")
            print(f"  操作: {s.action}")
    else:
        print("无紧急信号")

    if normal:
        print(f"\n{len(normal)} 条普通信号:")
        for s in normal:
            print(f"  [{s.type}] {s.name}({s.code}) — {s.reason}")

    if not signals:
        print("无信号，持仓正常")


def cmd_screener(_args=None):
    print("运行全市场筛选，需要几分钟，请耐心等待...")
    print()
    results = run_full_screening(n=20)

    dv = results.get("deep_value", [])
    panic = results.get("panic", [])
    arb = results.get("event_arb", [])

    print(f"\n===== 筛选结果 =====")
    print(f"\n深度价值候选 ({len(dv)} 只):")
    for c in dv[:15]:
        print(f"  {c.code} {c.name:<10} 评分:{c.score:>6.0f} {c.reason}")

    if panic:
        print(f"\n极端恐慌信号:")
        for c in panic:
            print(f"  {c.name}({c.code}) — {c.reason}")

    if arb:
        print(f"\n事件套利机会 ({len(arb)} 个):")
        for c in arb:
            print(f"  {c.name}({c.code}) — {c.reason}")


def cmd_trade(_args=None):
    acc = VirtualAccount()
    if acc.state.cash == 0 and acc.state.position_count == 0:
        print("账户未初始化，请先运行: python main.py init [金额]")
        return

    print("=== 交易执行 ===")
    print(f"当前现金: ¥{acc.state.cash:,.0f}")
    print()

    # 加载信号
    candidates = load_candidates()
    signals_path = "output/signals.csv"
    signals = []
    if os.path.exists(signals_path):
        signals_df = pd.read_csv(signals_path, dtype={"code": str})
        if not signals_df.empty:
            print("当前信号:")
            for _, row in signals_df.iterrows():
                print(f"  [{row['type']}] {row['name']}({row['code']}) — {row['reason']}")
            print()

    print("操作选项:")
    print("  1. 执行信号中的买入")
    print("  2. 执行信号中的卖出")
    print("  3. 手动买入")
    print("  4. 手动卖出")
    print("  0. 退出")

    try:
        choice = input("\n选择: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n已退出")
        return

    if choice == "3":
        code = input("股票代码: ").strip()
        price_str = input("价格 (回车=自动获取): ").strip()
        if price_str:
            price = float(price_str)
        else:
            price = fetch_current_price(code)
            if price is None:
                print("无法获取价格")
                return
            print(f"当前价格: {price:.2f}")

        qty_str = input("数量（股，100的倍数）: ").strip()
        qty = int(qty_str)

        strategy = input("策略 (deep_value/panic/event_arb): ").strip()
        reason = input("理由: ").strip()

        # 超限校验：ERP 分位动态仓位上限 + 行业集中度（申万二级≤2只）
        try:
            from tools.auto_trader import _check_erp_position_cap, _check_industry_limit
            erp_ok, erp_msg = _check_erp_position_cap(acc)
            if not erp_ok:
                print(f"✋ {erp_msg}")
                return
            ind_ok, ind_msg = _check_industry_limit(code, acc)
            if not ind_ok:
                print(f"✋ {ind_msg}，禁止买入")
                return
        except Exception:
            pass  # 校验异常时不阻断手动交易

        name = code
        try:
            from tools.data_fetcher import fetch_daily_kline
            kline = fetch_daily_kline(code)
            if not kline.empty:
                name = code
        except Exception:
            pass

        ok, msg = acc.buy(code, name, price, qty, strategy, reason)
        print(msg)

    elif choice == "4":
        code = input("股票代码: ").strip()
        pos = acc.get_position(code)
        if pos is None:
            print(f"未持仓 {code}")
            return

        print(f"持仓: {pos.quantity}股, 成本: {pos.avg_cost:.2f}")
        qty_str = input(f"卖出数量 (回车=全部): ").strip()
        qty = int(qty_str) if qty_str else pos.quantity

        price_str = input("价格 (回车=自动获取): ").strip()
        if price_str:
            price = float(price_str)
        else:
            price = fetch_current_price(code)
            if price is None:
                print("无法获取价格")
                return
            print(f"当前价格: {price:.2f}")

        reason = input("理由: ").strip()
        ok, msg = acc.sell(code, price, qty, reason)
        print(msg)

    elif choice in ["1", "2"]:
        print("请使用手动买入/卖出选项，并参考信号信息。")

    else:
        print("已退出")


def cmd_log(args: list[str]):
    acc = VirtualAccount()
    n = 20
    if args:
        try:
            n = int(args[0])
        except ValueError:
            pass
    show_trade_log(acc, n)


def cmd_report(args: list[str]):
    mode = args[0] if args else "weekly"
    if mode not in ("weekly", "monthly"):
        print("用法: python main.py report weekly|monthly")
        return
    if mode == "weekly":
        print(weekly_review())
    else:
        print(monthly_review())


def print_usage():
    print(__doc__)


COMMANDS = {
    "init": cmd_init,
    "status": cmd_status,
    "daily": cmd_daily,
    "weekly": cmd_weekly,
    "monitor": cmd_monitor,
    "screener": cmd_screener,
    "trade": cmd_trade,
    "log": cmd_log,
    "report": cmd_report,
}


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print_usage()
        sys.exit(0)

    cmd = args[0].lower()
    if cmd in COMMANDS:
        try:
            COMMANDS[cmd](args[1:])
        except KeyboardInterrupt:
            print("\n已中断")
    else:
        print(f"未知命令: {cmd}")
        print_usage()
