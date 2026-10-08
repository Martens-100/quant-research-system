"""
核心逻辑测试
============

用标准库 unittest，无外部依赖。

运行：
    cd qsys && PYTHONPATH=src python -m unittest discover -s tests -v

测试重点
--------
除了常规正确性，本套测试专门覆盖三类"容易静默出错"的场景：
  1. 制度约束（涨跌停/T+1/最小单位）—— 数值错了不会报错，只会算错
  2. 数据时效（盘中价、过期价、跨时点拼接）—— 过期数据照样能参与运算
  3. 决策分支覆盖 —— 未覆盖的分支会落到兜底，看起来"能跑"但结论是错的
"""

from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from qsys.data.base import (
    DataGap, FieldValue, FreshnessPolicy, TradingState, assert_same_series, pct_change,
)
from qsys.indicators.layers import CapitalFlowLayer, IndicatorLayer, TechnicalLayer, Thresholds, TriState
from qsys.review.scoring import (
    Emotion, EmotionTag, ExecutionType, Hypothesis, ParamCandidate, ParamWhitelist,
    TradeRecord, calc_discipline, calc_emotion, calc_logic, cvar, max_drawdown,
    rank_candidates, sortino_ratio,
)
from qsys.risk.management import DrawdownBudget, PositionSizer, StopBuilder
from qsys.rules.cn import CNMarketRules, Board, FeeSchedule, SuspensionRule
from qsys.screen.pool import CandidateInput, ExclusionLog, ExclusionRecord, Pool, PoolBuilder
from qsys.signals.tiers import Action, SignalEngine, SignalTier, Signal


# ============================================================================
# 制度约束
# ============================================================================

