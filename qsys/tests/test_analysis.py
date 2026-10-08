"""
分析层测试：四个模块 / 三类信号 / 映射引擎 / 复盘
================================================

运行：
    cd qsys && PYTHONPATH=src python -m unittest discover -s tests -v

测试重点
--------
分析层最容易出的问题不是"算错"，而是**自相矛盾** ——
评分说好、发现说坏；归因说不是纪律问题、修正动作却建议扣纪律分。
这类矛盾不会报错，只会让结论失去可信度，所以必须专门测。
"""

from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from qsys.analysis.engine import (
    Advice, AdviceEngine, FactorSignal, MonitorLevel, MonitorSignal,
    QuantAction, QuantSignal, build_factor_signal, generate_monitor_signals,
    max_monitor_level,
)
from qsys.analysis.modules import (
    AnalysisBundle, CashFlowInput, CashFlowModule, IndustryInput, IndustryModule,
    MoatInput, MoatModule, ResearchInput, ResearchModule,
)
from qsys.analysis.resolver import Resolver
from qsys.analysis.review import (
    Dimension, NoExecReason, Outcome, ReviewInput, run_review,
)


# ============================================================================
# 名称解析
# ============================================================================

class TestResolver(unittest.TestCase):

    def setUp(self) -> None:
        self.r = Resolver()

    def test_exact_code(self):
        res = self.r.resolve("002422")
        self.assertTrue(res.is_confident)
        self.assertEqual(res.name, "科伦药业")

    def test_exact_name(self):
        self.assertTrue(self.r.resolve("贵州茅台").is_confident)

    def test_ambiguous_prefix_requires_confirmation(self):
        """回归测试：'科伦' 同时命中科伦药业与科伦博泰，必须要求确认。

        猜错标的比报错严重得多 —— 后续所有分析都会基于错误的公司。
        """
        res = self.r.resolve("科伦")
        self.assertFalse(res.is_confident)
        self.assertGreaterEqual(len(res.candidates), 2)
        self.assertIn("确认", res.warning)

    def test_typo_low_confidence(self):
        res = self.r.resolve("科仑药业")
        self.assertFalse(res.is_confident)

    def test_no_match(self):
        res = self.r.resolve("不存在的公司")
        self.assertIsNone(res.code)
        self.assertEqual(res.confidence, 0.0)


# ============================================================================
# 四个分析模块
# ============================================================================

