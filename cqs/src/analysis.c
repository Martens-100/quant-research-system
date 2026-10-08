/* ============================================================================
 * cqs/analysis.c — 四模块评估 + 三类信号 + 建议映射
 * ==========================================================================*/

#include "cqs/analysis.h"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>

/* ---------------------------------------------------------------------------
 * 统一评估结构的写入辅助
 * ------------------------------------------------------------------------- */

void assess_init(assessment_t *a, const char *module) {
    memset(a, 0, sizeof *a);
    a->module = module;
    a->score  = score_new(0.0);
    a->grade  = GRADE_D;
}

void assess_finding(assessment_t *a, const char *fmt, ...) {
    va_list ap; va_start(ap, fmt);
    if (a->n_findings < CQS_MAX_FINDINGS)
        vsnprintf(a->findings[a->n_findings], 176, fmt, ap);
    a->n_findings++;
    va_end(ap);
}

void assess_gap(assessment_t *a, const char *fmt, ...) {
    va_list ap; va_start(ap, fmt);
    if (a->n_gaps < CQS_MAX_GAPS)
        vsnprintf(a->gaps[a->n_gaps], 144, fmt, ap);
    a->n_gaps++;
    va_end(ap);
}

void assess_caveat(assessment_t *a, const char *fmt, ...) {
    va_list ap; va_start(ap, fmt);
    if (a->n_caveats < CQS_MAX_CAVEATS)
        vsnprintf(a->caveats[a->n_caveats], 176, fmt, ap);
    a->n_caveats++;
    va_end(ap);
}

/**
 * 收尾：缺口过多时拒绝给分 —— 宁可说"数据不足"，也不给半截数据算出的分数。
 *
 * 判据按**检查项数**而非写死阈值：缺口数 × 2 >= 检查项数即拒绝。
 * 写死阈值（如"缺口>=4"）是坏味道 —— 现金流模块只有 3 项检查，
 * 全缺也只有 3 个缺口，写死 4 会让它带着 50 分"通过"。
 */
static void assess_finalize(assessment_t *a, score_t s, grade_t g, size_t n_checks) {
    if (n_checks > 0 && a->n_gaps * 2 >= n_checks) {
        a->score = score_new(0.0);
        a->grade = GRADE_D;
        return;
    }
    a->score = s;
    a->grade = g;
}

/* ---------------------------------------------------------------------------
 * 模块 A · 研报分析
 * ------------------------------------------------------------------------- */

assessment_t assess_research(const research_in_t *in) {
    assessment_t a;
    assess_init(&a, "研报分析");
    score_t s = score_new(50.0);

    /* 研报属非一手来源，警示恒存在 */
    assess_caveat(&a, "研报为非一手来源，引用需核实原文，不得与公司公告同等处理");

    if (in->target_price_mean > 0.0 && in->current_price > 0.0) {
        double up = (in->target_price_mean / in->current_price - 1.0) * 100.0;
        assess_finding(&a, "机构一致目标价 %.2f 元，较现价隐含 %+.2f%% 空间",
                       in->target_price_mean, up);
        s = score_shift(s, 10.0);
    } else {
        assess_gap(&a, "缺一致目标价或现价");
    }

    if (in->coverage_count > 0) {
        double bonus = (double)in->coverage_count * 0.7;
        s = score_shift(s, bonus > 20.0 ? 20.0 : bonus);
        if (in->coverage_count < 5)
            assess_caveat(&a, "仅 %d 家机构覆盖，样本过小，一致预期代表性弱",
                          in->coverage_count);
    } else {
        assess_gap(&a, "缺覆盖机构数");
    }

    if (in->rating_buy_pct >= 0.0) {
        if (in->rating_buy_pct >= 95.0) {
            /* 全体一致 -> 该信号失去区分度，扣分 */
            s = score_shift(s, -10.0);
            assess_caveat(&a,
                "买入/增持占比 %.0f%%，接近全体一致。该信号已失去区分度 —— "
                "当所有人都看多时，'看多'不携带信息", in->rating_buy_pct);
        } else {
            s = score_shift(s, 5.0);
        }
        assess_finding(&a, "近 6 个月买入/增持占比 %.0f%%", in->rating_buy_pct);
    } else {
        assess_gap(&a, "缺评级分布");
    }

    if (in->has_site_visit) s = score_shift(s, 15.0);
    else assess_caveat(&a, "未见机构调研记录，研报可能基于公开信息推演而非一手验证");

    assess_finalize(&a, s, grade_abcd(s), 4);   /* 目标价/覆盖/评级/调研 */
    return a;
}

