"""
三层指标：基本面 / 技术面 / 资金面
==================================

层级归属说明（避免与"三级信号"混淆）
------------------------------------
本模块 = **三层指标**（indicator_layer）：基本面 / 技术面 / 资金面 —— 按**信息类型**划分
signals 模块 = **三级信号**（signal_tier）：趋势 / 动量 / 极值 —— 按**决策用途**划分

两者是正交的：技术面层（指标）会同时供给趋势级与动量级（信号）。
代码中分别用 indicator_layer 与 signal_tier，不要混用。

设计原则
--------
1. 每个指标输出**三态**（多头/中性/空头 或 通过/预警/否决），而不是裸数值。
   裸数值会诱发"这个 42.61 到底算好还是坏"的伪精确讨论。
2. 每个指标必须回传所用阈值，便于审计"这个结论是基于哪个数得出的"。
3. 缺数据时返回 UNKNOWN，**不得默认为通过**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from qsys.data.base import FieldValue


# ============================================================================
# 三态
# ============================================================================

class TriState(str, Enum):
    BULL = "bull"        # 多头 / 通过
    NEUTRAL = "neutral"  # 中性 / 预警
    BEAR = "bear"        # 空头 / 否决
    OVERBOUGHT = "overbought"
    OVERSOLD = "oversold"
    UNKNOWN = "unknown"  # 数据缺失


class IndicatorLayer(str, Enum):
    FUNDAMENTAL = "fundamental"
    TECHNICAL = "technical"
    CAPITAL_FLOW = "capital_flow"


@dataclass
class IndicatorResult:
    """单个指标的评估结果。"""

    name: str
    layer: IndicatorLayer
    state: TriState
    value: object = None
    threshold: object = None
    reason: str = ""
    field_ref: Optional[FieldValue] = None

    def __str__(self) -> str:
        return f"[{self.layer.value:<12}] {self.name:<24} {self.state.value:<11} {self.reason}"


# ============================================================================
# 阈值配置
# ============================================================================

@dataclass
class Thresholds:
    """判据阈值。默认值来自《固定指标配置》，标记 PENDING 的项未经校准。"""

    # ---- 基本面 ----
    revenue_growth_min: float = 20.0
    deducted_np_growth_min: float = 0.0
    divergence_alert_pct: float = 15.0
    gross_margin_trend_window: int = 3
    roe_min: float = 15.0
    debt_to_asset_max: float = 60.0
    goodwill_to_equity_max: float = 10.0
    ocf_to_np_min: float = 0.8

    # ---- 技术面 ----
    ma_periods: tuple[int, ...] = (5, 10, 20, 60, 250)
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0
    kdj_overbought: float = 100.0
    kdj_oversold: float = 0.0
    atr_period: int = 20
    stop_loss_atr_multiple: float = 2.0
    volatility_window: int = 60
    volatility_percentile_window: int = 250

    # ---- 资金面 ----
    crowding_threshold: Optional[float] = None
    """⚠️ PENDING_CALIBRATION —— 原值 80% 分位来自单一案例，属方法论错误，已撤回。
    须先做分档回测校准：对历史全样本按分位分 5 档，
    检验各档后续 20 日收益与最大回撤是否单调分化。校准完成前不得写入规则。"""

    @property
    def crowding_is_calibrated(self) -> bool:
        return self.crowding_threshold is not None


# ============================================================================
# 基本面层
# ============================================================================

class FundamentalLayer:
    """基本面层：回答"这家公司值不值得进池"。"""

    def __init__(self, th: Thresholds) -> None:
        self.th = th

    def revenue_growth(self, current: FieldValue, prior: FieldValue) -> IndicatorResult:
        if current.value is None or prior.value in (None, 0):
            return IndicatorResult("营收增速", IndicatorLayer.FUNDAMENTAL, TriState.UNKNOWN, reason="数据缺失")
        g = (current.value - prior.value) / prior.value * 100.0
        state = TriState.BULL if g > self.th.revenue_growth_min else (
            TriState.NEUTRAL if g >= 0 else TriState.BEAR
        )
        return IndicatorResult(
            "营收增速", IndicatorLayer.FUNDAMENTAL, state, round(g, 2), self.th.revenue_growth_min,
            f"{g:+.2f}% vs 门槛 {self.th.revenue_growth_min:+.0f}%", current,
        )

    def deducted_np_growth(self, current: FieldValue, prior: FieldValue) -> IndicatorResult:
        """扣非净利增速。

        这是本层最重要的指标 —— 它的存在直接来自一个实证：
        科伦药业 2026H1 归母净利 +12.71%，而扣非净利 −29.03%，背离 41.7pct。
        只看归母会得出"基本面改善"的相反结论。
        """
        if current.value is None or prior.value in (None, 0):
            return IndicatorResult("扣非净利增速", IndicatorLayer.FUNDAMENTAL, TriState.UNKNOWN, reason="数据缺失")
        g = (current.value - prior.value) / prior.value * 100.0
        state = TriState.BULL if g > self.th.deducted_np_growth_min else TriState.BEAR
        return IndicatorResult(
            "扣非净利增速", IndicatorLayer.FUNDAMENTAL, state, round(g, 2), self.th.deducted_np_growth_min,
            f"{g:+.2f}% vs 门槛 {self.th.deducted_np_growth_min:+.0f}%（否决项）", current,
        )

    def divergence_ratio(self, np_growth: float, deducted_growth: float) -> IndicatorResult:
        """归母 / 扣非背离度。背离越大，利润质量越可疑。"""
        d = abs(np_growth - deducted_growth)
        state = TriState.BULL if d < self.th.divergence_alert_pct else TriState.NEUTRAL
        return IndicatorResult(
            "归母扣非背离度", IndicatorLayer.FUNDAMENTAL, state, round(d, 2), self.th.divergence_alert_pct,
            f"{d:.1f}pct vs 预警线 {self.th.divergence_alert_pct:.0f}pct",
        )

    def gross_margin_trend(self, margins: list[FieldValue]) -> IndicatorResult:
        """毛利率趋势。单期值无意义，趋势才有意义。"""
        w = self.th.gross_margin_trend_window
        vals = [m.value for m in margins[-w:] if m.value is not None]
        if len(vals) < w:
            return IndicatorResult("毛利率趋势", IndicatorLayer.FUNDAMENTAL, TriState.UNKNOWN, reason=f"不足 {w} 期")
        declining = all(vals[i] >= vals[i + 1] for i in range(len(vals) - 1))
        rising = all(vals[i] <= vals[i + 1] for i in range(len(vals) - 1))
        state = TriState.BEAR if declining else (TriState.BULL if rising else TriState.NEUTRAL)
        return IndicatorResult(
            "毛利率趋势", IndicatorLayer.FUNDAMENTAL, state, [round(v, 2) for v in vals], w,
            f"连续 {w} 期：{'持续下滑' if declining else ('持续上行' if rising else '震荡')}",
        )

    def ocf_to_np(self, ocf: FieldValue, np_val: FieldValue) -> IndicatorResult:
        """经营现金流 / 归母净利 —— 利润含金量。"""
        if not ocf.value or not np_val.value:
            return IndicatorResult("现金流/净利", IndicatorLayer.FUNDAMENTAL, TriState.UNKNOWN, reason="数据缺失")
        r = ocf.value / np_val.value
        state = TriState.BULL if r > self.th.ocf_to_np_min else TriState.BEAR
        return IndicatorResult(
            "现金流/净利", IndicatorLayer.FUNDAMENTAL, state, round(r, 2), self.th.ocf_to_np_min,
            f"{r:.2f} vs 门槛 {self.th.ocf_to_np_min:.2f}", ocf,
        )

    def debt_to_asset(self, total_liab: FieldValue, total_asset: FieldValue) -> IndicatorResult:
        if not total_asset.value:
            return IndicatorResult("资产负债率", IndicatorLayer.FUNDAMENTAL, TriState.UNKNOWN, reason="数据缺失")
        r = total_liab.value / total_asset.value * 100.0
        state = TriState.BULL if r < self.th.debt_to_asset_max else TriState.BEAR
        return IndicatorResult(
            "资产负债率", IndicatorLayer.FUNDAMENTAL, state, round(r, 2), self.th.debt_to_asset_max,
            f"{r:.2f}% vs 上限 {self.th.debt_to_asset_max:.0f}%",
        )

    def goodwill_to_equity(self, goodwill: FieldValue, equity: FieldValue) -> IndicatorResult:
        if not equity.value:
            return IndicatorResult("商誉/净资产", IndicatorLayer.FUNDAMENTAL, TriState.UNKNOWN, reason="数据缺失")
        r = goodwill.value / equity.value * 100.0
        state = TriState.BULL if r < self.th.goodwill_to_equity_max else TriState.NEUTRAL
        return IndicatorResult(
            "商誉/净资产", IndicatorLayer.FUNDAMENTAL, state, round(r, 2), self.th.goodwill_to_equity_max,
            f"{r:.2f}% vs 上限 {self.th.goodwill_to_equity_max:.0f}%",
        )


# ============================================================================
# 技术面层
# ============================================================================

class TechnicalLayer:
    """技术面层：回答"现在是不是合适的时点"。

    注意：本层输出的是**原始三态**；具体到"该不该买"由 signals 模块的
    三级信号与冲突规则决定（趋势级优先）。
    """

    def __init__(self, th: Thresholds) -> None:
        self.th = th

    # ---- 均线 ----

    @staticmethod
    def sma(values: list[float], period: int) -> Optional[float]:
        if len(values) < period:
            return None
        return sum(values[-period:]) / period

    def ma_alignment(self, closes: list[float]) -> IndicatorResult:
        """均线排列 —— 趋势级信号的主要来源。"""
        need = max(self.th.ma_periods)
        if len(closes) < need:
            return IndicatorResult("均线排列", IndicatorLayer.TECHNICAL, TriState.UNKNOWN, reason=f"需 {need} 根 K 线")
        mas = {p: self.sma(closes, p) for p in self.th.ma_periods}
        seq = [mas[p] for p in self.th.ma_periods if mas[p] is not None]
        price = closes[-1]
        ascending = all(seq[i] > seq[i + 1] for i in range(len(seq) - 1))
        descending = all(seq[i] < seq[i + 1] for i in range(len(seq) - 1))
        above_year = price > mas[250] if mas.get(250) else None

        if ascending and above_year:
            state, note = TriState.BULL, "多头排列且价在年线上方"
        elif descending or above_year is False:
            state, note = TriState.BEAR, "空头排列或价在年线下方"
        else:
            state, note = TriState.NEUTRAL, "排列不一致"
        return IndicatorResult(
            "均线排列", IndicatorLayer.TECHNICAL, state,
            {f"MA{p}": round(v, 2) for p, v in mas.items() if v} | {"price": price},
            self.th.ma_periods, note,
        )

    def price_vs_ma250(self, closes: list[float]) -> IndicatorResult:
        ma = self.sma(closes, 250)
        if ma is None:
            return IndicatorResult("价格vs年线", IndicatorLayer.TECHNICAL, TriState.UNKNOWN, reason="需 250 根 K 线")
        price = closes[-1]
        pct = (price / ma - 1) * 100.0
        state = TriState.BULL if pct > 0 else TriState.BEAR
        return IndicatorResult(
            "价格vs年线", IndicatorLayer.TECHNICAL, state, round(pct, 2), 0.0,
            f"价 {price:.2f} vs MA250 {ma:.2f}（{pct:+.2f}%）",
        )

    # ---- 动量 ----

    @staticmethod
    def ema(values: list[float], period: int) -> list[float]:
        if not values:
            return []
        k = 2.0 / (period + 1)
        out = [values[0]]
        for v in values[1:]:
            out.append(v * k + out[-1] * (1 - k))
        return out

    def macd(self, closes: list[float]) -> IndicatorResult:
        if len(closes) < 35:
            return IndicatorResult("MACD", IndicatorLayer.TECHNICAL, TriState.UNKNOWN, reason="需 35 根 K 线")
        dif = [a - b for a, b in zip(self.ema(closes, 12), self.ema(closes, 26))]
        dea = self.ema(dif, 9)
        hist = [(d - e) * 2 for d, e in zip(dif, dea)]
        golden = dif[-1] > dea[-1] and hist[-1] > 0
        dead = dif[-1] < dea[-1] and hist[-1] < 0
        state = TriState.BULL if golden else (TriState.BEAR if dead else TriState.NEUTRAL)
        return IndicatorResult(
            "MACD", IndicatorLayer.TECHNICAL, state,
            {"DIF": round(dif[-1], 4), "DEA": round(dea[-1], 4), "HIST": round(hist[-1], 4)},
            "DIF>DEA 且柱>0",
            f"DIF {dif[-1]:.3f} / DEA {dea[-1]:.3f} / 柱 {hist[-1]:+.3f}",
        )

    @staticmethod
    def rsi(closes: list[float], period: int = 12) -> Optional[float]:
        if len(closes) < period + 1:
            return None
        gains, losses = [], []
        for i in range(1, len(closes)):
            d = closes[i] - closes[i - 1]
            gains.append(max(d, 0.0))
            losses.append(max(-d, 0.0))
        ag = sum(gains[-period:]) / period
        al = sum(losses[-period:]) / period
        if al == 0:
            return 100.0
        return 100.0 - 100.0 / (1 + ag / al)

    def rsi_state(self, closes: list[float]) -> IndicatorResult:
        r = self.rsi(closes, 12)
        if r is None:
            return IndicatorResult("RSI12", IndicatorLayer.TECHNICAL, TriState.UNKNOWN, reason="数据不足")
        if r > self.th.rsi_overbought:
            state = TriState.OVERBOUGHT
        elif r < self.th.rsi_oversold:
            state = TriState.OVERSOLD
        else:
            state = TriState.NEUTRAL
        return IndicatorResult(
            "RSI12", IndicatorLayer.TECHNICAL, state, round(r, 2),
            (self.th.rsi_oversold, self.th.rsi_overbought), f"RSI12 = {r:.2f}",
        )

    # ---- 极值 ----

    @staticmethod
    def kdj(closes: list[float], period: int = 9) -> Optional[tuple[float, float, float]]:
        if len(closes) < period:
            return None
        k, d = 50.0, 50.0
        for i in range(period - 1, len(closes)):
            window = closes[i - period + 1: i + 1]
            lo, hi = min(window), max(window)
            rsv = 50.0 if hi == lo else (closes[i] - lo) / (hi - lo) * 100.0
            k = 2 / 3 * k + 1 / 3 * rsv
            d = 2 / 3 * d + 1 / 3 * k
        return k, d, 3 * k - 2 * d

    def kdj_state(self, closes: list[float]) -> IndicatorResult:
        r = self.kdj(closes)
        if r is None:
            return IndicatorResult("KDJ", IndicatorLayer.TECHNICAL, TriState.UNKNOWN, reason="数据不足")
        k, d, j = r
        if j > self.th.kdj_overbought:
            state = TriState.OVERBOUGHT
        elif j < self.th.kdj_oversold:
            state = TriState.OVERSOLD
        else:
            state = TriState.NEUTRAL
        return IndicatorResult(
            "KDJ", IndicatorLayer.TECHNICAL, state,
            {"K": round(k, 2), "D": round(d, 2), "J": round(j, 2)},
            (self.th.kdj_oversold, self.th.kdj_overbought), f"J = {j:.2f}",
        )

    def boll(self, closes: list[float], period: int = 20, mult: float = 2.0) -> IndicatorResult:
        if len(closes) < period:
            return IndicatorResult("BOLL", IndicatorLayer.TECHNICAL, TriState.UNKNOWN, reason="数据不足")
        window = closes[-period:]
        mid = sum(window) / period
        var = sum((x - mid) ** 2 for x in window) / period
        sd = var ** 0.5
        upper, lower = mid + mult * sd, mid - mult * sd
        price = closes[-1]
        if price >= upper:
            state = TriState.OVERBOUGHT
        elif price <= lower:
            state = TriState.OVERSOLD
        else:
            state = TriState.NEUTRAL
        return IndicatorResult(
            "BOLL", IndicatorLayer.TECHNICAL, state,
            {"upper": round(upper, 2), "mid": round(mid, 2), "lower": round(lower, 2)},
            (round(lower, 2), round(upper, 2)),
            f"价 {price:.2f} / 下轨 {lower:.2f} / 上轨 {upper:.2f}",
        )

    # ---- 波动率（风控输入） ----

    def atr(self, highs: list[float], lows: list[float], closes: list[float]) -> Optional[float]:
        """平均真实波幅 —— 止损宽度的基准。"""
        n = self.th.atr_period
        if len(closes) < n + 1:
            return None
        trs = []
        for i in range(1, len(closes)):
            trs.append(max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            ))
        return sum(trs[-n:]) / n

    @staticmethod
    def annualized_volatility(closes: list[float], window: int = 60) -> Optional[float]:
        if len(closes) < window + 1:
            return None
        rets = [
            (closes[i] / closes[i - 1] - 1)
            for i in range(len(closes) - window, len(closes))
        ]
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / len(rets)
        return (var ** 0.5) * (252 ** 0.5) * 100.0

    def volatility_percentile(self, closes: list[float]) -> IndicatorResult:
        """个股年化波动率分位。

        A 股波动率替代方案。**理由已修正**：原方案的理由是"中国波指已停止发布"，
        但该断言证据不足（CEIC 数据止于 2018-02-14，另有 2025-06 新指数消息，
        新浪 2026-09-28 仍有行情页），已撤回。

        本方法成立的理由不依赖指数是否存在：
        它回答的是"这只股票比它自己平时更波动吗"。
        """
        w, pw = self.th.volatility_window, self.th.volatility_percentile_window
        if len(closes) < w + pw:
            return IndicatorResult("波动率分位", IndicatorLayer.TECHNICAL, TriState.UNKNOWN,
                                   reason=f"需 {w + pw} 根 K 线")
        series = []
        for end in range(w, len(closes) + 1):
            v = self.annualized_volatility(closes[:end], w)
            if v is not None:
                series.append(v)
        series = series[-pw:]
        cur = series[-1]
        pct = sum(1 for v in series if v <= cur) / len(series) * 100.0
        state = TriState.BEAR if pct > 80 else (TriState.BULL if pct < 30 else TriState.NEUTRAL)
        return IndicatorResult(
            "波动率分位", IndicatorLayer.TECHNICAL, state, round(pct, 1), (30, 80),
            f"年化波动率 {cur:.1f}%，处于近 {pw} 日 {pct:.0f}% 分位",
        )


# ============================================================================
# 资金面层
# ============================================================================

@dataclass
class FlowReading:
    """单个维度的资金面读数。"""

    dimension: str
    value: object
    state: TriState
    note: str


class CapitalFlowLayer:
    """资金面层：回答"谁在买"。

    A 股四维：水位 / 方向 / 结构 / 筹码
    美股替换：机构持股 / 13F / 期权流 / 空头持仓

    设计要点：**水位与方向必须分开**。
    原框架只问"资金是否大幅流出"（方向），
    但融资余额已在 80% 分位（水位）—— 高位 + 方向转负是杠杆放大的前兆，
    不是抄底信号。方向与水位是两个问题。
    """

    def __init__(self, th: Thresholds) -> None:
        self.th = th

    def water_level(self, percentile: Optional[float]) -> FlowReading:
        if percentile is None:
            return FlowReading("水位", None, TriState.UNKNOWN, "数据缺口")
        if not self.th.crowding_is_calibrated:
            # 未校准时不得下结论 —— 这是对"80% 分位来自单样本"那处错误的修正
            return FlowReading(
                "水位", round(percentile, 1), TriState.UNKNOWN,
                f"分位 {percentile:.0f}%，但拥挤度阈值尚未校准，不出结论",
            )
        t = self.th.crowding_threshold
        state = TriState.BEAR if percentile > t else TriState.BULL
        return FlowReading("水位", round(percentile, 1), state, f"分位 {percentile:.0f}% vs 阈值 {t:.0f}%")

    def direction(self, net_flow_5d: Optional[float]) -> FlowReading:
        if net_flow_5d is None:
            return FlowReading("方向", None, TriState.UNKNOWN, "数据缺口")
        state = TriState.BULL if net_flow_5d > 0 else TriState.BEAR
        return FlowReading("方向", round(net_flow_5d, 2), state, f"近 5 日净流入 {net_flow_5d:+.2f}")

    def structure(self, institution_net: Optional[float], hot_money_net: Optional[float]) -> FlowReading:
        """结构：机构 vs 游资。"""
        if institution_net is None and hot_money_net is None:
            return FlowReading("结构", None, TriState.UNKNOWN, "数据缺口")
        inst = institution_net or 0.0
        hot = hot_money_net or 0.0
        if inst > 0 and hot <= 0:
            state, note = TriState.BULL, "机构席位净买入，游资未参与"
        elif hot > 0 and inst <= 0:
            state, note = TriState.BEAR, "纯游资参与，机构未跟"
        else:
            state, note = TriState.NEUTRAL, "机构与游资方向一致或均未参与"
        return FlowReading("结构", {"institution": inst, "hot_money": hot}, state, note)

    def chips(self, holder_change_pct: Optional[float]) -> FlowReading:
        """筹码：股东户数变动。户数下降 = 筹码集中。"""
        if holder_change_pct is None:
            return FlowReading("筹码", None, TriState.UNKNOWN, "数据缺口")
        state = TriState.BULL if holder_change_pct < 0 else TriState.BEAR
        return FlowReading("筹码", round(holder_change_pct, 2), state,
                           f"股东户数变动 {holder_change_pct:+.2f}%")

    def overall_rating(self, readings: list[FlowReading]) -> tuple[str, str]:
        """综合评级。

        关键规则：**水位高 + 方向负 = 背离**，而非"支持"。
        这正是原框架"资金合力不足"这一定性判断需要重新权衡的地方 ——
        同期基金持股比例 +3.82pct、机构评级 100% 买入。
        """
        valid = [r for r in readings if r.state != TriState.UNKNOWN]
        if not valid:
            return "未知", "所有维度均缺数据，无法评级"
        bears = sum(1 for r in valid if r.state == TriState.BEAR)
        bulls = sum(1 for r in valid if r.state == TriState.BULL)

        level = next((r for r in valid if r.dimension == "水位"), None)
        direct = next((r for r in valid if r.dimension == "方向"), None)
        if level and direct and level.state == TriState.BEAR and direct.state == TriState.BEAR:
            return "背离", "水位处高位且方向转负 —— 杠杆放大风险，非抄底信号"

        if bulls >= len(valid) - 1 and bears == 0:
            return "支持", f"{bulls}/{len(valid)} 维度支持"
        if bears >= len(valid) - 1:
            return "背离", f"{bears}/{len(valid)} 维度背离"
        return "中性", f"支持 {bulls} / 背离 {bears} / 共 {len(valid)} 维"
