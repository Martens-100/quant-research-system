"""
三级信号：趋势 / 动量 / 极值
============================

与"三层指标"的区别（重要）
--------------------------
  三层指标（indicator_layer）：基本面 / 技术面 / 资金面 —— 按**信息类型**分
  三级信号（signal_tier）    ：趋势 / 动量 / 极值     —— 按**决策用途**分

两者正交：技术面层（指标）同时供给趋势级与动量级（信号）。

核心设计
--------
1. **趋势级优先**：趋势级为空头时，动量级金叉不构成入场信号。
   这条规则的作用不是提高胜率，而是**让你不必在盘中现场裁决** ——
   现场裁决就是情绪决策的入口。

2. **信号衰减**：信号生成后 N 个交易日内未触发动作即失效。
   防止"三天前的金叉今天才执行"——那是拿旧信息做新决策。

3. **显式声明取舍**：趋势级优先的代价是**右侧晚入场**。
   趋势级是慢变量，它从空头转多头时价格往往已涨了一段。
   这是"用收益换确定性"的交易，不是免费的优势。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

from qsys.indicators.layers import IndicatorResult, TriState, Thresholds


class SignalTier(str, Enum):
    TREND = "trend"      # 趋势级 —— 决定"能不能买"
    MOMENTUM = "momentum"  # 动量级 —— 决定"什么时候买"
    EXTREME = "extreme"    # 极值级 —— 决定"要不要等"


class Action(str, Enum):
    """可执行动作。信号层只产出这四种，不产出"观察""关注"这类模糊词。"""

    ENTRY = "entry"          # 可入场
    WAIT = "wait"            # 等待（条件未满足，但不构成否决）
    BLOCK = "block"          # 否决（趋势级空头，无论其他级如何）
    EXIT = "exit"            # 退出


@dataclass
class Signal:
    """一条信号。"""

    tier: SignalTier
    name: str
    state: TriState
    as_of: datetime
    valid_until: Optional[datetime] = None
    reason: str = ""

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        if self.valid_until is None:
            return False
        return (now or datetime.now()) > self.valid_until

    def __str__(self) -> str:
        exp = f" 有效至 {self.valid_until:%m-%d %H:%M}" if self.valid_until else ""
        return f"[{self.tier.value:<8}] {self.name:<14} {self.state.value:<11}{exp}  {self.reason}"


@dataclass
class Decision:
    """信号层的最终决策。"""

    action: Action
    tier_that_decided: Optional[SignalTier]
    reason: str
    signals: list[Signal] = field(default_factory=list)
    blocked_by: Optional[str] = None

    def explain(self) -> str:
        lines = [f"决策: {self.action.value.upper()}  ({self.reason})"]
        if self.blocked_by:
            lines.append(f"否决来源: {self.blocked_by}")
        lines.append("信号明细:")
        for s in self.signals:
            lines.append("  " + str(s))
        return "\n".join(lines)


# ============================================================================
# 信号引擎
# ============================================================================

class SignalEngine:
    """三级信号引擎。

    参数
    ----
    decay_days
        信号有效期（交易日）。默认 3 天。
    """

    def __init__(self, th: Optional[Thresholds] = None, decay_days: int = 3) -> None:
        self.th = th or Thresholds()
        self.decay_days = decay_days

    # ---- 趋势级 ----

    def trend_tier(self, ma_result: IndicatorResult, year_result: IndicatorResult,
                   as_of: datetime) -> list[Signal]:
        """趋势级信号。这一级有**否决权**。"""
        return [
            Signal(SignalTier.TREND, "均线排列", ma_result.state, as_of,
                   as_of + timedelta(days=self.decay_days), ma_result.reason),
            Signal(SignalTier.TREND, "价格vs年线", year_result.state, as_of,
                   as_of + timedelta(days=self.decay_days), year_result.reason),
        ]

    # ---- 动量级 ----

    def momentum_tier(self, macd_result: IndicatorResult, rsi_result: IndicatorResult,
                      as_of: datetime) -> list[Signal]:
        return [
            Signal(SignalTier.MOMENTUM, "MACD", macd_result.state, as_of,
                   as_of + timedelta(days=self.decay_days), macd_result.reason),
            Signal(SignalTier.MOMENTUM, "RSI", rsi_result.state, as_of,
                   as_of + timedelta(days=self.decay_days), rsi_result.reason),
        ]

    # ---- 极值级 ----

    def extreme_tier(self, kdj_result: IndicatorResult, boll_result: IndicatorResult,
                     as_of: datetime) -> list[Signal]:
        return [
            Signal(SignalTier.EXTREME, "KDJ", kdj_result.state, as_of,
                   as_of + timedelta(days=self.decay_days), kdj_result.reason),
            Signal(SignalTier.EXTREME, "BOLL", boll_result.state, as_of,
                   as_of + timedelta(days=self.decay_days), boll_result.reason),
        ]

    # ---- 冲突解决 ----

    def decide(self, signals: list[Signal], now: Optional[datetime] = None) -> Decision:
        """按趋势级优先规则产出决策。

        决策树（严格按此顺序，不跳级）：

          1. 剔除已过期信号
          2. 趋势级存在 UNKNOWN   -> WAIT（不知道就不动，不猜）
          3. 趋势级全空头         -> BLOCK（否决，不看其他级）
          4. 趋势级含中性         -> WAIT（方向不明，不进场）
          5. 趋势级全多头
               a. 动量级全多头   -> ENTRY
               b. 动量级非全多头 -> WAIT（趋势对了，时点未到）
          6. 兜底                 -> WAIT

        注：早期版本漏了分支 4，导致「一个中性 + 一个多头」会落到兜底分支。
        中性必须显式处理 —— 它表示方向不明，不是"默认看多"。
        """
        ref = now or datetime.now()
        live = [s for s in signals if not s.is_expired(ref)]

        trend = [s for s in live if s.tier == SignalTier.TREND]
        momentum = [s for s in live if s.tier == SignalTier.MOMENTUM]
        extreme = [s for s in live if s.tier == SignalTier.EXTREME]

        # 分支 2：数据缺失优先判定 —— 不知道就不动
        if not trend or any(s.state == TriState.UNKNOWN for s in trend):
            return Decision(
                Action.WAIT, None, "趋势级数据缺失，不做判断（不猜）",
                signals, blocked_by="趋势级数据缺失",
            )

        trend_bull = sum(1 for s in trend if s.state == TriState.BULL)
        trend_bear = sum(1 for s in trend if s.state == TriState.BEAR)
        trend_neutral = sum(1 for s in trend if s.state == TriState.NEUTRAL)

        # 分支 3：趋势级全空头 -> 否决
        if trend_bear == len(trend):
            return Decision(
                Action.BLOCK, SignalTier.TREND,
                "趋势级全部空头，动量级信号不构成入场依据",
                signals, blocked_by="趋势级空头",
            )

        # 分支 4：趋势级含中性 -> 方向不明
        if trend_neutral > 0:
            return Decision(
                Action.WAIT, SignalTier.TREND,
                f"趋势级有 {trend_neutral}/{len(trend)} 项为中性，方向不明，等待明确",
                signals,
            )

        # 分支 4b：趋势级多空混合 -> 方向不一致
        # 早期版本漏了这个分支，导致 (bull, bear) 组合落到兜底。
        if trend_bull > 0 and trend_bear > 0:
            return Decision(
                Action.WAIT, SignalTier.TREND,
                f"趋势级方向不一致（{trend_bull} 多 / {trend_bear} 空），等待明确",
                signals,
            )

        # 分支 5：趋势级全多头
        if trend_bull == len(trend):
            mom_bull = sum(1 for s in momentum if s.state == TriState.BULL)
            if momentum and mom_bull == len(momentum):
                note = "趋势与动量一致向上"
                if any(s.state == TriState.OVERBOUGHT for s in extreme):
                    note += "；但极值级已超买，建议分批而非一次性建仓"
                return Decision(Action.ENTRY, SignalTier.MOMENTUM, note, signals)
            return Decision(
                Action.WAIT, SignalTier.MOMENTUM,
                "趋势已转多，但动量未确认（这是趋势级优先的固有代价：右侧入场）",
                signals,
            )

        # 分支 6：兜底（正常不应到达）
        return Decision(
            Action.WAIT, None,
            f"未匹配规则分支（bull={trend_bull} bear={trend_bear} "
            f"neutral={trend_neutral} n={len(trend)}）—— 请检查趋势级信号状态取值",
            signals,
        )

    # ---- 取舍声明 ----

    @staticmethod
    def tradeoff_note() -> str:
        """本引擎的取舍声明。

        必须显式说明，否则使用者会误以为"趋势级优先"是免费的优势。
        """
        return (
            "取舍声明：趋势级优先规则用**收益换确定性**。\n"
            "  得到：不必在盘中现场裁决，杜绝情绪决策；避免在下跌趋势中接飞刀。\n"
            "  付出：趋势级是慢变量，从空头转多头时价格往往已涨一段，"
            "系统会系统性右侧晚入场，牺牲部分启动段收益。\n"
            "  这是设计选择，不是缺陷。若无法接受，应改用其他规则，"
            "但需自行承担相应的盘中裁决压力。"
        )


if __name__ == "__main__":
    print("=" * 70)
    print("三级信号引擎 · 自检")
    print("=" * 70)
    print()
    print(SignalEngine.tradeoff_note())
    print()

    from qsys.indicators.layers import IndicatorResult, IndicatorLayer
    now = datetime(2026, 10, 8, 15, 0)
    th = Thresholds()
    eng = SignalEngine(th)

    def mk(name, state, reason=""):
        return IndicatorResult(name, IndicatorLayer.TECHNICAL, state, reason=reason)

    scenarios = [
        ("场景A · 趋势空头 + 动量金叉（原框架会在这里买入）",
         mk("均线排列", TriState.BEAR, "空头排列"), mk("价格vs年线", TriState.BEAR, "价在年线下方"),
         mk("MACD", TriState.BULL, "金叉"), mk("RSI", TriState.BULL, "偏强"),
         mk("KDJ", TriState.OVERSOLD, "超卖"), mk("BOLL", TriState.OVERSOLD, "触下轨")),

        ("场景B · 趋势多头 + 动量确认",
         mk("均线排列", TriState.BULL, "多头排列"), mk("价格vs年线", TriState.BULL, "价在年线上方"),
         mk("MACD", TriState.BULL, "金叉"), mk("RSI", TriState.BULL, "偏强"),
         mk("KDJ", TriState.NEUTRAL, "中性"), mk("BOLL", TriState.NEUTRAL, "中轨附近")),

        ("场景C · 趋势多头 + 动量未确认",
         mk("均线排列", TriState.BULL, "多头排列"), mk("价格vs年线", TriState.BULL, "价在年线上方"),
         mk("MACD", TriState.NEUTRAL, "未确认"), mk("RSI", TriState.NEUTRAL, "中性"),
         mk("KDJ", TriState.NEUTRAL, "中性"), mk("BOLL", TriState.NEUTRAL, "中轨附近")),

        ("场景D · 趋势数据缺失（只有 66 根 K 线，算不出 MA250）",
         mk("均线排列", TriState.UNKNOWN, "需 250 根 K 线"), mk("价格vs年线", TriState.UNKNOWN, "需 250 根 K 线"),
         mk("MACD", TriState.BULL, "金叉"), mk("RSI", TriState.BULL, "偏强"),
         mk("KDJ", TriState.NEUTRAL, "中性"), mk("BOLL", TriState.NEUTRAL, "中轨附近")),
    ]

    for title, ma, yr, macd, rsi, kdj, boll in scenarios:
        print("-" * 70)
        print(title)
        print("-" * 70)
        sigs = (eng.trend_tier(ma, yr, now)
                + eng.momentum_tier(macd, rsi, now)
                + eng.extreme_tier(kdj, boll, now))
        d = eng.decide(sigs, now=now)
        print(d.explain())
        print()

    print("-" * 70)
    print("信号衰减演示：三天前的金叉，今天还能用吗")
    print("-" * 70)
    old = Signal(SignalTier.MOMENTUM, "MACD", TriState.BULL,
                 now - timedelta(days=5), now - timedelta(days=2), "5 天前的金叉")
    print(f"  信号: {old}")
    print(f"  当前时刻: {now:%Y-%m-%d}")
    print(f"  已过期: {old.is_expired(now)}  -> 不参与决策")