/* ---------------------------------------------------------------------------
 * 模块 B · 所属行业
 * ------------------------------------------------------------------------- */

static double lookup(const char *key, const char *const *keys, const double *vals, size_t n) {
    if (key == NULL) return 50.0;
    for (size_t i = 0; i < n; i++)
        if (strcmp(key, keys[i]) == 0) return vals[i];
    return 50.0;
}

assessment_t assess_industry(const industry_in_t *in) {
    assessment_t a;
    assess_init(&a, "行业分析");

    static const char *const k_stage[]  = { "导入", "成长", "成熟", "下行" };
    static const double      k_stagev[] = { 60.0, 90.0, 65.0, 25.0 };
    static const char *const k_pos[]    = { "龙头", "细分龙头", "追赶者", "边缘" };
    static const double      k_posv[]   = { 95.0, 80.0, 55.0, 25.0 };
    static const char *const k_int[]    = { "low", "medium", "high" };
    static const double      k_intv[]   = { 85.0, 65.0, 40.0 };
    static const char *const k_mg[]     = { "up", "flat", "down" };
    static const double      k_mgv[]    = { 85.0, 60.0, 30.0 };

    if (in->industry == NULL) assess_gap(&a, "缺行业名称");
    else                       assess_finding(&a, "所属行业：%s", in->industry);

    if (in->stage == NULL)          assess_gap(&a, "缺生命周期阶段");
    if (in->position == NULL)       assess_gap(&a, "缺公司位置");
    if (in->intensity == NULL)      assess_gap(&a, "缺竞争强度");
    if (in->margin_trend == NULL)   assess_gap(&a, "缺行业毛利率趋势");

    double parts[4] = {
        lookup(in->stage,       k_stage, k_stagev, 4),
        lookup(in->position,    k_pos,   k_posv,   4),
        lookup(in->intensity,   k_int,   k_intv,   3),
        lookup(in->margin_trend,k_mg,    k_mgv,    3),
    };
    double base = (parts[0] + parts[1] + parts[2] + parts[3]) / 4.0;

    if (in->industry_growth != 0.0) {
        double d = in->industry_growth * 0.5;
        if (d > 15.0) d = 15.0;
        if (d < -15.0) d = -15.0;
        base += d;
    }

    if (in->stage && strcmp(in->stage, "下行") == 0)
        assess_finding(&a, "行业处下行阶段 —— 逆势标的胜率显著低于顺周期");
    if (in->intensity && strcmp(in->intensity, "high") == 0)
        assess_finding(&a, "竞争强度高 —— 需壁垒模块证明公司能维持超额收益");
    if (in->position && strcmp(in->position, "边缘") == 0)
        assess_finding(&a, "公司处行业边缘位置 —— 难以享受行业红利");
    if (in->relative_strength != 0.0)
        assess_finding(&a, "近 60 日相对行业 %+.1f%%", in->relative_strength);

    if (in->industry == NULL)
        assess_caveat(&a, "行业未识别，本模块结论不成立（同一指标跨行业不可比）");

    score_t s = score_new(base);
    assess_finalize(&a, s, grade_abcd(s), 5);   /* 行业/阶段/位置/强度/毛利趋势 */
    return a;
}

/* ---------------------------------------------------------------------------
 * 模块 C · 现金流
 * ------------------------------------------------------------------------- */