class TestMarketRules(unittest.TestCase):
    """制度约束引擎 —— 数值错了不会报错，只会算错，所以必须逐条测。"""

    def setUp(self) -> None:
        self.r = CNMarketRules()

    def test_board_classification(self):
        self.assertEqual(self.r.classify_board("600519"), Board.CN_MAIN)
        self.assertEqual(self.r.classify_board("002422"), Board.CN_MAIN)
        self.assertEqual(self.r.classify_board("688111"), Board.CN_STAR)
        self.assertEqual(self.r.classify_board("300750"), Board.CN_GEM)
        self.assertEqual(self.r.classify_board("301029"), Board.CN_GEM)
        self.assertEqual(self.r.classify_board("830799"), Board.CN_BSE)

    def test_price_limits_by_board(self):
        # 前收 39.26 -> 主板 ±10% = 35.33 / 43.19
        lo, hi = self.r.price_limits(39.26, "002422")
        self.assertAlmostEqual(lo, 35.33, places=2)
        self.assertAlmostEqual(hi, 43.19, places=2)
        # 科创板/创业板 ±20% = 31.41 / 47.11
        lo, hi = self.r.price_limits(39.26, "688111")
        self.assertAlmostEqual(lo, 31.41, places=2)
        self.assertAlmostEqual(hi, 47.11, places=2)

    def test_st_rule_is_10pct_not_5pct(self):
        """回归测试：主板 ST 涨跌幅已于 2026-07-06 由 5% 调整为 10%。

        若有人误按旧规写成 5%，本测试会失败。
        """
        lo, hi = self.r.price_limits(39.26, "600519", is_st=True)
        self.assertAlmostEqual(lo, 35.33, places=2, msg="主板 ST 应为 ±10%，不是 ±5%")
        self.assertAlmostEqual(hi, 43.19, places=2, msg="主板 ST 应为 ±10%，不是 ±5%")
        # 明确否定旧规数值
        self.assertNotAlmostEqual(lo, 37.30, places=2)

    def test_star_board_st_keeps_20pct(self):
        lo, hi = self.r.price_limits(39.26, "688111", is_st=True)
        self.assertAlmostEqual(lo, 31.41, places=2)

    def test_limit_hit_detection(self):
        self.assertTrue(self.r.is_limit_down(35.33, 39.26, "002422"))
        self.assertFalse(self.r.is_limit_down(35.34, 39.26, "002422"))
        self.assertTrue(self.r.is_limit_up(43.19, 39.26, "002422"))

    def test_t_plus_1(self):
        d = date(2026, 10, 8)
        self.assertFalse(self.r.can_sell(d, d), "当日买入不可当日卖出")
        self.assertTrue(self.r.can_sell(d, d + timedelta(days=1)))

    def test_min_order_quantity(self):
        # 主板 100 股整数倍
        self.assertEqual(self.r.round_quantity(357, "600519"), 300)
        self.assertEqual(self.r.round_quantity(99, "600519"), 0, "不足一手应为 0")
        # 科创板 200 股起、1 股递增
        self.assertEqual(self.r.round_quantity(357, "688111"), 357)
        self.assertEqual(self.r.round_quantity(150, "688111"), 0, "科创板不足 200 股应为 0")

    def test_worst_case_loss_amplification(self):
        """连续跌停使实际损失远超计划损失 —— 风险预算公式失效。"""
        wc = self.r.worst_case_loss(45.44, 10000, "002422", 40.00,
                                    max_consecutive_limit_down=3)
        self.assertAlmostEqual(wc["planned_loss"], 54400.0, places=2)
        # 45.44 * 0.9^3 = 33.1257 -> 损失 123142.4
        self.assertGreater(wc["realized_loss"], wc["planned_loss"])
        self.assertAlmostEqual(wc["amplification"], 2.26, places=2)
        self.assertFalse(wc["is_formula_valid"], "放大 2.26 倍时公式不应判为有效")

    def test_worst_case_no_limit_down(self):
        wc = self.r.worst_case_loss(45.44, 10000, "002422", 40.00,
                                    max_consecutive_limit_down=0)
        self.assertAlmostEqual(wc["amplification"], 1.0, places=2)
        self.assertTrue(wc["is_formula_valid"])

    def test_fee_schedule_incomplete_returns_none(self):
        """成本未填齐时必须返回 None，不得以 0 代替。"""
        f = FeeSchedule()
        self.assertFalse(f.is_complete())
        self.assertIsNone(f.round_trip_cost_pct())

    def test_fee_schedule_complete(self):
        f = FeeSchedule(commission_pct=0.025, commission_min=5.0,
                        transfer_fee_pct=0.001, slippage_pct=0.1)
        self.assertTrue(f.is_complete())
        self.assertAlmostEqual(f.round_trip_cost_pct(), 0.05 + 0.05 + 0.002 + 0.2, places=4)

    def test_suspension_defaults_are_safe(self):
        s = SuspensionRule()
        self.assertFalse(s.count_suspended_days_in_monitor,
                         "停牌日不得计入监控计时")
        self.assertFalse(s.first_day_after_resume_uses_normal_stop,
                         "复牌首日不得适用常规止损")


# ============================================================================
# 数据时效
# ============================================================================

