/* ============================================================================
 * cqs/tests/test_all.c — G2 单元测试 + G3 集成测试
 * ============================================================================
 * 测试重点不是"功能能不能跑"，而是三类**不会报错、只会悄悄算错**的场景：
 *   1. 制度约束数值（涨跌停 / T+1 / 最小单位 / ST 新规）
 *   2. 数据时效（盘中价、过期价、跨时点拼接）
 *   3. 决策分支覆盖（未覆盖的分支会落到兜底，看起来能跑但结论是错的）
 * ==========================================================================*/

#include "cqs/analysis.h"
#include "cqs/core.h"
#include "cqs/data.h"
#include "cqs/risk.h"
#include "cqs/rules.h"
#include "cqs/signal.h"

#include <math.h>
#include <stdio.h>
#include <string.h>

static int g_pass = 0;
static int g_fail = 0;

#define CHECK(cond, msg)                                                       \
    do {                                                                       \
        if (cond) { g_pass++; }                                                \
        else { g_fail++; printf("  ✗ %s  (%s:%d)\n", msg, __FILE__, __LINE__); }\
    } while (0)

#define CHECK_NEAR(a, b, eps, msg)                                             \
    do {                                                                       \
        double _a = (a), _b = (b);                                             \
        if (fabs(_a - _b) <= (eps)) { g_pass++; }                              \
        else { g_fail++; printf("  ✗ %s  期望 %.4f 实得 %.4f (%s:%d)\n",       \
                                msg, _b, _a, __FILE__, __LINE__); }            \
    } while (0)

static void section(const char *t) { printf("\n── %s\n", t); }

/* ==========================================================================
 * G2-1 制度约束
 * ========================================================================*/

static void test_rules(void) {
    section("制度约束");
    const market_rules_t *cn = market_cn();

    /* 板块识别 */
    CHECK(mr_classify(cn, "600519") == BOARD_CN_MAIN, "600519 应为沪深主板");
    CHECK(mr_classify(cn, "002422") == BOARD_CN_MAIN, "002422 应为沪深主板");
    CHECK(mr_classify(cn, "688111") == BOARD_CN_STAR, "688111 应为科创板");
    CHECK(mr_classify(cn, "300750") == BOARD_CN_GEM,  "300750 应为创业板");
    CHECK(mr_classify(cn, "301029") == BOARD_CN_GEM,  "301029 应为创业板");
    CHECK(mr_classify(cn, "830799") == BOARD_CN_BSE,  "830799 应为北交所");

    /* 涨跌停：前收 39.26 */
    double lo = 0.0, hi = 0.0;
    mr_price_limits(cn, 39.26, "002422", false, &lo, &hi);
    CHECK_NEAR(lo, 35.33, 0.01, "主板跌停价");
    CHECK_NEAR(hi, 43.19, 0.01, "主板涨停价");

    mr_price_limits(cn, 39.26, "688111", false, &lo, &hi);
    CHECK_NEAR(lo, 31.41, 0.01, "科创板跌停价");
    CHECK_NEAR(hi, 47.11, 0.01, "科创板涨停价");

    /* 回归：主板 ST 涨跌幅已于 2026-07-06 由 5% 调整为 10%。
     * 若有人误按旧规改成 5%，本测试会失败。 */
    mr_price_limits(cn, 39.26, "600519", true, &lo, &hi);
    CHECK_NEAR(lo, 35.33, 0.01, "主板 ST 应为 ±10%（非 ±5%）");
    CHECK(fabs(lo - 37.30) > 0.5, "主板 ST 不应等于旧规 5% 的 37.30");

    /* T+1 */
    CHECK(!mr_can_sell(cn, 100, 100), "当日买入不可当日卖出");
    CHECK(mr_can_sell(cn, 100, 101),  "次日可卖");
    CHECK(mr_can_sell(market_us(), 100, 100), "美股 T+0 可当日卖");

    /* 最小申报单位 */
    CHECK(mr_round_qty(cn, 357, "600519") == 300, "主板 357 -> 300（100 整数倍）");
    CHECK(mr_round_qty(cn, 99,  "600519") == 0,   "主板不足一手 -> 0");
    CHECK(mr_round_qty(cn, 357, "688111") == 357, "科创板 357 保留（1 股递增）");
    CHECK(mr_round_qty(cn, 150, "688111") == 0,   "科创板不足 200 -> 0");

    /* 最坏损失：45.44 入场 / 40.00 止损 / 1 万股 / 连续 3 跌停 */
    double wc = mr_worst_loss(cn, 45.44, 10000, "002422", 40.00, 3);
    double planned = (45.44 - 40.00) * 10000.0;
    CHECK_NEAR(planned, 54400.0, 1.0, "计划损失");
    CHECK(wc > planned, "最坏损失应大于计划损失");
    CHECK_NEAR(mr_loss_amplification(cn, 45.44, 10000, "002422", 40.00, 3),
               2.26, 0.02, "放大倍数应为 2.26x");

    /* 回归：0 连跌停时最坏损失应等于计划损失，而不是 0 */
    double wc0 = mr_worst_loss(cn, 45.44, 10000, "002422", 40.00, 0);
    CHECK_NEAR(wc0, planned, 1.0, "0 连跌停 -> 最坏损失 = 计划损失（不是 0）");

    /* 美股无涨跌停 -> 最坏损失即计划损失 */
    double wcus = mr_worst_loss(market_us(), 100.0, 100, "NVDA", 90.0, 5);
    CHECK_NEAR(wcus, 1000.0, 1.0, "美股无涨跌停 -> 最坏损失 = 计划损失");

    /* 费用完整性：未填齐必须返回 -1，不得返回 0 */
    fee_schedule_t f = fee_default();
    CHECK(!fee_complete(&f), "默认费用不完整");
    CHECK_NEAR(fee_round_trip_pct(&f), -1.0, 1e-9, "不完整时往返成本应为 -1");

    fee_schedule_t f2 = { 0.05, 0.025, 5.0, 0.001, 0.1 };
    CHECK(fee_complete(&f2), "填齐后应完整");
    CHECK_NEAR(fee_round_trip_pct(&f2), 0.05 + 0.05 + 0.002 + 0.2, 1e-6, "往返成本");

    /* 停牌默认值必须安全 */
    suspension_rule_t sr = suspension_default();
    CHECK(!sr.count_suspended_days_in_monitor, "停牌日不得计入监控计时");
    CHECK(!sr.first_day_after_resume_normal_stop, "复牌首日不得适用常规止损");
}