assessment_t assess_cashflow(const cashflow_in_t *in) {
    assessment_t a;
    assess_init(&a, "现金流分析");
    score_t s = score_new(50.0);

    if (in->has_ocf && in->has_np && in->net_profit != 0.0) {
        double ratio = in->ocf / in->net_profit;
        double d = (ratio - 0.8) * 50.0;
        if (d > 25.0) d = 25.0;
        if (d < -25.0) d = -25.0;
        s = score_shift(s, d);

        if (ratio < 0.8)
            assess_finding(&a,
                "经营现金流/归母净利 = %.2f（<0.8）—— 利润未有效转化为现金，需查应收与存货",
                ratio);
        else
            assess_finding(&a, "经营现金流/归母净利 = %.2f，利润含金量正常", ratio);
    } else {
        assess_gap(&a, "缺经营现金流或归母净利");
    }

    if (in->has_ocf && in->has_capex) {
        double fcf = in->ocf - in->capex;
        if (fcf > 0.0) {
            s = score_shift(s, 15.0);
            assess_finding(&a, "自由现金流为正（%.2f）", fcf);
        } else {
            s = score_shift(s, -10.0);
            assess_finding(&a,
                "自由现金流为负（%.2f）—— 扩张期特征，但需确认资本开支"
                "能形成未来收益而非沉没成本", fcf);
        }
    } else {
        assess_gap(&a, "缺资本开支");
    }

    if (in->has_debt && in->has_ocf && in->ocf != 0.0) {
        double yrs = in->interest_debt / in->ocf;
        if (yrs < 2.0)       s = score_shift(s, 10.0);
        else if (yrs > 4.0)  s = score_shift(s, -10.0);

        if (yrs > 3.0)
            assess_finding(&a, "有息负债需 %.1f 年经营现金流偿还，偿债压力偏高", yrs);
        else
            assess_finding(&a, "有息负债需 %.1f 年经营现金流偿还", yrs);
    } else {
        assess_gap(&a, "缺有息负债");
    }

    assess_finalize(&a, s, grade_abcd(s), 3);   /* 现金含量/FCF/偿债 */
    return a;
}

/* ---------------------------------------------------------------------------
 * 模块 D · 核心壁垒
 * ---------------------------------------------------------------------------
 * 两条硬约束（都会强制降级）：
 *   ① moat_source 为空 -> 等级强制「无」，得分 10。说不出就是没有。
 *   ② ROIC < WACC      -> 得分硬上限 45。壁垒若真实，必体现在资本回报上。
 * 第 ② 条来自一次实测：纯加权计分时类型分 85 + 证据分 15 + 持续性 10
 * 足以淹没 ROIC 的负分，得出「壁垒宽」而发现里写着「壁垒存疑」—— 自相矛盾。
 * ------------------------------------------------------------------------- */

assessment_t assess_moat(const moat_in_t *in) {
    assessment_t a;
    assess_init(&a, "核心壁垒");

    bool has_source = (in->moat_source != NULL && in->moat_source[0] != '\0');
    if (!has_source) {
        assess_caveat(&a,
            "未能给出可命名的壁垒来源 —— 按本模块硬约束，"
            "无论其他字段如何，等级判定为「无」");
        a.score = score_new(10.0);
        a.grade = GRADE_NONE;
        return a;
    }

    static const char *const k_type[]  = {
        "技术专利", "规模成本", "网络效应", "牌照", "品牌", "客户粘性", "无"
    };
    static const double      k_typev[] = { 85.0, 80.0, 90.0, 75.0, 70.0, 75.0, 10.0 };

    double base = lookup(in->moat_type, k_type, k_typev, 7);
    if (base == 50.0) base = 40.0;   /* 未知类型 */

    assess_finding(&a, "壁垒类型：%s；来源：%s",
                   in->moat_type ? in->moat_type : "未分类", in->moat_source);

    if (in->evidence_count > 0) {
        double b = (double)in->evidence_count * 5.0;
        base += b > 15.0 ? 15.0 : b;
        assess_finding(&a, "提供 %d 条可验证证据", in->evidence_count);
    } else {
        assess_caveat(&a, "壁垒来源已命名但无可验证证据 —— 属叙述性主张，非事实");
    }

    if (in->has_roic && in->has_wacc) {
        double spread = in->roic - in->wacc;
        double d = spread * 2.0;
        if (d > 20.0) d = 20.0;
        if (d < -20.0) d = -20.0;
        base += d;

        if (spread > 5.0)
            assess_finding(&a, "ROIC 超出 WACC %.1fpct —— 壁垒已转化为经济优势", spread);
        else if (spread > 0.0)
            assess_finding(&a, "ROIC 略高于 WACC %.1fpct —— 优势有限", spread);
        else
            assess_finding(&a,
                "ROIC 低于 WACC %.1fpct —— 所谓技术优势未转化为资本回报，壁垒存疑",
                spread);
    } else {
        assess_gap(&a, "缺 ROIC 或 WACC，无法做最硬的壁垒判据");
    }

    if (in->durability_years >= 5)      base += 10.0;
    else if (in->durability_years < 3)  base -= 10.0;

    if (in->substitute_count >= 3) base -= 15.0;

    score_t s = score_new(base);

    /* 硬约束 ②：ROIC < WACC 时不得给高等级 */
    if (in->has_roic && in->has_wacc && in->roic < in->wacc)
        s = score_min(s, score_new(45.0));

    a.score = s;
    a.grade = grade_moat(s);
    return a;
}