class TestModules(unittest.TestCase):

    def test_moat_requires_named_source(self):
        """硬约束：说不出壁垒来源，等级强制为「无」，无论其他字段多好看。"""
        m = MoatModule().analyze(MoatInput(
            moat_type="技术专利", roic=25.0, wacc=8.0,
            durability_years=10, evidence=["专利 100 项"],
        ))
        self.assertEqual(m.grade, "无")
        self.assertEqual(m.score, 10.0)
        self.assertTrue(any("可命名" in c for c in m.caveats))

    def test_moat_roic_below_wacc_caps_score(self):
        """回归测试：ROIC < WACC 时评分必须被硬上限压住。

        早期版本纯加权计分，类型分 85 + 证据分 15 + 持续性 10
        会把 ROIC 的负分淹没，得出「壁垒宽」而发现里写着「壁垒存疑」——
        自相矛盾。
        """
        m = MoatModule().analyze(MoatInput(
            moat_type="技术专利", moat_source="ADC 平台与默沙东绑定",
            durability_years=5, roic=5.94, wacc=9.0, substitute_count=2,
            evidence=["证据一", "证据二", "证据三"],
        ))
        self.assertLessEqual(m.score, 45.0)
        self.assertEqual(m.grade, "无")
        self.assertTrue(any("低于 WACC" in f for f in m.findings))

    def test_moat_good_case(self):
        m = MoatModule().analyze(MoatInput(
            moat_type="网络效应", moat_source="双边网络",
            durability_years=8, roic=25.0, wacc=9.0, substitute_count=0,
            evidence=["MAU 领先", "切换成本高"],
        ))
        self.assertGreater(m.score, 75)
        self.assertEqual(m.grade, "宽")

    def test_research_penalizes_unanimous_buy(self):
        """回归测试：买入占比 100% 时应扣分 —— 全体一致意味着信号无区分度。"""
        unanimous = ResearchModule().analyze(ResearchInput(
            target_price_mean=52.1, current_price=39.26,
            coverage_count=29, rating_buy_pct=100.0, has_site_visit=True,
        ))
        split = ResearchModule().analyze(ResearchInput(
            target_price_mean=52.1, current_price=39.26,
            coverage_count=29, rating_buy_pct=70.0, has_site_visit=True,
        ))
        self.assertLess(unanimous.score, split.score)
        self.assertTrue(any("失去区分度" in c for c in unanimous.caveats))

    def test_research_caveat_always_present(self):
        """研报属非一手来源，警示必须始终存在。"""
        m = ResearchModule().analyze(ResearchInput(target_price_mean=10.0, current_price=8.0))
        self.assertTrue(any("非一手来源" in c for c in m.caveats))

    def test_industry_stage_and_position_scoring(self):
        good = IndustryModule().analyze(IndustryInput(
            industry="新能源", lifecycle_stage="成长", industry_revenue_growth=25.0,
            industry_margin_trend="up", competitive_intensity="low",
            company_position="龙头",
        ))
        bad = IndustryModule().analyze(IndustryInput(
            industry="传统制造", lifecycle_stage="下行", industry_revenue_growth=-10.0,
            industry_margin_trend="down", competitive_intensity="high",
            company_position="边缘",
        ))
        self.assertGreater(good.score, bad.score)

    def test_cashflow_detects_paper_profit(self):
        """现金流模块要能识别「利润高但现金含量低」。"""
        bad = CashFlowModule().analyze(CashFlowInput(
            ocf=2.0, net_profit=20.0, capex=1.0,
            ocf_series=[5.0, 3.0, 2.0],
        ))
        self.assertTrue(any("未有效转化为现金" in f for f in bad.findings))

    def test_cashflow_negative_fcf_flagged(self):
        m = CashFlowModule().analyze(CashFlowInput(
            ocf=10.0, net_profit=8.0, capex=15.0,
        ))
        self.assertTrue(any("自由现金流为负" in f for f in m.findings))

    def test_module_gaps_make_it_unusable(self):
        """缺口过多时拒绝给分。"""
        m = CashFlowModule().analyze(CashFlowInput())
        self.assertEqual(m.score, 0.0)
        self.assertFalse(m.is_usable())

    def test_composite_weights(self):
        """慢变量（现金流/壁垒）权重应高于快变量（研报/行业）。"""
        b = AnalysisBundle(
            "X", "X",
            ResearchModule().analyze(ResearchInput(target_price_mean=10.0, current_price=10.0)),
            IndustryModule().analyze(IndustryInput(industry="x", lifecycle_stage="成熟",
                                                   industry_revenue_growth=5.0,
                                                   industry_margin_trend="flat",
                                                   competitive_intensity="medium",
                                                   company_position="龙头")),
            CashFlowModule().analyze(CashFlowInput(ocf=20.0, net_profit=20.0, capex=5.0,
                                                   ocf_series=[15.0, 18.0, 20.0],
                                                   interest_bearing_debt=10.0)),
            MoatModule().analyze(MoatInput(moat_type="品牌", moat_source="品牌",
                                           durability_years=8, roic=18.0, wacc=9.0,
                                           substitute_count=1, evidence=["e1"])),
        )
        # 现金流 100 分权重 0.30，其影响应大于研报
        self.assertGreater(b.cashflow.score * 0.30, b.research.score * 0.20)


# ============================================================================
# 三类信号
# ============================================================================