/* ==========================================================================
 * G2-2 数据时效
 * ========================================================================*/

static void test_data(void) {
    section("数据时效");
    time_t now = fixture_last_day();

    /* 评分钳位 */
    CHECK_NEAR(score_of(score_new(-50.0)), 0.0,   1e-9, "评分下钳位");
    CHECK_NEAR(score_of(score_new(150.0)), 100.0, 1e-9, "评分上钳位");

    /* 盘中价必须被拒绝 —— 同一字段不同秒取数会得到不同值 */
    field_value_t intra = fv_make("close", 39.26, now, TS_INTRADAY, "test");
    const char *why = fv_reject(intra, freshness_days(FC_PRICE), now);
    CHECK(why != NULL, "盘中价应被拒绝");
    CHECK(why && strstr(why, "盘中") != NULL, "拒绝原因应提到盘中价");

    /* 回归：45.44 元是 2026-08-07 的收盘价，两个月后不可用于决策 */
    field_value_t stale = fv_make("close", 45.44,
                                  now - (time_t)62 * 86400, TS_CLOSED, "test");
    why = fv_reject(stale, freshness_days(FC_PRICE), now);
    CHECK(why != NULL, "过期价应被拒绝");
    CHECK(why && strstr(why, "过期") != NULL, "拒绝原因应提到过期");

    /* 新鲜收盘价应通过 */
    field_value_t fresh = fv_make("close", 39.26, now, TS_CLOSED, "test");
    CHECK(fv_reject(fresh, freshness_days(FC_PRICE), now) == NULL, "新鲜收盘价应可用");

    /* 停牌应被拒绝 */
    field_value_t susp = fv_make("close", 39.26, now, TS_SUSPENDED, "test");
    why = fv_reject(susp, freshness_days(FC_PRICE), now);
    CHECK(why && strstr(why, "停牌") != NULL, "停牌应被拒绝并说明原因");

    /* 回归：'38.52 -> 45.44 涨 18%' 是跨时点拼接，必须拦下 */
    field_value_t a = fv_make("close", 38.52, now - (time_t)99 * 86400, TS_CLOSED, "test");
    field_value_t b = fv_make("close", 45.44, now - (time_t)62 * 86400, TS_CLOSED, "test");
    why = NULL;
    CHECK(!series_compatible(a, b, 1.0, &why), "跨时点拼接应被拒绝");
    CHECK(why != NULL, "应给出拒绝原因");
    CHECK(!fv_pct_change(a, b).has_value, "不兼容时涨跌幅应为空值");

    /* 同源同日可比较 */
    field_value_t p = fv_make("close", 40.00, now - 86400, TS_CLOSED, "test");
    CHECK(series_compatible(p, fresh, 1.0, NULL), "相邻交易日应可比较");
    field_value_t chg = fv_pct_change(p, fresh);
    CHECK(chg.has_value, "可比较时应算出涨跌幅");
    CHECK_NEAR(chg.value, -1.85, 0.01, "涨跌幅计算");

    /* 数据源不同应拒绝 */
    field_value_t other = fv_make("close", 40.00, now, TS_CLOSED, "另一个源");
    CHECK(!series_compatible(fresh, other, 1.0, NULL), "数据源不同应拒绝");

    /* 缺口日志 */
    gap_log_t g;
    gap_log_init(&g);
    gap_log_add(&g, "002422", "margin_balance", "接口限频");
    char buf[256];
    CHECK(gap_log_dump(&g, buf, sizeof buf) == 1, "缺口日志应有 1 条");
    CHECK(strstr(buf, "margin_balance") != NULL, "缺口日志应含字段名");

    /* 缺口不得越界 */
    for (int i = 0; i < 200; i++) gap_log_add(&g, "X", "f", "r");
    CHECK(g.count == CQS_GAP_MAX, "缺口日志应在上限处停止");
}

