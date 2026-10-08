"""
风控层：仓位 / 止损 / 最坏损失
==============================

核心修正
--------
原框架的风控顺序是「先定仓位（8%–12%），再找止损位放哪」。
正确顺序是反的：

    ① 先定单笔最大可承受损失（由回撤预算决定，不由行情决定）
    ② 再定止损宽度（用 ATR 倍数，而非固定整数）
    ③ 仓位 = 风险预算 / 止损宽度（反推出来的结果）

这个顺序的差别不是形式上的：
  - 正序（先仓位）—— 无法保证单笔最大损失可控
  - 反序（先损失）—— 单笔最大损失是**输入**，不是**结果**

两层止损
--------
  价格止损 —— 回答"我亏了多少"
  逻辑止损 —— 回答"我为什么买它还成立吗"

逻辑止损优先级**高于**价格止损。原因：前者是原因，后者是结果。
如果买入理由已不成立，继续用价格止损等反弹，本质是在赌运气。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from qsys.rules.cn import CNMarketRules, Board


# ============================================================================
# 止损
# ============================================================================

class StopType(str, Enum):
    STRUCTURE = "structure"    # 结构位（跌破前低）
    ATR = "atr"                # 波动率（N × ATR）
    LOGIC = "logic"            # 逻辑（基本面证伪）
    HARD = "hard"              # 硬约束（回撤熔断）


@dataclass
class StopLevel:
    """一个止损档位。"""

    stop_type: StopType
    price: Optional[float]
    width_pct: float
    reason: str
    is_active: bool = True


@dataclass
class StopPlan:
    """完整的止损方案。"""

    entry_price: float
    levels: list[StopLevel] = field(default_factory=list)
    logic_stop_triggered: bool = False
    logic_stop_reason: str = ""

    @property
    def effective_stop(self) -> Optional[StopLevel]:
        """生效的止损位。

        规则：**取所有价格类档位中距现价最近的那个**。

        这是对原方案「跌破前低 或 2×ATR，任一触发」的修正 ——
        原表述下，若两条相差很远，实际生效的永远是更紧的那条，
        另一条形同虚设。不如直接说明：取更近者。

        逻辑止损不参与价格比较，它单独判定且优先级更高。
        """
        price_levels = [
            lv for lv in self.levels
            if lv.is_active and lv.price is not None and lv.stop_type != StopType.LOGIC
        ]
        if not price_levels:
            return None
        return max(price_levels, key=lambda lv: lv.price)

    def evaluate(self, current_price: float) -> dict:
        """评估当前是否触发止损。"""
        if self.logic_stop_triggered:
            return {
                "triggered": True,
                "type": StopType.LOGIC.value,
                "reason": f"逻辑止损优先于价格止损：{self.logic_stop_reason}",
                "action": "降仓（即使价格未到止损线）",
            }
        eff = self.effective_stop
        if eff is None:
            return {"triggered": False, "reason": "无生效止损档位"}
        if current_price <= eff.price:
            return {
                "triggered": True,
                "type": eff.stop_type.value,
                "stop_price": eff.price,
                "reason": f"现价 {current_price:.2f} 跌破 {eff.stop_type.value} 止损位 {eff.price:.2f}",
                "action": "按预案减仓",
            }
        return {
            "triggered": False,
            "stop_price": eff.price,
            "distance_pct": round((current_price / eff.price - 1) * 100, 2),
            "reason": f"现价 {current_price:.2f} 距止损位 {eff.price:.2f} 还有 {(current_price / eff.price - 1) * 100:.2f}%",
        }


class StopBuilder:
    """止损方案构建器。"""

    def __init__(self, atr_multiple: float = 2.0) -> None:
        self.atr_multiple = atr_multiple

    def build(
        self,
        entry_price: float,
        atr20: Optional[float],
        prior_low: Optional[float],
        logic_conditions: Optional[list[tuple[str, bool]]] = None,
    ) -> StopPlan:
        """构建止损方案。

        参数
        ----
        prior_low
            结构位。**注意**：必须是真实的近期低点，不能想当然。
            实证教训：原框架把 38.52 元当作"前低"，但它其实是 2026-07-01 的
            **开盘价**（也是 6/30 收盘价），既不是 7 月低点（36.92），
            也不是 9 月低点（38.42）。
        logic_conditions
            [(条件描述, 是否已触发), ...]。任一触发即启用逻辑止损。
        """
        plan = StopPlan(entry_price=entry_price)

        if prior_low is not None and prior_low < entry_price:
            plan.levels.append(StopLevel(
                StopType.STRUCTURE, round(prior_low, 2),
                round((prior_low / entry_price - 1) * 100, 2),
                f"结构位止损：跌破前低 {prior_low:.2f}",
            ))

        if atr20 is not None and atr20 > 0:
            atr_stop = entry_price - self.atr_multiple * atr20
            plan.levels.append(StopLevel(
                StopType.ATR, round(atr_stop, 2),
                round((atr_stop / entry_price - 1) * 100, 2),
                f"波动率止损：{self.atr_multiple:g} × ATR20({atr20:.3f})",
            ))

        if logic_conditions:
            fired = [(d, t) for d, t in logic_conditions if t]
            if fired:
                plan.logic_stop_triggered = True
                plan.logic_stop_reason = "；".join(d for d, _ in fired)
            plan.levels.append(StopLevel(
                StopType.LOGIC, None, 0.0,
                "逻辑止损：" + ("；".join(d for d, _ in fired) if fired else "所有逻辑条件均未触发"),
                is_active=bool(fired),
            ))

        return plan


# ============================================================================
# 仓位
# ============================================================================

@dataclass
class SizingResult:
    quantity: int
    capital: float
    capital_pct: float
    risk_budget: float
    per_share_risk: float
    method: str
    warnings: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        w = ("  ⚠ " + "；".join(self.warnings)) if self.warnings else ""
        return (f"{self.method}: {self.quantity} 股 / {self.capital:,.0f} 元 "
                f"({self.capital_pct:.2f}% 仓位){w}")


class PositionSizer:
    """仓位计算器。

    两种方法：
      standard    —— 标准风险预算法（假设止损必然成交）
      worst_case  —— 制度修正版（用连续跌停后的最坏损失反推）
    """

    def __init__(self, rules: Optional[CNMarketRules] = None) -> None:
        self.rules = rules or CNMarketRules()

    def by_standard(
        self,
        total_capital: float,
        risk_pct: float,
        entry_price: float,
        stop_price: float,
        symbol: str,
        max_position_pct: Optional[float] = None,
    ) -> SizingResult:
        """标准风险预算法。

        ⚠️ 隐含假设：止损一定能在 stop_price 成交。
        A 股连续跌停时该假设不成立 —— 请优先使用 by_worst_case。
        """
        if stop_price >= entry_price:
            raise ValueError(f"止损价 {stop_price} 必须低于入场价 {entry_price}")

        budget = total_capital * risk_pct / 100.0
        per_share = entry_price - stop_price
        raw_qty = int(budget / per_share)
        qty = self.rules.round_quantity(raw_qty, symbol)

        warnings = ["标准法假设止损必然成交；A 股连续跌停时该假设不成立"]

        cap = qty * entry_price
        if max_position_pct is not None:
            cap_limit = total_capital * max_position_pct / 100.0
            if cap > cap_limit:
                qty = self.rules.round_quantity(int(cap_limit / entry_price), symbol)
                cap = qty * entry_price
                warnings.append(f"受单标的上限 {max_position_pct:.0f}% 约束已下调")

        return SizingResult(
            quantity=qty, capital=round(cap, 2),
            capital_pct=round(cap / total_capital * 100, 2),
            risk_budget=round(budget, 2), per_share_risk=round(per_share, 4),
            method="标准风险预算法", warnings=warnings,
        )

    def by_worst_case(
        self,
        total_capital: float,
        risk_pct: float,
        entry_price: float,
        stop_price: float,
        symbol: str,
        max_consecutive_limit_down: int,
        max_position_pct: Optional[float] = None,
        is_st: bool = False,
    ) -> SizingResult:
        """制度修正版：用连续跌停后的最坏损失反推仓位。

        max_consecutive_limit_down **必须由用户提供** ——
        它是该标的的历史极端值，不同标的差异极大，无法由系统推定。
        """
        wc = self.rules.worst_case_loss(
            entry_price=entry_price, quantity=1, symbol=symbol,
            stop_price=stop_price,
            max_consecutive_limit_down=max_consecutive_limit_down,
            is_st=is_st,
        )
        per_share = wc["realized_loss"]
        if per_share <= 0:
            raise ValueError("最坏损失为 0，请检查入场价与止损价")

        budget = total_capital * risk_pct / 100.0
        qty = self.rules.round_quantity(int(budget / per_share), symbol)
        cap = qty * entry_price

        warnings: list[str] = []
        if not wc["is_formula_valid"]:
            warnings.append(
                f"该标的最坏情形损失是计划损失的 {wc['amplification']:.2f} 倍，"
                f"标准风险预算公式在此标的上不成立，已按最坏情形折减仓位"
            )
        if max_position_pct is not None:
            cap_limit = total_capital * max_position_pct / 100.0
            if cap > cap_limit:
                qty = self.rules.round_quantity(int(cap_limit / entry_price), symbol)
                cap = qty * entry_price
                warnings.append(f"受单标的上限 {max_position_pct:.0f}% 约束已下调")

        return SizingResult(
            quantity=qty, capital=round(cap, 2),
            capital_pct=round(cap / total_capital * 100, 2),
            risk_budget=round(budget, 2), per_share_risk=round(per_share, 4),
            method=f"制度修正版（{max_consecutive_limit_down} 连跌停假设）",
            warnings=warnings,
        )

    def compare(
        self,
        total_capital: float,
        risk_pct: float,
        entry_price: float,
        stop_price: float,
        symbol: str,
        max_consecutive_limit_down: int,
    ) -> dict:
        """对比两种方法，量化制度约束带来的折减。"""
        a = self.by_standard(total_capital, risk_pct, entry_price, stop_price, symbol)
        b = self.by_worst_case(total_capital, risk_pct, entry_price, stop_price,
                               symbol, max_consecutive_limit_down)
        ratio = (b.capital_pct / a.capital_pct) if a.capital_pct > 0 else 0.0
        return {
            "standard": a,
            "worst_case": b,
            "shrink_ratio": round(ratio, 4),
            "conclusion": (
                f"制度修正后仓位为标准法的 {ratio * 100:.1f}%，"
                f"即折减 {(1 - ratio) * 100:.1f}%。"
                f"若忽略制度约束，实际风险敞口将超出预算 {(1 / ratio - 1) * 100:.0f}%。"
                if ratio > 0 else "无法比较"
            ),
        }


# ============================================================================
# 回撤预算
# ============================================================================

@dataclass
class DrawdownBudget:
    """回撤预算 —— 单笔风险预算的锚点。

    原框架的「8%–12% 仓位」没有对应的总资金基数与单笔损失上限，
    所以它无法回答"如果连续错 5 次会怎样"。
    """

    total_capital: float
    max_drawdown_pct: float
    max_loss_per_trade_pct: float
    max_concurrent_positions: int

    def validate(self) -> dict:
        """校验预算的自洽性。"""
        issues: list[str] = []

        worst_all = self.max_loss_per_trade_pct * self.max_concurrent_positions
        if worst_all > self.max_drawdown_pct:
            issues.append(
                f"全部持仓同时触发止损将亏损 {worst_all:.1f}%，"
                f"超过回撤熔断线 {self.max_drawdown_pct:.1f}%。"
                f"建议单笔风险降至 {self.max_drawdown_pct / self.max_concurrent_positions:.2f}% 以下"
            )

        if self.max_loss_per_trade_pct <= 0:
            issues.append("单笔风险预算必须为正")
        if self.max_drawdown_pct <= 0:
            issues.append("回撤熔断线必须为正")

        consec = self.max_drawdown_pct / self.max_loss_per_trade_pct if self.max_loss_per_trade_pct else 0
        return {
            "is_valid": not issues,
            "issues": issues,
            "max_loss_if_all_stopped": round(worst_all, 2),
            "consecutive_losses_to_breaker": round(consec, 1),
            "note": f"连续 {consec:.1f} 次全额止损即触及熔断线",
        }


if __name__ == "__main__":
    print("=" * 72)
    print("风控层 · 自检")
    print("=" * 72)

    sizer = PositionSizer()
    builder = StopBuilder(atr_multiple=2.0)

    print("\n【一】止损方案（入场 39.26，ATR20 = 0.98，前低 38.42）")
    print("-" * 72)
    plan = builder.build(
        entry_price=39.26, atr20=0.98, prior_low=38.42,
        logic_conditions=[
            ("扣非净利连续 2 季降幅扩大", False),
            ("经营现金流转负", False),
        ],
    )
    for lv in plan.levels:
        p = f"{lv.price:.2f}" if lv.price else "—"
        print(f"  {lv.stop_type.value:<10} {p:>8}  {lv.width_pct:>7.2f}%  {lv.reason}")
    eff = plan.effective_stop
    print(f"\n  生效止损位（取更近者）: {eff.stop_type.value} @ {eff.price:.2f} "
          f"（宽度 {eff.width_pct:.2f}%）")
    print(f"  说明：结构位 {38.42:.2f} 比 ATR 位 {39.26 - 2 * 0.98:.2f} 更近，故结构位生效；")
    print(f"        原方案「跌破前低 或 2×ATR，任一触发」的实际效果与此相同，")
    print(f"        但原表述会让人误以为两条都在起作用。")

    print("\n【二】逻辑止损优先级演示")
    print("-" * 72)
    plan2 = builder.build(
        entry_price=39.26, atr20=0.98, prior_low=38.42,
        logic_conditions=[("扣非净利连续 2 季降幅扩大", True)],
    )
    r = plan2.evaluate(current_price=39.00)
    print(f"  现价 39.00（未触及任何价格止损位）")
    print(f"  评估结果: {r}")

    print("\n【三】仓位对比：标准法 vs 制度修正版")
    print("-" * 72)
    cmp = sizer.compare(
        total_capital=1_000_000, risk_pct=1.0,
        entry_price=45.44, stop_price=40.00,
        symbol="002422", max_consecutive_limit_down=3,
    )
    print(f"  {cmp['standard']}")
    print(f"  {cmp['worst_case']}")
    print(f"\n  {cmp['conclusion']}")

    print("\n【四】回撤预算自洽性校验")
    print("-" * 72)
    for cfg in [
        dict(total_capital=1_000_000, max_drawdown_pct=20.0,
             max_loss_per_trade_pct=1.0, max_concurrent_positions=10),
        dict(total_capital=1_000_000, max_drawdown_pct=10.0,
             max_loss_per_trade_pct=2.0, max_concurrent_positions=10),
    ]:
        b = DrawdownBudget(**cfg)
        v = b.validate()
        tag = "通过" if v["is_valid"] else "不通过"
        print(f"  回撤熔断 {cfg['max_drawdown_pct']:.0f}% / 单笔 {cfg['max_loss_per_trade_pct']:.1f}% "
              f"/ 最多 {cfg['max_concurrent_positions']} 只  ->  {tag}")
        for i in v["issues"]:
            print(f"      ⚠ {i}")
        print(f"      {v['note']}")

    print("\n【五】对原框架「8%–12% 仓位」的检验")
    print("-" * 72)
    print("  原框架：卫星仓位 8%–12%，止损 40 元（成本 45.44，宽度 11.97%）")
    implied_risk = 10.0 * 0.1197
    print(f"  隐含单笔风险 = 10% 仓位 × 11.97% 止损宽度 = {implied_risk:.2f}% 总资金")
    print(f"  若回撤熔断线为 20%，则连续 {20 / implied_risk:.1f} 次止损即触及熔断")
    print(f"  若同时持有 5 只同类仓位，全部止损将亏损 {implied_risk * 5:.2f}%")
    print("  -> 原框架的仓位与止损是「先定仓位再找止损」的产物，未经此校验")
