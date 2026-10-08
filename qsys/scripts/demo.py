#!/usr/bin/env python
"""
端到端演示：五步法完整流水线
============================

本脚本把五个模块串成一条完整链路，用真实数据（科伦药业 002422.SZ）跑一遍：

    数据层 -> 三层指标 -> 三级信号 -> 风控 -> 筛选池 -> 复盘

运行：
    cd qsys && PYTHONPATH=src python scripts/demo.py

设计说明
--------
本演示**不联网**。行情数据以常量形式内嵌（来自 2026-10-08 的真实取数结果），
目的是让整条链路可以在无网络环境下验证。

生产环境中，这些数据应由 data 层的 adapter 提供 —— 见 `src/qsys/data/`。
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from qsys.data.base import DataGap, FieldValue, FreshnessPolicy, TradingState, assert_same_series
from qsys.indicators.layers import CapitalFlowLayer, FundamentalLayer, Thresholds, TriState
from qsys.review.scoring import (
    Emotion, EmotionTag, ExecutionType, Hypothesis, ParamCandidate, ParamWhitelist,
    TradeRecord, calc_discipline, calc_emotion, calc_logic, rank_candidates,
)
from qsys.risk.management import DrawdownBudget, PositionSizer, StopBuilder
from qsys.rules.cn import CNMarketRules, FeeSchedule, RULES_VERIFIED_AT
from qsys.screen.pool import CandidateInput, PoolBuilder
from qsys.signals.tiers import Action, SignalEngine


# ============================================================================
# 真实行情数据（前复权收盘价，来源：2026-10-08 逐日 K 线接口取数）
# ============================================================================

KLINE_DATES = [
    "07-01", "07-02", "07-03", "07-06", "07-07", "07-08", "07-09", "07-10",
    "07-13", "07-14", "07-15", "07-16", "07-17", "07-20", "07-21", "07-22",
    "07-23", "07-24", "07-27", "07-28", "07-29", "07-30", "07-31",
    "08-03", "08-04", "08-05", "08-06", "08-07", "08-10", "08-11", "08-12",
    "08-13", "08-14", "08-17", "08-18", "08-19", "08-20", "08-21",
    "08-24", "08-25", "08-26", "08-27", "08-28", "08-31",
    "09-01", "09-02", "09-03", "09-04", "09-07", "09-08", "09-09", "09-10",
    "09-11", "09-14", "09-15", "09-16", "09-17", "09-18", "09-21", "09-22",
    "09-23", "09-24", "09-28", "09-29", "09-30", "10-08",
]

KLINE_CLOSES = [
    41.95, 44.75, 47.98, 47.73, 43.61, 41.98, 42.70, 44.07,
    44.80, 48.50, 49.07, 49.90, 44.91, 47.80, 47.36, 49.22,
    51.35, 46.65, 47.51, 45.21, 43.04, 42.79, 43.44,
    41.33, 42.29, 42.90, 42.51, 45.44, 46.41, 46.42, 46.91,
    46.30, 45.11, 46.69, 46.26, 44.97, 47.15, 44.87,
    41.44, 42.57, 43.64, 42.28, 41.75, 40.58,
    40.83, 41.75, 41.82, 41.92, 41.54, 40.76, 40.37, 40.14,
    39.35, 41.38, 40.65, 40.52, 40.02, 39.77, 42.48, 42.00,
    41.70, 38.86, 38.60, 39.22, 40.85, 39.26,
]

# 逐日高低价（用于 ATR 计算，取近似：high = close*1.012, low = close*0.988）
KLINE_HIGHS = [round(c * 1.012, 2) for c in KLINE_CLOSES]
KLINE_LOWS = [round(c * 0.988, 2) for c in KLINE_CLOSES]

SYMBOL = "002422"
NAME = "科伦药业"
AS_OF = datetime(2026, 10, 8, 15, 0)


def banner(title: str) -> None:
    print()
    print("=" * 78)
    print(f"  {title}")
    print("=" * 78)


def sub(title: str) -> None:
    print()
    print(f"── {title} " + "─" * max(0, 72 - len(title)))


# ============================================================================

def main() -> None:
    print()
    print("╔" + "═" * 76 + "╗")
    print("║" + "五步法完整流水线 · 端到端演示".center(68) + "║")
    print("║" + f"标的：{SYMBOL} {NAME}    取数时点：{AS_OF:%Y-%m-%d %H:%M}".center(64) + "║")
    print("║" + f"A股制度规则核验日期：{RULES_VERIFIED_AT}".center(68) + "║")
    print("╚" + "═" * 76 + "╝")

    # ========================================================================
    banner("第 0 层 · 数据时效纪律")
    # ========================================================================

    policy = FreshnessPolicy()

    sub("0.1 盘中价不能当收盘价")
    intraday = FieldValue("close", 39.26, datetime(2026, 10, 8, 11, 28),
                          "行情接口", TradingState.INTRADAY, unit="元")
    ok, why = policy.check(intraday, "price", now=AS_OF)
    print(f"    11:28 取数 {intraday.value} 元 -> 可用于决策: {ok}")
    print(f"    原因: {why}")

    sub("0.2 原框架的 45.44 元 —— 过期多久了")
    stale = FieldValue("close", 45.44, datetime(2026, 8, 7, 15, 0),
                       "行情接口", TradingState.CLOSED, unit="元")
    ok, why = policy.check(stale, "price", now=AS_OF)
    print(f"    2026-08-07 收盘 45.44 元，距今天 {stale.age_days(AS_OF):.0f} 天")
    print(f"    可用于决策: {ok} | {why}")

    sub("0.3 跨时点拼接校验")
    a = FieldValue("close", 38.52, datetime(2026, 7, 1, 9, 30), "行情接口", TradingState.CLOSED, unit="元")
    b = FieldValue("close", 45.44, datetime(2026, 8, 7, 15, 0), "行情接口", TradingState.CLOSED, unit="元")
    ok, why = assert_same_series(a, b)
    print(f"    '38.52 -> 45.44 涨 18%' 校验通过: {ok}")
    print(f"    {why}")

    sub("0.4 数据缺口必须显式标记")
    try:
        raise DataGap("margin_balance", "接口限频，本轮未返回", SYMBOL)
    except DataGap as e:
        print(f"    {e}")
        print(f"    -> 该字段不得进入决策，不得以 0 代替")

    # ========================================================================
    banner("第一步 · 好公司池构建")
    # ========================================================================

    builder = PoolBuilder()
    cand = CandidateInput(
        symbol=SYMBOL, name=NAME,
        revenue_growth=0.75,          # 2026H1 医药制造口径同比
        deducted_np_growth=-29.03,    # 2026H1 扣非同比
        gross_margins=[47.89, 47.85, 47.57],
        ocf=23.41,                    # 2026H1 经营现金流净额（亿元）
        deducted_np_series=[-29.03],
        turnaround_driver="创新药 ADC 管线全球兑现 + 传统输液业务周期出清",
        debt_to_asset=28.11,
        goodwill_to_equity=0.96 / 246.17 * 100,
    )
    res = builder.screen(cand, AS_OF.date())
    print(res.explain())
    print()
    print(f"    >> 结论：进入 {res.pool.value} 池")

    sub("排除日志（用相对基准，不用绝对涨幅）")
    print("    记录被排除标的 + 排除原因 + 后续相对基准的超额收益")
    print("    解读：超额 >+10% 占比持续偏高 -> 门槛可能过严")
    print("         若出现『放宽门槛后占比更高』的循环 -> 停止放宽，检查数据质量")

    # ========================================================================
    banner("第二步 · 多维监控信号")
    # ========================================================================

    th = Thresholds()
    tech_ok = len(KLINE_CLOSES) >= 250
    print(f"    可用 K 线根数: {len(KLINE_CLOSES)}（趋势级需 250 根）")
    if not tech_ok:
        print(f"    ⚠ 不足 250 根，趋势级指标将返回 UNKNOWN，系统据此拒绝判定（不猜）")

    # 用短窗口指标演示（生产环境由 adapter 提供完整历史）
    from qsys.indicators.layers import IndicatorLayer, TechnicalLayer
    tl = TechnicalLayer(th)

    sub("技术面指标（部分窗口受数据长度限制）")
    for label, result in [
        ("MACD", tl.macd(KLINE_CLOSES)),
        ("RSI12", tl.rsi_state(KLINE_CLOSES)),
        ("KDJ", tl.kdj_state(KLINE_CLOSES)),
        ("BOLL", tl.boll(KLINE_CLOSES)),
        ("均线排列", tl.ma_alignment(KLINE_CLOSES)),
    ]:
        state = result.state.value
        print(f"    {label:<10} {state:<12} {result.reason}")

    atr = tl.atr(KLINE_HIGHS, KLINE_LOWS, KLINE_CLOSES)
    print(f"    {'ATR20':<10} {'—':<12} {atr:.4f}" if atr else "    ATR20 数据不足")

    # 用 120 根合成序列演示完整趋势级（明确标注为合成数据）
    sub("趋势级完整演示（300 根合成序列，仅用于验证逻辑，非真实数据）")
    import random
    random.seed(7)
    synth = [30.0]
    for _ in range(299):
        synth.append(synth[-1] * (1 + random.gauss(0.0012, 0.018)))
    ma_res = tl.ma_alignment(synth)
    yr_res = tl.price_vs_ma250(synth)
    print(f"    {'均线排列':<10} {ma_res.state.value:<12} {ma_res.reason}")
    print(f"    {'价格vs年线':<10} {yr_res.state.value:<12} {yr_res.reason}")

    sub("三级信号引擎")
    eng = SignalEngine(th, decay_days=3)
    print(eng.tradeoff_note())
    print()
    sigs = (eng.trend_tier(ma_res, yr_res, AS_OF)
            + eng.momentum_tier(tl.macd(synth), tl.rsi_state(synth), AS_OF)
            + eng.extreme_tier(tl.kdj_state(synth), tl.boll(synth), AS_OF))
    decision = eng.decide(sigs, now=AS_OF)
    print(decision.explain())

    # ========================================================================
    banner("第三步 · 大资金流向验证")
    # ========================================================================

    cfl = CapitalFlowLayer(th)
    print("    资金面四维（水位 / 方向 / 结构 / 筹码）")
    print("    关键：水位与方向必须分开看 —— 高位 + 方向转负是杠杆放大风险，不是抄底信号")
    print()

    readings = [
        cfl.water_level(80.0),          # 8/19 融资余额超近一年 80% 分位
        cfl.direction(-2932.0),          # 9/30 融资净卖出 2932 万元
        cfl.structure(None, None),       # 龙虎榜席位性质未取到
        cfl.chips(None),                 # 股东户数未取到
    ]
    for r in readings:
        print(f"    {r.dimension:<6} {str(r.value):<14} {r.state.value:<10} {r.note}")

    rating, reason = cfl.overall_rating(readings)
    print()
    print(f"    >> 资金结构评级：{rating} —— {reason}")
    print()
    print("    注意：原框架的定性判断是『资金合力不足，短线博弈成分高』，")
    print("          但同期基金持股比例 14.89%（+3.82pct）、主动偏股基金 122 家（+46 家），")
    print("          机构评级买入/增持占比 100%。这个定性需要重新权衡。")

    # ========================================================================
    banner("第四步 · 信号触发 -> 小仓位试仓（风控前置）")
    # ========================================================================

    rules = CNMarketRules()
    sizer = PositionSizer(rules)
    stop_builder = StopBuilder(atr_multiple=th.stop_loss_atr_multiple)

    sub("4.1 止损方案（结构位 + ATR 双条件）")
    prior_low = 38.42   # 2026-09-29 盘中低点（真实前低，非 38.52）
    plan = stop_builder.build(
        entry_price=39.26, atr20=atr if atr else 0.98, prior_low=prior_low,
        logic_conditions=[("扣非净利连续 2 季降幅扩大", False)],
    )
    for lv in plan.levels:
        p = f"{lv.price:.2f}" if lv.price else "—"
        print(f"    {lv.stop_type.value:<10} {p:>8}  {lv.width_pct:>7.2f}%  {lv.reason}")
    eff = plan.effective_stop
    print()
    print(f"    生效止损位（取更近者）: {eff.stop_type.value} @ {eff.price:.2f}")

    sub("4.2 仓位：标准法 vs 制度修正版")
    print("    原框架顺序：先定 8%–12% 仓位，再找止损位 -> 无法保证单笔损失可控")
    print("    修正顺序：先定单笔损失 -> ATR 定宽度 -> 反推仓位")
    print()
    cmp = sizer.compare(
        total_capital=1_000_000, risk_pct=1.0,
        entry_price=45.44, stop_price=40.00,
        symbol=SYMBOL, max_consecutive_limit_down=3,
    )
    print(f"    {cmp['standard']}")
    print(f"    {cmp['worst_case']}")
    print()
    print(f"    {cmp['conclusion']}")

    sub("4.3 A 股制度约束（本轮核验）")
    for label, txt in [
        ("涨跌停", f"主板 ±10% | 科创板/创业板 ±20% | 北交所 ±30%"),
        ("ST 新规", "主板 ST/*ST 已由 ±5% 调整为 ±10%（2026-07-06 起施行）"),
        ("最小单位", "主板/创业板 100 股 | 科创板 200 股起、1 股递增"),
        ("T+1", "当日买入不可当日卖出"),
        ("停牌", "以不停牌为原则；停牌日不计入监控计时；复牌首日不适用常规止损"),
    ]:
        print(f"    {label:<8} {txt}")

    sub("4.4 回撤预算自洽性校验")
    budget = DrawdownBudget(
        total_capital=1_000_000, max_drawdown_pct=20.0,
        max_loss_per_trade_pct=1.0, max_concurrent_positions=10,
    )
    v = budget.validate()
    print(f"    回撤熔断 20% / 单笔 1% / 最多 10 只 -> {'通过' if v['is_valid'] else '不通过'}")
    print(f"    {v['note']}")
    for i in v["issues"]:
        print(f"    ⚠ {i}")

    # ========================================================================
    banner("第五步 · 长期持有与动态调整")
    # ========================================================================

    print("    监控清单必须长成『阈值 + 动作』，而不是『是否……？』")
    print()
    watchlist = [
        ("扣非净利同比", "季", "连续 2 季降幅扩大", "降仓 1/3 + 逻辑复核"),
        ("归母vs扣非背离", "季", ">15pct 且无一次性原因", "标记利润质量存疑，禁止加仓"),
        ("科伦博泰药品销售", "半年", "环比不增", "逻辑降级，重估持仓理由"),
        ("融资余额分位", "日", "待校准（原 80% 已撤回）", "转入风控观察"),
        ("资产负债率", "季", ">60%", "风控复核"),
    ]
    print(f"    {'指标':<18} {'频率':<6} {'变坏阈值':<26} {'动作'}")
    print("    " + "-" * 72)
    for w in watchlist:
        print(f"    {w[0]:<18} {w[1]:<6} {w[2]:<26} {w[3]}")

    sub("财报日历（下一节点）")
    print("    2026-10-29  三季报预约披露日  <- 原框架清单中缺失")
    print("    机制：披露前 3 天自动提醒；把『等财报』从被动等待变成主动排期")

    sub("空窗期持有依据（两次财报之间的 90 天）")
    print("    基本面拿不到新数据时，改用两条可日频观测的替代条件：")
    print("      ① 价格结构未破坏（未跌破前低）")
    print("      ② 资金结构未背离（方向未转负）")
    print("    任一条破坏即进入观察 —— 消除『90 天决策真空』")

    # ========================================================================
    banner("第六步 · 复盘迭代（闭环）")
    # ========================================================================

    sub("6.1 纪律分")
    skipped = [TradeRecord(SYMBOL, 40.00, 0.0, 10000, date(2026, 9, 29),
                           ExecutionType.STOP_LOSS, plan_was_mandatory=True)]
    d = calc_discipline([], skipped)
    print(f"    {d}")
    for v_ in d.violations:
        print(f"      - {v_}")

    sub("6.2 逻辑分")
    hyps = [
        Hypothesis("科伦博泰 ADC 全球放量", "falsified",
                   "2026H1 药品销售 6.57 亿，但利润含 7.03 亿一次性和解收入"),
        Hypothesis("传统输液业务企稳", "verified", "非输液收入同比 +5.72%"),
        Hypothesis("扣非利润改善", "falsified", "2026H1 扣非 -29.03%"),
    ]
    l = calc_logic(hyps)
    print(json.dumps(l, ensure_ascii=False, indent=6))

    sub("6.3 情绪分")
    tags = [
        EmotionTag(date(2026, 8, 24), Emotion.RELUCTANCE, "hold", "不甘心在 42 元减仓"),
        EmotionTag(date(2026, 9, 24), Emotion.ANXIETY, "hold", "焦虑但没动"),
    ]
    e = calc_emotion(tags)
    print(json.dumps(e, ensure_ascii=False, indent=6))

    sub("6.4 参数寻优（Sortino 为主，CVaR 为约束）")
    import random as rnd
    rnd.seed(42)
    cands = [
        ParamCandidate("止损 1.5×ATR", [rnd.gauss(0.0009, 0.022) for _ in range(250)]),
        ParamCandidate("止损 2.0×ATR", [rnd.gauss(0.0010, 0.016) for _ in range(250)]),
        ParamCandidate("止损 3.0×ATR", [rnd.gauss(0.0007, 0.013) for _ in range(250)]),
    ]
    print(f"    {'组合':<16} {'Sortino':>9} {'CVaR5%':>9} {'最大回撤':>10} {'卡玛':>8}")
    for row in rank_candidates(cands):
        print(f"    {row['label']:<16} {row['sortino']:>9.3f} {row['cvar_5pct']:>9.4f} "
              f"{row['max_drawdown']:>10.4f} {row['calmar']:>8.3f}")
    print("    注：卡玛仅展示，不作寻优目标（最大回撤路径依赖、易过拟合）")

    sub("6.5 参数白名单（防过拟合）")
    wl = ParamWhitelist()
    print("    可调参数（适应市场）：")
    for k, (lo, hi) in wl.adjustable.items():
        print(f"      {k:<32} 区间 [{lo}, {hi}]")
    print("    固定约束（约束你自己，不许优化）：")
    for k, note in wl.fixed.items():
        print(f"      {k:<32} {note}")

    # ========================================================================
    banner("执行顺序（P0 阻塞全部）")
    # ========================================================================

    print("""
    P0  ① 确认 2026-08-24 与 2026-09-29 两次动作是否执行
        ② 填写 8 组个人约束（资金规模/单笔损失/熔断线/单标的上限/
                              持仓数/期限/交易权限/实际成本）
        >> 纯输入，不需加工；缺了它们后续全是空转

    P1  ③ 补「选股筛选研报」最简版（门槛 + 否决原因）
        ④ 建立排除日志（用相对基准）
        >> 筛选是唯一带否决权的环节，其错误会被复制到池内每一只标的

    P2  ⑤ 校准拥挤度阈值（分档回测）
        ⑥ 定义交易成本与 T+1 建模
        ⑦ 补涨跌停/停牌的损失重算规则
        >> 在它们完成前跑出的任何参数结论都不可信

    P3  ⑧ 11 指标压缩为三级信号 + 三态定义
        ⑨ 第四步改为风险预算法
        ⑩ 第五步监控清单补阈值与动作

    P4  ⑪ 用 NVDA 跑一遍完整五步，验证美股侧
        ⑫ 参数寻优（Sortino 目标）
    """)

    print("=" * 78)
    print("  演示结束。所有模块均可独立运行：")
    for m in ["rules/cn.py", "data/base.py", "indicators/layers.py",
              "signals/tiers.py", "risk/management.py", "screen/pool.py", "review/scoring.py"]:
        print(f"    PYTHONPATH=src python src/qsys/{m}")
    print("=" * 78)
    print()


if __name__ == "__main__":
    main()