class TestDataFreshness(unittest.TestCase):
    """数据时效纪律 —— 过期数据照样能参与运算，不会报错。"""

    def setUp(self) -> None:
        self.p = FreshnessPolicy()
        self.now = datetime(2026, 10, 8, 15, 0)

    def test_intraday_price_rejected(self):
        fv = FieldValue("close", 39.26, datetime(2026, 10, 8, 11, 28),
                        "行情接口", TradingState.INTRADAY, unit="元")
        ok, why = self.p.check(fv, "price", now=self.now)
        self.assertFalse(ok)
        self.assertIn("盘中价", why)

    def test_stale_price_rejected(self):
        """回归测试：45.44 元是 2026-08-07 的收盘价，两个月后不可用于决策。"""
        fv = FieldValue("close", 45.44, datetime(2026, 8, 7, 15, 0),
                        "行情接口", TradingState.CLOSED, unit="元")
        ok, why = self.p.check(fv, "price", now=self.now)
        self.assertFalse(ok)
        self.assertIn("过期", why)
        self.assertGreater(fv.age_days(self.now), 60)

    def test_fresh_closed_price_accepted(self):
        fv = FieldValue("close", 39.26, datetime(2026, 10, 8, 15, 0),
                        "行情接口", TradingState.CLOSED, unit="元")
        ok, _ = self.p.check(fv, "price", now=self.now)
        self.assertTrue(ok)

    def test_suspended_rejected(self):
        fv = FieldValue("close", 39.26, datetime(2026, 10, 8, 15, 0),
                        "行情接口", TradingState.SUSPENDED, unit="元")
        ok, why = self.p.check(fv, "price", now=self.now)
        self.assertFalse(ok)
        self.assertIn("停牌", why)

    def test_cross_period_splice_rejected(self):
        """回归测试：'38.52 -> 45.44 涨 18%' 是跨时点拼接，必须拦下。"""
        a = FieldValue("close", 38.52, datetime(2026, 7, 1, 9, 30),
                       "行情接口", TradingState.CLOSED, unit="元")
        b = FieldValue("close", 45.44, datetime(2026, 8, 7, 15, 0),
                       "行情接口", TradingState.CLOSED, unit="元")
        ok, why = assert_same_series(a, b)
        self.assertFalse(ok)
        self.assertIn("间隔", why)
        with self.assertRaises(ValueError):
            pct_change(a, b)

    def test_same_series_accepted(self):
        a = FieldValue("close", 40.00, datetime(2026, 10, 7, 15, 0),
                       "行情接口", TradingState.CLOSED, unit="元")
        b = FieldValue("close", 39.26, datetime(2026, 10, 8, 15, 0),
                       "行情接口", TradingState.CLOSED, unit="元")
        ok, _ = assert_same_series(a, b)
        self.assertTrue(ok)
        self.assertAlmostEqual(pct_change(a, b).value, -1.85, places=2)

    def test_financial_field_keeps_announced_at(self):
        fv = FieldValue("revenue", 88.39, datetime(2026, 10, 8, 11, 30),
                        "定期报告", TradingState.CLOSED, period="2026H1",
                        announced_at=datetime(2026, 8, 27), unit="亿元")
        self.assertIsNotNone(fv.announced_at)
        self.assertEqual(fv.period, "2026H1")

    def test_data_gap_raises(self):
        with self.assertRaises(DataGap) as ctx:
            raise DataGap("margin_balance", "接口限频", "002422")
        self.assertIn("数据缺口", str(ctx.exception))


# ============================================================================
# 信号决策树
# ============================================================================