class TestSignals(unittest.TestCase):

    def test_monitor_signals_carry_window(self):
        """每个监控信号都必须带时间窗，否则无法排期执行。"""
        sigs = generate_monitor_signals(days_to_next_report=3, data_gap_count=2)
        self.assertTrue(sigs)
        for s in sigs:
            self.assertTrue(s.window, f"{s.trigger} 缺时间窗")

    def test_monitor_level_escalation(self):
        self.assertEqual(max_monitor_level([]), MonitorLevel.INFO)
        sigs = generate_monitor_signals(logic_falsified_count=1)
        self.assertEqual(max_monitor_level(sigs), MonitorLevel.CRITICAL)

    def test_report_proximity_escalates(self):
        far = generate_monitor_signals(days_to_next_report=10)
        near = generate_monitor_signals(days_to_next_report=3)
        self.assertEqual(max_monitor_level(far), MonitorLevel.INFO)
        self.assertEqual(max_monitor_level(near), MonitorLevel.WARNING)

    def test_factor_layer_boundaries(self):
        b = AnalysisBundle(
            "X", "X",
            ResearchModule().analyze(ResearchInput(target_price_mean=10.0, current_price=10.0)),
            IndustryModule().analyze(IndustryInput(industry="x", lifecycle_stage="成长",
                                                   industry_revenue_growth=20.0,
                                                   industry_margin_trend="up",
                                                   competitive_intensity="low",
                                                   company_position="龙头")),
            CashFlowModule().analyze(CashFlowInput(ocf=30.0, net_profit=20.0, capex=5.0,
                                                   ocf_series=[20.0, 25.0, 30.0],
                                                   interest_bearing_debt=10.0)),
            MoatModule().analyze(MoatInput(moat_type="网络效应", moat_source="网络",
                                           durability_years=8, roic=25.0, wacc=9.0,
                                           substitute_count=0, evidence=["e1", "e2"])),
        )
        f = build_factor_signal(b)
        self.assertIn(f.layer, ("Q1", "Q2", "Q3", "Q4", "Q5"))
        self.assertGreater(f.composite, 0)

    def test_factor_uses_peer_sample_when_provided(self):
        """分位定义：『有多少同业低于你』。同业普遍很低时，该标的应进 Q1。"""
        strong = AnalysisBundle(
            "X", "X",
            ResearchModule().analyze(ResearchInput(target_price_mean=60.0, current_price=50.0,
                                                   coverage_count=25, rating_buy_pct=80.0,
                                                   has_site_visit=True)),
            IndustryModule().analyze(IndustryInput(industry="成长行业", lifecycle_stage="成长",
                                                   industry_revenue_growth=30.0,
                                                   industry_margin_trend="up",
                                                   competitive_intensity="low",
                                                   company_position="龙头")),
            CashFlowModule().analyze(CashFlowInput(ocf=40.0, net_profit=25.0, capex=5.0,
                                                   ocf_series=[30.0, 35.0, 40.0],
                                                   interest_bearing_debt=5.0)),
            MoatModule().analyze(MoatInput(moat_type="网络效应", moat_source="双边网络效应",
                                           durability_years=10, roic=30.0, wacc=9.0,
                                           substitute_count=0, evidence=["e1", "e2", "e3"])),
        )
        f = build_factor_signal(strong, peer_composites=[10.0, 15.0, 20.0, 25.0, 30.0])
        self.assertEqual(f.layer, "Q1", f"综合 {f.composite} 应高于全部同业样本")

        # 反向验证：弱标的 + 强同业样本 -> Q5
        weak = AnalysisBundle(
            "Y", "Y",
            ResearchModule().analyze(ResearchInput()),
            IndustryModule().analyze(IndustryInput(industry="传统制造", lifecycle_stage="下行",
                                                   industry_revenue_growth=-10.0,
                                                   industry_margin_trend="down",
                                                   competitive_intensity="high",
                                                   company_position="边缘")),
            CashFlowModule().analyze(CashFlowInput(ocf=2.0, net_profit=20.0, capex=3.0,
                                                   ocf_series=[5.0, 3.0, 2.0])),
            MoatModule().analyze(MoatInput(moat_type="无", moat_source=None)),
        )
        f2 = build_factor_signal(weak, peer_composites=[80.0, 85.0, 90.0, 95.0, 99.0])
        self.assertEqual(f2.layer, "Q5", f"综合 {f2.composite} 应低于全部同业样本")


# ============================================================================
# 映射引擎
# ============================================================================

