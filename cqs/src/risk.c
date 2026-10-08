/* ============================================================================
 * cqs/risk.c — 风控层实现
 * ==========================================================================*/

#include "cqs/risk.h"

#include <math.h>
#include <stdio.h>
#include <string.h>

/* ---------------------------------------------------------------------------
 * 止损
 * ------------------------------------------------------------------------- */

static void add_level(stop_plan_t *p, stop_kind_t kind, double price,
                      double entry, const char *fmt, double arg) {
    if (p->n_levels >= sizeof p->levels / sizeof p->levels[0]) return;
    stop_level_t *lv = &p->levels[p->n_levels++];
    lv->kind      = kind;
    lv->price     = price;
    lv->width_pct = (entry > 0.0) ? (price / entry - 1.0) * 100.0 : 0.0;
    lv->active    = true;
    snprintf(lv->reason, sizeof lv->reason, fmt, arg);
}

stop_plan_t stop_build(double entry, double atr20, bool has_atr,
                       double prior_low, bool has_prior_low,
                       const char *logic_hit) {
    stop_plan_t p;
    memset(&p, 0, sizeof p);
    p.entry = entry;

    if (has_prior_low && prior_low > 0.0 && prior_low < entry)
        add_level(&p, STOP_STRUCTURE, prior_low, entry,
                  "结构位止损：跌破前低 %.2f", prior_low);

    if (has_atr && atr20 > 0.0) {
        double atr_stop = entry - 2.0 * atr20;
        add_level(&p, STOP_ATR, atr_stop, entry,
                  "波动率止损：2 × ATR20（%.4f/日）", atr20);
    }

    /* 逻辑止损不参与价格比较，单独判定且优先级更高 */
    p.logic_triggered = (logic_hit != NULL && logic_hit[0] != '\0');
    stop_level_t *lv = &p.levels[p.n_levels++];
    lv->kind      = STOP_LOGIC;
    lv->price     = 0.0;
    lv->width_pct = 0.0;
    lv->active    = p.logic_triggered;
    snprintf(lv->reason, sizeof lv->reason, "逻辑止损：%s",
             p.logic_triggered ? logic_hit : "所有逻辑条件均未触发");

    return p;
}

const stop_level_t *stop_effective(const stop_plan_t *p) {
    const stop_level_t *best = NULL;
    for (size_t i = 0; i < p->n_levels; i++) {
        const stop_level_t *lv = &p->levels[i];
        if (!lv->active || lv->kind == STOP_LOGIC || lv->price <= 0.0) continue;
        if (best == NULL || lv->price > best->price) best = lv;   /* 取更近者 */
    }
    return best;
}

bool stop_evaluate(const stop_plan_t *p, double price, char *out, size_t cap) {
    if (p->logic_triggered) {
        snprintf(out, cap, "逻辑止损优先于价格止损：%s", p->levels[p->n_levels - 1].reason);
        return true;
    }
    const stop_level_t *eff = stop_effective(p);
    if (eff == NULL) {
        snprintf(out, cap, "无生效止损档位");
        return false;
    }
    if (price <= eff->price) {
        snprintf(out, cap, "现价 %.2f 跌破 %s 止损位 %.2f",
                 price, stop_kind_name(eff->kind), eff->price);
        return true;
    }
    snprintf(out, cap, "现价 %.2f 距止损位 %.2f 还有 %.2f%%",
             price, eff->price, (price / eff->price - 1.0) * 100.0);
    return false;
}

/* ---------------------------------------------------------------------------
 * 仓位
 * ------------------------------------------------------------------------- */

static sizing_t mk_sizing(int qty, double entry, double budget,
                          double per_share, const char *method) {
    sizing_t s;
    memset(&s, 0, sizeof s);
    s.qty           = qty;
    s.capital       = (double)qty * entry;
    s.risk_budget   = budget;
    s.per_share_risk = per_share;
    s.method        = method;
    return s;
}

sizing_t sizing_standard(const market_rules_t *mr, double capital, double risk_pct,
                         double entry, double stop, const char *symbol) {
    double budget = capital * risk_pct / 100.0;
    double per    = entry - stop;

    if (per <= 0.0) {
        sizing_t s = mk_sizing(0, entry, budget, per, "标准风险预算法");
        snprintf(s.warning, sizeof s.warning,
                 "止损价 %.2f 必须低于入场价 %.2f", stop, entry);
        return s;
    }
    int qty = mr_round_qty(mr, (int)(budget / per), symbol);
    sizing_t s = mk_sizing(qty, entry, budget, per, "标准风险预算法");
    s.capital_pct = (capital > 0.0) ? s.capital / capital * 100.0 : 0.0;
    snprintf(s.warning, sizeof s.warning,
             "标准法假设止损必然成交；A 股连续跌停时该假设不成立");
    return s;
}

sizing_t sizing_worst(const market_rules_t *mr, double capital, double risk_pct,
                      double entry, double stop, const char *symbol,
                      int consec_limit_down) {
    double budget = capital * risk_pct / 100.0;
    double per    = mr_worst_loss(mr, entry, 1, symbol, stop, consec_limit_down);

    if (per <= 0.0) {
        sizing_t s = mk_sizing(0, entry, budget, per, "制度修正版");
        snprintf(s.warning, sizeof s.warning, "最坏损失为 0，请检查入场价与止损价");
        return s;
    }
    int qty = mr_round_qty(mr, (int)(budget / per), symbol);
    char method[64];
    snprintf(method, sizeof method, "制度修正版（%d 连跌停假设）", consec_limit_down);

    sizing_t s = mk_sizing(qty, entry, budget, per, "制度修正版");
    s.capital_pct = (capital > 0.0) ? s.capital / capital * 100.0 : 0.0;

    double amp = mr_loss_amplification(mr, entry, 1, symbol, stop, consec_limit_down);
    if (amp > 1.5)
        snprintf(s.warning, sizeof s.warning,
                 "该标的最坏情形损失是计划损失的 %.2f 倍，"
                 "标准风险预算公式在此标的上不成立，已按最坏情形折减仓位", amp);
    return s;
}

/* ---------------------------------------------------------------------------
 * 回撤预算
 * ------------------------------------------------------------------------- */

dd_check_t dd_validate(const dd_budget_t *b) {
    dd_check_t r;
    memset(&r, 0, sizeof r);
    r.valid = true;

    if (b->per_trade_pct <= 0.0 || b->max_dd_pct <= 0.0 || b->max_positions <= 0) {
        r.valid = false;
        snprintf(r.issue, sizeof r.issue, "预算参数必须为正");
        return r;
    }

    r.all_stopped_pct = b->per_trade_pct * (double)b->max_positions;
    r.consec_to_breaker = b->max_dd_pct / b->per_trade_pct;

    if (r.all_stopped_pct > b->max_dd_pct) {
        r.valid = false;
        snprintf(r.issue, sizeof r.issue,
                 "全部持仓同时触发止损将亏损 %.1f%%，超过回撤熔断线 %.1f%%。"
                 "建议单笔风险降至 %.2f%% 以下",
                 r.all_stopped_pct, b->max_dd_pct,
                 b->max_dd_pct / (double)b->max_positions);
    }
    return r;
}