/* ==========================================================================
 * G2-3 信号决策树
 * ========================================================================*/

static signal_t mk_sig(signal_tier_t tier, tri_state_t st, const char *name) {
    return (signal_t){ tier, name, st, false, "test" };
}

static void test_signals(void) {
    section("信号决策树");

    /* 核心规则：趋势空头时动量金叉不构成入场依据 */
    {
        signal_t s[4] = {
            mk_sig(TIER_TREND, TRI_BEAR, "均线"), mk_sig(TIER_TREND, TRI_BEAR, "年线"),
            mk_sig(TIER_MOMENTUM, TRI_BULL, "MACD"), mk_sig(TIER_MOMENTUM, TRI_BULL, "RSI"),
        };
        decision_t d = signals_decide(s, 4);
        CHECK(d.action == ACT_BLOCK, "趋势全空头 + 动量金叉 -> BLOCK");
        CHECK(d.has_decided_by && d.decided_by == TIER_TREND, "应由趋势级决定");
    }

    /* 趋势全多头 + 动量确认 -> ENTRY */
    {
        signal_t s[4] = {
            mk_sig(TIER_TREND, TRI_BULL, "均线"), mk_sig(TIER_TREND, TRI_BULL, "年线"),
            mk_sig(TIER_MOMENTUM, TRI_BULL, "MACD"), mk_sig(TIER_MOMENTUM, TRI_BULL, "RSI"),
        };
        CHECK(signals_decide(s, 4).action == ACT_ENTRY, "趋势+动量一致 -> ENTRY");
    }

    /* 回归：趋势级含中性必须显式处理，不得落到兜底分支。
     * 早期版本漏了此分支，「一中性一多头」会返回"未匹配"。 */
    {
        signal_t s[4] = {
            mk_sig(TIER_TREND, TRI_NEUTRAL, "均线"), mk_sig(TIER_TREND, TRI_BULL, "年线"),
            mk_sig(TIER_MOMENTUM, TRI_BULL, "MACD"), mk_sig(TIER_MOMENTUM, TRI_BULL, "RSI"),
        };
        decision_t d = signals_decide(s, 4);
        CHECK(d.action == ACT_WAIT, "趋势含中性 -> WAIT");
        CHECK(strstr(d.reason, "中性") != NULL, "理由应提到中性");
        CHECK(strstr(d.reason, "未匹配") == NULL, "不应落到兜底分支");
    }

    /* 回归：趋势级多空混合也必须显式处理 */
    {
        signal_t s[2] = {
            mk_sig(TIER_TREND, TRI_BULL, "均线"), mk_sig(TIER_TREND, TRI_BEAR, "年线"),
        };
        decision_t d = signals_decide(s, 2);
        CHECK(d.action == ACT_WAIT, "趋势多空混合 -> WAIT");
        CHECK(strstr(d.reason, "不一致") != NULL, "理由应提到方向不一致");
        CHECK(strstr(d.reason, "未匹配") == NULL, "不应落到兜底分支");
    }

    /* 趋势级数据缺失 -> 不猜 */
    {
        signal_t s[3] = {
            mk_sig(TIER_TREND, TRI_UNKNOWN, "均线"), mk_sig(TIER_TREND, TRI_UNKNOWN, "年线"),
            mk_sig(TIER_MOMENTUM, TRI_BULL, "MACD"),
        };
        decision_t d = signals_decide(s, 3);
        CHECK(d.action == ACT_WAIT, "趋势缺失 -> WAIT");
        CHECK(strstr(d.reason, "不猜") != NULL, "理由应说明不猜");
    }

    /* 穷举 4x4 趋势级状态组合，确保没有组合落到兜底分支 */
    {
        const tri_state_t st[4] = { TRI_BULL, TRI_BEAR, TRI_NEUTRAL, TRI_UNKNOWN };
        int uncovered = 0;
        for (int i = 0; i < 4; i++) {
            for (int j = 0; j < 4; j++) {
                signal_t s[3] = {
                    mk_sig(TIER_TREND, st[i], "均线"),
                    mk_sig(TIER_TREND, st[j], "年线"),
                    mk_sig(TIER_MOMENTUM, TRI_BULL, "MACD"),
                };
                decision_t d = signals_decide(s, 3);
                if (strstr(d.reason, "未匹配") != NULL) uncovered++;
            }
        }
        CHECK(uncovered == 0, "所有趋势级组合都应有明确分支（不得落到兜底）");
    }

    /* 信号衰减：过期信号不参与决策 */
    {
        signal_t s[3] = {
            mk_sig(TIER_TREND, TRI_BULL, "均线"), mk_sig(TIER_TREND, TRI_BULL, "年线"),
            { TIER_MOMENTUM, "MACD", TRI_BULL, true, "已过期" },
        };
        decision_t d = signals_decide(s, 3);
        CHECK(d.action == ACT_WAIT, "动量信号过期后不应触发 ENTRY");
    }

    /* 技术指标：数据不足时必须返回 UNKNOWN，不得猜 */
    {
        double short_series[50];
        for (int i = 0; i < 50; i++) short_series[i] = 10.0 + (double)i * 0.1;
        tech_snapshot_t snap = tech_compute(short_series, 50);
        tech_indicators_t ind = tech_indicators(&snap, short_series, 50);
        CHECK(ind.ma_align.state == TRI_UNKNOWN, "K 线不足时均线排列应为 UNKNOWN");
        CHECK(ind.vol_pct.state == TRI_UNKNOWN, "样本不足时波动率分位应为 UNKNOWN");
        CHECK(ind.macd.state != TRI_UNKNOWN, "50 根足够算 MACD");
    }

    /* 上升序列应识别为多头 */
    {
        double up[300];
        for (int i = 0; i < 300; i++) up[i] = 10.0 + (double)i;
        tech_snapshot_t snap = tech_compute(up, 300);
        tech_indicators_t ind = tech_indicators(&snap, up, 300);
        CHECK(ind.ma_align.state == TRI_BULL, "单调上升序列应判为多头排列");
    }
}