/* ---------------------------------------------------------------------------
 * 四维汇总
 * ------------------------------------------------------------------------- */

score_t bundle_composite(const bundle_t *b) {
    return score_new(
        score_of(b->research.score) * 0.20 +
        score_of(b->industry.score) * 0.25 +
        score_of(b->cashflow.score) * 0.30 +
        score_of(b->moat.score)     * 0.25);
}

factor_signal_t factor_from_bundle(const bundle_t *b) {
    factor_signal_t f;
    f.composite  = bundle_composite(b);
    f.percentile = score_of(f.composite);   /* 无同业样本时以绝对分近似 */

    if (f.percentile >= 80.0)      f.layer = "Q1";
    else if (f.percentile >= 60.0) f.layer = "Q2";
    else if (f.percentile >= 40.0) f.layer = "Q3";
    else if (f.percentile >= 20.0) f.layer = "Q4";
    else                           f.layer = "Q5";
    return f;
}

mon_level_t monitors_max_level(const monitor_signal_t *m, size_t n) {
    mon_level_t best = MON_INFO;
    for (size_t i = 0; i < n; i++)
        if ((int)m[i].level > (int)best) best = m[i].level;
    return best;
}

/* ---------------------------------------------------------------------------
 * 建议映射引擎
 * ------------------------------------------------------------------------- */

static void chain_add(advice_result_t *r, const char *fmt, ...) {
    if (r->n_chain >= sizeof r->chain / sizeof r->chain[0]) return;
    va_list ap; va_start(ap, fmt);
    vsnprintf(r->chain[r->n_chain], 176, fmt, ap);
    va_end(ap);
    r->n_chain++;
}

static void blocked_add(advice_result_t *r, const char *fmt, ...) {
    if (r->n_blocked >= sizeof r->blocked / sizeof r->blocked[0]) return;
    va_list ap; va_start(ap, fmt);
    vsnprintf(r->blocked[r->n_blocked], 176, fmt, ap);
    va_end(ap);
    r->n_blocked++;
}

static advice_result_t result(advice_t adv, double pct, const char *by) {
    advice_result_t r;
    memset(&r, 0, sizeof r);
    r.advice = adv;
    r.position_pct = pct;
    r.decided_by = by;
    return r;
}

