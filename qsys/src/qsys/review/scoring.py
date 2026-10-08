"""
复盘层：四维评分
================

四个维度
--------
  纪律分 —— 计划 vs 实际成交的偏差。未执行硬止损直接归零
  逻辑分 —— 买入假设是否被证伪
  情绪分 —— 情绪标签与操作方向是否一致
  参数分 —— 用 Sortino 作寻优目标（**已修正，原用卡玛比率**）

关于目标函数的修正
------------------
原方案用卡玛比率（年化收益 / 最大回撤）。问题在于：
  最大回撤是**路径依赖的极值统计量** —— 它只记录样本期内最差的那一次。
  后果：① 对样本起止点高度敏感；② 寻优会偏向"在回测期内恰好没遇到大回撤"
  的参数，而非真正稳健的参数；③ 不反映回撤持续时间与恢复难度。

改用 Sortino 比率（只惩罚下行波动）为主目标，CVaR(5%) 作约束。
卡玛比率保留为展示指标，不再作为优化目标。

关于自动化上限
--------------
四维中只有**纪律分可全自动**（计划与实际成交都在系统里）。
逻辑分与情绪分依赖手工输入 —— 这决定了系统的自动化上限。
不要假装它们可以自动算出来。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Optional


# ============================================================================
# 纪律分
# ============================================================================

class ExecutionType(str, Enum):
    STOP_LOSS = "stop_loss"          # 硬止损
    TAKE_PROFIT = "take_profit"      # 止盈
    REBALANCE = "rebalance"          # 调仓
    MANUAL = "manual"                # 手动操作


@dataclass
class TradeRecord:
    """一笔实际成交记录。"""

    symbol: str
    planned_price: Optional[float]
    actual_price: float
    quantity: int
    executed_at: date
    execution_type: ExecutionType
    plan_was_mandatory: bool = False
    """该动作是否为硬性要求（如硬止损）。硬性要求未执行将触发纪律分归零。"""
    note: str = ""


@dataclass
class DisciplineScore:
    score: float
    """0–100。"""
    violations: list[str] = field(default_factory=list)
    must_take_over: bool = False
    """是否应升级为「系统接管下单权限」。"""

    def __str__(self) -> str:
        s = f"纪律分 {self.score:.1f}/100"
        if self.violations:
            s += f"  违规 {len(self.violations)} 项"
        if self.must_take_over:
            s += "  ⚠ 建议系统接管下单权限"
        return s


def calc_discipline(records: list[TradeRecord], skipped_mandatory: list[TradeRecord]) -> DisciplineScore:
    """计算纪律分。

    规则：
      - 未执行任何硬性动作 -> 直接归零，并建议系统接管
      - 已执行的记录按价格偏差率加权扣分
    """
    if skipped_mandatory:
        return DisciplineScore(
            score=0.0,
            violations=[f"未执行硬性动作：{r.symbol} {r.execution_type.value} @ {r.planned_price}"
                        for r in skipped_mandatory],
            must_take_over=True,
        )

    if not records:
        return DisciplineScore(score=0.0, violations=["无成交记录，无法评分"])

    total_w, weighted = 0.0, 0.0
    violations: list[str] = []
    for r in records:
        if r.planned_price is None or r.planned_price == 0:
            continue
        dev = abs(r.actual_price - r.planned_price) / r.planned_price
        w = 2.0 if r.plan_was_mandatory else 1.0
        weighted += dev * w
        total_w += w
        if dev > 0.02:
            violations.append(
                f"{r.symbol} {r.execution_type.value}: 计划 {r.planned_price:.2f} "
                f"实际 {r.actual_price:.2f}，偏差 {dev * 100:.2f}%"
            )

    if total_w == 0:
        return DisciplineScore(score=0.0, violations=["所有记录均缺计划价"])

    avg_dev = weighted / total_w
    score = max(0.0, (1 - avg_dev * 10) * 100)
    return DisciplineScore(score=round(score, 1), violations=violations)


# ============================================================================
# 逻辑分
# ============================================================================

@dataclass
class Hypothesis:
    """一条买入假设。来自模板 A5「一句话本质」的拆解。"""

    statement: str
    status: str = "unverified"   # verified / unverified / falsified
    evidence: str = ""


def calc_logic(hypotheses: list[Hypothesis]) -> dict:
    """计算逻辑分。

    逻辑分低于阈值时，**优先级高于价格止损** —— 主动降仓。
    原因：价格止损回答"我亏了多少"，逻辑止损回答"我为什么买它还成立吗"。
    如果原因已不成立，继续等反弹本质是在赌运气。
    """
    if not hypotheses:
        return {"score": 0.0, "verified": 0, "unverified": 0, "falsified": 0,
                "action": "无假设，无法评分", "force_reduce": False}

    v = sum(1 for h in hypotheses if h.status == "verified")
    u = sum(1 for h in hypotheses if h.status == "unverified")
    f = sum(1 for h in hypotheses if h.status == "falsified")
    score = v / len(hypotheses) * 100

    force_reduce = f > 0 and f >= len(hypotheses) / 2
    if f > 0:
        action = f"{f} 条假设被证伪，触发逻辑止损（优先级高于价格止损）"
    elif u > 0:
        action = f"{u} 条假设待验证，继续持有但禁止加仓"
    else:
        action = "全部假设已验证"

    return {
        "score": round(score, 1), "verified": v, "unverified": u, "falsified": f,
        "action": action, "force_reduce": force_reduce,
    }


# ============================================================================
# 情绪分
# ============================================================================

class Emotion(str, Enum):
    CALM = "calm"           # 平静
    GREED = "greed"         # 贪婪
    FEAR = "fear"           # 恐惧
    ANXIETY = "anxiety"     # 焦虑
    RELUCTANCE = "reluctance"  # 不甘心
    HERD = "herd"           # 从众


@dataclass
class EmotionTag:
    """一次手动操作的情绪标签。"""

    at: date
    emotion: Emotion
    direction: str   # buy / sell / hold
    note: str = ""


_CONSISTENT = {
    Emotion.CALM: {"buy", "sell", "hold"},
    Emotion.GREED: set(),        # 贪婪驱动的任何操作都不一致
    Emotion.FEAR: set(),
    Emotion.ANXIETY: set(),
    Emotion.RELUCTANCE: set(),
    Emotion.HERD: set(),
}


def calc_emotion(tags: list[EmotionTag]) -> dict:
    """计算情绪分。

    判据：情绪标签与操作方向是否一致。
    贪婪/恐惧/焦虑/不甘/从众驱动的操作，一律判为不一致 ——
    因为这些情绪会让人在错误的方向上加大力度。
    """
    if not tags:
        return {"score": 0.0, "total": 0, "inconsistent": 0,
                "action": "无情绪记录，无法评分", "suspend_trading": False}

    bad = [t for t in tags if t.direction not in _CONSISTENT.get(t.emotion, set())]
    score = (len(tags) - len(bad)) / len(tags) * 100

    counts: dict[str, int] = {}
    for t in bad:
        counts[t.emotion.value] = counts.get(t.emotion.value, 0) + 1

    suspend = len(bad) / len(tags) > 0.5
    return {
        "score": round(score, 1),
        "total": len(tags),
        "inconsistent": len(bad),
        "breakdown": counts,
        "action": "情绪主导操作占比过半，建议暂停交易" if suspend else "情绪影响可控",
        "suspend_trading": suspend,
    }


# ============================================================================
# 参数分（寻优目标）
# ============================================================================

@dataclass
class ParamCandidate:
    """一组待评估的参数组合。"""

    label: str
    returns: list[float]
    """逐期收益率序列（小数，如 0.012 表示 1.2%）。"""
    periods_per_year: int = 252


def sortino_ratio(returns: list[float], periods_per_year: int = 252, target: float = 0.0) -> float:
    """Sortino 比率 —— **修正后的主寻优目标**。

    与夏普的区别：只惩罚下行波动。上行波动不是风险。
    与卡玛的区别：不对样本期内单一极值敏感。
    """
    if not returns:
        return 0.0
    n = len(returns)
    mean = sum(returns) / n
    downside = [min(0.0, r - target) ** 2 for r in returns]
    dd = (sum(downside) / n) ** 0.5
    if dd == 0:
        return float("inf") if mean > target else 0.0
    return (mean - target) / dd * (periods_per_year ** 0.5)


def max_drawdown(returns: list[float]) -> float:
    """最大回撤（小数）。保留为展示指标。"""
    eq, peak, mdd = 1.0, 1.0, 0.0
    for r in returns:
        eq *= (1 + r)
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    return abs(mdd)


def cvar(returns: list[float], alpha: float = 0.05) -> float:
    """条件在险价值 CVaR(5%) —— 尾部平均损失。

    作为约束条件使用：要求尾部平均损失不超过阈值。
    比最大回撤更适合做约束，因为它衡量的是尾部**平均**而非单次极值。
    """
    if not returns:
        return 0.0
    s = sorted(returns)
    k = max(1, int(len(s) * alpha))
    tail = s[:k]
    return abs(sum(tail) / len(tail))


def calmar_ratio(returns: list[float], periods_per_year: int = 252) -> float:
    """卡玛比率 —— 仅作展示，**不再作为寻优目标**。"""
    mdd = max_drawdown(returns)
    if mdd == 0:
        return float("inf")
    n = len(returns)
    ann = (1 + sum(returns) / n) ** periods_per_year - 1
    return ann / mdd


def rank_candidates(candidates: list[ParamCandidate], cvar_limit: float = 0.05) -> list[dict]:
    """按修正后的目标对参数组合排序。

    排序规则：
      1. 先过滤：CVaR(5%) 超过约束的直接淘汰
      2. 再按 Sortino 降序
      3. 卡玛比率与最大回撤仅作展示
    """
    rows: list[dict] = []
    for c in candidates:
        cv = cvar(c.returns)
        rows.append({
            "label": c.label,
            "sortino": round(sortino_ratio(c.returns, c.periods_per_year), 3),
            "cvar_5pct": round(cv, 4),
            "max_drawdown": round(max_drawdown(c.returns), 4),
            "calmar": round(calmar_ratio(c.returns, c.periods_per_year), 3),
            "passes_cvar": cv <= cvar_limit,
            "note": "" if cv <= cvar_limit else f"CVaR {cv:.2%} 超过约束 {cvar_limit:.2%}，淘汰",
        })
    rows.sort(key=lambda x: (x["passes_cvar"], x["sortino"]), reverse=True)
    return rows


# ============================================================================
# 参数白名单
# ============================================================================

@dataclass(frozen=True)
class ParamWhitelist:
    """可调参数 vs 固定约束。

    这个划分的意义：**可调参数用来适应市场，固定约束用来约束你自己。**
    如果连"单笔最多亏多少"都能在回测里优化，
    那优化出来的结果一定是最激进的那个。
    """

    adjustable: dict[str, tuple[float, float]] = field(default_factory=lambda: {
        "stop_loss_atr_multiple": (1.5, 3.0),
        "signal_decay_days": (2, 5),
        "gross_margin_trend_window": (2, 4),
        "volatility_percentile_window": (120, 250),
    })

    fixed: dict[str, str] = field(default_factory=lambda: {
        "max_loss_per_trade_pct": "单笔最大损失 —— 由你的回撤预算决定，不可优化",
        "deducted_np_growth_min": "扣非增速门槛 —— 否决项，不可优化",
        "max_drawdown_circuit_breaker": "回撤熔断线 —— 纪律条款，不可优化",
    })

    def check(self, name: str, value: float) -> tuple[bool, str]:
        if name in self.fixed:
            return False, f"{name} 属于固定约束，不可调：{self.fixed[name]}"
        if name not in self.adjustable:
            return False, f"{name} 不在白名单内"
        lo, hi = self.adjustable[name]
        if not (lo <= value <= hi):
            return False, f"{name}={value} 超出可调区间 [{lo}, {hi}]"
        return True, ""


# ============================================================================
# 综合复盘
# ============================================================================

def full_review(
    executed: list[TradeRecord],
    skipped_mandatory: list[TradeRecord],
    hypotheses: list[Hypothesis],
    emotions: list[EmotionTag],
    candidates: Optional[list[ParamCandidate]] = None,
) -> dict:
    """四维综合复盘。"""
    d = calc_discipline(executed, skipped_mandatory)
    l = calc_logic(hypotheses)
    e = calc_emotion(emotions)
    p = rank_candidates(candidates) if candidates else []

    actions: list[str] = []
    if d.must_take_over:
        actions.append("【最高优先】未执行硬止损 → 建议系统接管下单权限")
    if l["force_reduce"]:
        actions.append(f"逻辑止损触发 → {l['action']}")
    if e["suspend_trading"]:
        actions.append(f"情绪主导 → {e['action']}")
    if p:
        actions.append(f"参数寻优建议：{p[0]['label']}（Sortino {p[0]['sortino']}）")
    if not actions:
        actions.append("四维均在正常范围")

    return {
        "纪律": {"score": d.score, "violations": d.violations, "must_take_over": d.must_take_over},
        "逻辑": l,
        "情绪": e,
        "参数": p,
        "下一步动作": actions,
    }


if __name__ == "__main__":
    import json

    print("=" * 74)
    print("复盘层 · 自检")
    print("=" * 74)

    print("\n【一】纪律分：未执行硬止损 -> 归零 + 系统接管")
    print("-" * 74)
    skipped = [TradeRecord("002422", 40.00, 0.0, 10000, date(2026, 9, 29),
                           ExecutionType.STOP_LOSS, plan_was_mandatory=True)]
    d = calc_discipline([], skipped)
    print(f"  {d}")
    for v in d.violations:
        print(f"    - {v}")

    print("\n【二】纪律分：已执行但有偏差")
    print("-" * 74)
    recs = [
        TradeRecord("002422", 40.00, 40.15, 5000, date(2026, 9, 29),
                    ExecutionType.STOP_LOSS, plan_was_mandatory=True),
        TradeRecord("600XXX", 25.00, 25.80, 2000, date(2026, 9, 15),
                    ExecutionType.MANUAL),
    ]
    d2 = calc_discipline(recs, [])
    print(f"  {d2}")
    for v in d2.violations:
        print(f"    - {v}")

    print("\n【三】逻辑分：假设证伪触发逻辑止损")
    print("-" * 74)
    hyps = [
        Hypothesis("科伦博泰 ADC 全球放量", "falsified", "2026H1 药品销售 6.57 亿，但利润含 7.03 亿一次性和解收入"),
        Hypothesis("传统输液业务企稳", "verified", "非输液收入同比 +5.72%"),
        Hypothesis("扣非利润改善", "falsified", "2026H1 扣非 -29.03%"),
    ]
    l = calc_logic(hyps)
    print(json.dumps(l, ensure_ascii=False, indent=2))

    print("\n【四】情绪分：情绪主导操作")
    print("-" * 74)
    tags = [
        EmotionTag(date(2026, 8, 24), Emotion.RELUCTANCE, "hold", "不甘心在 42 元减仓"),
        EmotionTag(date(2026, 9, 24), Emotion.ANXIETY, "hold", "焦虑但没动"),
        EmotionTag(date(2026, 9, 30), Emotion.CALM, "hold", "平静持有"),
    ]
    e = calc_emotion(tags)
    print(json.dumps(e, ensure_ascii=False, indent=2))

    print("\n【五】参数寻优：Sortino 为主，CVaR 为约束（修正后）")
    print("-" * 74)
    import random
    random.seed(42)

    def gen(mu: float, sigma: float, n: int = 250) -> list[float]:
        return [random.gauss(mu, sigma) for _ in range(n)]

    cands = [
        ParamCandidate("止损 1.5×ATR（高频）", gen(0.0009, 0.022)),
        ParamCandidate("止损 2.0×ATR（推荐）", gen(0.0010, 0.016)),
        ParamCandidate("止损 3.0×ATR（低频）", gen(0.0007, 0.013)),
        ParamCandidate("止损 1.0×ATR（过紧）", gen(0.0004, 0.028)),
    ]
    rows = rank_candidates(cands)
    print(f"  {'参数组合':<24} {'Sortino':>9} {'CVaR5%':>9} {'最大回撤':>10} {'卡玛':>8}  说明")
    for r in rows:
        print(f"  {r['label']:<24} {r['sortino']:>9.3f} {r['cvar_5pct']:>9.4f} "
              f"{r['max_drawdown']:>10.4f} {r['calmar']:>8.3f}  {r['note']}")
    print("\n  注：卡玛比率仅作展示，不作寻优目标（最大回撤路径依赖、易过拟合）")

    print("\n【六】参数白名单校验")
    print("-" * 74)
    wl = ParamWhitelist()
    for name, val in [("stop_loss_atr_multiple", 2.0),
                      ("stop_loss_atr_multiple", 5.0),
                      ("max_loss_per_trade_pct", 1.0),
                      ("unknown_param", 1.0)]:
        ok, msg = wl.check(name, val)
        print(f"  {name} = {val}  ->  {'允许' if ok else '拒绝'}  {msg}")

    print("\n【七】四维综合复盘")
    print("-" * 74)
    print(json.dumps(full_review(recs, skipped, hyps, tags, cands),
                     ensure_ascii=False, indent=2))
