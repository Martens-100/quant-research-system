#!/usr/bin/env python
"""
手动输入股票分析入口
====================

用法：
    # 分析模式（输入股票名称，产出研报 + 三类信号 + 操作建议）
    PYTHONPATH=src python scripts/analyze.py 科伦药业

    # 复盘模式（记录操作结果，产出复盘报告）
    PYTHONPATH=src python scripts/analyze.py 科伦药业 --review executed \
        --planned-price 40.00 --actual-price 40.15 --planned-qty 10000 --actual-qty 5000

    # 未操作复盘
    PYTHONPATH=src python scripts/analyze.py 科伦药业 --review not-executed \
        --reason triggered_not_acted --note "看好创新药长线逻辑"

数据来源
--------
本脚本读取 fixtures/<code>.json。生产环境应替换为 data 层的 adapter。
fixtures 缺失时，用内置的科伦药业样本数据演示。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from qsys.analysis.engine import (
    AdviceEngine, MonitorLevel, MonitorSignal, QuantAction, QuantSignal,
    build_factor_signal, generate_monitor_signals,
)
from qsys.analysis.modules import (
    AnalysisBundle, CashFlowInput, CashFlowModule, IndustryInput, IndustryModule,
    MoatInput, MoatModule, ResearchInput, ResearchModule,
)
from qsys.analysis.resolver import Resolver
from qsys.analysis.review import NoExecReason, Outcome, ReviewInput, run_review


# ============================================================================
# 内置样本（fixtures 缺失时使用）
# ============================================================================

SAMPLE = {
    "002422": {
        "name": "科伦药业", "market": "深市主板",
        "research": dict(target_price_mean=52.10, target_price_high=52.10, target_price_low=52.10,
                         current_price=39.26, coverage_count=29, rating_buy_pct=100.0,
                         rating_hold_pct=0.0, rating_sell_pct=0.0,
                         eps_forecast={"2026E": 1.23, "2027E": 1.55, "2028E": 1.88},
                         has_site_visit=True),
        "industry": dict(industry="医药生物-化学制药", lifecycle_stage="成长",
                         industry_revenue_growth=3.5, industry_margin_trend="down",
                         competitive_intensity="high", company_position="细分龙头",
                         relative_strength_60d=-6.5, sector_flow="out"),
        "cashflow": dict(ocf=23.41, ocf_series=[11.90, 26.42, 23.41], net_profit=11.28,
                         capex=23.90, interest_bearing_debt=44.18, cash=61.58,
                         receivables=30.0, revenue=88.39),
        "moat": dict(moat_type="技术专利",
                     moat_source="ADC 平台 OptiDC 与默沙东的 17 项全球 III 期临床绑定",
                     durability_years=5, roic=5.94, wacc=9.0, rd_expense_ratio=13.7,
                     rd_ratio_vs_peers="高于", substitute_count=2,
                     evidence=["全球首个在肺癌获批的 TROP2 ADC", "17 项全球 III 期临床"]),
        "quant": dict(action="block", tier="trend",
                      reason="趋势级全部空头，动量级信号不构成入场依据",
                      stop_price=38.42, effective_stop_type="structure",
                      standard_position_pct=8.18, worst_case_position_pct=3.64),
        "days_to_next_report": 21,
        "logic_falsified": 2,
        "hypotheses": [["科伦博泰 ADC 全球放量", "falsified"],
                       ["传统输液业务企稳", "verified"],
                       ["扣非利润改善", "falsified"]],
        "data_gap_count": 2,
        "holding": True,
    }
}


# ============================================================================
# 渲染
# ============================================================================

def h(title: str, ch: str = "=") -> str:
    return f"\n{ch * 78}\n  {title}\n{ch * 78}"


def render_report(resolved, bundle: AnalysisBundle, monitors, quant, factor, advice) -> str:
    """生成个股研报（Markdown）。"""
    L: list[str] = []
    L.append(f"# {bundle.symbol} {bundle.name} · 个股研报")
    L.append("")
    L.append(f"- 市场：{resolved.market}")
    L.append(f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M}")
    L.append(f"- 四维综合分：**{bundle.composite():.1f}/100**")
    L.append("")
    L.append("> 本研报由规则化流程生成，不构成投资建议。"
             "所有数据须核对来源与时点，研报类信息为非一手来源，引用前需核实原文。")
    L.append("")

    # ---- 四维分析 ----
    L.append("## 一、四维分析")
    L.append("")
    L.append("| 模块 | 得分 | 等级 | 数据缺口 | 警示 |")
    L.append("|---|---|---|---|---|")
    for m in (bundle.research, bundle.industry, bundle.cashflow, bundle.moat):
        L.append(f"| {m.module} | {m.score:.0f} | {m.grade} | {len(m.gaps)} | {len(m.caveats)} |")
    L.append("")

    for m in (bundle.research, bundle.industry, bundle.cashflow, bundle.moat):
        L.append(f"### {m.module} — {m.score:.0f}/100（{m.grade}）")
        L.append("")
        if m.fields:
            L.append("**结构化字段**")
            L.append("")
            L.append("| 字段 | 值 |")
            L.append("|---|---|")
            for k, v in m.fields.items():
                L.append(f"| {k} | {v} |")
            L.append("")
        if m.findings:
            L.append("**关键发现**")
            L.append("")
            for f in m.findings:
                L.append(f"- {f}")
            L.append("")
        if m.gaps:
            L.append("**数据缺口**")
            L.append("")
            for g in m.gaps:
                L.append(f"- ⚠️ {g}")
            L.append("")
        if m.caveats:
            L.append("**警示**")
            L.append("")
            for c in m.caveats:
                L.append(f"- ▲ {c}")
            L.append("")

    # ---- 三类信号 ----
    L.append("## 二、三类信号")
    L.append("")
    L.append("### 2.1 监控信号")
    L.append("")
    L.append("回答「有什么变化需要我知道」。**不直接产生买卖动作**，只改变观察状态。")
    L.append("")
    if monitors:
        L.append("| 级别 | 触发 | 时间窗 | 含义 | 来源 |")
        L.append("|---|---|---|---|---|")
        for s in monitors:
            L.append(f"| {s.level.value} | {s.trigger} | {s.window} | {s.implication} | {s.source} |")
    else:
        L.append("（无）")
    L.append("")

    L.append("### 2.2 量化信号")
    L.append("")
    L.append("回答「规则说该做什么」。可执行、有明确价位。")
    L.append("")
    L.append(f"- **动作**：`{quant.action.value}`")
    L.append(f"- **决定层**：{quant.tier_decided}")
    L.append(f"- **理由**：{quant.reason}")
    if quant.stop_price:
        L.append(f"- **止损位**：{quant.stop_price}（{quant.effective_stop_type}）")
    L.append(f"- **标准仓位**：{quant.standard_position_pct}%")
    L.append(f"- **制度修正仓位**：{quant.worst_case_position_pct}%")
    for w in quant.warnings:
        L.append(f"- ⚠️ {w}")
    L.append("")

    L.append("### 2.3 多因子信号")
    L.append("")
    L.append("回答「这只股票在同类里排第几」。**只影响仓位大小，不影响方向。**")
    L.append("")
    L.append(f"- **综合分**：{factor.composite:.1f}")
    L.append(f"- **分层**：{factor.layer}（{factor.percentile:.0f}% 分位）")
    L.append("")
    L.append("| 因子 | 得分 | 权重贡献 |")
    L.append("|---|---|---|")
    for k, v in factor.factor_scores.items():
        L.append(f"| {k} | {v:.0f} | {factor.contributions.get(k, 0)} |")
    L.append("")

    # ---- 操作建议 ----
    L.append("## 三、操作建议")
    L.append("")
    L.append(f"### 建议动作：**{advice.advice.value}**")
    L.append("")
    L.append(f"- 建议仓位：**{advice.position_pct:.2f}%**")
    L.append(f"- 决定者：{advice.decided_by}")
    L.append("")
    if advice.blocked_reasons:
        L.append("**否决/约束**")
        L.append("")
        for b in advice.blocked_reasons:
            L.append(f"- ✗ {b}")
        L.append("")
    L.append("**判定链**（可审计）")
    L.append("")
    for c in advice.chain:
        L.append(f"1. {c}")
    L.append("")

    # ---- 缺口汇总 ----
    gaps = bundle.all_gaps()
    caveats = bundle.all_caveats()
    if gaps or caveats:
        L.append("## 四、缺口与警示汇总")
        L.append("")
        if gaps:
            L.append("**数据缺口**")
            L.append("")
            for g in gaps:
                L.append(f"- {g}")
            L.append("")
        if caveats:
            L.append("**警示**")
            L.append("")
            for c in caveats:
                L.append(f"- {c}")
            L.append("")

    return "\n".join(L)


# ============================================================================
# 主流程
# ============================================================================

def build_bundle(code: str, data: dict) -> AnalysisBundle:
    return AnalysisBundle(
        symbol=code, name=data["name"],
        research=ResearchModule().analyze(ResearchInput(**data["research"])),
        industry=IndustryModule().analyze(IndustryInput(**data["industry"])),
        cashflow=CashFlowModule().analyze(CashFlowInput(**data["cashflow"])),
        moat=MoatModule().analyze(MoatInput(**data["moat"])),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="手动输入股票分析")
    ap.add_argument("name", help="股票名称或代码，如 '科伦药业' 或 '002422'")
    ap.add_argument("--review", choices=["executed", "not-executed"], help="进入复盘模式")
    ap.add_argument("--planned-price", type=float)
    ap.add_argument("--actual-price", type=float)
    ap.add_argument("--planned-qty", type=int, default=0)
    ap.add_argument("--actual-qty", type=int, default=0)
    ap.add_argument("--reason", help="未执行原因: signal_not_triggered / triggered_not_acted / "
                                     "condition_unmet / missed_monitoring")
    ap.add_argument("--note", default="", help="自述原因")
    ap.add_argument("--price-t5", type=float)
    ap.add_argument("--price-t20", type=float)
    ap.add_argument("--save", action="store_true", help="保存研报到 logs/")
    args = ap.parse_args()

    # ---- 1. 解析名称 ----
    print(h("1. 标的解析"))
    r = Resolver().resolve(args.name)
    print(r.explain())
    if not r.is_confident:
        print()
        print("解析置信度不足，已中止。")
        print("原因：猜错标的比报错严重得多 —— 后续所有分析都会基于错误的公司。")
        print("请用完整名称或 6 位代码重试。")
        return 2

    # ---- 2. 取数 ----
    fixture = ROOT / "fixtures" / f"{r.code}.json"
    if fixture.exists():
        data = json.loads(fixture.read_text(encoding="utf-8"))
        print(f"\n数据来源: {fixture}")
    elif r.code in SAMPLE:
        data = SAMPLE[r.code]
        print(f"\n数据来源: 内置样本（生产环境应替换为 data 层 adapter）")
    else:
        print(f"\n未找到 {r.code} 的 fixture，且无内置样本。")
        print(f"请创建 {fixture} 后重试。")
        return 3

    # ---- 3. 四维分析 ----
    print(h("2. 四维分析"))
    bundle = build_bundle(r.code, data)
    print(bundle.summary())
    for m in (bundle.research, bundle.industry, bundle.cashflow, bundle.moat):
        if m.gaps:
            print(f"    ! {m.module} 缺口: {'; '.join(m.gaps)}")
        if m.caveats:
            print(f"    ▲ {m.module} 警示: {'; '.join(m.caveats)}")

    # ---- 4. 三类信号 ----
    print(h("3. 三类信号"))
    monitors = generate_monitor_signals(
        days_to_next_report=data.get("days_to_next_report"),
        data_gap_count=data.get("data_gap_count", 0),
        caveat_count=len(bundle.all_caveats()),
        crowding_pending=True,
        logic_falsified_count=data.get("logic_falsified", 0),
    )
    print("  【监控信号】")
    for s in monitors:
        print(f"    {s}")
    if not monitors:
        print("    （无）")

    q = data["quant"]
    quant = QuantSignal(
        QuantAction(q["action"]), q["tier"], q["reason"],
        q.get("stop_price"), q.get("effective_stop_type"),
        q.get("standard_position_pct"), q.get("worst_case_position_pct"),
        ["标准法假设止损必然成交；A 股连续跌停时该假设不成立"],
    )
    print("\n  【量化信号】")
    print(f"    {quant}")

    factor = build_factor_signal(bundle)
    print("\n  【多因子信号】")
    print(f"    {factor}")
    print(f"    因子贡献: {factor.contributions}")

    # ---- 5. 操作建议 ----
    print(h("4. 操作建议"))
    eng = AdviceEngine(worst_case_position_pct=quant.worst_case_position_pct or 0.0)
    advice = eng.decide(quant, factor, monitors,
                        holding=data.get("holding", False),
                        logic_falsified=data.get("logic_falsified", 0))
    print(advice.explain())

    # ---- 6. 研报输出 ----
    report_md = render_report(r, bundle, monitors, quant, factor, advice)
    print(h("5. 研报"))
    print(f"  已生成 Markdown 研报，{len(report_md.splitlines())} 行")
    if args.save:
        out = ROOT / "logs" / f"report-{r.code}-{date.today():%Y%m%d}.md"
        out.parent.mkdir(exist_ok=True)
        out.write_text(report_md, encoding="utf-8")
        print(f"  已保存: {out}")

    # ---- 7. 复盘（可选） ----
    if args.review:
        print(h("6. 复盘"))
        outcome = Outcome.EXECUTED if args.review == "executed" else Outcome.NOT_EXECUTED
        reason = NoExecReason(args.reason) if args.reason else None
        if outcome == Outcome.NOT_EXECUTED and reason is None:
            print("  未操作复盘必须提供 --reason（signal_not_triggered / "
                  "triggered_not_acted / condition_unmet / missed_monitoring）")
            print("  原因：不归因就无法区分『计划错』与『手软』，两者修正方向相反。")
            return 4

        ri = ReviewInput(
            symbol=r.code, name=r.name or "", review_date=date.today(),
            advice=advice, quant=quant, factor=factor, monitors=monitors,
            outcome=outcome,
            planned_price=args.planned_price, actual_price=args.actual_price,
            planned_qty=args.planned_qty, actual_qty=args.actual_qty,
            no_exec_reason=reason, no_exec_note=args.note,
            price_t5=args.price_t5, price_t20=args.price_t20,
            hypotheses=[tuple(x) for x in data.get("hypotheses", [])],
            data_gap_count=data.get("data_gap_count", 0),
            mandatory_action=True,
        )
        print(run_review(ri).explain())

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
