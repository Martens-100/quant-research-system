"""
四个分析模块：研报 / 行业 / 现金流 / 核心壁垒
==============================================

统一输出结构
------------
四个模块全部返回 ModuleOutput，字段固定：

    module      模块名
    score       0–100 分
    grade       等级（A/B/C/D 或 宽/窄/无）
    fields      结构化字段（供研报与信号层消费）
    findings    关键发现（人读）
    gaps        数据缺口（必须显式列出，不得静默）
    caveats     警示项（如"研报为非一手来源，需核实原文"）

统一结构的目的：让信号层可以对四个模块做**同构处理**，
而不必为每个模块写一套适配逻辑。

设计取向
--------
每个模块都必须能回答一个**可证伪的问题**，否则它只是资料堆砌：

    研报模块   -> 市场已经定价了什么？预期差在哪？
    行业模块   -> 这家公司在它自己的赛道里排第几？
    现金流模块 -> 利润是真的吗？
    壁垒模块   -> 说出壁垒是什么，说不出就是没有
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# ============================================================================
# 统一输出结构
# ============================================================================

@dataclass
class ModuleOutput:
    """所有分析模块的统一输出。"""

    module: str
    score: float
    grade: str
    fields: dict[str, Any] = field(default_factory=dict)
    findings: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)

    def is_usable(self) -> bool:
        """数据缺口是否多到无法得出结论。

        规则：缺口数 >= 关键字段数的一半时，拒绝给出评分。
        宁可说"数据不足"，也不给一个基于半截数据的分数。
        """
        if not self.fields:
            return False
        return len(self.gaps) < max(1, len(self.fields) // 2)

    def summary(self) -> str:
        s = f"{self.module}: {self.score:.0f}/100 ({self.grade})"
        if self.gaps:
            s += f"  [缺口 {len(self.gaps)}]"
        if self.caveats:
            s += f"  [警示 {len(self.caveats)}]"
        return s


# ============================================================================
# 模块 A · 研报分析
# ============================================================================

@dataclass
class ResearchInput:
    target_price_mean: Optional[float] = None
    target_price_high: Optional[float] = None
    target_price_low: Optional[float] = None
    current_price: Optional[float] = None
    coverage_count: Optional[int] = None
    rating_buy_pct: Optional[float] = None
    rating_hold_pct: Optional[float] = None
    rating_sell_pct: Optional[float] = None
    eps_forecast: dict[str, float] = field(default_factory=dict)
    recent_reports: list[dict[str, str]] = field(default_factory=list)
    has_site_visit: bool = False


class ResearchModule:
    """研报分析模块。

    核心问题：**市场已经定价了什么，预期差在哪？**

    两条必须写进输出的警示：
      1. 研报属**非一手来源**，引用需核实原文，不得与公司公告同等处理。
      2. 卖方评级存在**系统性乐观偏差** —— 若买入占比接近 100%，
         该信号本身已失去区分度（科伦药业实测：近 6 个月买入/增持占比 100%）。
    """

    name = "研报分析"

    def analyze(self, inp: ResearchInput) -> ModuleOutput:
        fields: dict[str, Any] = {}
        findings: list[str] = []
        gaps: list[str] = []
        caveats: list[str] = []

        caveats.append("研报为非一手来源，引用需核实原文，不得与公司公告同等处理")

        # ---- 一致预期与上行空间 ----
        if inp.target_price_mean is not None and inp.current_price:
            upside = (inp.target_price_mean / inp.current_price - 1) * 100
            fields["target_price_mean"] = inp.target_price_mean
            fields["upside_pct"] = round(upside, 2)
            findings.append(
                f"机构一致目标价 {inp.target_price_mean:.2f} 元，"
                f"较现价隐含 {upside:+.2f}% 空间"
            )
        else:
            gaps.append("缺一致目标价或现价")

        # ---- 卖方分歧度 ----
        if None not in (inp.target_price_high, inp.target_price_low, inp.target_price_mean):
            spread = (inp.target_price_high - inp.target_price_low) / inp.target_price_mean * 100
            fields["target_spread_pct"] = round(spread, 2)
            if spread > 80:
                findings.append(
                    f"目标价分歧极大（最高 {inp.target_price_high:.2f} / "
                    f"最低 {inp.target_price_low:.2f}，跨度 {spread:.0f}%）"
                    f"—— 说明卖方对基本面判断未收敛，一致预期不可靠"
                )
            else:
                findings.append(f"目标价跨度 {spread:.0f}%，卖方判断相对收敛")
        else:
            gaps.append("缺目标价上下限，无法评估分歧度")

        # ---- 评级分布与乐观偏差 ----
        if inp.rating_buy_pct is not None:
            fields["rating_buy_pct"] = inp.rating_buy_pct
            if inp.rating_buy_pct >= 95:
                caveats.append(
                    f"买入/增持占比 {inp.rating_buy_pct:.0f}%，接近全体一致。"
                    f"该信号已失去区分度 —— 当所有人都看多时，'看多'不携带信息"
                )
            findings.append(f"近 6 个月买入/增持占比 {inp.rating_buy_pct:.0f}%")
        else:
            gaps.append("缺评级分布")

        # ---- 覆盖广度 ----
        if inp.coverage_count is not None:
            fields["coverage_count"] = inp.coverage_count
            if inp.coverage_count < 5:
                caveats.append(f"仅 {inp.coverage_count} 家机构覆盖，样本过小，一致预期代表性弱")
        else:
            gaps.append("缺覆盖机构数")

        # ---- 盈利预测 ----
        if inp.eps_forecast:
            fields["eps_forecast"] = inp.eps_forecast
            yrs = sorted(inp.eps_forecast.keys())
            if len(yrs) >= 2:
                g = (inp.eps_forecast[yrs[-1]] / inp.eps_forecast[yrs[0]]) ** (1 / (len(yrs) - 1)) - 1
                fields["eps_cagr_pct"] = round(g * 100, 2)
                findings.append(f"预测 EPS 年化增速 {g * 100:.1f}%")
        else:
            gaps.append("缺盈利预测")

        # ---- 一手数据 ----
        fields["has_site_visit"] = inp.has_site_visit
        if not inp.has_site_visit:
            caveats.append("未见机构调研记录，研报可能基于公开信息推演而非一手验证")

        # ---- 评分 ----
        score = self._score(inp, gaps)
        grade = "A" if score >= 80 else "B" if score >= 60 else "C" if score >= 40 else "D"

        return ModuleOutput(self.name, score, grade, fields, findings, gaps, caveats)

    @staticmethod
    def _score(inp: ResearchInput, gaps: list[str]) -> float:
        """研报分。

        注意：本分数衡量的是**研报信息的可用性**，不是公司的好坏。
        覆盖广 + 分歧小 + 有一手数据 = 信息可用性高。

        但有一个反向项：**评级过度一致会降低信息量**。
        当买入占比 >= 95% 时，'看多'不再携带区分度 ——
        你不知道卖方是真的看好，还是不敢给负面评级。
        """
        if len(gaps) >= 4:
            return 0.0
        s = 50.0
        if inp.coverage_count:
            s += min(20.0, inp.coverage_count * 0.7)
        if inp.has_site_visit:
            s += 15.0
        if inp.target_price_mean and inp.current_price:
            s += 10.0
        if inp.rating_buy_pct is not None:
            if inp.rating_buy_pct >= 95:
                s -= 10.0   # 全体一致 -> 信息量为零
            else:
                s += 5.0    # 存在分歧 -> 分歧本身是有效信息
        return round(max(0.0, min(100.0, s)), 1)


# ============================================================================
# 模块 B · 所属行业
# ============================================================================

@dataclass
class IndustryInput:
    industry: Optional[str] = None
    lifecycle_stage: Optional[str] = None      # 导入/成长/成熟/下行
    industry_revenue_growth: Optional[float] = None   # 行业中位营收增速 %
    industry_margin_trend: Optional[str] = None       # up/flat/down
    competitive_intensity: Optional[str] = None       # low/medium/high
    company_position: Optional[str] = None     # leader/sub_leader/challenger/marginal
    relative_strength_60d: Optional[float] = None     # 相对行业指数强弱 %
    sector_flow: Optional[str] = None          # 板块资金合力 in/out/neutral


class IndustryModule:
    """行业分析模块。

    核心问题：**这家公司在它自己的赛道里排第几？**

    为什么不能跳过这一步：同一个财务指标在不同行业含义完全不同。
    毛利率 47% 在医药是常态，在零售是异常。脱离行业谈估值是无效的。
    """

    name = "行业分析"

    STAGE_SCORE = {"导入": 60, "成长": 90, "成熟": 65, "下行": 25}
    POSITION_SCORE = {"龙头": 95, "细分龙头": 80, "追赶者": 55, "边缘": 25}
    INTENSITY_SCORE = {"low": 85, "medium": 65, "high": 40}
    MARGIN_SCORE = {"up": 85, "flat": 60, "down": 30}

    def analyze(self, inp: IndustryInput) -> ModuleOutput:
        fields: dict[str, Any] = {}
        findings: list[str] = []
        gaps: list[str] = []
        caveats: list[str] = []

        for label, val in [
            ("行业名称", inp.industry),
            ("生命周期阶段", inp.lifecycle_stage),
            ("竞争强度", inp.competitive_intensity),
            ("公司位置", inp.company_position),
        ]:
            if val is None:
                gaps.append(f"缺{label}")
            else:
                fields[label] = val

        if inp.industry_revenue_growth is not None:
            fields["行业中位营收增速"] = inp.industry_revenue_growth
        else:
            gaps.append("缺行业中位营收增速")

        if inp.industry_margin_trend is not None:
            fields["行业毛利率趋势"] = inp.industry_margin_trend
        else:
            gaps.append("缺行业毛利率趋势")

        if inp.relative_strength_60d is not None:
            fields["相对行业强弱60日"] = inp.relative_strength_60d
            if inp.relative_strength_60d > 10:
                findings.append(f"近 60 日跑赢行业 {inp.relative_strength_60d:+.1f}%")
            elif inp.relative_strength_60d < -10:
                findings.append(f"近 60 日跑输行业 {inp.relative_strength_60d:+.1f}%")

        if inp.sector_flow:
            fields["板块资金"] = inp.sector_flow

        # ---- 关键发现 ----
        if inp.lifecycle_stage == "下行":
            findings.append("行业处下行阶段 —— 逆势标的的胜率显著低于顺周期")
        if inp.competitive_intensity == "high":
            findings.append("竞争强度高 —— 需要壁垒模块证明公司能维持超额收益")
        if inp.company_position == "边缘":
            findings.append("公司处行业边缘位置 —— 难以享受行业红利")

        if inp.industry is None:
            caveats.append("行业未识别，本模块结论不成立（同一指标跨行业不可比）")

        score = self._score(inp, gaps)
        grade = "A" if score >= 80 else "B" if score >= 60 else "C" if score >= 40 else "D"
        return ModuleOutput(self.name, score, grade, fields, findings, gaps, caveats)

    def _score(self, inp: IndustryInput, gaps: list[str]) -> float:
        if len(gaps) >= 5:
            return 0.0
        parts = [
            self.STAGE_SCORE.get(inp.lifecycle_stage or "", 50),
            self.POSITION_SCORE.get(inp.company_position or "", 50),
            self.INTENSITY_SCORE.get(inp.competitive_intensity or "", 50),
            self.MARGIN_SCORE.get(inp.industry_margin_trend or "", 50),
        ]
        base = sum(parts) / len(parts)
        if inp.industry_revenue_growth is not None:
            base += max(-15, min(15, inp.industry_revenue_growth * 0.5))
        return round(max(0.0, min(100.0, base)), 1)


# ============================================================================
# 模块 C · 现金流
# ============================================================================

@dataclass
class CashFlowInput:
    ocf: Optional[float] = None                 # 经营现金流净额
    ocf_series: list[float] = field(default_factory=list)
    net_profit: Optional[float] = None          # 归母净利
    capex: Optional[float] = None               # 资本开支
    interest_bearing_debt: Optional[float] = None
    cash: Optional[float] = None
    receivables: Optional[float] = None
    revenue: Optional[float] = None


class CashFlowModule:
    """现金流分析模块。

    核心问题：**利润是真的吗？**

    这是四个模块里最能识别"纸面利润"的一个。
    实证：科伦药业 2026H1 归母净利 11.28 亿元（+12.71%），
    但扣非仅 6.99 亿元（−29.03%）—— 只看利润表会得出相反结论。
    """

    name = "现金流分析"

    def analyze(self, inp: CashFlowInput) -> ModuleOutput:
        fields: dict[str, Any] = {}
        findings: list[str] = []
        gaps: list[str] = []
        caveats: list[str] = []

        # ---- 现金含量 ----
        if inp.ocf is not None and inp.net_profit:
            ratio = inp.ocf / inp.net_profit
            fields["现金含量"] = round(ratio, 2)
            if ratio < 0.8:
                findings.append(
                    f"经营现金流/归母净利 = {ratio:.2f}（<0.8）—— "
                    f"利润未有效转化为现金，需查应收与存货"
                )
            else:
                findings.append(f"经营现金流/归母净利 = {ratio:.2f}，利润含金量正常")
        else:
            gaps.append("缺经营现金流或归母净利")

        # ---- 自由现金流 ----
        if inp.ocf is not None and inp.capex is not None:
            fcf = inp.ocf - inp.capex
            fields["自由现金流"] = round(fcf, 2)
            if fcf < 0:
                findings.append(
                    f"自由现金流为负（{fcf:.2f}）—— 扩张期特征，"
                    f"但需确认资本开支能形成未来收益而非沉没成本"
                )
            else:
                findings.append(f"自由现金流为正（{fcf:.2f}）")
        else:
            gaps.append("缺资本开支")

        # ---- 现金流趋势 ----
        if len(inp.ocf_series) >= 3:
            fields["经营现金流序列"] = inp.ocf_series
            if all(inp.ocf_series[i] < inp.ocf_series[i + 1] for i in range(len(inp.ocf_series) - 1)):
                findings.append("经营现金流连续改善")
            elif all(inp.ocf_series[i] > inp.ocf_series[i + 1] for i in range(len(inp.ocf_series) - 1)):
                findings.append("经营现金流连续恶化")
        else:
            gaps.append("经营现金流序列不足 3 期")

        # ---- 偿债能力 ----
        if inp.interest_bearing_debt is not None and inp.ocf:
            yrs = inp.interest_bearing_debt / inp.ocf
            fields["有息负债/经营现金流"] = round(yrs, 2)
            if yrs > 3:
                findings.append(f"有息负债需 {yrs:.1f} 年经营现金流偿还，偿债压力偏高")
            else:
                findings.append(f"有息负债需 {yrs:.1f} 年经营现金流偿还")
        else:
            gaps.append("缺有息负债")

        # ---- 营运资本占用 ----
        if inp.receivables is not None and inp.revenue:
            r = inp.receivables / inp.revenue * 100
            fields["应收/营收"] = round(r, 2)
            if r > 50:
                findings.append(f"应收占营收 {r:.1f}%，回款压力大 —— 增长可能靠放宽账期换取")

        score = self._score(inp, gaps)
        grade = "A" if score >= 80 else "B" if score >= 60 else "C" if score >= 40 else "D"
        return ModuleOutput(self.name, score, grade, fields, findings, gaps, caveats)

    @staticmethod
    def _score(inp: CashFlowInput, gaps: list[str]) -> float:
        if len(gaps) >= 4:
            return 0.0
        s = 50.0
        if inp.ocf is not None and inp.net_profit:
            ratio = inp.ocf / inp.net_profit
            s += max(-25.0, min(25.0, (ratio - 0.8) * 50))
        if inp.ocf is not None and inp.capex is not None:
            s += 15.0 if (inp.ocf - inp.capex) > 0 else -10.0
        if inp.interest_bearing_debt is not None and inp.ocf:
            yrs = inp.interest_bearing_debt / inp.ocf
            s += 10.0 if yrs < 2 else (-10.0 if yrs > 4 else 0.0)
        return round(max(0.0, min(100.0, s)), 1)


# ============================================================================
# 模块 D · 核心技术壁垒
# ============================================================================

@dataclass
class MoatInput:
    moat_type: Optional[str] = None
    """壁垒类型：技术专利/规模成本/网络效应/牌照/品牌/客户粘性/无"""
    moat_source: Optional[str] = None
    """具体来源。**必须可命名** —— 说不出就不算壁垒。"""
    durability_years: Optional[int] = None
    roic: Optional[float] = None
    wacc: Optional[float] = None
    rd_expense_ratio: Optional[float] = None
    rd_ratio_vs_peers: Optional[float] = None   # 高于/持平/低于同业
    substitute_count: Optional[int] = None
    evidence: list[str] = field(default_factory=list)
    """支撑壁垒的**可验证**证据（专利数、临床数据、市占率等）"""


class MoatModule:
    """核心技术壁垒模块。

    核心问题：**说出壁垒是什么，说不出就是没有。**

    这是四个模块里最容易被写成叙事的一个。因此设了一条硬约束：
    `moat_source` 必须非空且能给出至少一条可验证证据，
    否则无论其他字段多好看，等级一律为「无」。

    另一条判据：ROIC 与 WACC 的差。真实壁垒会体现在资本回报上 ——
    如果 ROIC 长期低于 WACC，那所谓的"技术优势"没有转化为经济优势。
    """

    name = "核心壁垒"

    TYPE_SCORE = {
        "技术专利": 85, "规模成本": 80, "网络效应": 90,
        "牌照": 75, "品牌": 70, "客户粘性": 75, "无": 10,
    }

    def analyze(self, inp: MoatInput) -> ModuleOutput:
        fields: dict[str, Any] = {}
        findings: list[str] = []
        gaps: list[str] = []
        caveats: list[str] = []

        # ---- 硬约束：壁垒必须可命名 ----
        has_source = bool(inp.moat_source and inp.moat_source.strip())
        if not has_source:
            caveats.append(
                "未能给出可命名的壁垒来源 —— 按本模块硬约束，"
                "无论其他字段如何，等级判定为「无」"
            )
            fields["壁垒来源"] = "未提供"
            return ModuleOutput(self.name, 10.0, "无", fields, findings, gaps, caveats)

        fields["壁垒类型"] = inp.moat_type or "未分类"
        fields["壁垒来源"] = inp.moat_source

        # ---- 可验证证据 ----
        if inp.evidence:
            fields["证据"] = inp.evidence
            findings.append(f"提供 {len(inp.evidence)} 条可验证证据")
        else:
            caveats.append("壁垒来源已命名但无可验证证据 —— 属叙述性主张，非事实")

        # ---- 持续性 ----
        if inp.durability_years is not None:
            fields["持续性(年)"] = inp.durability_years
            if inp.durability_years < 3:
                caveats.append(f"壁垒持续性仅 {inp.durability_years} 年，短于中线持有周期")
        else:
            gaps.append("缺壁垒持续性估计")

        # ---- ROIC vs WACC（最硬的判据）----
        if inp.roic is not None and inp.wacc is not None:
            spread = inp.roic - inp.wacc
            fields["ROIC-WACC"] = round(spread, 2)
            if spread > 5:
                findings.append(f"ROIC 超出 WACC {spread:.1f}pct —— 壁垒已转化为经济优势")
            elif spread > 0:
                findings.append(f"ROIC 略高于 WACC {spread:.1f}pct —— 优势有限")
            else:
                findings.append(
                    f"ROIC 低于 WACC {spread:.1f}pct —— "
                    f"所谓技术优势未转化为资本回报，壁垒存疑"
                )
        else:
            gaps.append("缺 ROIC 或 WACC，无法做最硬的壁垒判据")

        # ---- 研发强度 ----
        if inp.rd_expense_ratio is not None:
            fields["研发费用率"] = inp.rd_expense_ratio
            if inp.rd_ratio_vs_peers:
                fields["研发强度对比"] = inp.rd_ratio_vs_peers
                if inp.rd_ratio_vs_peers == "低于":
                    caveats.append("研发强度低于同业 —— 技术类壁垒的可持续性存疑")
        else:
            gaps.append("缺研发费用率")

        # ---- 可替代性 ----
        if inp.substitute_count is not None:
            fields["替代方案数"] = inp.substitute_count
            if inp.substitute_count == 0:
                findings.append("暂无可替代方案")
            elif inp.substitute_count >= 3:
                caveats.append(f"存在 {inp.substitute_count} 个替代方案，客户切换成本可能被高估")
        else:
            gaps.append("缺替代方案数量")

        score = self._score(inp)
        if score >= 75:
            grade = "宽"
        elif score >= 50:
            grade = "窄"
        else:
            grade = "无"
        return ModuleOutput(self.name, score, grade, fields, findings, gaps, caveats)

    def _score(self, inp: MoatInput) -> float:
        base = self.TYPE_SCORE.get(inp.moat_type or "无", 40)
        if inp.evidence:
            base += min(15.0, len(inp.evidence) * 5)
        if inp.roic is not None and inp.wacc is not None:
            base += max(-20.0, min(20.0, (inp.roic - inp.wacc) * 2))
        if inp.durability_years is not None:
            base += 10.0 if inp.durability_years >= 5 else (-10.0 if inp.durability_years < 3 else 0.0)
        if inp.substitute_count is not None and inp.substitute_count >= 3:
            base -= 15.0

        base = max(0.0, min(100.0, base))

        # ---- 硬性上限：ROIC < WACC 时不得给高等级 ----
        # 理由：壁垒若真实存在，必然体现在资本回报上。
        # 如果 ROIC 长期低于 WACC，说明所谓的"技术优势"没有转化为经济优势 ——
        # 此时叙事再完整，也不构成壁垒。
        #
        # 这条上限的存在，是因为纯加权的评分会出现自相矛盾：
        # 类型分 85 + 证据分 15 + 持续性 10 足以把 ROIC 的负分淹没，
        # 得出"壁垒宽"的同时发现里写着"壁垒存疑"。
        # 硬上限用来消除这种矛盾。
        if inp.roic is not None and inp.wacc is not None and inp.roic < inp.wacc:
            base = min(base, 45.0)

        return round(base, 1)


# ============================================================================
# 汇总
# ============================================================================

@dataclass
class AnalysisBundle:
    """四个模块的汇总。"""

    symbol: str
    name: str
    research: ModuleOutput
    industry: ModuleOutput
    cashflow: ModuleOutput
    moat: ModuleOutput

    def composite(self) -> float:
        """四维加权综合分。

        权重依据：现金流与壁垒是**慢变量**（决定能不能买），
        研报与行业是**快变量**（决定什么时候买、市场怎么看）。
        慢变量给更高权重，避免被短期预期牵着走。
        """
        w = {"research": 0.20, "industry": 0.25, "cashflow": 0.30, "moat": 0.25}
        return round(
            self.research.score * w["research"]
            + self.industry.score * w["industry"]
            + self.cashflow.score * w["cashflow"]
            + self.moat.score * w["moat"], 1,
        )

    def all_gaps(self) -> list[str]:
        out: list[str] = []
        for m in (self.research, self.industry, self.cashflow, self.moat):
            out.extend(f"[{m.module}] {g}" for g in m.gaps)
        return out

    def all_caveats(self) -> list[str]:
        out: list[str] = []
        for m in (self.research, self.industry, self.cashflow, self.moat):
            out.extend(f"[{m.module}] {c}" for c in m.caveats)
        return out

    def summary(self) -> str:
        lines = [
            f"{self.symbol} {self.name}  四维综合 {self.composite():.1f}/100",
            f"  {self.research.summary()}",
            f"  {self.industry.summary()}",
            f"  {self.cashflow.summary()}",
            f"  {self.moat.summary()}",
        ]
        return "\n".join(lines)


if __name__ == "__main__":
    print("=" * 76)
    print("四个分析模块 · 自检（以科伦药业 002422 的真实数据为例）")
    print("=" * 76)

    bundle = AnalysisBundle(
        symbol="002422", name="科伦药业",
        research=ResearchModule().analyze(ResearchInput(
            target_price_mean=52.10, target_price_high=52.10, target_price_low=52.10,
            current_price=39.26, coverage_count=29,
            rating_buy_pct=100.0, rating_hold_pct=0.0, rating_sell_pct=0.0,
            eps_forecast={"2026E": 1.23, "2027E": 1.55, "2028E": 1.88},
            has_site_visit=True,
        )),
        industry=IndustryModule().analyze(IndustryInput(
            industry="医药生物-化学制药", lifecycle_stage="成长",
            industry_revenue_growth=3.5, industry_margin_trend="down",
            competitive_intensity="high", company_position="细分龙头",
            relative_strength_60d=-6.5, sector_flow="out",
        )),
        cashflow=CashFlowModule().analyze(CashFlowInput(
            ocf=23.41, ocf_series=[11.90, 26.42, 23.41],
            net_profit=11.28, capex=23.90,
            interest_bearing_debt=44.18, cash=61.58,
            receivables=30.0, revenue=88.39,
        )),
        moat=MoatModule().analyze(MoatInput(
            moat_type="技术专利",
            moat_source="ADC 平台 OptiDC 与默沙东的 17 项全球 III 期临床绑定",
            durability_years=5, roic=5.94, wacc=9.0,
            rd_expense_ratio=13.7, rd_ratio_vs_peers="高于",
            substitute_count=2,
            evidence=["全球首个在肺癌获批的 TROP2 ADC", "17 项全球 III 期临床"],
        )),
    )

    print()
    print(bundle.summary())

    for m in (bundle.research, bundle.industry, bundle.cashflow, bundle.moat):
        print()
        print("-" * 76)
        print(f"【{m.module}】{m.score:.0f}/100  等级 {m.grade}")
        print("-" * 76)
        print("  结构化字段:")
        for k, v in m.fields.items():
            print(f"    {k:<20} {v}")
        if m.findings:
            print("  关键发现:")
            for f in m.findings:
                print(f"    · {f}")
        if m.gaps:
            print("  数据缺口:")
            for g in m.gaps:
                print(f"    ! {g}")
        if m.caveats:
            print("  警示:")
            for c in m.caveats:
                print(f"    ▲ {c}")

    print()
    print("=" * 76)
    print("壁垒模块硬约束演示：说不出壁垒来源 -> 等级强制为「无」")
    print("=" * 76)
    m = MoatModule().analyze(MoatInput(
        moat_type="技术专利", roic=25.0, wacc=8.0, durability_years=10,
        evidence=["专利 100 项"],
    ))
    print(f"  {m.summary()}")
    for c in m.caveats:
        print(f"    ▲ {c}")
