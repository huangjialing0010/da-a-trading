# 文档索引

本目录只保存跨会话仍需查阅的规则、变更和设计依据。当前运行口径以根目录 `README.md`、`CLAUDE.md` 和代码为准；设计稿记录决策边界，不等于尚未完成的待办。

## 权威文档

- `../README.md`：使用入口、当前账户定位和关键安全规则。
- `../CLAUDE.md`：策略参数、开发约定和 AI 协作边界。
- `CHANGES.md`：按日期记录已经落地的重要变更和未完成事项。

## 设计稿状态

| 设计稿 | 状态 | 说明 |
|---|---|---|
| `superpowers/specs/2026-08-07-t1-paper-order-execution-design.md` | 已实施 | T+1 订单状态机与趋势 V2 |
| `superpowers/specs/2026-08-07-daily-workflow-reliability-design.md` | 已实施 | Actions 测试、缓存和并发边界 |
| `superpowers/specs/2026-08-07-absolute-market-date-fuse-design.md` | 已实施 | 交易日历与分仓数据熔断 |
| `superpowers/specs/2026-08-07-erp-cap-fail-safe-design.md` | 已实施 | ERP 异常按 30% 上限降级 |
| `superpowers/specs/2026-08-07-hs300-universe-candidate-alignment-design.md` | 已实施 | 沪深300母集与趋势候选顺序 |
| `superpowers/specs/2026-08-07-trade-funnel-design.md` | 已实施 | 零交易原因与完整回合口径 |
| `superpowers/specs/2026-08-07-trend-date-safety-design.md` | 已实施并升级 | 持仓日期安全；熔断已由绝对交易日设计升级 |
| `superpowers/specs/2026-08-07-trend-post-trade-holdings-design.md` | 已实施 | 趋势盘后持仓报告一致性 |
| `superpowers/specs/2026-08-07-virtual-validation-dashboard-design.md` | 已被 V2 取代 | 保留为历史决策，不作为当前口径 |
| `superpowers/specs/2026-08-07-minimal-selection-enhancement-design.md` | 部分实施/暂缓 | 日报隔离已完成；完整五问与前瞻追踪暂缓，首仓改走最小闭环 |
| `superpowers/specs/2026-08-07-minimal-deep-entry-design.md` | 待实施 | 用最小结构化建议闭合普通深价候选到 T+1 虚拟首仓订单；完整五问平台暂缓 |
| `superpowers/specs/2026-08-07-p0-runtime-reliability-design.md` | 已实施 | GBK输出、研究队列文案与深价语义幂等 |

## 维护规则

- 新设计先写状态，再进入实施；落地后同步状态和 `CHANGES.md`。
- 被取代的设计保留并标明替代关系，不继续作为当前操作手册。
- 不在 `CLAUDE.md` 追加开发流水账；过程留在 Git，稳定规则才进入主手册。