class TestSignalEngine(unittest.TestCase):
    """三级信号 —— 分支未覆盖时会落到兜底，看起来能跑但结论是错的。"""

    def setUp(self) -> None:
        self.eng = SignalEngine(decay_days=3)
        self.now = datetime(2026, 10, 8, 15, 0)

    def _mk(self, tier, state, name="x"):
        return Signal(tier, name, state, self.now,
                      self.now + timedelta(days=3), "")

    def test_trend_bear_blocks_momentum_bull(self):
        """核心规则：趋势空头时，动量金叉不构成入场依据。"""
        sigs = [
            self._mk(SignalTier.TREND, TriState.BEAR, "均线"),
            self._mk(SignalTier.TREND, TriState.BEAR, "年线"),
            self._mk(SignalTier.MOMENTUM, TriState.BULL, "MACD"),
            self._mk(SignalTier.MOMENTUM, TriState.BULL, "RSI"),
        ]
        d = self.eng.decide(sigs, now=self.now)
        self.assertEqual(d.action, Action.BLOCK)
        self.assertEqual(d.blocked_by, "趋势级空头")

    def test_trend_bull_momentum_bull_entry(self):
        sigs = [
            self._mk(SignalTier.TREND, TriState.BULL, "均线"),
            self._mk(SignalTier.TREND, TriState.BULL, "年线"),
            self._mk(SignalTier.MOMENTUM, TriState.BULL, "MACD"),
            self._mk(SignalTier.MOMENTUM, TriState.BULL, "RSI"),
        ]
        self.assertEqual(self.eng.decide(sigs, now=self.now).action, Action.ENTRY)

    def test_trend_neutral_branch_covered(self):
        """回归测试：趋势级含中性必须显式处理。

        早期版本漏了这个分支，导致「一个中性 + 一个多头」落到兜底分支，
        返回"未匹配任何规则分支"。中性表示方向不明，不是默认看多。
        """
        sigs = [
            self._mk(SignalTier.TREND, TriState.NEUTRAL, "均线"),
            self._mk(SignalTier.TREND, TriState.BULL, "年线"),
            self._mk(SignalTier.MOMENTUM, TriState.BULL, "MACD"),
            self._mk(SignalTier.MOMENTUM, TriState.BULL, "RSI"),
        ]
        d = self.eng.decide(sigs, now=self.now)
        self.assertEqual(d.action, Action.WAIT)
        self.assertIn("中性", d.reason)
        self.assertNotIn("未匹配", d.reason, "不应落到兜底分支")

    def test_trend_unknown_waits(self):
        sigs = [
            self._mk(SignalTier.TREND, TriState.UNKNOWN, "均线"),
            self._mk(SignalTier.TREND, TriState.UNKNOWN, "年线"),
            self._mk(SignalTier.MOMENTUM, TriState.BULL, "MACD"),
        ]
        d = self.eng.decide(sigs, now=self.now)
        self.assertEqual(d.action, Action.WAIT)
        self.assertIn("数据缺失", d.reason)

    def test_signal_decay(self):
        old = Signal(SignalTier.MOMENTUM, "MACD", TriState.BULL,
                     self.now - timedelta(days=5), self.now - timedelta(days=2), "")
        self.assertTrue(old.is_expired(self.now))
        # 过期信号不参与决策 -> 动量级为空 -> 趋势全多但动量未确认 -> WAIT
        sigs = [
            self._mk(SignalTier.TREND, TriState.BULL, "均线"),
            self._mk(SignalTier.TREND, TriState.BULL, "年线"),
            old,
        ]
        d = self.eng.decide(sigs, now=self.now)
        self.assertEqual(d.action, Action.WAIT)

    def test_all_branches_covered(self):
        """穷举趋势级状态组合，确保没有组合落到兜底分支。"""
        for a in [TriState.BULL, TriState.BEAR, TriState.NEUTRAL, TriState.UNKNOWN]:
            for b in [TriState.BULL, TriState.BEAR, TriState.NEUTRAL, TriState.UNKNOWN]:
                sigs = [
                    self._mk(SignalTier.TREND, a, "均线"),
                    self._mk(SignalTier.TREND, b, "年线"),
                    self._mk(SignalTier.MOMENTUM, TriState.BULL, "MACD"),
                    self._mk(SignalTier.MOMENTUM, TriState.BULL, "RSI"),
                ]
                d = self.eng.decide(sigs, now=self.now)
                self.assertNotIn(
                    "未匹配", d.reason,
                    msg=f"趋势级组合 ({a.value}, {b.value}) 落到兜底分支",
                )


# ============================================================================
# 风控
# ============================================================================

