/* ============================================================================
 * cqs/main.c — 命令行入口
 * ============================================================================
 * 演示完整链路：数据 -> 三层指标 -> 三级信号 -> 风控 -> 四模块 -> 建议
 * ==========================================================================*/

#include "cqs/analysis.h"
#include "cqs/core.h"
#include "cqs/data.h"
#include "cqs/risk.h"
#include "cqs/rules.h"
#include "cqs/signal.h"

#include <stdio.h>
#include <string.h>
#include <time.h>

#define RULE "----------------------------------------------------------------------"

static void banner(const char *title) {
    printf("\n" RULE "\n  %s\n" RULE "\n", title);
}

static void print_assessment(const assessment_t *a) {
    printf("\n【%s】%.0f/100  等级 %s\n", a->module,
           score_of(a->score), grade_name(a->grade));
    if (a->n_findings) {
        printf("  关键发现:\n");
        for (size_t i = 0; i < a->n_findings && i < CQS_MAX_FINDINGS; i++)
            printf("    · %s\n", a->findings[i]);
    }
    if (a->n_gaps) {
        printf("  数据缺口:\n");
        for (size_t i = 0; i < a->n_gaps && i < CQS_MAX_GAPS; i++)
            printf("    ! %s\n", a->gaps[i]);
    }
    if (a->n_caveats) {
        printf("  警示:\n");
        for (size_t i = 0; i < a->n_caveats && i < CQS_MAX_CAVEATS; i++)
            printf("    ▲ %s\n", a->caveats[i]);
    }
}

