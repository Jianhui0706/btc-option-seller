"""执行报告:把一次分组分批建仓的结构化记录渲染成 Markdown,供事后核验。

对应设计文档《盘口下单算法说明》第九节:
  文件名 exec_report_{日期}_{时间}_{合约}.md,内容含
  ① 汇总 ② Combo 阶段 ③ 分组明细 ④ 遗留处理 ⑤ 腿差核验。
执行过程中 ExecReport 会把实时日志逐行追加落盘(crash 也留痕),
执行结束再用本模块把整份报告重写成结构化版本。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo


CHINA_TZ = ZoneInfo("Asia/Shanghai")


@dataclass
class AttemptRecord:
    """某组内某一次尝试,买/卖两腿各自的报价与成交。"""
    attempt: int
    buy_kind: str = ""
    buy_price: Optional[float] = None
    buy_qty: float = 0.0
    buy_filled: float = 0.0
    buy_cancelled: float = 0.0
    sell_kind: str = ""
    sell_price: Optional[float] = None
    sell_qty: float = 0.0
    sell_filled: float = 0.0
    sell_cancelled: float = 0.0


@dataclass
class GroupRecord:
    """一组的完整记录:累计目标、各次尝试、组末两腿累计量。"""
    index: int
    cum_target: float
    group_target: float = 0.0
    combo_covered: bool = False
    reached_at: int = 0                       # 两腿达标于第几次(0=3次仍未达标)
    attempts: list[AttemptRecord] = field(default_factory=list)
    buy_after: float = 0.0
    sell_after: float = 0.0
    buy_carry: float = 0.0
    sell_carry: float = 0.0
    imbalance: float = 0.0


def _n(x: Optional[float], nd: int = 4) -> str:
    if x is None:
        return "—"
    return f"{x:.{nd}f}"


def _q(x: float) -> str:
    return f"{x:.1f}"


def render_report_md(
    *,
    buy_leg: str,
    sell_leg: str,
    target: float,
    n_groups: int,
    dry_run: bool,
    combo_target: float,
    combo_filled: float,
    combo_avg_net: float,
    combo_seconds: float,
    groups: list[GroupRecord],
    leftover: dict[str, dict],
    rebalance: list[dict],
    completion_error: Optional[str],
    buy_filled: float,
    sell_filled: float,
    buy_avg: float,
    sell_avg: float,
    events: list[str],
) -> str:
    """把结构化记录渲染成完整 Markdown 报告。"""
    now = datetime.now(CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S CST")
    diff = round(buy_filled - sell_filled, 4)
    balanced = abs(diff) < 1e-6
    completed = balanced and buy_filled + 1e-9 >= target and sell_filled + 1e-9 >= target
    net_debit = buy_avg - sell_avg

    out: list[str] = []
    out.append(f"# 价差建仓执行报告\n")
    out.append(f"- **生成时间**: {now}")
    out.append(f"- **买腿**: `{buy_leg}`")
    out.append(f"- **卖腿**: `{sell_leg}`")
    out.append(f"- **目标量**: {_q(target)} 张/腿 · 拆 {n_groups} 组")
    if dry_run:
        out.append(f"- **模式**: dry-run(仅计划,未真实下单)")
    out.append("")

    # ① 汇总
    out.append("## ① 汇总\n")
    out.append("| 项目 | 买腿 | 卖腿 |")
    out.append("|---|---|---|")
    out.append(f"| 目标量 | {_q(target)} | {_q(target)} |")
    out.append(f"| 实际成交 | {_q(buy_filled)} | {_q(sell_filled)} |")
    out.append(f"| 加权均价 | {_n(buy_avg)} | {_n(sell_avg)} |")
    out.append(f"| 净权利金(买−卖) | {_n(net_debit)} | — |")
    out.append(f"| 最终腿差 | {diff:+.1f} 张 | {'✅ 平衡' if balanced else '⚠ 不平衡'} |")
    out.append(f"| 执行完成度 | {_q(min(buy_filled, target))}/{_q(target)} | "
               f"{'✅ 完整完成' if completed else '⚠ 未完整完成,已上报策略层'} |")
    out.append("")

    # ② Combo 阶段
    out.append("## ② Combo 组合簿阶段\n")
    out.append(f"- 限定目标: **{_q(combo_target)}** 张/腿(仅第1组,不会占用后续4组)")
    if combo_filled > 1e-9:
        out.append(f"- 成交: **{_q(combo_filled)}** 张(两腿等量,原子成交)")
        out.append(f"- 组合净价: {_n(combo_avg_net)}")
    else:
        out.append("- 未成交(组合簿无对手或超时),已转入分组建仓")
    out.append(f"- 用时: {combo_seconds:.1f}s")
    out.append("")

    # ③ 分组明细
    out.append("## ③ 分组执行明细\n")
    if not groups:
        out.append("_(无分组执行:combo 已满足全部目标)_\n")
    for gr in groups:
        reach = ("Combo阶段达标" if gr.combo_covered else
                 (f"第 {gr.reached_at} 次达标" if gr.reached_at else "3 次未完全达标"))
        out.append(f"### 第 {gr.index} 组 · 本组目标 {_q(gr.group_target)} · "
                   f"累计目标 {_q(gr.cum_target)} 张/腿 · {reach}\n")
        out.append("| 尝试 | 类型 | 买腿委托(量@价) | 买腿成交 | 买腿撤单 | 卖腿委托(量@价) | 卖腿成交 | 卖腿撤单 |")
        out.append("|---|---|---|---|---|---|---|---|")
        for a in gr.attempts:
            kind = "IOC taker" if a.attempt >= 3 else f"maker {a.buy_kind or a.sell_kind}"
            out.append(
                f"| A{a.attempt} | {kind} "
                f"| {_q(a.buy_qty)}@{_n(a.buy_price)} | {_q(a.buy_filled)} | {_q(a.buy_cancelled)} "
                f"| {_q(a.sell_qty)}@{_n(a.sell_price)} | {_q(a.sell_filled)} | {_q(a.sell_cancelled)} |"
            )
        out.append(f"\n组末累计: 买腿 {_q(gr.buy_after)} · 卖腿 {_q(gr.sell_after)} · "
                   f"腿差 {gr.imbalance:+.1f}\n")
        out.append(f"组末遗留池: 买腿 {_q(gr.buy_carry)} · 卖腿 {_q(gr.sell_carry)} "
                   "(自动带入下一组)\n")

    # ④ 遗留处理
    out.append("## ④ 遗留处理\n")
    if leftover:
        out.append("| 腿 | 遗留量 | IOC 兜底成交 | 处理后剩余 |")
        out.append("|---|---|---|---|")
        for name, rec in leftover.items():
            short = rec.get("short", 0.0)
            swept = rec.get("swept", 0.0)
            remaining = rec.get("remaining", max(0.0, short - swept))
            out.append(f"| `{name}` | {_q(short)} | {_q(swept)} | {_q(remaining)} |")
    else:
        out.append("_无遗留:所有组均在组内 3 次尝试中补齐。_")
    out.append("")

    # ⑤ 腿差核验
    out.append("## ⑤ 腿差核验\n")
    if rebalance:
        out.append("| 再平衡尝试 | 腿 | 反向IOC | 请求量 | 成交量 | 剩余腿差 |")
        out.append("|---|---|---|---|---|---|")
        for rec in rebalance:
            out.append(f"| {rec.get('attempt')} | `{rec.get('instrument')}` | {rec.get('direction')} "
                       f"| {_q(rec.get('qty', 0.0))} | {_q(rec.get('filled', 0.0))} "
                       f"| {rec.get('remaining_diff', 0.0):+.1f} |")
        out.append("")
    out.append("| 买腿总成交 | 卖腿总成交 | 腿差 | 状态 |")
    out.append("|---|---|---|---|")
    out.append(f"| {_q(buy_filled)} | {_q(sell_filled)} | {diff:+.1f} | "
               f"{'✅ 两腿等量,无裸腿' if balanced else '⚠ 存在裸腿,需人工处理'} |")
    if completion_error:
        out.append(f"\n> **执行异常已上报策略层**: {completion_error}")
    out.append("")

    # 附:实时日志全文(crash 时也是这份的前缀)
    out.append("## 附录 · 实时执行日志\n")
    out.append("```")
    out.extend(events)
    out.append("```")
    out.append("")
    return "\n".join(out)