class TestRiskManagement(unittest.TestCase):

    def setUp(self) -> None:
        self.sizer = PositionSizer()
        self.builder = StopBuilder(atr_multiple=2.0)

    def test_effective_stop_takes_nearer(self):
        """回归测试：双条件止损的实际生效者是更近的那条。"""
        plan = self.builder.build(entry_price=39.26, atr20=0.98, prior_low=38.42)
        eff = plan.effective_stop
        self.assertEqual(eff.stop_type.value, "structure")
        self.assertAlmostEqual(eff.price, 38.42, places=2)

    def test_effective_stop_takes_atr_when_nearer(self):
        plan = self.builder.build(entry_price=39.26, atr20=0.30, prior_low=30.00)
        self.assertEqual(plan.effective_stop.stop_type.value, "atr")

    def test_logic_stop_overrides_price(self):
        plan = self.builder.build(
            entry_price=39.26, atr20=0.98, prior_low=38.42,
            logic_conditions=[("扣非降幅扩大", True)],
        )
        r = plan.evaluate(current_price=39.00)  # 未触及任何价格止损
        self.assertTrue(r["triggered"])
        self.assertEqual(r["type"], "logic")

    def test_worst_case_sizing_smaller_than_standard(self):
        """制度修正版仓位必须 <= 标准版。"""
        cmp = self.sizer.compare(
            total_capital=1_000_000, risk_pct=1.0,
            entry_price=45.44, stop_price=40.00,
            symbol="002422", max_consecutive_limit_down=3,
        )
        self.assertLess(cmp["worst_case"].quantity, cmp["standard"].quantity)
        self.assertLess(cmp["shrink_ratio"], 1.0)
        # 800/1800 ≈ 0.445（用四舍五入后的 capital_pct 相除，容许小幅误差）
        self.assertAlmostEqual(cmp["shrink_ratio"], 0.445, delta=0.005)

    def test_sizing_respects_board_min_qty(self):
        r = self.sizer.by_standard(1_000_000, 1.0, 100.0, 90.0, "688111")
        # 科创板 200 股起、1 股递增
        self.assertEqual(r.quantity % 1, 0)
        if r.quantity > 0:
            self.assertGreaterEqual(r.quantity, 200)

    def test_invalid_stop_raises(self):
        with self.assertRaises(ValueError):
            self.sizer.by_standard(1_000_000, 1.0, 40.0, 45.0, "002422")

    def test_drawdown_budget_detects_inconsistency(self):
        bad = DrawdownBudget(1_000_000, max_drawdown_pct=10.0,
                             max_loss_per_trade_pct=2.0, max_concurrent_positions=10)
        v = bad.validate()
        self.assertFalse(v["is_valid"])
        self.assertTrue(any("超过回撤熔断线" in i for i in v["issues"]))

    def test_drawdown_budget_ok(self):
        good = DrawdownBudget(1_000_000, max_drawdown_pct=20.0,
                              max_loss_per_trade_pct=1.0, max_concurrent_positions=10)
        self.assertTrue(good.validate()["is_valid"])


# ============================================================================
# 筛选
# ============================================================================

class TestScreen(unittest.TestCase):

    def setUp(self) -> None:
        self.b = PoolBuilder()

    def test_data_gap_refuses_judgement(self):
        """数据缺口时必须拒绝判定，不得猜测。"""
        c = CandidateInput("X", "某标的", revenue_growth=25.0,
                           deducted_np_growth=18.0,
                           data_gaps=["接口限频"])
        res = self.b.screen(c)
        self.assertEqual(res.pool, Pool.EXCLUDED)
        self.assertIn("数据缺口", res.excluded_reason)

    def test_growth_pool_pass(self):
        c = CandidateInput("600XXX", "成长标的", revenue_growth=32.5,
                           deducted_np_growth=28.1,
                           gross_margins=[38.2, 39.5, 41.0], ocf=15.6)
        self.assertEqual(self.b.screen(c).pool, Pool.GROWTH)

    def test_deducted_growth_is_veto(self):
        """扣非增速为负 -> 不得进成长池（否决项）。"""
        c = CandidateInput("600XXX", "标的", revenue_growth=35.0,
                           deducted_np_growth=-5.0,
                           gross_margins=[38.2, 39.5, 41.0], ocf=15.6)
        self.assertNotEqual(self.b.screen(c).pool, Pool.GROWTH)

    def test_turnaround_requires_named_driver(self):
        """困境反转池必须有可命名的反转驱动，否则不许进池。"""
        base = dict(symbol="600XXX", name="标的", ocf=5.0,
                    deducted_np_series=[-30.0, -20.0], debt_to_asset=40.0)
        without = CandidateInput(**base, turnaround_driver=None)
        self.assertNotEqual(self.b.screen(without).pool, Pool.TURNAROUND)
        with_driver = CandidateInput(**base, turnaround_driver="新品放量")
        self.assertEqual(self.b.screen(with_driver).pool, Pool.TURNAROUND)

    def test_exclusion_log_uses_relative_return(self):
        """回归测试：排除日志必须用相对基准的超额收益，不用绝对涨幅。

        绝对涨幅会随大盘上涨而虚高，得出"门槛太严"的错误结论。
        """
        rec = ExclusionRecord(
            symbol="A", name="A", excluded_at=date(2026, 9, 1), reason="x",
            ref_close=10.0, benchmark_close=3000.0,
            later_close=13.5, later_benchmark=3060.0,
        )
        # 绝对涨幅 +35%，但基准 +2%，超额应为 +33%
        self.assertAlmostEqual(rec.excess_return_pct, 33.0, places=2)

        # 大盘普涨场景：个股 +20%，基准 +20%，超额应为 0
        rec2 = ExclusionRecord(
            symbol="B", name="B", excluded_at=date(2026, 9, 1), reason="x",
            ref_close=10.0, benchmark_close=3000.0,
            reviewed_at=date(2026, 10, 8),
            later_close=12.0, later_benchmark=3600.0,
        )
        self.assertAlmostEqual(rec2.excess_return_pct, 0.0, places=2)

    def test_exclusion_log_incomplete_returns_none(self):
        rec = ExclusionRecord("C", "C", date(2026, 9, 1), "x", ref_close=10.0)
        self.assertIsNone(rec.excess_return_pct)