/* ==========================================================================
 * G2-4 风控
 * ========================================================================*/

static void test_risk(void) {
    section("风控");
    const market_rules_t *cn = market_cn();

    /* 生效止损位取更近者 */
    stop_plan_t sp = stop_build(39.26, 0.98, true, 38.42, true, NULL);
    const stop_level_t *eff = stop_effective(&sp);
    CHECK(eff != NULL, "应有生效止损位");
    CHECK(eff && eff->kind == STOP_STRUCTURE, "结构位 38.42 比 ATR 位更近，应生效");
    CHECK_NEAR(eff ? eff->price : 0.0, 38.42, 0.01, "生效止损价");

    /* ATR 位更近时生效 ATR */
    stop_plan_t sp2 = stop_build(39.26, 0.30, true, 30.00, true, NULL);
    const stop_level_t *e2 = stop_effective(&sp2);
    CHECK(e2 && e2->kind == STOP_ATR, "ATR 位更近时应生效 ATR");

    /* 逻辑止损优先于价格止损 */
    stop_plan_t sp3 = stop_build(39.26, 0.98, true, 38.42, true, "扣非降幅扩大");
    CHECK(sp3.logic_triggered, "逻辑条件命中应触发逻辑止损");
    char buf[192];
    CHECK(stop_evaluate(&sp3, 39.00, buf, sizeof buf), "未触及价格位也应触发（逻辑优先）");
    CHECK(strstr(buf, "逻辑") != NULL, "原因应说明是逻辑止损");

    /* 未触及时应给出距离 */
    stop_plan_t sp4 = stop_build(39.26, 0.98, true, 38.42, true, NULL);
    CHECK(!stop_evaluate(&sp4, 39.00, buf, sizeof buf), "未跌破不应触发");
    CHECK(strstr(buf, "还有") != NULL, "应给出距止损位距离");

    /* 制度修正版仓位必须小于标准版 */
    sizing_t std = sizing_standard(cn, 1000000.0, 1.0, 45.44, 40.00, "002422");
    sizing_t wc  = sizing_worst(cn, 1000000.0, 1.0, 45.44, 40.00, "002422", 3);
    CHECK(wc.qty < std.qty, "制度修正版仓位应小于标准版");
    CHECK_NEAR(wc.capital_pct / std.capital_pct, 0.445, 0.02, "折减比例应约为 44.5%");

    /* 止损价高于入场价应报错而非静默 */
    sizing_t bad = sizing_standard(cn, 1000000.0, 1.0, 40.00, 45.00, "002422");
    CHECK(bad.qty == 0, "止损高于入场 -> 0 股");
    CHECK(bad.warning[0] != '\0', "应给出警示");

    /* 回撤预算自洽性 */
    dd_budget_t badb = { 1000000.0, 10.0, 2.0, 10 };
    dd_check_t bc = dd_validate(&badb);
    CHECK(!bc.valid, "全部止损亏损 20% 超过 10% 熔断线 -> 不自洽");
    CHECK(strstr(bc.issue, "超过回撤熔断线") != NULL, "应说明原因");

    dd_budget_t goodb = { 1000000.0, 20.0, 1.0, 10 };
    CHECK(dd_validate(&goodb).valid, "自洽的预算应通过");
    CHECK_NEAR(dd_validate(&goodb).consec_to_breaker, 20.0, 0.1, "连续 20 次触及熔断");
}

