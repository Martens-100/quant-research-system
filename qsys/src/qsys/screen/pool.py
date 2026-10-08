"""
筛选层：双池三门槛 + 排除日志
============================

设计动机
--------
原框架只有一个「初筛」，被淘汰的标的没有去处，于是产生了两个问题：
  1. 「困境反转池」没有准入条件 —— 任何被淘汰的标的都可以被解释成"困境反转"，
     它实际成了收容所
  2. 没有排除记录 —— 无法评估筛选标准本身的好坏

本模块给出双池三门槛结构，并把**排除日志**作为一等产出。

关于排除日志的判据（一处重要修正）
----------------------------------
原设计用「被排除的标的后来涨了没有」作判据。这有两个缺陷：
  ① 绝对涨幅会随大盘整体上涨而虚高
  ② 有反向风险：如果门槛太松，会导致不断放宽标准直到把垃圾也放进来

修正：改用**相对基准指数的超额收益**。
  只有当被排除标的显著跑赢同期基准时，才说明门槛可能设错了。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Optional


class Pool(str, Enum):
    GROWTH = "growth"              # 稳健成长池
    TURNAROUND = "turnaround"      # 困境反转池
    EXCLUDED = "excluded"          # 排除池


@dataclass
class ScreenCheck:
    """单项门槛的检查结果。"""

    name: str
    passed: bool
    value: object
    threshold: object
    note: str = ""

    def __str__(self) -> str:
        mark = "✓" if self.passed else "✗"
        return f"{mark} {self.name:<16} {self.value} (门槛 {self.threshold})  {self.note}"


@dataclass
class ScreenResult:
    """单个标的的筛选结果。"""

    symbol: str
    name: str
    pool: Pool
    checks: list[ScreenCheck] = field(default_factory=list)
    excluded_reason: str = ""
    screened_at: Optional[date] = None

    def explain(self) -> str:
        lines = [f"{self.symbol} {self.name}  ->  {self.pool.value}"]
        for c in self.checks:
            lines.append("  " + str(c))
        if self.excluded_reason:
            lines.append(f"  排除原因: {self.excluded_reason}")
        return "\n".join(lines)


# ============================================================================
# 门槛定义
# ============================================================================

@dataclass
class ScreenThresholds:
    """筛选门槛。

    注意：`deducted_np_growth_min` 建议设为**固定约束**而非可调参数 ——
    它是否决项，不该在参数寻优中被优化掉。
    """

    # 稳健成长池
    revenue_growth_min: float = 20.0
    deducted_np_growth_min: float = 0.0
    gross_margin_trend_window: int = 3
    ocf_positive: bool = True

    # 困境反转池
    deducted_decline_narrowing_periods: int = 2
    requires_named_driver: bool = True
    debt_to_asset_max: float = 60.0

    # 通用
    goodwill_to_equity_max: float = 10.0


@dataclass
class CandidateInput:
    """候选标的的输入数据。调用方负责从数据层取数并填充。"""

    symbol: str
    name: str
    revenue_growth: Optional[float] = None
    deducted_np_growth: Optional[float] = None
    gross_margins: list[float] = field(default_factory=list)
    ocf: Optional[float] = None
    deducted_np_series: list[float] = field(default_factory=list)
    turnaround_driver: Optional[str] = None
    debt_to_asset: Optional[float] = None
    goodwill_to_equity: Optional[float] = None
    data_gaps: list[str] = field(default_factory=list)


class PoolBuilder:
    """双池三门槛筛选器。"""

    def __init__(self, th: Optional[ScreenThresholds] = None) -> None:
        self.th = th or ScreenThresholds()

    def screen(self, c: CandidateInput, screened_at: Optional[date] = None) -> ScreenResult:
        """筛选单个标的。

        判定顺序（严格）：
          1. 数据完整性 —— 缺口存在则拒绝判定，不猜
          2. 稳健成长池 —— 四条全满足
          3. 困境反转池 —— 四条全满足
          4. 排除
        """
        res = ScreenResult(
            symbol=c.symbol, name=c.name, pool=Pool.EXCLUDED,
            screened_at=screened_at or date.today(),
        )

        # 步骤 1：数据完整性
        if c.data_gaps:
            res.excluded_reason = f"数据缺口，拒绝判定：{'；'.join(c.data_gaps)}"
            res.checks.append(ScreenCheck(
                "数据完整性", False, f"{len(c.data_gaps)} 项缺口", 0,
                "缺口存在时不猜，直接拒绝判定",
            ))
            return res

        res.checks.append(ScreenCheck("数据完整性", True, "完整", 0))

        # 步骤 2：稳健成长池
        growth_checks = self._growth_checks(c)
        res.checks.extend(growth_checks)
        if all(x.passed for x in growth_checks):
            res.pool = Pool.GROWTH
            return res

        # 步骤 3：困境反转池
        turn_checks = self._turnaround_checks(c)
        res.checks.extend(turn_checks)
        if all(x.passed for x in turn_checks):
            res.pool = Pool.TURNAROUND
            return res

        # 步骤 4：排除
        failed_growth = [x.name for x in growth_checks if not x.passed]
        failed_turn = [x.name for x in turn_checks if not x.passed]
        res.excluded_reason = (
            f"成长池未通过：{'、'.join(failed_growth) or '无'}；"
            f"反转池未通过：{'、'.join(failed_turn) or '无'}"
        )
        return res

    def _growth_checks(self, c: CandidateInput) -> list[ScreenCheck]:
        checks: list[ScreenCheck] = []

        rg = c.revenue_growth
        checks.append(ScreenCheck(
            "营收增速", rg is not None and rg > self.th.revenue_growth_min,
            f"{rg:+.2f}%" if rg is not None else "缺失",
            f">{self.th.revenue_growth_min:.0f}%",
        ))

        dn = c.deducted_np_growth
        checks.append(ScreenCheck(
            "扣非净利增速", dn is not None and dn > self.th.deducted_np_growth_min,
            f"{dn:+.2f}%" if dn is not None else "缺失",
            f">{self.th.deducted_np_growth_min:+.0f}%",
            "否决项：防非经常性损益掩盖主营恶化",
        ))

        w = self.th.gross_margin_trend_window
        gm = c.gross_margins[-w:] if len(c.gross_margins) >= w else []
        declining = len(gm) == w and all(gm[i] >= gm[i + 1] for i in range(len(gm) - 1))
        checks.append(ScreenCheck(
            f"毛利率连续 {w} 期不下滑", len(gm) == w and not declining,
            [round(x, 2) for x in gm] if gm else "不足",
            f"{w} 期",
        ))

        checks.append(ScreenCheck(
            "经营现金流为正", c.ocf is not None and c.ocf > 0,
            c.ocf if c.ocf is not None else "缺失", ">0",
        ))
        return checks

    def _turnaround_checks(self, c: CandidateInput) -> list[ScreenCheck]:
        """困境反转池准入。

        第 3 条是本池最关键的设计 —— **必须能说出反转驱动是什么**。
        说不出就不许进池，否则这个池子会变成收容所。
        """
        checks: list[ScreenCheck] = []

        n = self.th.deducted_decline_narrowing_periods
        s = c.deducted_np_series[-n:] if len(c.deducted_np_series) >= n else []
        narrowing = False
        if len(s) == n and all(v < 0 for v in s):
            # 降幅逐期收窄：负数绝对值递减
            narrowing = all(abs(s[i]) > abs(s[i + 1]) for i in range(len(s) - 1))
        checks.append(ScreenCheck(
            f"扣非降幅连续 {n} 期收窄", narrowing,
            [round(v, 2) for v in s] if s else "不足", f"{n} 期",
        ))

        checks.append(ScreenCheck(
            "经营现金流转正", c.ocf is not None and c.ocf > 0,
            c.ocf if c.ocf is not None else "缺失", ">0",
        ))

        has_driver = bool(c.turnaround_driver and c.turnaround_driver.strip())
        checks.append(ScreenCheck(
            "有可命名的反转驱动", has_driver,
            c.turnaround_driver or "未提供",
            "必须提供",
            "硬约束：说不出驱动是什么，就不许进池",
        ))

        da = c.debt_to_asset
        checks.append(ScreenCheck(
            "资产负债率", da is not None and da < self.th.debt_to_asset_max,
            f"{da:.2f}%" if da is not None else "缺失",
            f"<{self.th.debt_to_asset_max:.0f}%",
        ))
        return checks


# ============================================================================
# 排除日志
# ============================================================================

@dataclass
class ExclusionRecord:
    """一条排除记录。"""

    symbol: str
    name: str
    excluded_at: date
    reason: str
    ref_close: Optional[float] = None
    benchmark_close: Optional[float] = None
    reviewed_at: Optional[date] = None
    later_close: Optional[float] = None
    later_benchmark: Optional[float] = None

    @property
    def excess_return_pct(self) -> Optional[float]:
        """相对基准的超额收益（%）。

        **这是本模块最重要的修正**：不用绝对涨幅，用相对基准的超额收益。
        绝对涨幅会随大盘整体上涨而虚高，得出"门槛太严"的错误结论。
        """
        if None in (self.ref_close, self.benchmark_close, self.later_close, self.later_benchmark):
            return None
        if not self.ref_close or not self.benchmark_close:
            return None
        stock_ret = (self.later_close / self.ref_close - 1) * 100
        bench_ret = (self.later_benchmark / self.benchmark_close - 1) * 100
        return round(stock_ret - bench_ret, 2)


class ExclusionLog:
    """排除日志。

    每周统计一次：被排除的标的中，有多少**显著跑赢基准**。
    这是筛选标准唯一能自我校准的机制。

    解读规则（防止不断放宽标准）：
      - 若超额收益 > +10% 的占比持续偏高  -> 门槛可能过严
      - 若该占比极低                       -> 门槛有效
      - 若出现"放宽一次门槛后占比更高"的循环 -> 停止放宽，改为检查数据质量
    """

    EXCESS_THRESHOLD = 10.0

    def __init__(self) -> None:
        self.records: list[ExclusionRecord] = []

    def record(self, res: ScreenResult, ref_close: Optional[float] = None,
               benchmark_close: Optional[float] = None) -> ExclusionRecord:
        rec = ExclusionRecord(
            symbol=res.symbol, name=res.name,
            excluded_at=res.screened_at or date.today(),
            reason=res.excluded_reason,
            ref_close=ref_close, benchmark_close=benchmark_close,
        )
        self.records.append(rec)
        return rec

    def weekly_review(self) -> dict:
        """周度复盘统计。"""
        reviewed = [r for r in self.records if r.excess_return_pct is not None]
        if not reviewed:
            return {
                "total_excluded": len(self.records),
                "reviewed": 0,
                "note": "尚无已结算的排除记录，无法评估门槛有效性",
            }
        beat = [r for r in reviewed if r.excess_return_pct > self.EXCESS_THRESHOLD]
        ratio = len(beat) / len(reviewed) * 100

        if ratio > 40:
            verdict = "门槛可能过严：被排除标的中显著跑赢基准的占比偏高"
        elif ratio < 15:
            verdict = "门槛有效：被排除标的中跑赢基准的占比低"
        else:
            verdict = "门槛中性：继续观察"

        return {
            "total_excluded": len(self.records),
            "reviewed": len(reviewed),
            "beat_benchmark": len(beat),
            "beat_ratio_pct": round(ratio, 1),
            "verdict": verdict,
            "top_beaten": sorted(
                [{"symbol": r.symbol, "excess_pct": r.excess_return_pct} for r in beat],
                key=lambda x: -x["excess_pct"],
            )[:5],
        }


if __name__ == "__main__":
    print("=" * 72)
    print("筛选层 · 自检")
    print("=" * 72)

    b = PoolBuilder()
    today = date(2026, 10, 8)

    print("\n【一】科伦药业（002422）按真实数据筛选")
    print("-" * 72)
    kl = CandidateInput(
        symbol="002422", name="科伦药业",
        revenue_growth=0.75,               # 2026H1 医药制造口径同比
        deducted_np_growth=-29.03,         # 2026H1 扣非同比
        gross_margins=[47.89, 47.85, 47.57],
        ocf=23.41,
        deducted_np_series=[-29.03],
        turnaround_driver="创新药 ADC 管线全球兑现 + 传统输液业务周期出清",
        debt_to_asset=28.11,
        goodwill_to_equity=0.96 / 246.17 * 100,
    )
    r1 = b.screen(kl, today)
    print(r1.explain())

    print("\n【二】数据缺口时拒绝判定（不猜）")
    print("-" * 72)
    gap = CandidateInput(
        symbol="XXXXXX", name="某标的",
        revenue_growth=25.0, deducted_np_growth=18.0,
        data_gaps=["margin_balance 接口限频", "gross_margin 字段错位"],
    )
    print(b.screen(gap, today).explain())

    print("\n【三】合格的成长池标的")
    print("-" * 72)
    good = CandidateInput(
        symbol="600XXX", name="某成长标的",
        revenue_growth=32.5, deducted_np_growth=28.1,
        gross_margins=[38.2, 39.5, 41.0], ocf=15.6,
        debt_to_asset=35.0, goodwill_to_equity=2.1,
    )
    print(b.screen(good, today).explain())

    print("\n【四】排除日志 · 周度复盘（用相对基准而非绝对涨幅）")
    print("-" * 72)
    log = ExclusionLog()
    for sym, nm, ref, bench, later, later_b in [
        ("A", "标的A", 10.0, 3000.0, 13.5, 3060.0),   # 超额 +33.0%
        ("B", "标的B", 20.0, 3000.0, 20.4, 3060.0),   # 超额 -0.0%
        ("C", "标的C", 15.0, 3000.0, 18.0, 3030.0),   # 超额 +19.0%
        ("D", "标的D", 8.0,  3000.0, 8.1,  3060.0),   # 超额 -0.75%
    ]:
        rec = ExclusionRecord(
            symbol=sym, name=nm, excluded_at=date(2026, 9, 1), reason="示例",
            ref_close=ref, benchmark_close=bench,
            reviewed_at=today, later_close=later, later_benchmark=later_b,
        )
        log.records.append(rec)
        print(f"  {sym} 排除后股价 {ref}->{later}，基准 {bench}->{later_b}，"
              f"超额收益 {rec.excess_return_pct:+.2f}%")

    import json
    print()
    print(json.dumps(log.weekly_review(), ensure_ascii=False, indent=2))
    print("\n  说明：若按绝对涨幅判读，标的C（+20%）和标的A（+35%）都会")
    print("        被误判为『门槛太严』；但扣除基准涨幅后，只有 A、C 真正跑赢。")