class TestAdviceEngine(unittest.TestCase):

    def setUp(self) -> None:
        self.eng = AdviceEngine(worst_case_position_pct=4.0)
        self.entry = QuantSignal(QuantAction.ENTRY, "momentum", "趋势动量一致", 38.0, "structure", 8.0, 4.0)
        self.block = QuantSignal(QuantAction.BLOCK, "trend", "趋势全空", 38.0, "structure", 8.0, 4.0)
        self.exit_ = QuantSignal(QuantAction.EXIT, "trend", "趋势破位", 38.0, "structure", 8.0, 4.0)

    @staticmethod
    def F(layer: str) -> FactorSignal:
        return FactorSignal({}, 70.0, layer, 70.0)

    def test_logic_stop_has_top_priority(self):
        """逻辑止损优先级最高 —— 即使量化 ENTRY + 因子 Q1。"""
        r = self.eng.decide(self.entry, self.F("Q1"), [], logic_falsified=2)
        self.assertEqual(r.advice, Advice.CLEAR)
        self.assertIn("逻辑止损", r.decided_by)

    def test_logic_stop_partial_reduce(self):
        r = self.eng.decide(self.entry, self.F("Q1"), [], logic_falsified=1)
        self.assertEqual(r.advice, Advice.REDUCE)

    def test_quant_block_beats_good_factor(self):
        """量化 BLOCK 时，因子再好也不入场 —— 相对好 ≠ 现在该买。"""
        r = self.eng.decide(self.block, self.F("Q1"), [])
        self.assertEqual(r.advice, Advice.NO_ENTRY)
        self.assertIn("量化", r.decided_by)

    def test_factor_layer_controls_position(self):
        q1 = self.eng.decide(self.entry, self.F("Q1"), [])
        q3 = self.eng.decide(self.entry, self.F("Q3"), [])
        q4 = self.eng.decide(self.entry, self.F("Q4"), [])
        self.assertEqual(q1.advice, Advice.BUILD)
        self.assertEqual(q1.position_pct, 4.0)
        self.assertEqual(q3.advice, Advice.PROBE)
        self.assertLess(q3.position_pct, q1.position_pct)
        self.assertEqual(q4.advice, Advice.NO_ENTRY)
        self.assertEqual(q4.position_pct, 0.0)

    def test_monitor_critical_suspends(self):
        mons = [MonitorSignal(MonitorLevel.CRITICAL, "停牌", "即时", "无法交易")]
        r = self.eng.decide(self.entry, self.F("Q1"), mons)
        self.assertEqual(r.advice, Advice.SUSPEND)
        self.assertEqual(r.position_pct, 0.0)

    def test_monitor_warning_halves_position(self):
        mons = [MonitorSignal(MonitorLevel.WARNING, "财报临近", "3 天", "禁止新增")]
        r = self.eng.decide(self.entry, self.F("Q1"), mons)
        self.assertEqual(r.advice, Advice.BUILD_HALF)
        self.assertEqual(r.position_pct, 2.0)

    def test_holding_exit_clears(self):
        r = self.eng.decide(self.exit_, self.F("Q1"), [], holding=True)
        self.assertEqual(r.advice, Advice.CLEAR)

    def test_holding_factor_decline_reduces(self):
        r = self.eng.decide(self.entry, self.F("Q5"), [], holding=True)
        self.assertEqual(r.advice, Advice.REDUCE)

    def test_chain_is_auditable(self):
        """判定链必须可审计 —— 能回答『这个建议基于什么』。

        审计标记是 ①②③④⑤ 序号，对应引擎里的优先级顺序。
        """
        r = self.eng.decide(self.entry, self.F("Q1"), [
            MonitorSignal(MonitorLevel.WARNING, "财报临近", "3 天", "禁止新增"),
        ])
        self.assertGreaterEqual(len(r.chain), 4)
        # 必须包含带序号的判定步骤
        self.assertTrue(any(c.startswith(("①", "②", "③", "④", "⑤")) for c in r.chain),
                        f"判定链缺少序号标记: {r.chain}")

    def test_all_layer_monitor_combinations_covered(self):
        """穷举因子分层 × 监控级别，确保每种组合都给出明确建议。"""
        for layer in ["Q1", "Q2", "Q3", "Q4", "Q5"]:
            for lvl in MonitorLevel:
                r = self.eng.decide(self.entry, self.F(layer),
                                    [MonitorSignal(lvl, "x", "y", "z")])
                self.assertIsInstance(r.advice, Advice)
                self.assertGreaterEqual(r.position_pct, 0.0)


# ============================================================================
# 复盘
# ============================================================================