/* ==========================================================================
 * G2-5 分析模块
 * ========================================================================*/

static void test_analysis(void) {
    section("分析模块");

    /* 硬约束：说不出壁垒来源 -> 等级强制「无」 */
    {
        moat_in_t m = { .moat_type = "技术专利", .moat_source = NULL,
                        .roic = 25.0, .wacc = 8.0, .has_roic = true, .has_wacc = true,
                        .durability_years = 10, .evidence_count = 3 };
        assessment_t a = assess_moat(&m);
        CHECK(a.grade == GRADE_NONE, "壁垒来源为空 -> 等级「无」");
        CHECK_NEAR(score_of(a.score), 10.0, 0.01, "壁垒来源为空 -> 10 分");
    }

    /* 回归：ROIC < WACC 时评分必须被硬上限压住。
     * 纯加权计分会让类型分+证据分+持续性淹没 ROIC 的负分，
     * 得出「壁垒宽」而发现里写着「壁垒存疑」—— 自相矛盾。 */
    {
        moat_in_t m = { .moat_type = "技术专利",
                        .moat_source = "ADC 平台与默沙东绑定",
                        .roic = 5.94, .wacc = 9.0, .has_roic = true, .has_wacc = true,
                        .durability_years = 5, .substitute_count = 2, .evidence_count = 3 };
        assessment_t a = assess_moat(&m);
        CHECK(score_of(a.score) <= 45.0, "ROIC < WACC -> 得分应被压到 45 以下");
        CHECK(a.grade == GRADE_NONE, "ROIC < WACC -> 等级不应为宽");

        int found = 0;
        for (size_t i = 0; i < a.n_findings; i++)
            if (strstr(a.findings[i], "低于 WACC") != NULL) found = 1;
        CHECK(found, "应给出「ROIC 低于 WACC」的发现");
    }

    /* 好壁垒应判为「宽」 */
    {
        moat_in_t m = { .moat_type = "网络效应", .moat_source = "双边网络效应",
                        .roic = 30.0, .wacc = 9.0, .has_roic = true, .has_wacc = true,
                        .durability_years = 10, .substitute_count = 0, .evidence_count = 3 };
        assessment_t a = assess_moat(&m);
        CHECK(a.grade == GRADE_WIDE, "强壁垒应判为「宽」");
    }

    /* 回归：买入占比 100% 应扣分 —— 全体一致意味着信号无区分度 */
    {
        research_in_t unanimous = { .target_price_mean = 52.1, .current_price = 39.26,
                                    .coverage_count = 29, .rating_buy_pct = 100.0,
                                    .has_site_visit = true };
        research_in_t split = unanimous;
        split.rating_buy_pct = 70.0;
        assessment_t au = assess_research(&unanimous);
        assessment_t as = assess_research(&split);
        CHECK(score_of(au.score) < score_of(as.score), "全体一致应比有分歧扣分更多");

        int caveat = 0;
        for (size_t i = 0; i < au.n_caveats; i++)
            if (strstr(au.caveats[i], "失去区分度") != NULL) caveat = 1;
        CHECK(caveat, "应给出「信号失去区分度」的警示");
    }

    /* 研报警示恒存在（非一手来源） */
    {
        research_in_t r = { .target_price_mean = 10.0, .current_price = 8.0 };
        assessment_t a = assess_research(&r);
        int found = 0;
        for (size_t i = 0; i < a.n_caveats; i++)
            if (strstr(a.caveats[i], "非一手来源") != NULL) found = 1;
        CHECK(found, "研报模块必须始终带「非一手来源」警示");
    }

    /* 现金流：利润含金量低应被识别 */
    {
        cashflow_in_t c = { .ocf = 2.0, .net_profit = 20.0, .capex = 1.0,
                            .has_ocf = true, .has_np = true, .has_capex = true };
        assessment_t a = assess_cashflow(&c);
        int found = 0;
        for (size_t i = 0; i < a.n_findings; i++)
            if (strstr(a.findings[i], "未有效转化为现金") != NULL) found = 1;
        CHECK(found, "现金含量 <0.8 应被识别");
    }

    /* 缺口过多时拒绝给分 */
    {
        cashflow_in_t c = { .has_ocf = false, .has_np = false,
                            .has_capex = false, .has_debt = false };
        assessment_t a = assess_cashflow(&c);
        CHECK_NEAR(score_of(a.score), 0.0, 0.01, "缺口过多应拒绝给分");
    }

    /* 行业：成长+龙头 应优于 下行+边缘 */
    {
        industry_in_t good = { .industry = "新能源", .stage = "成长", .position = "龙头",
                               .intensity = "low", .margin_trend = "up",
                               .industry_growth = 25.0 };
        industry_in_t bad  = { .industry = "传统制造", .stage = "下行", .position = "边缘",
                               .intensity = "high", .margin_trend = "down",
                               .industry_growth = -10.0 };
        assessment_t ag = assess_industry(&good);
        assessment_t ab = assess_industry(&bad);
        CHECK(score_of(ag.score) > score_of(ab.score), "成长+龙头应优于下行+边缘");
    }

    /* 综合权重：慢变量（现金流）权重应高于快变量（研报） */
    {
        bundle_t b;
        memset(&b, 0, sizeof b);
        b.research.score = score_new(50.0); b.research.grade = GRADE_C;
        b.industry.score = score_new(50.0); b.industry.grade = GRADE_C;
        b.cashflow.score = score_new(100.0); b.cashflow.grade = GRADE_A;
        b.moat.score     = score_new(50.0); b.moat.grade = GRADE_C;
        score_t comp = bundle_composite(&b);
        CHECK_NEAR(score_of(comp), 50.0*0.20 + 50.0*0.25 + 100.0*0.30 + 50.0*0.25,
                   0.01, "四维加权");
    }
}