# ============================================================================
# 复盘
# ============================================================================

class TestReview(unittest.TestCase):

    def test_skipped_mandatory_zeroes_discipline(self):
        """未执行硬止损 -> 纪律分归零 + 建议系统接管。"""
        skipped = [TradeRecord("002422", 40.00, 0.0, 10000,
                               date(2026, 9, 29), ExecutionType.STOP_LOSS,
                               plan_was_mandatory=True)]
        d = calc_discipline([], skipped)
        self.assertEqual(d.score, 0.0)
        self.assertTrue(d.must_take_over)

    def test_discipline_penalizes_deviation(self):
        recs = [TradeRecord("600XXX", 25.00, 25.80, 2000,
                            date(2026, 9, 15), ExecutionType.MANUAL)]
        d = calc_discipline(recs, [])
        self.assertGreater(d.score, 0)
        self.assertLess(d.score, 100)
        self.assertTrue(d.violations)

    def test_logic_falsified_forces_reduce(self):
        hyps = [
            Hypothesis("假设1", "falsified"),
            Hypothesis("假设2", "falsified"),
            Hypothesis("假设3", "verified"),
        ]
        r = calc_logic(hyps)
        self.assertTrue(r["force_reduce"])
        self.assertEqual(r["falsified"], 2)

    def test_logic_all_verified(self):
        r = calc_logic([Hypothesis("a", "verified"), Hypothesis("b", "verified")])
        self.assertEqual(r["score"], 100.0)
        self.assertFalse(r["force_reduce"])

    def test_emotion_detects_inconsistency(self):
        tags = [
            EmotionTag(date(2026, 8, 24), Emotion.RELUCTANCE, "hold"),
            EmotionTag(date(2026, 9, 24), Emotion.ANXIETY, "hold"),
        ]
        r = calc_emotion(tags)
        self.assertEqual(r["inconsistent"], 2)
        self.assertTrue(r["suspend_trading"])

    def test_emotion_calm_is_consistent(self):
        r = calc_emotion([EmotionTag(date(2026, 10, 8), Emotion.CALM, "hold")])
        self.assertEqual(r["inconsistent"], 0)
        self.assertFalse(r["suspend_trading"])

    def test_sortino_only_penalizes_downside(self):
        """Sortino 只惩罚下行波动 —— 上行波动不是风险。"""
        up_only = [0.01, 0.02, 0.015, 0.03, 0.005]
        self.assertEqual(sortino_ratio(up_only), float("inf"))

    def test_cvar_captures_tail(self):
        rets = [-0.10, -0.08, -0.05, 0.01, 0.02, 0.03, 0.01, 0.02, 0.01, 0.02]
        cv = cvar(rets, alpha=0.1)
        self.assertGreater(cv, 0.09)

    def test_rank_candidates_filters_by_cvar(self):
        """CVaR 超约束的组合必须被淘汰，即使 Sortino 更高。"""
        good = ParamCandidate("good", [0.001] * 100)
        risky = ParamCandidate("risky", [0.02] * 95 + [-0.30] * 5)
        rows = rank_candidates([good, risky], cvar_limit=0.05)
        by_label = {r["label"]: r for r in rows}
        self.assertFalse(by_label["risky"]["passes_cvar"])
        self.assertTrue(rows[0]["passes_cvar"], "通过约束的应排在前")

    def test_param_whitelist_blocks_fixed(self):
        wl = ParamWhitelist()
        ok, msg = wl.check("max_loss_per_trade_pct", 1.0)
        self.assertFalse(ok)
        self.assertIn("固定约束", msg)

    def test_param_whitelist_range(self):
        wl = ParamWhitelist()
        self.assertTrue(wl.check("stop_loss_atr_multiple", 2.0)[0])
        self.assertFalse(wl.check("stop_loss_atr_multiple", 5.0)[0])