class TestReview(unittest.TestCase):

    def _base(self, **kw):
        q = QuantSignal(QuantAction.BLOCK, "trend", "趋势全空", 38.42, "structure", 8.18, 3.64)
        d = dict(
            symbol="002422", name="科伦药业", review_date=date(2026, 10, 8),
            advice=__import__("qsys.analysis.engine", fromlist=["AdviceResult"])
            .AdviceResult(Advice.NO_ENTRY, 0.0, "量化", [], ["趋势级全部空头"]),
            quant=q, factor=FactorSignal({}, 66.2, "Q3", 66.2), monitors=[],
            planned_price=40.00, data_gap_count=0, price_t20=38.60,
            hypotheses=[("假设A", "falsified"), ("假设B", "verified")],
            mandatory_action=True,
        )
        d.update(kw)
        return ReviewInput(**d)

    def test_condition_unmet_is_not_discipline_issue(self):
        """回归测试：跌停/停牌导致无法执行时，不构成纪律问题。

        早期版本同时给出「设计问题」的归因和「系统接管」的修正 ——
        但系统在跌停时同样卖不出去，接管没有意义。
        """
        r = run_review(self._base(outcome=Outcome.NOT_EXECUTED,
                                  no_exec_reason=NoExecReason.CONDITION_UNMET))
        self.assertFalse(r.must_take_over)
        self.assertIn("设计问题", r.attribution)
        b3 = next(d for d in r.dimensions if d.code == "B3")
        self.assertIsNone(b3.score)
        self.assertIn("不适用", b3.verdict)

    def test_triggered_not_acted_is_discipline_issue(self):
        r = run_review(self._base(outcome=Outcome.NOT_EXECUTED,
                                  no_exec_reason=NoExecReason.TRIGGERED_NOT_ACTED))
        self.assertTrue(r.must_take_over)
        self.assertIn("权限问题", r.attribution)
        b3 = next(d for d in r.dimensions if d.code == "B3")
        self.assertEqual(b3.score, 0.0)

    def test_signal_not_triggered_is_normal(self):
        r = run_review(self._base(outcome=Outcome.NOT_EXECUTED,
                                  no_exec_reason=NoExecReason.SIGNAL_NOT_TRIGGERED,
                                  mandatory_action=False))
        self.assertFalse(r.must_take_over)
        self.assertIn("正常", r.attribution)
        b1 = next(d for d in r.dimensions if d.code == "B1")
        self.assertEqual(b1.score, 100.0)

    def test_executed_path_has_execution_dimensions(self):
        r = run_review(self._base(outcome=Outcome.EXECUTED,
                                  actual_price=40.15, planned_qty=10000,
                                  actual_qty=5000, actual_date=date(2026, 9, 30)))
        codes = {d.code for d in r.dimensions}
        self.assertIn("A1", codes)
        self.assertIn("A2", codes)
        self.assertIn("A3", codes)
        self.assertNotIn("B1", codes)

    def test_position_over_allocation_flagged(self):
        r = run_review(self._base(outcome=Outcome.EXECUTED,
                                  actual_price=40.15, planned_qty=10000,
                                  actual_qty=20000))
        a2 = next(d for d in r.dimensions if d.code == "A2")
        self.assertIn("超", a2.verdict)

    def test_common_dimensions_always_present(self):
        """D1/D2/D3/D4/E1 是共同维度，两种情形都必须有。"""
        for outcome, extra in [
            (Outcome.EXECUTED, dict(actual_price=40.15, planned_qty=100, actual_qty=100)),
            (Outcome.NOT_EXECUTED, dict(no_exec_reason=NoExecReason.SIGNAL_NOT_TRIGGERED)),
        ]:
            r = run_review(self._base(outcome=outcome, **extra))
            codes = {d.code for d in r.dimensions}
            for c in ("D1", "D2", "D3", "D4", "E1"):
                self.assertIn(c, codes, f"{outcome.value} 缺维度 {c}")

    def test_corrections_deduplicated(self):
        r = run_review(self._base(outcome=Outcome.NOT_EXECUTED,
                                  no_exec_reason=NoExecReason.CONDITION_UNMET))
        keys = [c.split("] ", 1)[-1] for c in r.corrections]
        self.assertEqual(len(keys), len(set(keys)), "修正清单存在重复项")


if __name__ == "__main__":
    unittest.main(verbosity=2)