/* ==========================================================================
 * G2-6 建议映射
 * ========================================================================*/

static void test_advice(void) {
    section("建议映射");
    advice_engine_t eng = { 4.0 };

    /* 逻辑止损优先级最高：即使量化 ENTRY + 因子 Q1 */
    {
        advice_result_t r = advice_decide(&eng, ACT_ENTRY, "趋势动量一致", "Q1",
                                          NULL, 0, false, 2);
        CHECK(r.advice == ADV_CLEAR, "2 条证伪 -> 清仓（优先级 ①）");
        CHECK(strstr(r.decided_by, "逻辑止损") != NULL, "应由逻辑止损决定");
    }
    {
        advice_result_t r = advice_decide(&eng, ACT_ENTRY, "趋势动量一致", "Q1",
                                          NULL, 0, false, 1);
        CHECK(r.advice == ADV_REDUCE, "1 条证伪 -> 降仓");
    }

    /* 量化 BLOCK 时因子再好也不入场 */
    {
        advice_result_t r = advice_decide(&eng, ACT_BLOCK, "趋势级全部空头", "Q1",
                                          NULL, 0, false, 0);
        CHECK(r.advice == ADV_NO_ENTRY, "量化 BLOCK -> 不入场（即使因子 Q1）");
        CHECK(strstr(r.decided_by, "量化") != NULL, "应由量化信号决定");
    }

    /* 因子分层决定仓位量级 */
    {
        advice_result_t q1 = advice_decide(&eng, ACT_ENTRY, "x", "Q1", NULL, 0, false, 0);
        advice_result_t q3 = advice_decide(&eng, ACT_ENTRY, "x", "Q3", NULL, 0, false, 0);
        advice_result_t q4 = advice_decide(&eng, ACT_ENTRY, "x", "Q4", NULL, 0, false, 0);
        CHECK(q1.advice == ADV_BUILD, "Q1 -> 建仓");
        CHECK_NEAR(q1.position_pct, 4.0, 0.01, "Q1 标准仓位");
        CHECK(q3.advice == ADV_PROBE, "Q3 -> 试仓");
        CHECK(q3.position_pct < q1.position_pct, "Q3 仓位应小于 Q1");
        CHECK(q4.advice == ADV_NO_ENTRY, "Q4 -> 不入场（因子不支持）");
        CHECK_NEAR(q4.position_pct, 0.0, 1e-9, "Q4 仓位应为 0");
    }

    /* 监控 CRITICAL 暂缓建仓 */
    {
        monitor_signal_t m[1] = { { MON_CRITICAL, "停牌", "即时", "无法交易" } };
        advice_result_t r = advice_decide(&eng, ACT_ENTRY, "x", "Q1", m, 1, false, 0);
        CHECK(r.advice == ADV_SUSPEND, "监控 CRITICAL -> 暂缓建仓");
        CHECK_NEAR(r.position_pct, 0.0, 1e-9, "暂缓时仓位应为 0");
    }

    /* 监控 WARNING 仓位减半 */
    {
        monitor_signal_t m[1] = { { MON_WARNING, "财报临近", "3 天", "禁止新增" } };
        advice_result_t r = advice_decide(&eng, ACT_ENTRY, "x", "Q1", m, 1, false, 0);
        CHECK(r.advice == ADV_BUILD_HALF, "监控 WARNING -> 半仓");
        CHECK_NEAR(r.position_pct, 2.0, 0.01, "半仓应为 2.0%");
    }

    /* 持仓：量化 EXIT -> 清仓 */
    {
        advice_result_t r = advice_decide(&eng, ACT_EXIT, "趋势破位", "Q1", NULL, 0, true, 0);
        CHECK(r.advice == ADV_CLEAR, "持仓 + EXIT -> 清仓");
    }

    /* 持仓：因子跌至 Q5 -> 降仓 */
    {
        advice_result_t r = advice_decide(&eng, ACT_ENTRY, "x", "Q5", NULL, 0, true, 0);
        CHECK(r.advice == ADV_REDUCE, "持仓 + 因子 Q5 -> 降仓");
    }

    /* 判定链必须可审计 */
    {
        monitor_signal_t m[1] = { { MON_WARNING, "财报临近", "3 天", "禁止新增" } };
        advice_result_t r = advice_decide(&eng, ACT_ENTRY, "x", "Q1", m, 1, false, 0);
        CHECK(r.n_chain >= 4, "判定链应有至少 4 步");
        int has_step = 0;
        for (size_t i = 0; i < r.n_chain; i++)
            if (strstr(r.chain[i], "①") || strstr(r.chain[i], "②")
             || strstr(r.chain[i], "③") || strstr(r.chain[i], "④")
             || strstr(r.chain[i], "⑤")) has_step = 1;
        CHECK(has_step, "判定链应含优先级序号");
    }

    /* 穷举 因子分层 x 监控级别，确保每种组合都给出明确建议 */
    {
        const char *layers[5] = { "Q1", "Q2", "Q3", "Q4", "Q5" };
        int missing = 0;
        for (int i = 0; i < 5; i++) {
            for (int lv = 0; lv < 3; lv++) {
                monitor_signal_t m[1] = { { (mon_level_t)lv, "t", "w", "i" } };
                advice_result_t r = advice_decide(&eng, ACT_ENTRY, "x", layers[i],
                                                  m, 1, false, 0);
                if (r.decided_by == NULL || r.position_pct < 0.0) missing++;
            }
        }
        CHECK(missing == 0, "所有 因子x监控 组合都应给出明确建议");
    }
}