int main(int argc, char **argv) {
    const char *symbol = (argc > 1) ? argv[1] : "002422";
    time_t now = fixture_last_day();

    printf("\n");
    printf("==============================================================\n");
    printf("  cqs — C 语言量化投研系统\n");
    printf("  标的 %s   规则核验日 %s\n", symbol, CQS_RULES_VERIFIED_AT);
    printf("==============================================================\n");

    /* ---------------- 1. 数据层：时效纪律 ---------------- */
    banner("1. 数据层 · 时效纪律");
    const data_adapter_t *da = fixture_002422();
    field_value_t q = da_quote(da, symbol, now);
    if (q.has_value) {
        printf("  最新收盘 %.2f 元（%s，%s）\n", q.value,
               trading_state_name(q.state), q.source);
        const char *why = fv_reject(q, freshness_days(FC_PRICE), now);
        printf("  时效判定: %s\n", why ? why : "可用");
    } else {
        printf("  未取到报价 —— 记入缺口，不参与决策\n");
    }

    double closes[CQS_SERIES_MAX];
    size_t n = da_closes(da, symbol, closes, CQS_SERIES_MAX);
    printf("  可用 K 线: %zu 根（趋势级需 250 根）\n", n);

    /* 跨时点拼接校验：这是修正 "38.52 -> 45.44 涨 18%" 那类错误的手段 */
    field_value_t a = fv_make("close", 38.52, now - (time_t)99 * 86400, TS_CLOSED, "fixture");
    field_value_t b = fv_make("close", 45.44, now - (time_t)62 * 86400, TS_CLOSED, "fixture");
    const char *why = NULL;
    if (!series_compatible(a, b, 1.0, &why))
        printf("  跨时点拼接校验: 拒绝 —— %s\n", why);

    /* ---------------- 2. 三层指标 + 三级信号 ---------------- */
    banner("2. 三层指标 · 三级信号");
    tech_snapshot_t snap = tech_compute(closes, n);
    tech_indicators_t ind = tech_indicators(&snap, closes, n);

    printf("  ATR20（口径：收盘价绝对变动均值，非真 ATR）: %s%.4f\n",
           snap.atr_ok ? "" : "数据不足 ", snap.atr20);
    printf("\n  指标明细:\n");
    const indicator_t *list[] = {
        &ind.ma_align, &ind.price_vs_ma250, &ind.macd,
        &ind.rsi, &ind.kdj, &ind.boll, &ind.vol_pct
    };
    for (size_t i = 0; i < sizeof list / sizeof list[0]; i++)
        printf("    %-12s %-11s %s\n", list[i]->name,
               tri_state_name(list[i]->state), list[i]->reason);

    signal_t sigs[CQS_MAX_SIGNALS];
    size_t ns = signals_build(&ind, sigs, CQS_MAX_SIGNALS);
    decision_t dec = signals_decide(sigs, ns);

    printf("\n  %s\n", signals_tradeoff_note());
    printf("\n  决策: %s", action_name(dec.action));
    if (dec.has_decided_by) printf("  （决定层 %s）", signal_tier_name(dec.decided_by));
    printf("\n  理由: %s\n", dec.reason);

    /* ---------------- 3. 风控 ---------------- */
    banner("3. 风控 · 制度约束");
    const market_rules_t *mr = market_cn();
    printf("  市场: %s\n", mr_label(mr));
    printf("  制度: %s\n", mr_profile(mr)->price_limit_note);

    double prev = (n >= 2) ? closes[n - 2] : 39.26;
    double lo = 0.0, hi = 0.0;
    mr_price_limits(mr, prev, symbol, false, &lo, &hi);
    printf("  涨跌停（前收 %.2f）: %.2f ~ %.2f\n", prev, lo, hi);

    /* 止损方案 */
    double entry = 39.26, prior_low = 38.42;
    stop_plan_t sp = stop_build(entry, snap.atr20, snap.atr_ok,
                                prior_low, true, NULL);
    printf("\n  止损方案（入场 %.2f）:\n", entry);
    for (size_t i = 0; i < sp.n_levels; i++)
        printf("    %-10s %8.2f  %7.2f%%  %s\n",
               stop_kind_name(sp.levels[i].kind), sp.levels[i].price,
               sp.levels[i].width_pct, sp.levels[i].reason);

    const stop_level_t *eff = stop_effective(&sp);
    if (eff)
        printf("    生效止损位（取更近者）: %s @ %.2f\n",
               stop_kind_name(eff->kind), eff->price);

    /* 仓位对比：标准法 vs 制度修正版 */
    double capital = 1000000.0, risk_pct = 1.0;
    double e2 = 45.44, s2 = 40.00;
    sizing_t std = sizing_standard(mr, capital, risk_pct, e2, s2, symbol);
    sizing_t wc  = sizing_worst(mr, capital, risk_pct, e2, s2, symbol, 3);

    printf("\n  仓位对比（总资金 %.0f，单笔风险 %.1f%%，入场 %.2f，止损 %.2f）:\n",
           capital, risk_pct, e2, s2);
    printf("    标准风险预算法  %5d 股  %10.0f 元  %.2f%%\n",
           std.qty, std.capital, std.capital_pct);
    printf("    制度修正版      %5d 股  %10.0f 元  %.2f%%\n",
           wc.qty, wc.capital, wc.capital_pct);
    printf("    ⚠ %s\n", wc.warning);

    double amp = mr_loss_amplification(mr, e2, 10000, symbol, s2, 3);
    printf("    连续 3 跌停放大倍数: %.2f x  ->  风险预算公式%s\n",
           amp, amp > 1.5 ? "失效" : "仍成立");

    /* 回撤预算自洽性 */
    dd_budget_t db = { capital, 20.0, 1.0, 10 };
    dd_check_t dc = dd_validate(&db);
    printf("\n  回撤预算（熔断 %.0f%% / 单笔 %.1f%% / 最多 %d 只）: %s\n",
           db.max_dd_pct, db.per_trade_pct, db.max_positions,
           dc.valid ? "通过" : "不通过");
    if (!dc.valid) printf("    ⚠ %s\n", dc.issue);

    /* 费用完整性 */
    fee_schedule_t fee = fee_default();
    printf("  交易费用完整性: %s（往返成本 %.2f%%）\n",
           fee_complete(&fee) ? "完整" : "不完整",
           fee_round_trip_pct(&fee));
    if (!fee_complete(&fee))
        printf("    -> 成本未知时禁止用于回测与参数寻优\n");

    /* ---------------- 4. 四模块评估 ---------------- */
    banner("4. 四模块评估");

    research_in_t ri = {
        .target_price_mean = 52.10, .current_price = 39.26,
        .coverage_count = 29, .rating_buy_pct = 100.0, .has_site_visit = true,
    };
    industry_in_t ii = {
        .industry = "医药生物-化学制药", .stage = "成长", .position = "细分龙头",
        .intensity = "high", .margin_trend = "down",
        .industry_growth = 3.5, .relative_strength = -6.5,
    };
    cashflow_in_t ci = {
        .ocf = 23.41, .net_profit = 11.28, .capex = 23.90, .interest_debt = 44.18,
        .has_ocf = true, .has_np = true, .has_capex = true, .has_debt = true,
    };
    moat_in_t mi = {
        .moat_type = "技术专利",
        .moat_source = "ADC 平台 OptiDC 与默沙东的 17 项全球 III 期临床绑定",
        .durability_years = 5, .roic = 5.94, .wacc = 9.0,
        .has_roic = true, .has_wacc = true,
        .substitute_count = 2, .evidence_count = 2,
    };

    bundle_t bundle = {
        .research = assess_research(&ri),
        .industry = assess_industry(&ii),
        .cashflow = assess_cashflow(&ci),
        .moat     = assess_moat(&mi),
    };
    print_assessment(&bundle.research);
    print_assessment(&bundle.industry);
    print_assessment(&bundle.cashflow);
    print_assessment(&bundle.moat);

    printf("\n  四维综合: %.1f/100\n", score_of(bundle_composite(&bundle)));

    /* 硬约束演示：说不出壁垒来源 */
    printf("\n  [硬约束演示] 壁垒来源留空:\n");
    moat_in_t bad = mi;
    bad.moat_source = NULL;
    assessment_t ba = assess_moat(&bad);
    printf("    -> %.0f/100 等级 %s（无论其他字段多好看）\n",
           score_of(ba.score), grade_name(ba.grade));

    /* ---------------- 5. 三类信号 + 建议 ---------------- */
    banner("5. 三类信号 -> 操作建议");

    factor_signal_t fs = factor_from_bundle(&bundle);
    printf("  【多因子信号】综合 %.1f  %s（%.0f%% 分位）\n",
           score_of(fs.composite), fs.layer, fs.percentile);
    printf("  【量化信号】%s —— %s\n", action_name(dec.action), dec.reason);

    monitor_signal_t mons[3];
    size_t nm = 0;
    mons[nm++] = (monitor_signal_t){ MON_CRITICAL, "买入假设被证伪", "即时",
        "触发逻辑止损，优先级高于价格止损" };
    mons[nm++] = (monitor_signal_t){ MON_WARNING, "存在数据缺口", "本轮",
        "缺口字段不得进入决策" };
    mons[nm++] = (monitor_signal_t){ MON_INFO, "拥挤度阈值待校准", "长期",
        "该维度暂不出结论" };

    printf("  【监控信号】\n");
    for (size_t i = 0; i < nm; i++)
        printf("    [%-8s] %-18s %-6s %s\n",
               mon_level_name(mons[i].level), mons[i].trigger,
               mons[i].window, mons[i].implication);

    advice_engine_t eng = { wc.capital_pct };
    advice_result_t adv = advice_decide(&eng, dec.action, dec.reason,
                                        fs.layer, mons, nm, true, 2);

    printf("\n  操作建议: %s   建议仓位 %.2f%%\n",
           advice_name(adv.advice), adv.position_pct);
    printf("  决定者: %s\n", adv.decided_by);
    if (adv.n_blocked) {
        printf("  否决/约束:\n");
        for (size_t i = 0; i < adv.n_blocked; i++)
            printf("    ✗ %s\n", adv.blocked[i]);
    }
    printf("  判定链（可审计）:\n");
    for (size_t i = 0; i < adv.n_chain; i++)
        printf("    %s\n", adv.chain[i]);

    printf("\n" RULE "\n");
    printf("  演示结束。完整测试： make test\n");
    printf(RULE "\n\n");
    return 0;
}
