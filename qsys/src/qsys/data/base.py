"""
数据层：字段时效纪律
====================

设计动机
--------
上一轮数据核对暴露出三类错误，全部源于同一个根因 —— **数值与时间脱钩**：

  1. "45.44 元"被当作"当前价"，实际是 2026-08-07 的收盘价，已过期两个月
  2. "38.52 → 45.44 涨 18%" 把两个不相关时点拼在一起，抹掉了中间 −12.6% 的回撤
  3. 同一天取数，11:24 得 39.36、11:28 得 39.26 —— 因为当时 A 股仍在交易中

因此本层强制：**任何进入规则的数值，必须携带 值 + 取数时刻 + 交易状态 + 来源**。
四者缺一，该字段即不可用于决策。

核心类型
--------
FieldValue   —— 带元数据的字段值（本层的基本单位）
DataGap      —— 数据缺口（取不到时必须显式标记，禁止填 0）
DataAdapter  —— 数据源抽象接口（换市场只换实现）
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


# ============================================================================
# 交易状态
# ============================================================================

class TradingState(str, Enum):
    """取数时刻的市场状态。

    这个枚举存在的唯一理由：**盘中价不能当收盘价用**。
    2026-10-08 上午取数时 A 股仍在交易中，同一时刻不同秒的"最新价"都不同。
    """

    PRE_OPEN = "pre_open"      # 盘前
    INTRADAY = "intraday"      # 盘中（交易中）
    CLOSED = "closed"          # 收盘（已定盘）
    SUSPENDED = "suspended"    # 停牌
    UNKNOWN = "unknown"        # 未知


# ============================================================================
# 字段值
# ============================================================================

@dataclass
class FieldValue:
    """带完整元数据的字段值。

    这是数据层的基本单位。**不要用裸 float 在系统里传递行情与财务数据** ——
    裸值会丢失时间信息，而时间信息是判断数据是否可用的唯一依据。
    """

    name: str
    value: Any
    as_of: datetime
    """数据的**取数时刻**（非报告期）。"""
    source: str
    state: TradingState = TradingState.UNKNOWN
    period: Optional[str] = None
    """统计周期（如 "2026H1" / "2025FY"）。财务字段必填。"""
    announced_at: Optional[datetime] = None
    """财务字段的**公告日**。Point-in-Time 一致性的关键 —— 
    回测必须用公告日而非报告期判断信息是否可得，否则会产生前视偏差。"""
    unit: str = ""
    is_derived: bool = False
    """是否为派生值（如同比增速）。派生值需可追溯到原始值。"""

    # ---- 时效判定 ----

    def age_days(self, now: Optional[datetime] = None) -> float:
        """距取数时刻的天数。"""
        ref = now or datetime.now()
        return (ref - self.as_of).total_seconds() / 86400.0

    def is_stale(self, max_age_days: float, now: Optional[datetime] = None) -> bool:
        """是否已超出有效期。

        这是修正"45.44 元被当作当前价"的直接手段 ——
        价位类字段的有效期通常只有 1 天，超出即不可用于决策。
        """
        return self.age_days(now) > max_age_days

    def is_decision_grade(self, max_age_days: float, now: Optional[datetime] = None) -> tuple[bool, str]:
        """该字段是否达到"可用于决策"的标准。

        返回 (是否可用, 原因)。原因非空时说明为何不可用 ——
        把拒绝理由显式说出来，而不是静默返回 False。
        """
        if self.value is None:
            return False, "值为空"
        if self.state == TradingState.SUSPENDED:
            return False, "标的停牌，价格不具备交易意义"
        if self.state == TradingState.INTRADAY:
            return False, f"当前为盘中价（{self.as_of:%H:%M}），非收盘价"
        if self.is_stale(max_age_days, now):
            return False, f"数据已过期 {self.age_days(now):.1f} 天（上限 {max_age_days} 天）"
        return True, ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["as_of"] = self.as_of.isoformat()
        d["announced_at"] = self.announced_at.isoformat() if self.announced_at else None
        d["state"] = self.state.value
        return d

    def __str__(self) -> str:
        p = f" [{self.period}]" if self.period else ""
        return f"{self.name}={self.value}{self.unit}{p} @{self.as_of:%Y-%m-%d %H:%M} ({self.state.value}, {self.source})"


class DataGap(Exception):
    """数据缺口。

    取数失败时**必须抛这个异常**，禁止返回 0 或前值。
    静默填 0 会让下游把"没取到"误读为"值为零" —— 这是最危险的一类 bug，
    因为它不会报错，只会让结论悄悄变错。
    """

    def __init__(self, field_name: str, reason: str, symbol: str = "") -> None:
        self.field_name = field_name
        self.reason = reason
        self.symbol = symbol
        super().__init__(f"[数据缺口] {symbol} {field_name}: {reason}")


# ============================================================================
# 缺口日志
# ============================================================================

@dataclass
class GapLog:
    """数据缺口日志。

    缺口不是异常情况，是常态 —— 尤其对爬虫型数据源。
    记录它，并在产出物中显式标注，而不是假装数据完整。
    """

    entries: list[dict[str, str]] = field(default_factory=list)

    def record(self, field_name: str, reason: str, symbol: str = "") -> None:
        self.entries.append({
            "symbol": symbol,
            "field": field_name,
            "reason": reason,
            "logged_at": datetime.now().isoformat(timespec="seconds"),
        })

    def summary(self) -> dict[str, Any]:
        return {
            "gap_count": len(self.entries),
            "gaps": self.entries,
        }

    def has_gaps(self) -> bool:
        return len(self.entries) > 0


# ============================================================================
# 数据源抽象
# ============================================================================

class DataAdapter(ABC):
    """数据源抽象接口。

    换市场只换实现，模板与规则层不动 —— 这是"通用层 + 市场适配层"架构的落点。
    """

    market: str = ""

    @abstractmethod
    def price_history(
        self, symbol: str, start: str, end: str, adjust: str = "qfq"
    ) -> list[FieldValue]:
        """日线行情（前复权）。"""

    @abstractmethod
    def quote(self, symbol: str) -> FieldValue:
        """最新报价。**必须标注 TradingState**，否则调用方无法判断能否用于决策。"""

    @abstractmethod
    def fundamentals(self, symbol: str, periods: int = 12) -> dict[str, FieldValue]:
        """财务字段。**必须带 announced_at（公告日）**。"""

    def capital_flow(self, symbol: str) -> dict[str, FieldValue]:
        """资金面字段。默认不实现 —— 说明该市场无此层。"""
        raise NotImplementedError(f"{self.market} 未实现资金面层")

    def health_check(self) -> dict[str, Any]:
        return {"market": self.market, "ok": True}


# ============================================================================
# 时效策略：不同字段类型有不同的有效期
# ============================================================================

@dataclass(frozen=True)
class FreshnessPolicy:
    """字段时效策略。

    关键洞察：**不是所有字段都需要同样新鲜**。
    把价位和财报用同一个有效期管理，必然导致要么过度取数、要么用过期数据。
    """

    max_age_days: dict[str, float] = field(default_factory=lambda: {
        # 价位类：当日有效。这是修正"45.44 被当作当前价"的直接手段
        "price": 1.0,
        "quote": 1.0,
        "technical": 1.0,
        # 资金面：可容忍一个交易日
        "capital_flow": 2.0,
        # 估值：可容忍一周（估值变动慢）
        "valuation": 7.0,
        # 财务：按披露周期，半年报可容忍 200 天
        "fundamental": 200.0,
        # 制度约束：核验后长期有效，但需定期复核
        "market_rule": 180.0,
    })

    def limit_for(self, category: str) -> float:
        return self.max_age_days.get(category, 1.0)

    def check(
        self, fv: FieldValue, category: str, now: Optional[datetime] = None
    ) -> tuple[bool, str]:
        return fv.is_decision_grade(self.limit_for(category), now)


# ============================================================================
# 一致性校验
# ============================================================================

def assert_same_series(
    start: FieldValue, end: FieldValue, max_gap_days: float = 1.0
) -> tuple[bool, str]:
    """校验两个字段是否来自同一序列、可用于计算区间涨跌幅。

    这是修正"38.52 → 45.44 涨 18%"那类错误的直接手段：
    该表述把 7/1 开盘价与 8/7 收盘价拼在一起，中间跨越 27 个交易日，
    抹掉了 −12.6% 的回撤。

    规则：两点必须同源、同口径、同复权方式，且间隔不超过 max_gap_days。
    跨时点拼接区间数据时，必须显式说明中间发生了什么。
    """
    if start.source != end.source:
        return False, f"数据源不同：{start.source} vs {end.source}"
    if start.name != end.name:
        return False, f"字段不同：{start.name} vs {end.name}"
    gap = (end.as_of - start.as_of).total_seconds() / 86400.0
    if gap > max_gap_days:
        return False, (
            f"两点间隔 {gap:.0f} 天（上限 {max_gap_days:.0f} 天），"
            f"不可直接相减计算涨跌幅；如需跨期比较，必须改用同一连续序列并说明中间路径"
        )
    return True, ""


def pct_change(start: FieldValue, end: FieldValue) -> FieldValue:
    """计算区间涨跌幅，附带一致性校验。"""
    ok, reason = assert_same_series(start, end)
    if not ok:
        raise ValueError(f"区间数据不可直接比较：{reason}")
    chg = (end.value - start.value) / start.value * 100.0
    return FieldValue(
        name=f"{start.name}_pct_change",
        value=round(chg, 2),
        as_of=end.as_of,
        source=end.source,
        state=end.state,
        unit="%",
        is_derived=True,
    )


if __name__ == "__main__":
    from datetime import timedelta

    print("=" * 66)
    print("数据层时效纪律 · 自检")
    print("=" * 66)

    policy = FreshnessPolicy()
    now = datetime(2026, 10, 8, 11, 28)

    print("\n[1] 同一个字段、不同取数时刻 —— 盘中价不能当收盘价")
    for minute, price, state in [(24, 39.36, TradingState.INTRADAY), (28, 39.26, TradingState.INTRADAY)]:
        fv = FieldValue(
            name="close", value=price,
            as_of=now.replace(minute=minute), source="行情接口",
            state=state, unit="元",
        )
        ok, why = policy.check(fv, "price", now=now)
        print(f"    11:{minute} 取数 -> {price} 元 | 可用于决策: {ok} | 原因: {why}")

    print("\n[2] 45.44 元这个价位 —— 过期两个月后还能用吗")
    stale = FieldValue(
        name="close", value=45.44,
        as_of=datetime(2026, 8, 7, 15, 0), source="行情接口",
        state=TradingState.CLOSED, unit="元",
    )
    ok, why = policy.check(stale, "price", now=now)
    print(f"    2026-08-07 收盘 45.44 元")
    print(f"    距今 {stale.age_days(now):.0f} 天 | 可用于决策: {ok}")
    print(f"    原因: {why}")

    print("\n[3] 跨时点拼接校验 —— '38.52 → 45.44 涨 18%' 为什么是错的")
    a = FieldValue("close", 38.52, datetime(2026, 7, 1, 9, 30), "行情接口", TradingState.CLOSED, unit="元")
    b = FieldValue("close", 45.44, datetime(2026, 8, 7, 15, 0), "行情接口", TradingState.CLOSED, unit="元")
    ok, why = assert_same_series(a, b)
    print(f"    校验通过: {ok}")
    print(f"    原因: {why}")

    print("\n[4] 财务字段的 Point-in-Time 校验")
    fin = FieldValue(
        name="revenue", value=88.39, unit="亿元",
        as_of=datetime(2026, 10, 8, 11, 30),
        period="2026H1", announced_at=datetime(2026, 8, 27, 0, 0),
        source="定期报告", state=TradingState.CLOSED,
    )
    print(f"    {fin}")
    print(f"    统计周期 {fin.period}，公告日 {fin.announced_at:%Y-%m-%d}")
    print(f"    -> 回测中判断该数据是否可得，必须用公告日而非报告期")

    print("\n[5] 数据缺口必须显式抛错，禁止填 0")
    gap_log = GapLog()
    try:
        raise DataGap("margin_balance", "接口限频，本轮未返回", symbol="002422")
    except DataGap as e:
        gap_log.record(e.field_name, e.reason, e.symbol)
        print(f"    捕获: {e}")
    print(f"    缺口日志: {json.dumps(gap_log.summary(), ensure_ascii=False)}")