/* ==========================================================================
 * G3 集成：跨模块一致性
 * ========================================================================*/

static void test_integration(void) {
    section("集成 · 跨模块一致性");

    const market_rules_t *cn = market_cn();
    const data_adapter_t *da = fixture_002422();

    /* 全链路跑通 */
    double closes[CQS_SERIES_MAX];
    size_t n = da_closes(da, "002422", closes, CQS_SERIES_MAX);
    CHECK(n == 66, "样例应提供 66 根 K 线");

    tech_snapshot_t snap = tech_compute(closes, n);
    tech_indicators_t ind = tech_indicators(&snap, closes, n);
    signal_t sigs[CQS_MAX_SIGNALS];
    size_t ns = signals_build(&ind, sigs, CQS_MAX_SIGNALS);
    decision_t dec = signals_decide(sigs, ns);
    CHECK(ns == 6, "应装配 6 条信号（2 趋势 + 2 动量 + 2 极值）");
    CHECK(dec.action < action_COUNT, "决策动作应在枚举范围内");

    /* 四模块 + 综合 + 因子 + 建议 全链路 */
    research_in_t ri = { .target_price_mean = 52.10, .current_price = 39.26,
                         .coverage_count = 29, .rating_buy_pct = 100.0,
                         .has_site_visit = true };
    industry_in_t ii = { .industry = "医药生物", .stage = "成长", .position = "细分龙头",
                         .intensity = "high", .margin_trend = "down",
                         .industry_growth = 3.5, .relative_strength = -6.5 };
    cashflow_in_t ci = { .ocf = 23.41, .net_profit = 11.28, .capex = 23.90,
                         .interest_debt = 44.18, .has_ocf = true, .has_np = true,
                         .has_capex = true, .has_debt = true };
    moat_in_t mi = { .moat_type = "技术专利", .moat_source = "ADC 平台与默沙东绑定",
                     .durability_years = 5, .roic = 5.94, .wacc = 9.0,
                     .has_roic = true, .has_wacc = true,
                     .substitute_count = 2, .evidence_count = 2 };

    bundle_t b = { assess_research(&ri), assess_industry(&ii),
                   assess_cashflow(&ci), assess_moat(&mi) };

    /* 一致性：壁垒模块的等级必须与它的发现自洽。
     * 若发现说"壁垒存疑"，等级就不能是「宽」。 */
    int says_doubtful = 0;
    for (size_t i = 0; i < b.moat.n_findings; i++)
        if (strstr(b.moat.findings[i], "壁垒存疑") != NULL) says_doubtful = 1;
    if (says_doubtful)
        CHECK(b.moat.grade != GRADE_WIDE,
              "一致性：发现说「壁垒存疑」时等级不得为「宽」");

    score_t comp = bundle_composite(&b);
    CHECK(score_of(comp) > 0.0 && score_of(comp) <= 100.0, "综合分应在 (0,100]");

    factor_signal_t fs = factor_from_bundle(&b);
    CHECK(fs.layer[0] == 'Q', "因子分层应以 Q 开头");

    /* 制度约束与风控联动：制度修正版仓位必须不超过标准版 */
    sizing_t std = sizing_standard(cn, 1000000.0, 1.0, 45.44, 40.00, "002422");
    sizing_t wc  = sizing_worst(cn, 1000000.0, 1.0, 45.44, 40.00, "002422", 3);
    advice_engine_t eng = { wc.capital_pct };

    monitor_signal_t mons[1] = { { MON_INFO, "阈值待校准", "长期", "暂不出结论" } };
    advice_result_t adv = advice_decide(&eng, dec.action, dec.reason, fs.layer,
                                        mons, 1, false, 0);
    CHECK(adv.position_pct <= wc.capital_pct + 1e-9,
          "建议仓位不得超过制度修正版给出的基准仓位");

    /* 建议仓位不得超过单标的上限（若量化信号为 ENTRY） */
    if (dec.action == ACT_ENTRY && strcmp(fs.layer, "Q1") == 0)
        CHECK_NEAR(adv.position_pct, wc.capital_pct, 0.01,
                   "ENTRY+Q1+INFO 时建议仓位应等于基准仓位");

    /* 核心断言：制度修正后的仓位应显著低于标准法 */
    CHECK(wc.capital_pct < std.capital_pct * 0.6,
          "制度修正后仓位应显著低于标准法（本标的约折减 55%）");
}

/* ==========================================================================*/

int main(void) {
    printf("\n");
    printf("==============================================================\n");
    printf("  cqs 测试套件（G2 单元 + G3 集成）\n");
    printf("  规则核验日 %s\n", CQS_RULES_VERIFIED_AT);
    printf("==============================================================\n");

    test_rules();
    test_data();
    test_signals();
    test_risk();
    test_analysis();
    test_advice();
    test_integration();

    printf("\n==============================================================\n");
    printf("  通过 %d   失败 %d\n", g_pass, g_fail);
    printf("==============================================================\n\n");
    return g_fail == 0 ? 0 : 1;
}