advice_result_t advice_decide(const advice_engine_t *eng,
                              action_t quant_action, const char *quant_reason,
                              const char *factor_layer,
                              const monitor_signal_t *mons, size_t n_mons,
                              bool holding, int logic_falsified) {
    double base = eng->worst_case_position_pct;
    mon_level_t mlevel = monitors_max_level(mons, n_mons);
    advice_result_t r = result(ADV_OBSERVE, 0.0, "未判定");

    chain_add(&r, "输入: quant=%s factor=%s monitor=%s holding=%d",
              action_name(quant_action), factor_layer, mon_level_name(mlevel), (int)holding);

    /* ---- 优先级 ①：逻辑止损 ---- */
    if (logic_falsified > 0) {
        chain_add(&r, "① 逻辑止损触发 —— 优先级最高，跳过其余判定");
        blocked_add(&r, "%d 条买入假设被证伪", logic_falsified);
        r.advice = (logic_falsified >= 2) ? ADV_CLEAR : ADV_REDUCE;
        r.position_pct = (logic_falsified >= 2) ? 0.0 : base / 2.0;
        r.decided_by = "逻辑止损（优先级 ①）";
        return r;
    }
    chain_add(&r, "① 逻辑止损：未触发");

    /* ---- 持仓分支 ---- */
    if (holding) {
        if (quant_action == ACT_EXIT) {
            chain_add(&r, "② 量化 EXIT —— 清仓");
            r.advice = ADV_CLEAR; r.position_pct = 0.0;
            r.decided_by = "量化信号（优先级 ②）";
            return r;
        }
        if (quant_action == ACT_BLOCK) {
            chain_add(&r, "② 量化 BLOCK —— 持仓状态下减至观察仓");
            r.advice = ADV_REDUCE; r.position_pct = base / 3.0;
            r.decided_by = "量化信号（优先级 ②）";
            return r;
        }
        if (mlevel == MON_CRITICAL) {
            chain_add(&r, "③ 监控 CRITICAL —— 转入风控观察，禁止加仓");
            blocked_add(&r, "监控 CRITICAL：禁止一切加仓动作");
            r.advice = ADV_HOLD; r.position_pct = base;
            r.decided_by = "监控信号（优先级 ③）";
            return r;
        }
        if (strcmp(factor_layer, "Q4") == 0 || strcmp(factor_layer, "Q5") == 0) {
            chain_add(&r, "④ 因子分层 %s —— 相对位置落后，降仓 1/3", factor_layer);
            r.advice = ADV_REDUCE; r.position_pct = base * 2.0 / 3.0;
            r.decided_by = "多因子信号（优先级 ④）";
            return r;
        }
        chain_add(&r, "④ 因子分层可接受 —— 持有");
        r.advice = ADV_HOLD; r.position_pct = base;
        r.decided_by = "维持";
        return r;
    }

    /* ---- 空仓分支 ---- */
    if (quant_action == ACT_BLOCK) {
        chain_add(&r, "② 量化 BLOCK —— 不入场");
        blocked_add(&r, "%s", quant_reason);
        r.advice = ADV_NO_ENTRY; r.position_pct = 0.0;
        r.decided_by = "量化信号（优先级 ②）";
        return r;
    }
    if (quant_action == ACT_WAIT) {
        chain_add(&r, "② 量化 WAIT —— 观察");
        blocked_add(&r, "%s", quant_reason);
        r.advice = ADV_OBSERVE; r.position_pct = 0.0;
        r.decided_by = "量化信号（优先级 ②）";
        return r;
    }
    chain_add(&r, "② 量化 ENTRY —— 通过，进入仓位判定");

    /* ---- 优先级 ③：监控 CRITICAL 暂缓 ---- */
    if (mlevel == MON_CRITICAL) {
        chain_add(&r, "③ 监控 CRITICAL —— 暂缓建仓");
        for (size_t i = 0; i < n_mons; i++)
            if (mons[i].level == MON_CRITICAL) blocked_add(&r, "%s", mons[i].trigger);
        r.advice = ADV_SUSPEND; r.position_pct = 0.0;
        r.decided_by = "监控信号（优先级 ③）";
        return r;
    }
    chain_add(&r, "③ 监控：无 CRITICAL");

    /* ---- 优先级 ④：因子分层决定仓位 ---- */
    double pct;
    bool top = (strcmp(factor_layer, "Q1") == 0 || strcmp(factor_layer, "Q2") == 0);
    if (top) {
        pct = base;
        chain_add(&r, "④ 因子 %s（前 40%%）—— 标准仓位", factor_layer);
    } else if (strcmp(factor_layer, "Q3") == 0) {
        pct = base / 4.0;
        chain_add(&r, "④ 因子 Q3（中位）—— 试仓 1/4");
    } else {
        chain_add(&r, "④ 因子 %s（后 40%%）—— 因子不支持，不入场", factor_layer);
        blocked_add(&r, "因子分层 %s 位于后 40%%，即使量化信号 ENTRY 也不入场",
                    factor_layer);
        r.advice = ADV_NO_ENTRY; r.position_pct = 0.0;
        r.decided_by = "多因子信号（优先级 ④）";
        return r;
    }

    /* ---- 优先级 ⑤：监控 WARNING 减半 ---- */
    if (mlevel == MON_WARNING) {
        chain_add(&r, "⑤ 监控 WARNING —— 仓位减半");
        blocked_add(&r, "监控 WARNING：存在需观察的事件");
        r.advice = top ? ADV_BUILD_HALF : ADV_PROBE;
        r.position_pct = pct / 2.0;
        r.decided_by = "监控信号（优先级 ⑤ 降级）";
        return r;
    }

    chain_add(&r, "⑤ 监控 INFO —— 不降级");
    r.advice = top ? ADV_BUILD : ADV_PROBE;
    r.position_pct = pct;
    r.decided_by = "量化 + 因子（优先级 ②④）";
    return r;
}
