"""
三类信号与操作建议映射引擎
==========================

这是整个系统的中枢。核心问题不是"如何生成信号"，而是
**当三类信号互相矛盾时，听谁的。**

三类信号的分工（互不重叠）
--------------------------
  监控信号 MonitorSignal —— 回答"有什么变化需要我知道"
      性质：事件驱动 / 阈值告警。**不直接产生买卖动作**，只改变观察状态。
      来源：财报日历、公告、资金异动、数据缺口、规则变更。

  量化信号 QuantSignal   —— 回答"规则说该做什么"
      性质：可执行、有明确价位。由确定性代码产生，无解释空间。
      来源：qsys.signals（三级信号）+ qsys.risk（风控）。

  多因子信号 FactorSignal —— 回答"这只股票在同类里排第几"
      性质：相对比较，不是绝对判断。**只影响仓位大小，不影响方向。**
      来源：四个分析模块的得分合成。

优先级（决定权从高到低）
------------------------
  ① 逻辑止损（基本面证伪）   —— 最高。原因不成立时，价格止损没有意义
  ② 量化信号 BLOCK / EXIT   —— 规则否决
  ③ 监控信号 critical       —— 暂缓一切加仓动作
  ④ 多因子分层              —— 决定仓位大小
  ⑤ 量化信号 ENTRY          —— 触发建仓

为什么是这个顺序
----------------
- 逻辑止损排第一：它回答"我为什么买它还成立吗"，是原因层；
  其他信号都在回答"现在该做什么"，是动作层。原因不成立时，动作层无意义。
- 量化信号排第二：它是确定性的，不含主观判断，必须优先于需要解释的信号。
- 多因子排第四（而非第二）：因为它衡量的是**相对**位置，
  一家排第 10% 的公司也可能处在下跌趋势中。相对好 ≠ 现在该买。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Optional

from qsys.analysis.modules import AnalysisBundle


# ============================================================================
# 一、监控信号
# ============================================================================

class MonitorLevel(str, Enum):
    INFO = "info"          # 仅记录
    WARNING = "warning"    # 进入观察，禁止加仓
    CRITICAL = "critical"  # 暂缓一切动作


@dataclass
class MonitorSignal:
    """监控信号。

    **不直接产生买卖动作。** 它的作用是改变"观察状态"，
    进而影响量化信号能否被执行（见映射矩阵）。
    """

    level: MonitorLevel
    trigger: str
    window: str
    implication: str
    source: str = ""

    def __str__(self) -> str:
        return f"[{self.level.value:<8}] {self.trigger:<24} {self.implication}"


def generate_monitor_signals(
    *,
    days_to_next_report: Optional[int] = None,
    data_gap_count: int = 0,
    caveat_count: int = 0,
    crowding_pending: bool = False,
    logic_falsified_count: int = 0,
    price_limit_proximity_pct: Optional[float] = None,
    suspension: bool = False,
) -> list[MonitorSignal]:
    """从事件与状态生成监控信号。

    注意：每个信号都必须带 window（时间窗），否则它无法被排期执行。
    """
    sigs: list[MonitorSignal] = []

    if suspension:
        sigs.append(MonitorSignal(
            MonitorLevel.CRITICAL, "标的停牌", "即时",
            "停牌期间无法交易；停牌日不计入监控计时；复牌首日不适用常规止损",
            "交易所公告",
        ))

    if logic_falsified_count > 0:
        sigs.append(MonitorSignal(
            MonitorLevel.CRITICAL, "买入假设被证伪", "即时",
            f"{logic_falsified_count} 条核心假设已证伪 —— 触发逻辑止损，优先级高于价格止损",
            "逻辑验证",
        ))

    if days_to_next_report is not None and days_to_next_report <= 5:
        lvl = MonitorLevel.WARNING if days_to_next_report > 1 else MonitorLevel.CRITICAL
        sigs.append(MonitorSignal(
            lvl, "财报临近披露", f"{days_to_next_report} 天",
            "披露前禁止新增仓位；披露后 1 个交易日内完成逻辑复核",
            "财报日历",
        ))

    if price_limit_proximity_pct is not None and price_limit_proximity_pct < 1.5:
        sigs.append(MonitorSignal(
            MonitorLevel.WARNING, "接近涨跌停", "当日",
            f"距涨跌停仅 {price_limit_proximity_pct:.2f}% —— "
            f"止损可能无法成交，实际损失将超过止损宽度",
            "制度约束引擎",
        ))

    if data_gap_count > 0:
        sigs.append(MonitorSignal(
            MonitorLevel.WARNING, "存在数据缺口", "本轮",
            f"{data_gap_count} 项字段缺失 —— 结论须标注缺口，缺口字段不得进入决策",
            "数据层",
        ))

    if crowding_pending:
        sigs.append(MonitorSignal(
            MonitorLevel.INFO, "拥挤度阈值待校准", "长期",
            "该维度暂不出结论（原阈值来自单样本，已撤回）",
            "参数校准",
        ))

    if caveat_count > 0:
        sigs.append(MonitorSignal(
            MonitorLevel.INFO, "分析含警示项", "本轮",
            f"{caveat_count} 条警示，详见研报；引用前需核实原文",
            "分析层",
        ))

    return sigs


def max_monitor_level(sigs: list[MonitorSignal]) -> MonitorLevel:
    order = {MonitorLevel.INFO: 0, MonitorLevel.WARNING: 1, MonitorLevel.CRITICAL: 2}
    if not sigs:
        return MonitorLevel.INFO
    return max((s.level for s in sigs), key=lambda x: order[x])


# ============================================================================
# 二、量化信号
# ============================================================================

class QuantAction(str, Enum):
    ENTRY = "entry"
    WAIT = "wait"
    BLOCK = "block"
    EXIT = "exit"


@dataclass
class QuantSignal:
    """量化信号。可执行、有明确价位。"""

    action: QuantAction
    tier_decided: str
    reason: str
    stop_price: Optional[float] = None
    effective_stop_type: Optional[str] = None
    standard_position_pct: Optional[float] = None
    worst_case_position_pct: Optional[float] = None
    warnings: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return (f"[quant] {self.action.value.upper():<6} 止损 {self.stop_price}  "
                f"标准仓位 {self.standard_position_pct}% / 制度修正 {self.worst_case_position_pct}%")


# ============================================================================
# 三、多因子信号
# ============================================================================

@dataclass
class FactorSignal:
    """多因子信号。相对比较，只影响仓位大小。"""

    factor_scores: dict[str, float]
    composite: float
    layer: str          # Q1–Q5
    percentile: float
    contributions: dict[str, float] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"[factor] 综合 {self.composite:.1f}  {self.layer}（{self.percentile:.0f}% 分位）"


def build_factor_signal(bundle: AnalysisBundle, peer_composites: Optional[list[float]] = None) -> FactorSignal:
    """由四个分析模块合成多因子信号。

    分层规则（相对同类）：
      Q1 前 20%  · Q2 20–40% · Q3 40–60% · Q4 60–80% · Q5 后 20%

    未提供同业样本时，用绝对分位映射（明确标注为近似）。
    """
    comp = bundle.composite()
    scores = {
        "研报": bundle.research.score,
        "行业": bundle.industry.score,
        "现金流": bundle.cashflow.score,
        "壁垒": bundle.moat.score,
    }
    w = {"研报": 0.20, "行业": 0.25, "现金流": 0.30, "壁垒": 0.25}
    contributions = {k: round(v * w[k], 2) for k, v in scores.items()}

    if peer_composites:
        below = sum(1 for p in peer_composites if p <= comp)
        pct = below / len(peer_composites) * 100
    else:
        pct = comp  # 近似：以绝对分当分位，需用同业样本替换

    if pct >= 80:
        layer = "Q1"
    elif pct >= 60:
        layer = "Q2"
    elif pct >= 40:
        layer = "Q3"
    elif pct >= 20:
        layer = "Q4"
    else:
        layer = "Q5"

    return FactorSignal(scores, comp, layer, round(pct, 1), contributions)


# ============================================================================
# 四、映射引擎
# ============================================================================

class Advice(str, Enum):
    NO_ENTRY = "不入场"
    OBSERVE = "观察"
    PROBE = "试仓"
    BUILD = "建仓"
    BUILD_HALF = "建仓（半仓）"
    SUSPEND = "暂缓建仓"
    HOLD = "持有"
    REDUCE = "降仓"
    CLEAR = "清仓"


@dataclass
class AdviceResult:
    advice: Advice
    position_pct: float
    decided_by: str
    chain: list[str] = field(default_factory=list)
    blocked_reasons: list[str] = field(default_factory=list)

    def explain(self) -> str:
        lines = [f"操作建议: {self.advice.value}   建议仓位 {self.position_pct:.2f}%",
                 f"决定者: {self.decided_by}"]
        if self.blocked_reasons:
            lines.append("否决/约束:")
            for b in self.blocked_reasons:
                lines.append(f"  ✗ {b}")
        lines.append("判定链:")
        for c in self.chain:
            lines.append(f"  {c}")
        return "\n".join(lines)


class AdviceEngine:
    """三类信号 -> 操作建议 的映射引擎。

    严格按优先级判定，每一步都记录进 chain，便于事后审计
    "这个建议是基于什么得出的"。
    """

    def __init__(
        self,
        standard_position_pct: float = 0.0,
        worst_case_position_pct: float = 0.0,
    ) -> None:
        self.standard_position_pct = standard_position_pct
        self.worst_case_position_pct = worst_case_position_pct

    def decide(
        self,
        quant: QuantSignal,
        factor: FactorSignal,
        monitors: list[MonitorSignal],
        *,
        holding: bool = False,
        logic_falsified: int = 0,
    ) -> AdviceResult:
        chain: list[str] = []
        blocked: list[str] = []
        mlevel = max_monitor_level(monitors)
        chain.append(f"输入: quant={quant.action.value} factor={factor.layer} "
                     f"monitor={mlevel.value} holding={holding}")

        # ---- 优先级 ①：逻辑止损 ----
        if logic_falsified > 0:
            chain.append("① 逻辑止损触发 —— 优先级最高，跳过其余判定")
            return AdviceResult(
                Advice.CLEAR if logic_falsified >= 2 else Advice.REDUCE,
                0.0 if logic_falsified >= 2 else self.worst_case_position_pct / 2,
                "逻辑止损（优先级 ①）",
                chain,
                [f"{logic_falsified} 条买入假设被证伪"],
            )
        chain.append("① 逻辑止损：未触发")

        # ---- 持仓分支 ----
        if holding:
            if quant.action == QuantAction.EXIT:
                chain.append("② 量化 EXIT —— 清仓")
                return AdviceResult(Advice.CLEAR, 0.0, "量化信号（优先级 ②）", chain)
            if quant.action == QuantAction.BLOCK:
                chain.append("② 量化 BLOCK —— 持仓状态下减至观察仓")
                return AdviceResult(
                    Advice.REDUCE, self.worst_case_position_pct / 3,
                    "量化信号（优先级 ②）", chain,
                )
            if mlevel == MonitorLevel.CRITICAL:
                chain.append("③ 监控 CRITICAL —— 转入风控观察，禁止加仓")
                return AdviceResult(
                    Advice.HOLD, self.worst_case_position_pct,
                    "监控信号（优先级 ③）", chain,
                    ["监控 CRITICAL：禁止一切加仓动作"],
                )
            if factor.layer in ("Q4", "Q5"):
                chain.append(f"④ 因子分层 {factor.layer} —— 相对位置落后，降仓 1/3")
                return AdviceResult(
                    Advice.REDUCE, round(self.worst_case_position_pct * 2 / 3, 2),
                    "多因子信号（优先级 ④）", chain,
                )
            chain.append("④ 因子分层可接受 —— 持有")
            return AdviceResult(Advice.HOLD, self.worst_case_position_pct, "维持", chain)

        # ---- 空仓分支 ----
        if quant.action == QuantAction.BLOCK:
            chain.append("② 量化 BLOCK —— 不入场")
            return AdviceResult(
                Advice.NO_ENTRY, 0.0, "量化信号（优先级 ②）", chain,
                [quant.reason],
            )
        if quant.action == QuantAction.WAIT:
            chain.append("② 量化 WAIT —— 观察")
            return AdviceResult(Advice.OBSERVE, 0.0, "量化信号（优先级 ②）", chain,
                                [quant.reason])
        chain.append("② 量化 ENTRY —— 通过，进入仓位判定")

        # ---- 优先级 ③：监控 critical 暂缓 ----
        if mlevel == MonitorLevel.CRITICAL:
            chain.append("③ 监控 CRITICAL —— 暂缓建仓")
            return AdviceResult(
                Advice.SUSPEND, 0.0, "监控信号（优先级 ③）", chain,
                [s.trigger for s in monitors if s.level == MonitorLevel.CRITICAL],
            )
        chain.append("③ 监控：无 CRITICAL")

        # ---- 优先级 ④：因子分层决定仓位 ----
        base = self.worst_case_position_pct
        if factor.layer in ("Q1", "Q2"):
            pct, note = base, f"④ 因子 {factor.layer}（前 40%）—— 标准仓位"
        elif factor.layer == "Q3":
            pct, note = round(base / 4, 2), f"④ 因子 {factor.layer}（中位）—— 试仓 1/4"
        else:
            chain.append(f"④ 因子 {factor.layer}（后 40%）—— 因子不支持，不入场")
            return AdviceResult(
                Advice.NO_ENTRY, 0.0, "多因子信号（优先级 ④）", chain,
                [f"因子分层 {factor.layer} 位于后 40%，即使量化信号 ENTRY 也不入场"],
            )
        chain.append(note)

        # ---- 优先级 ⑤：监控 warning 减半 ----
        if mlevel == MonitorLevel.WARNING:
            pct = round(pct / 2, 2)
            chain.append("⑤ 监控 WARNING —— 仓位减半")
            blocked.append("监控 WARNING：存在需观察的事件")
            return AdviceResult(
                Advice.BUILD_HALF if factor.layer in ("Q1", "Q2") else Advice.PROBE,
                pct, "监控信号（优先级 ⑤ 降级）", chain, blocked,
            )

        chain.append("⑤ 监控 INFO —— 不降级")
        advice = Advice.BUILD if factor.layer in ("Q1", "Q2") else Advice.PROBE
        return AdviceResult(advice, pct, "量化 + 因子（优先级 ②④）", chain)


# ============================================================================
# 自检
# ============================================================================

if __name__ == "__main__":
    from qsys.analysis.modules import (
        CashFlowInput, CashFlowModule, IndustryInput, IndustryModule,
        MoatInput, MoatModule, ResearchInput, ResearchModule,
    )

    def make_bundle(comp_target: str) -> AnalysisBundle:
        """构造不同质量档次的标的，用于验证映射矩阵。"""
        if comp_target == "good":
            return AnalysisBundle(
                "600XXX", "优质标的",
                ResearchModule().analyze(ResearchInput(52.0, 60.0, 45.0, 50.0, 20, 85.0, 15.0, 0.0,
                                                       {"2026E": 2.0}, [], True)),
                IndustryModule().analyze(IndustryInput("成长行业", "成长", 25.0, "up", "medium", "龙头", 12.0, "in")),
                CashFlowModule().analyze(CashFlowInput(30.0, [20.0, 25.0, 30.0], 20.0, 10.0, 10.0, 30.0, 10.0, 100.0)),
                MoatModule().analyze(MoatInput("技术专利", "核心专利族", 8, 20.0, 9.0, 12.0, "高于", 1, ["专利 50 项"])),
            )
        return AnalysisBundle(
            "002422", "科伦药业",
            ResearchModule().analyze(ResearchInput(52.10, 52.10, 52.10, 39.26, 29, 100.0, 0.0, 0.0,
                                                   {"2026E": 1.23, "2027E": 1.55}, [], True)),
            IndustryModule().analyze(IndustryInput("医药生物", "成长", 3.5, "down", "high", "细分龙头", -6.5, "out")),
            CashFlowModule().analyze(CashFlowInput(23.41, [11.90, 26.42, 23.41], 11.28, 23.90, 44.18, 61.58, 30.0, 88.39)),
            MoatModule().analyze(MoatInput("技术专利", "ADC 平台与默沙东绑定", 5, 5.94, 9.0, 13.7, "高于", 2,
                                           ["全球首个肺癌获批 TROP2 ADC", "17 项全球 III 期"])),
        )

    print("=" * 78)
    print("三类信号与映射引擎 · 自检")
    print("=" * 78)

    for tag, label in [("good", "优质标的"), ("kelun", "科伦药业")]:
        b = make_bundle(tag)
        f = build_factor_signal(b)
        print()
        print("─" * 78)
        print(f"【{label}】{b.summary().splitlines()[0]}")
        print("─" * 78)
        print(f"  多因子信号: {f}")
        print(f"    因子贡献: {f.contributions}")

    print()
    print("=" * 78)
    print("映射矩阵验证：同一组量化信号，不同因子分层/监控级别下的建议")
    print("=" * 78)

    quant_entry = QuantSignal(QuantAction.ENTRY, "momentum", "趋势与动量一致向上",
                              38.42, "structure", 8.18, 3.64)
    quant_block = QuantSignal(QuantAction.BLOCK, "trend", "趋势级全部空头", 38.42, "structure", 8.18, 3.64)

    eng = AdviceEngine(worst_case_position_pct=3.64)

    cases = [
        ("量化 ENTRY + 因子 Q1 + 监控 INFO", quant_entry, "Q1", [], False),
        ("量化 ENTRY + 因子 Q2 + 监控 INFO", quant_entry, "Q2", [], False),
        ("量化 ENTRY + 因子 Q3 + 监控 INFO", quant_entry, "Q3", [], False),
        ("量化 ENTRY + 因子 Q4 + 监控 INFO", quant_entry, "Q4", [], False),
        ("量化 ENTRY + 因子 Q1 + 监控 WARNING", quant_entry, "Q1",
         [MonitorSignal(MonitorLevel.WARNING, "财报临近", "3 天", "禁止新增仓位")], False),
        ("量化 ENTRY + 因子 Q1 + 监控 CRITICAL", quant_entry, "Q1",
         [MonitorSignal(MonitorLevel.CRITICAL, "假设被证伪", "即时", "触发逻辑止损")], False),
        ("量化 BLOCK + 因子 Q1（因子好也没用）", quant_block, "Q1", [], False),
        ("量化 ENTRY + 因子 Q1 + 逻辑证伪 1 条", quant_entry, "Q1", [], False),
        ("【持仓】量化 BLOCK", quant_block, "Q1", [], True),
        ("【持仓】因子跌至 Q5", quant_entry, "Q5", [], True),
    ]

    for label, q, layer, mons, holding in cases:
        f = FactorSignal({}, 70.0, layer, 70.0)
        lf = 1 if "逻辑证伪" in label else 0
        r = eng.decide(q, f, mons, holding=holding, logic_falsified=lf)
        print()
        print(f"● {label}")
        print(f"  -> {r.advice.value}  仓位 {r.position_pct:.2f}%  （决定者：{r.decided_by}）")
        for b in r.blocked_reasons:
            print(f"     ✗ {b}")

    print()
    print("=" * 78)
    print("完整判定链演示（可审计）")
    print("=" * 78)
    f = FactorSignal({}, 70.0, "Q1", 70.0)
    r = eng.decide(quant_entry, f, [
        MonitorSignal(MonitorLevel.WARNING, "财报临近披露", "3 天", "禁止新增仓位"),
    ])
    print(r.explain())