# ============================================================================
# 技术指标
# ============================================================================

class TestIndicators(unittest.TestCase):

    def setUp(self) -> None:
        self.tl = TechnicalLayer(Thresholds())

    def test_insufficient_data_returns_unknown(self):
        """数据不足时必须返回 UNKNOWN，不得猜测。"""
        short = [10.0] * 50
        r = self.tl.ma_alignment(short)
        self.assertEqual(r.state, TriState.UNKNOWN)
        self.assertEqual(r.layer, IndicatorLayer.TECHNICAL)

    def test_sma(self):
        self.assertAlmostEqual(self.tl.sma([1, 2, 3, 4, 5], 5), 3.0)
        self.assertIsNone(self.tl.sma([1, 2], 5))

    def test_rsi_bounds(self):
        rising = list(range(1, 40))
        r = self.tl.rsi(rising, 12)
        self.assertAlmostEqual(r, 100.0, places=1)
        falling = list(range(40, 1, -1))
        r2 = self.tl.rsi(falling, 12)
        self.assertAlmostEqual(r2, 0.0, places=1)

    def test_ma_alignment_bull(self):
        rising = [float(i) for i in range(1, 300)]
        self.assertEqual(self.tl.ma_alignment(rising).state, TriState.BULL)

    def test_ma_alignment_bear(self):
        falling = [float(i) for i in range(300, 1, -1)]
        self.assertEqual(self.tl.ma_alignment(falling).state, TriState.BEAR)

    def test_atr_positive(self):
        closes = [10.0 + i * 0.1 for i in range(50)]
        highs = [c + 0.2 for c in closes]
        lows = [c - 0.2 for c in closes]
        self.assertGreater(self.tl.atr(highs, lows, closes), 0)

    def test_capital_flow_crowding_not_calibrated(self):
        """回归测试：拥挤度阈值未校准时不得下结论。"""
        cfl = CapitalFlowLayer(Thresholds())  # crowding_threshold=None
        r = cfl.water_level(80.0)
        self.assertEqual(r.state, TriState.UNKNOWN)
        self.assertIn("尚未校准", r.note)

    def test_capital_flow_crowding_after_calibration(self):
        cfl = CapitalFlowLayer(Thresholds(crowding_threshold=85.0))
        self.assertEqual(cfl.water_level(80.0).state, TriState.BULL)
        self.assertEqual(cfl.water_level(90.0).state, TriState.BEAR)

    def test_capital_flow_high_level_plus_negative_direction_is_divergence(self):
        """水位高 + 方向负 = 背离（杠杆放大风险），不是支持。"""
        cfl = CapitalFlowLayer(Thresholds(crowding_threshold=75.0))
        readings = [cfl.water_level(80.0), cfl.direction(-1000.0)]
        rating, _ = cfl.overall_rating(readings)
        self.assertEqual(rating, "背离")


if __name__ == "__main__":
    unittest.main(verbosity=2)
