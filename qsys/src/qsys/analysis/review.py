"""
复盘模块：执行 / 未执行 双向复盘
==============================

为什么复盘要分两种情形
----------------------
因为它们的修正方向**完全相反**：

  执行了但结果不好  -> 可能计划错（参数问题）-> 调参数
  没执行（该执行时）-> 可能手软（权限问题）  -> 调执行机制

如果把这两种混在一起，会得出"系统需要优化"这种无用结论。

复盘维度总览
------------
共同维度（两种情形都要评）：
  D1 信号一致性   实际动作 vs 三类信号的建议是否一致
  D2 逻辑验证     买入假设是否被证伪
  D3 数据质量     本轮分析的数据缺口数（缺口多则结论不可信）
  D4 事后验证     T+5 / T+20 回看，判断当时动作的对错

情形 A（已执行）额外维度：
  A1 执行偏差     计划价 vs 实际成交价、时间偏差
  A2 仓位合规     实际仓位 vs 建议仓位
  A3 成本核算     实际成本 vs 假设成本

情形 B（未操作）额外维度：
  B1 未执行归因   信号未触发 / 触发了没执行 / 条件不满足
  B2 信号有效性   事后验证：信号与人，谁对
  B3 纪律扣分     硬性动作未执行 -> 归零
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Optional

from qsys.analysis.engine import Advice, AdviceResult, FactorSignal, MonitorSignal, QuantSignal


# ============================================================================
# 复盘输入
# ============================================================================

class Outcome(str, Enum):
    EXECUTED = "executed"
    NOT_EXECUTED = "not_executed"


class NoExecReason(str, Enum):
    SIGNAL_NOT_TRIGGERED = "signal_not_triggered"
    """信号未触发 —— 未操作是正确行为，不扣分。"""
    TRIGGERED_NOT_ACTED = "triggered_not_acted"
    """信号已触发但未执行 —— 纪律问题，须扣分并考虑系统接管。"""
    CONDITION_UNMET = "condition_unmet"
    """执行条件不满足（如跌停无法卖出）—— 设计问题，需修执行条件。"""
    MISSED_MONITORING = "missed_monitoring"
    """未跟踪 —— 流程问题。"""


@dataclass
class ReviewInput:
    symbol: str
    name: str
    review_date: date
    advice: AdviceResult
    quant: QuantSignal
    factor: FactorSignal
    monitors: list[MonitorSignal]

    outcome: Outcome
    planned_price: Optional[float] = None
    actual_price: Optional[float] = None
    planned_qty: int = 0
    actual_qty: int = 0
    planned_date: Optional[date] = None
    actual_date: Optional[date] = None

    no_exec_reason: Optional[NoExecReason] = None
    no_exec_note: str = ""

    price_t5: Optional[float] = None
    price_t20: Optional[float] = None

    hypotheses: list[tuple[str, str]] = field(default_factory=list)
    """[(假设描述, 状态 verified/unverified/falsified), ...]"""

    data_gap_count: int = 0
    emotion_tags: list[tuple[date, str, str]] = field(default_factory=list)
    """[(日期, 情绪, 操作方向), ...]"""

    mandatory_action: bool = False
    """该动作是否为硬性要求（如硬止损）。未执行则纪律分归零。"""


# ============================================================================
# 复盘维度
# ============================================================================

@dataclass
class Dimension:
    code: str
    name: str
    verdict: str
    score: Optional[float]
    details: list[str] = field(default_factory=list)
    correction: str = ""

    def __str__(self) -> str:
        s = f"{self.code} {self.name:<10} {self.verdict}"
        if self.score is not None:
            s += f"  [{self.score:.0f}/100]"
        return s


@dataclass
class ReviewReport:
    symbol: str
    name: str
    outcome: Outcome
    dimensions: list[Dimension]
    attribution: str
    corrections: list[str]
    must_take_over: bool = False

    def explain(self) -> str:
        lines = [
            "=" * 74,
            f"复盘报告 · {self.symbol} {self.name}",
            f"情形：{'已执行' if self.outcome == Outcome.EXECUTED else '未操作'}",
            "=" * 74,
        ]
        for d in self.dimensions:
            lines.append(f"  {d}")
            for x in d.details:
                lines.append(f"      · {x}")
            if d.correction:
                lines.append(f"      -> 修正: {d.correction}")
        lines.append("")
        lines.append(f"  归因结论：{self.attribution}")
        if self.corrections:
            lines.append("  修正动作：")
            for c in self.corrections:
                lines.append(f"    □ {c}")
        if self.must_take_over:
            lines.append("  ⚠ 建议系统接管下单权限")
        return "\n".join(lines)


# ============================================================================
# 各维度计算
# ============================================================================

def _d1_signal_consistency(inp: ReviewInput) -> Dimension:
    """D1 信号一致性：实际动作 vs 三类信号建议。"""
    details: list[str] = []
    details.append(f"系统建议：{inp.advice.advice.value}（仓位 {inp.advice.position_pct:.2f}%）")

    if inp.outcome == Outcome.NOT_EXECUTED:
        details.append("实际动作：未操作")
        if inp.advice.advice in (Advice.NO_ENTRY, Advice.OBSERVE, Advice.SUSPEND):
            return Dimension("D1", "信号一致性", "一致（建议本就是不动）", 100.0, details)
        return Dimension("D1", "信号一致性", "不一致（建议动作未执行）", 0.0, details,
                         "见 B1 未执行归因")

    details.append(f"实际动作：{inp.planned_qty} 股 @ {inp.actual_price}")
    if inp.advice.advice in (Advice.BUILD, Advice.BUILD_HALF, Advice.PROBE, Advice.REDUCE, Advice.CLEAR):
        return Dimension("D1", "信号一致性", "一致", 100.0, details)
    return Dimension("D1", "信号一致性", "实际动作与建议不符", 50.0, details,
                     "复核是否存在计划外操作")


def _d2_logic(inp: ReviewInput) -> Dimension:
    """D2 逻辑验证：买入假设是否被证伪。"""
    if not inp.hypotheses:
        return Dimension("D2", "逻辑验证", "无假设可验证", None, ["未提供买入假设"],
                         "补录买入时的核心假设（模板 A5「一句话本质」的拆解）")

    v = sum(1 for _, s in inp.hypotheses if s == "verified")
    f = sum(1 for _, s in inp.hypotheses if s == "falsified")
    u = sum(1 for _, s in inp.hypotheses if s == "unverified")
    score = v / len(inp.hypotheses) * 100

    details = [f"{d}: {s}" for d, s in inp.hypotheses]
    if f > 0:
        verdict = f"{f} 条证伪 / {v} 条验证 / {u} 条待验证"
        correction = "触发逻辑止损 —— 优先级高于价格止损"
    elif u > 0:
        verdict = f"{u} 条待验证"
        correction = "继续持有但禁止加仓"
    else:
        verdict = "全部假设已验证"
        correction = ""
    return Dimension("D2", "逻辑验证", verdict, round(score, 1), details, correction)


def _d3_data_quality(inp: ReviewInput) -> Dimension:
    """D3 数据质量：缺口多则结论不可信。"""
    n = inp.data_gap_count
    score = max(0.0, 100.0 - n * 20)
    if n == 0:
        return Dimension("D3", "数据质量", "无缺口", 100.0, [])
    details = [f"{n} 项字段缺失，相关结论须标注缺口"]
    correction = "缺口字段不得进入决策；补充取数后重跑分析"
    return Dimension("D3", "数据质量", f"{n} 项缺口", score, details, correction)


def _d4_hindsight(inp: ReviewInput) -> Dimension:
    """D4 事后验证：T+5 / T+20 回看，判断当时动作的对错。"""
    ref = inp.actual_price if inp.outcome == Outcome.EXECUTED else None
    if ref is None:
        ref = inp.planned_price
    if ref is None or (inp.price_t5 is None and inp.price_t20 is None):
        return Dimension("D4", "事后验证", "窗口未到或数据缺失", None,
                         ["需 T+5 与 T+20 价格才能回看"],
                         "在 T+5 / T+20 到期后补跑本维度")

    details: list[str] = []
    moved_up = False
    if inp.price_t5 is not None:
        r5 = (inp.price_t5 / ref - 1) * 100
        details.append(f"T+5：{ref:.2f} -> {inp.price_t5:.2f}（{r5:+.2f}%）")
        moved_up = r5 > 0
    if inp.price_t20 is not None:
        r20 = (inp.price_t20 / ref - 1) * 100
        details.append(f"T+20：{ref:.2f} -> {inp.price_t20:.2f}（{r20:+.2f}%）")
        moved_up = r20 > 0

    if inp.outcome == Outcome.EXECUTED:
        verdict = "执行方向事后看偏错（价格反向）" if moved_up else "执行方向事后看正确（价格同向）"
    else:
        verdict = "未操作事后看偏错（错过了上涨）" if moved_up else "未操作事后看正确（避开了下跌）"

    return Dimension("D4", "事后验证", verdict, None, details)


def _a1_execution_deviation(inp: ReviewInput) -> Dimension:
    """A1 执行偏差：计划价 vs 实际成交价。"""
    if inp.planned_price is None or inp.actual_price is None:
        return Dimension("A1", "执行偏差", "缺计划价或成交价", None, [],
                         "补录计划价，否则无法评估执行质量")

    dev = abs(inp.actual_price - inp.planned_price) / inp.planned_price * 100
    details = [f"计划 {inp.planned_price:.2f} -> 实际 {inp.actual_price:.2f}，偏差 {dev:.2f}%"]
    if inp.planned_date and inp.actual_date:
        lag = (inp.actual_date - inp.planned_date).days
        details.append(f"计划日 {inp.planned_date} -> 实际日 {inp.actual_date}，滞后 {lag} 天")

    score = max(0.0, 100.0 - dev * 10)
    verdict = "偏差可接受" if dev <= 1.0 else ("偏差偏大" if dev <= 3.0 else "偏差过大")
    return Dimension("A1", "执行偏差", verdict, round(score, 1), details,
                     "" if dev <= 1.0 else "复核下单方式（限价单 vs 市价单）")


def _a2_position_compliance(inp: ReviewInput) -> Dimension:
    """A2 仓位合规：实际仓位 vs 建议仓位。"""
    if not inp.planned_qty or inp.actual_price is None or inp.planned_price is None:
        return Dimension("A2", "仓位合规", "数据不足", None, [])

    actual_capital = inp.actual_qty * inp.actual_price
    planned_capital = inp.planned_qty * inp.planned_price
    if planned_capital == 0:
        return Dimension("A2", "仓位合规", "计划仓位为 0", None, [])

    ratio = actual_capital / planned_capital
    details = [
        f"建议 {inp.planned_qty} 股（{planned_capital:,.0f} 元）",
        f"实际 {inp.actual_qty} 股（{actual_capital:,.0f} 元）",
        f"实际/建议 = {ratio * 100:.1f}%",
    ]
    if ratio > 1.1:
        return Dimension("A2", "仓位合规", "实际仓位超建议", 60.0, details,
                         "超配会放大风险敞口，复核是否偏离风险预算")
    if ratio < 0.9:
        return Dimension("A2", "仓位合规", "实际仓位低于建议", 70.0, details,
                         "低配不构成风险，但会削弱信号有效性；确认是有意为之")
    return Dimension("A2", "仓位合规", "与建议一致", 100.0, details)


def _a3_cost(inp: ReviewInput) -> Dimension:
    """A3 成本核算：实际成本 vs 假设成本。"""
    details = [
        "印花税 0.05%（卖出单边，已核验）",
        "佣金/过户费/滑点须从对账单取，系统不代填",
    ]
    return Dimension("A3", "成本核算", "需对账单核对", None, details,
                     "用实际成本回填配置，否则参数寻优会偏向高频")


def _b1_no_exec_attribution(inp: ReviewInput) -> Dimension:
    """B1 未执行归因 —— 本模块最关键的一个维度。

    必须区分三类原因，因为它们对应完全不同的修正：
      信号未触发    -> 正常，不扣分
      触发了没执行  -> 纪律问题，扣分 + 考虑系统接管
      条件不满足    -> 设计问题，修执行条件
    """
    if inp.no_exec_reason is None:
        return Dimension("B1", "未执行归因", "未提供原因", None, [],
                         "必须归因：否则无法区分『计划错』与『手软』")

    mapping = {
        NoExecReason.SIGNAL_NOT_TRIGGERED: (
            "信号未触发", 100.0,
            "建议本就是不动，未操作是正确行为",
            "",
        ),
        NoExecReason.TRIGGERED_NOT_ACTED: (
            "信号已触发但未执行", 0.0,
            "纪律问题 —— 需区分『计划错』还是『手软』",
            "若理由是长期逻辑（如『看好长线』），追问：这个理由在信号触发日成立，"
            "在下一次触发时是否也成立？永远成立的理由等于没有理由",
        ),
        NoExecReason.CONDITION_UNMET: (
            "执行条件不满足", 50.0,
            "设计问题 —— 如跌停无法卖出、停牌无法交易",
            "修执行条件（增加不可成交情形的最坏损失重算），而非扣纪律分",
        ),
        NoExecReason.MISSED_MONITORING: (
            "未跟踪标的", 30.0,
            "流程问题 —— 未纳入监控清单",
            "把标的加入财报日历与日频监控",
        ),
    }
    verdict, score, note, correction = mapping[inp.no_exec_reason]
    details = [note]
    if inp.no_exec_note:
        details.append(f"自述原因：{inp.no_exec_note}")
    return Dimension("B1", "未执行归因", verdict, score, details, correction)


def _b2_signal_validity(inp: ReviewInput) -> Dimension:
    """B2 信号有效性：事后验证信号与人谁对。

    注意一处关键区分：**当未执行的原因是"客观条件不允许"时，
    不能得出"未执行是错的"这个结论** —— 因为执行本身就不可能。
    此时正确的修正是降低该类标的的仓位上限，而不是强化执行纪律。
    """
    if inp.planned_price is None or inp.price_t20 is None:
        return Dimension("B2", "信号有效性", "窗口未到", None, [],
                         "T+20 到期后补跑")

    r20 = (inp.price_t20 / inp.planned_price - 1) * 100
    sig_wanted_exit = inp.advice.advice in (Advice.CLEAR, Advice.REDUCE, Advice.NO_ENTRY)
    price_fell = r20 < 0
    impossible = inp.no_exec_reason == NoExecReason.CONDITION_UNMET

    details = [f"T+20 相对计划价 {r20:+.2f}%",
               f"信号倾向：{'减/不入' if sig_wanted_exit else '建仓'}"]

    if impossible:
        # 客观条件不允许执行 —— 不评判对错，只评判预案是否充分
        if sig_wanted_exit and price_fell:
            return Dimension(
                "B2", "信号有效性", "信号正确，但客观上无法执行", 50.0, details,
                "改用『最坏损失重算』降低此类标的仓位上限，"
                "使不可成交情形下的损失仍在风险预算内",
            )
        return Dimension("B2", "信号有效性", "客观上无法执行", None, details,
                         "复核仓位上限是否已计入不可成交情形")

    if sig_wanted_exit and price_fell:
        verdict, correction, score = "信号正确，未执行是错的", "强化执行机制（考虑系统接管下单权限）", 0.0
    elif sig_wanted_exit and not price_fell:
        verdict, correction, score = "信号偏保守，未执行反而更好", "复核止损宽度与信号阈值是否过紧", 80.0
    elif not sig_wanted_exit and not price_fell:
        verdict, correction, score = "信号正确（看多但价格下跌，需复核）", "复核入场时点", 40.0
    else:
        verdict, correction, score = "信号正确，未执行错过机会", "强化执行机制", 0.0

    return Dimension("B2", "信号有效性", verdict, score, details, correction)


def _b3_discipline(inp: ReviewInput) -> Dimension:
    """B3 纪律扣分：硬性动作未执行 -> 归零。

    但有一个例外：**客观条件不允许时，不构成纪律问题**。
    跌停封板、停牌等情形下，系统接管同样无法成交 ——
    此时把责任归到"纪律"上是错的，应归到"仓位上限设计"。
    """
    if inp.no_exec_reason == NoExecReason.CONDITION_UNMET:
        return Dimension(
            "B3", "纪律扣分", "不适用（客观条件不允许）", None,
            ["跌停封板/停牌等情形下，系统同样无法成交 —— 不构成纪律问题"],
            "改用『最坏损失重算』降低此类标的仓位上限",
        )
    if inp.mandatory_action:
        return Dimension("B3", "纪律扣分", "硬性动作未执行 -> 归零", 0.0,
                         ["该动作属硬性要求（如硬止损），未执行直接归零"],
                         "升级为系统接管下单权限")
    return Dimension("B3", "纪律扣分", "非硬性动作，不扣分", 100.0, [])


def _emotion(inp: ReviewInput) -> Dimension:
    """情绪标签：识别情绪是否主导了操作。"""
    if not inp.emotion_tags:
        return Dimension("E1", "情绪标签", "无记录", None, [],
                         "每次手动加减仓时打标签：贪婪/恐惧/焦虑/不甘/从众")

    consistent_emotions = {"平静"}
    bad = [(d, e, dir_) for d, e, dir_ in inp.emotion_tags if e not in consistent_emotions]
    details = [f"{d} {e} / {dir_}" for d, e, dir_ in inp.emotion_tags]
    ratio = len(bad) / len(inp.emotion_tags)
    if ratio > 0.5:
        return Dimension("E1", "情绪标签", "情绪主导操作", 30.0, details,
                         "建议暂停交易，待情绪平复")
    return Dimension("E1", "情绪标签", "情绪影响可控", 80.0, details)


# ============================================================================
# 主复盘流程
# ============================================================================

def run_review(inp: ReviewInput) -> ReviewReport:
    """执行复盘，按情形走不同维度。"""
    dims: list[Dimension] = [
        _d1_signal_consistency(inp),
        _d2_logic(inp),
        _d3_data_quality(inp),
    ]

    if inp.outcome == Outcome.EXECUTED:
        dims.extend([_a1_execution_deviation(inp), _a2_position_compliance(inp), _a3_cost(inp)])
    else:
        dims.extend([_b1_no_exec_attribution(inp), _b2_signal_validity(inp), _b3_discipline(inp)])

    dims.extend([_d4_hindsight(inp), _emotion(inp)])

    # ---- 归因 ----
    must_take_over = False
    corrections: list[str] = []

    b1 = next((d for d in dims if d.code == "B1"), None)
    a1 = next((d for d in dims if d.code == "A1"), None)
    d2 = next((d for d in dims if d.code == "D2"), None)

    if inp.outcome == Outcome.NOT_EXECUTED and inp.no_exec_reason == NoExecReason.TRIGGERED_NOT_ACTED:
        attribution = "【权限问题】信号已触发但未执行 —— 修正方向是执行机制，不是参数"
        must_take_over = True
    elif inp.outcome == Outcome.NOT_EXECUTED and inp.no_exec_reason == NoExecReason.CONDITION_UNMET:
        attribution = "【设计问题】执行条件不满足 —— 修正方向是执行条件，不是纪律"
    elif inp.outcome == Outcome.NOT_EXECUTED and inp.no_exec_reason == NoExecReason.SIGNAL_NOT_TRIGGERED:
        attribution = "【正常】信号未触发，未操作是正确行为"
    elif inp.outcome == Outcome.EXECUTED and a1 and a1.score is not None and a1.score < 90:
        attribution = "【参数问题】已执行但偏差大 —— 修正方向是下单方式与参数"
    elif d2 and d2.correction:
        attribution = "【逻辑问题】买入假设已证伪 —— 修正方向是逻辑止损，不是价格止损"
    else:
        attribution = "【正常】本轮各维度未见显著异常"

    for d in dims:
        if d.correction:
            corrections.append(f"[{d.code}] {d.correction}")

    # 去重：不同维度可能给出同一修正（如 B2 与 B3 在"条件不满足"情形下
    # 都会指向"降低仓位上限"）。保留首次出现，避免修正清单里同一件事出现两次。
    seen: set[str] = set()
    deduped: list[str] = []
    for c in corrections:
        key = c.split("] ", 1)[-1]
        if key not in seen:
            seen.add(key)
            deduped.append(c)
    corrections = deduped

    if must_take_over:
        corrections.insert(0, "[最高优先] 未执行硬性动作 -> 系统接管下单权限")

    return ReviewReport(inp.symbol, inp.name, inp.outcome, dims, attribution,
                        corrections, must_take_over)


# ============================================================================
# 自检
# ============================================================================

if __name__ == "__main__":
    from qsys.analysis.engine import Advice, AdviceResult, FactorSignal, MonitorLevel, MonitorSignal, QuantAction, QuantSignal

    print("=" * 74)
    print("复盘模块 · 自检")
    print("=" * 74)

    quant = QuantSignal(QuantAction.BLOCK, "trend", "趋势级全部空头", 38.42, "structure", 8.18, 3.64)
    factor = FactorSignal({}, 66.2, "Q3", 66.2)
    monitors = [MonitorSignal(MonitorLevel.WARNING, "财报临近披露", "3 天", "禁止新增仓位")]
    advice = AdviceResult(Advice.NO_ENTRY, 0.0, "量化信号（优先级 ②）", [], ["趋势级全部空头"])

    base = dict(
        symbol="002422", name="科伦药业", review_date=date(2026, 10, 8),
        advice=advice, quant=quant, factor=factor, monitors=monitors,
        planned_price=40.00, planned_date=date(2026, 9, 29),
        hypotheses=[
            ("科伦博泰 ADC 全球放量", "falsified"),
            ("传统输液业务企稳", "verified"),
            ("扣非利润改善", "falsified"),
        ],
        data_gap_count=2,
        emotion_tags=[(date(2026, 8, 24), "不甘", "hold"), (date(2026, 9, 24), "焦虑", "hold")],
        price_t20=38.60,
    )

    print("\n【情形 A】已执行（止损 40 元，实际 40.15）")
    print("─" * 74)
    a = ReviewInput(**base, outcome=Outcome.EXECUTED,
                    actual_price=40.15, planned_qty=10000, actual_qty=5000,
                    actual_date=date(2026, 9, 30), price_t5=40.85, mandatory_action=True)
    print(run_review(a).explain())

    print("\n【情形 B-1】未操作 · 信号已触发但未执行（纪律问题）")
    print("─" * 74)
    b1 = ReviewInput(**base, outcome=Outcome.NOT_EXECUTED,
                     no_exec_reason=NoExecReason.TRIGGERED_NOT_ACTED,
                     no_exec_note="看好创新药长线逻辑", mandatory_action=True)
    print(run_review(b1).explain())

    print("\n【情形 B-2】未操作 · 信号未触发（正常）")
    print("─" * 74)
    b2 = ReviewInput(**base, outcome=Outcome.NOT_EXECUTED,
                     no_exec_reason=NoExecReason.SIGNAL_NOT_TRIGGERED, mandatory_action=False)
    print(run_review(b2).explain())

    print("\n【情形 B-3】未操作 · 执行条件不满足（跌停无法卖出）")
    print("─" * 74)
    b3 = ReviewInput(**base, outcome=Outcome.NOT_EXECUTED,
                     no_exec_reason=NoExecReason.CONDITION_UNMET,
                     no_exec_note="连续跌停无法成交", mandatory_action=True)
    print(run_review(b3).explain())

    print()
    print("=" * 74)
    print("三种未执行情形的归因对比（修正方向完全不同）")
    print("=" * 74)
    for r, label in [(b1, "触发了没执行"), (b2, "信号未触发"), (b3, "条件不满足")]:
        rep = run_review(r)
        print(f"  {label:<14} -> {rep.attribution}")
