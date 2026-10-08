"""
A股 / 美股 制度约束引擎
=========================

设计动机
--------
本模块存在的理由只有一个：**风险预算公式在制度约束下会失效，而失效方向永远是坏的。**

   仓位 = 单笔风险预算 / 止损宽度

这个公式隐含假设「止损一定能在设定价位成交」。但在 A 股：
  - T+1      → 当日买入当日卖不掉
  - 涨跌停   → 连续跌停时根本卖不出去，实际损失远超止损宽度
  - 停牌     → 停牌期间无法交易，「跌破 N 日未收回」会被停牌日错误满足

因此制度约束必须作为**一等公民**进入系统，而不是散落在注释里。

规则核验
--------
所有规则均经外部来源实时核验，核验日期见各常量旁的 VERIFIED 注释。
禁止凭记忆修改本文件中的任何数值 —— 请先核验再改。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum
from typing import Optional


# ============================================================================
# 核验记录
# ============================================================================

RULES_VERIFIED_AT = "2026-10-08"
"""本文件全部规则的最后核验日期。修改任何规则前请更新此字段。"""


class Board(str, Enum):
    """上市板块。不同板块的涨跌幅与最小交易单位不同。"""

    CN_MAIN = "cn_main"          # 沪深主板
    CN_STAR = "cn_star"          # 科创板（688xxx）
    CN_GEM = "cn_gem"            # 创业板（300xxx / 301xxx）
    CN_BSE = "cn_bse"            # 北交所（8xxxxx / 4xxxxx）
    US = "us"                    # 美股


# ============================================================================
# 板块规则表
# ============================================================================

@dataclass(frozen=True)
class BoardRule:
    """单个板块的交易制度约束。"""

    board: Board
    label: str

    price_limit_pct: float
    """常规交易日涨跌幅限制（百分比）。0 表示无限制。"""

    min_order_qty: int
    """单笔买入最小申报数量（股）。"""

    qty_increment: int
    """申报数量递增单位（股）。"""

    has_price_cage: bool
    """是否存在「价格笼子」（有效竞价范围）机制。"""

    source: str = ""
    verified_at: str = RULES_VERIFIED_AT


# VERIFIED 2026-10-08 —— 沪深主板普通股 ±10%
# VERIFIED 2026-10-08 —— 科创板 / 创业板普通股 ±20%
# VERIFIED 2026-10-08 —— 北交所 ±30%
# VERIFIED 2026-10-08 —— 科创板最小申报 200 股，以 1 股递增（主板为 100 股整数倍）
_CN_MAIN = BoardRule(
    board=Board.CN_MAIN, label="沪深主板",
    price_limit_pct=10.0, min_order_qty=100, qty_increment=100,
    has_price_cage=False,
    source="沪深交易所交易规则；2026-07 交易新规",
)
_CN_STAR = BoardRule(
    board=Board.CN_STAR, label="科创板",
    price_limit_pct=20.0, min_order_qty=200, qty_increment=1,
    has_price_cage=True,
    source="上交所科创板股票交易特别规定",
)
_CN_GEM = BoardRule(
    board=Board.CN_GEM, label="创业板",
    price_limit_pct=20.0, min_order_qty=100, qty_increment=100,
    has_price_cage=True,
    source="深交所创业板股票交易特别规定",
)
_CN_BSE = BoardRule(
    board=Board.CN_BSE, label="北交所",
    price_limit_pct=30.0, min_order_qty=100, qty_increment=1,
    has_price_cage=True,
    source="北交所交易规则",
)
_US = BoardRule(
    board=Board.US, label="美股",
    price_limit_pct=0.0, min_order_qty=1, qty_increment=1,
    has_price_cage=False,
    source="无涨跌停，实行 LULD 波动性熔断",
)

_BOARD_TABLE: dict[Board, BoardRule] = {
    Board.CN_MAIN: _CN_MAIN,
    Board.CN_STAR: _CN_STAR,
    Board.CN_GEM: _CN_GEM,
    Board.CN_BSE: _CN_BSE,
    Board.US: _US,
}


# ============================================================================
# 交易费用
# ============================================================================

@dataclass(frozen=True)
class FeeSchedule:
    """交易费用结构。佣金/过户费/滑点因券商与个人而异，须由用户提供。"""

    stamp_duty_sell_pct: float = 0.05
    """印花税，卖出单边（百分比）。

    VERIFIED 2026-10-08 —— 0.05%，卖出单边。
    经 2025-11 与 2026-09 两批独立公开资料交叉确认。
    """

    commission_pct: Optional[float] = None
    """佣金费率（双向，百分比）。**必须由用户从券商对账单提供。**"""

    commission_min: Optional[float] = None
    """单笔最低佣金（元）。小资金交易时它可能主导成本。**须用户提供。**"""

    transfer_fee_pct: Optional[float] = None
    """过户费费率（双向，百分比）。**须用户提供。**"""

    slippage_pct: Optional[float] = None
    """滑点假设（百分比）。建议保守取值。**须用户提供。**"""

    def is_complete(self) -> bool:
        """费用结构是否完整。不完整时禁止用于回测与参数寻优。"""
        return all(
            v is not None
            for v in (
                self.commission_pct,
                self.commission_min,
                self.transfer_fee_pct,
                self.slippage_pct,
            )
        )

    def round_trip_cost_pct(self) -> Optional[float]:
        """一次完整买卖（买+卖）的显性成本占比（百分比）。

        未填齐时返回 None —— 调用方必须显式处理「成本未知」，
        而不是拿 0 当成本静默算下去。
        """
        if not self.is_complete():
            return None
        return (
            self.stamp_duty_sell_pct
            + 2 * self.commission_pct          # type: ignore[operator]
            + 2 * self.transfer_fee_pct        # type: ignore[operator]
            + 2 * self.slippage_pct            # type: ignore[operator]
        )


# ============================================================================
# 停牌
# ============================================================================

@dataclass(frozen=True)
class SuspensionRule:
    """停牌处理规则。

    VERIFIED 2026-10-08 —— 监管口径为「以不停牌为原则、停牌为例外，
    短期停牌为原则、长期停牌为例外，间断性停牌为原则」。
    依据：证监会《上市公司股票停复牌规则》（2022 年第 7 号公告，2022-01-05 施行）
          上交所《上市公司自律监管指引第 4 号——停复牌》（2025-03 修订）

    实务注意：重大资产重组仍可能触发停牌（例：新亚制程 002388 自 2026-10-08 起停牌）。
    """

    count_suspended_days_in_monitor: bool = False
    """停牌日是否计入监控计时。

    必须为 False。否则「跌破止损线且 N 日未收回」会被停牌日错误满足 ——
    停牌期间根本没有交易，谈不上「收回」。
    """

    first_day_after_resume_uses_normal_stop: bool = False
    """复牌首日是否适用常规止损。

    必须为 False。复牌常伴跳空缺口，常规止损价已失去意义。
    正确做法：复牌后 N 个交易日内重新评估。
    """

    reevaluate_window_days: int = 5
    """复牌后重新评估窗口（交易日）。"""


# ============================================================================
# 市场规则引擎
# ============================================================================

@dataclass
class Position:
    """一笔持仓。"""

    symbol: str
    board: Board
    entry_price: float
    quantity: int
    entry_date: date
    is_st: bool = False


class CNMarketRules:
    """A 股制度约束引擎。

    职责：把制度约束显式建模，供风控与回测调用。
    """

    def __init__(
        self,
        fees: Optional[FeeSchedule] = None,
        suspension: Optional[SuspensionRule] = None,
    ) -> None:
        self.fees = fees or FeeSchedule()
        self.suspension = suspension or SuspensionRule()

    # ---- 板块识别 ----

    @staticmethod
    def classify_board(symbol: str) -> Board:
        """按证券代码前缀识别板块。

        688 / 689 → 科创板
        300 / 301 → 创业板
        8 / 4 / 920 → 北交所
        其余 6 位数字 → 沪深主板
        """
        code = symbol.split(".")[0].strip()
        if code.startswith(("688", "689")):
            return Board.CN_STAR
        if code.startswith(("300", "301")):
            return Board.CN_GEM
        if code.startswith(("8", "4", "920")):
            return Board.CN_BSE
        return Board.CN_MAIN

    def rule_of(self, symbol: str, is_st: bool = False) -> BoardRule:
        """取得该标的适用的板块规则。

        注意 ST 对涨跌幅的影响（VERIFIED 2026-10-08）：
          沪深主板 ST / *ST 股票涨跌幅限制**已由 5% 调整为 10%**，
          自 2026-07-06 起施行，与主板普通股一致。
          科创板 / 创业板 ST 股仍适用其板块的 ±20%。

        因此：主板 ST 与主板普通股的涨跌幅**相同**，无需特殊处理；
        但本方法仍接收 is_st 参数，以便未来规则变化时可扩展。
        """
        board = self.classify_board(symbol)
        return _BOARD_TABLE[board]

    # ---- 涨跌停 ----

    def price_limits(
        self, prev_close: float, symbol: str, is_st: bool = False
    ) -> tuple[float, float]:
        """返回 (跌停价, 涨停价)。四舍五入到 2 位小数，与交易所口径一致。"""
        rule = self.rule_of(symbol, is_st)
        if rule.price_limit_pct <= 0:
            return (0.0, float("inf"))
        ratio = rule.price_limit_pct / 100.0
        lower = round(prev_close * (1 - ratio) + 1e-9, 2)
        upper = round(prev_close * (1 + ratio) + 1e-9, 2)
        return (lower, upper)

    def is_limit_down(self, price: float, prev_close: float, symbol: str, is_st: bool = False) -> bool:
        lower, _ = self.price_limits(prev_close, symbol, is_st)
        return price <= lower + 1e-9

    def is_limit_up(self, price: float, prev_close: float, symbol: str, is_st: bool = False) -> bool:
        _, upper = self.price_limits(prev_close, symbol, is_st)
        return price >= upper - 1e-9

    # ---- T+1 ----

    @staticmethod
    def can_sell(entry_date: date, sell_date: date) -> bool:
        """T+1 约束：当日买入的仓位最早于下一交易日卖出。

        VERIFIED 2026-10-08 —— A 股实行 T+1。
        注意：本方法只做自然日比较的保守近似；
        精确判定需接入交易日历，剔除周末与节假日。
        """
        return sell_date > entry_date

    # ---- 数量取整 ----

    def round_quantity(self, target_qty: int, symbol: str) -> int:
        """按板块最小申报单位向下取整。取整后为 0 表示该仓位无法建立。"""
        rule = self.rule_of(symbol)
        if target_qty < rule.min_order_qty:
            return 0
        excess = (target_qty - rule.min_order_qty) % rule.qty_increment
        return target_qty - excess

    # ---- 最坏损失重算（本模块最核心的方法） ----

    def worst_case_loss(
        self,
        entry_price: float,
        quantity: int,
        symbol: str,
        stop_price: float,
        max_consecutive_limit_down: int,
        is_st: bool = False,
    ) -> dict[str, float]:
        """在「连续跌停无法卖出」情形下重算真实最坏损失。

        这是对风险预算公式的修正。原公式假设止损必然成交，
        但在连续跌停中，你只能眼睁睁看着，直到跌停打开才能卖。

        参数
        ----
        max_consecutive_limit_down
            该标的历史上出现过的最大连续跌停天数（交易日）。
            由用户从历史数据统计得出 —— 这是必须由用户提供的参数，
            因为不同标的的极端情况差异极大。

        返回
        ----
        dict，包含：
            planned_loss       —— 原风控计划下的损失（假设止损成交）
            realized_loss      —— 连续跌停情形下的实际损失
            amplification      —— 放大倍数（realized / planned）
            is_formula_valid   —— 风险预算公式在此标的上是否仍成立
        """
        rule = self.rule_of(symbol, is_st)
        ratio = rule.price_limit_pct / 100.0

        planned_loss = max(0.0, (entry_price - stop_price) * quantity)

        # 连续跌停：每跌停一天，价格乘以 (1 - ratio)
        # 止损价在第一天就被击穿，但无法成交；直到跌停打开才按当日价格卖出。
        # 保守假设：跌停打开当日即卖出，价格取第 N 天跌停价。
        floor_price = entry_price * ((1 - ratio) ** max_consecutive_limit_down)
        limit_down_loss = max(0.0, (entry_price - floor_price) * quantity)

        # 最坏损失取「计划损失」与「连续跌停损失」的较大者。
        # 若 max_consecutive_limit_down = 0，说明该标的未出现过连续跌停，
        # 此时止损可正常成交，最坏损失即为计划损失 —— 而不是 0。
        realized_loss = max(planned_loss, limit_down_loss)

        amplification = (realized_loss / planned_loss) if planned_loss > 0 else float("inf")

        return {
            "planned_loss": round(planned_loss, 2),
            "realized_loss": round(realized_loss, 2),
            "amplification": round(amplification, 2),
            "is_formula_valid": amplification <= 1.5,
        }

    def max_position_by_worst_case(
        self,
        total_capital: float,
        max_loss_per_trade_pct: float,
        entry_price: float,
        stop_price: float,
        symbol: str,
        max_consecutive_limit_down: int,
        is_st: bool = False,
    ) -> dict[str, float]:
        """用「最坏损失」而非「计划损失」反推仓位上限。

        这是对标准风险预算法的制度修正版：
          标准版 —— 仓位 = 风险预算 / (入场价 - 止损价)
          修正版 —— 仓位 = 风险预算 / (入场价 - 连续跌停后价格)

        修正版给出的仓位一定**不高于**标准版，且标的越极端、折扣越大。
        """
        risk_budget = total_capital * (max_loss_per_trade_pct / 100.0)
        wc = self.worst_case_loss(
            entry_price=entry_price,
            quantity=1,
            symbol=symbol,
            stop_price=stop_price,
            max_consecutive_limit_down=max_consecutive_limit_down,
            is_st=is_st,
        )
        per_share_worst = wc["realized_loss"]
        if per_share_worst <= 0:
            return {"quantity": 0.0, "capital": 0.0, "note": "最坏损失为 0，请检查参数"}

        qty_raw = risk_budget / per_share_worst
        qty = self.round_quantity(int(qty_raw), symbol)
        return {
            "quantity": float(qty),
            "capital": round(qty * entry_price, 2),
            "capital_pct": round(qty * entry_price / total_capital * 100, 2),
            "per_share_worst_loss": round(per_share_worst, 4),
            "risk_budget": round(risk_budget, 2),
        }


class USMarketRules:
    """美股制度约束引擎。

    ⚠️ 验证状态：**未实测**。

    本类目前只有制度层面的设计，尚未用真实标的跑通。
    在美股侧完成至少一个完整标的的验证之前，
    不应把「双市场通用」当作既成事实。
    """

    T_PLUS = 0
    HAS_PRICE_LIMIT = False
    HAS_LULD = True
    MIN_ORDER_QTY = 1
    SETTLEMENT_DAYS = 1  # 2024-05 起美股结算周期为 T+1（此处指结算，非交易）

    @staticmethod
    def is_verified() -> bool:
        return False


if __name__ == "__main__":
    r = CNMarketRules()

    print("=" * 66)
    print("A股制度约束引擎 · 自检")
    print(f"规则核验日期: {RULES_VERIFIED_AT}")
    print("=" * 66)

    print("\n[1] 板块识别")
    for s in ["600519", "002422", "688111", "300750", "301029", "830799", "920001"]:
        print(f"    {s} -> {r.classify_board(s).value}")

    print("\n[2] 涨跌停（前收 39.26 元）")
    for s, st in [("002422", False), ("600519", False), ("688111", False), ("300750", False)]:
        lo, hi = r.price_limits(39.26, s, is_st=st)
        rule = r.rule_of(s, st)
        print(f"    {s} {rule.label:<8} ±{rule.price_limit_pct:>4.0f}%  ->  {lo:.2f} ~ {hi:.2f}")

    print("\n[3] ST 涨跌幅（VERIFIED 2026-07-06 新规：主板 ST 由 5% 调整为 10%）")
    lo, hi = r.price_limits(39.26, "600519", is_st=True)
    print(f"    600519 主板ST    ->  {lo:.2f} ~ {hi:.2f}   （若按旧规 5% 应为 37.30 ~ 41.22）")

    print("\n[4] 最小交易单位")
    for s in ["600519", "688111", "300750"]:
        rule = r.rule_of(s)
        q = r.round_quantity(357, s)
        print(f"    {s} {rule.label:<8} 最小 {rule.min_order_qty:>3} 股, 递增 {rule.qty_increment:>3}  ->  357 股取整为 {q}")

    print("\n[5] T+1")
    d1, d2 = date(2026, 10, 8), date(2026, 10, 9)
    print(f"    {d1} 买入, {d1} 卖出 -> {'可卖' if r.can_sell(d1, d1) else '不可卖'}")
    print(f"    {d1} 买入, {d2} 卖出 -> {'可卖' if r.can_sell(d1, d2) else '不可卖'}")

    print("\n[6] 最坏损失重算（002422 主板, 入场 45.44, 止损 40.00, 1 万股）")
    wc = r.worst_case_loss(45.44, 10000, "002422", 40.00, max_consecutive_limit_down=3)
    print(f"    计划损失   {wc['planned_loss']:>12,.2f} 元")
    print(f"    实际损失   {wc['realized_loss']:>12,.2f} 元")
    print(f"    放大倍数   {wc['amplification']:>12.2f} x")
    print(f"    公式是否仍成立: {wc['is_formula_valid']}")

    print("\n[7] 用最坏损失反推仓位（总资金 100 万, 单笔风险 1%）")
    pos = r.max_position_by_worst_case(
        total_capital=1_000_000, max_loss_per_trade_pct=1.0,
        entry_price=45.44, stop_price=40.00, symbol="002422",
        max_consecutive_limit_down=3,
    )
    for k, v in pos.items():
        print(f"    {k:<22} {v}")

    print("\n[8] 交易费用完整性")
    print(f"    默认（仅印花税）完整? {r.fees.is_complete()}")
    print(f"    往返成本: {r.fees.round_trip_cost_pct()}")
    print("    -> None 表示成本未知，禁止用于回测与参数寻优")
