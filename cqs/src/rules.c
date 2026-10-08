/* ============================================================================
 * cqs/rules.c — 制度约束引擎实现
 * ============================================================================
 * 一份逻辑 + 两份数据：CN / US 共用全部算法，差异收敛在 k_profile_* 里。
 * ==========================================================================*/

#include "cqs/rules.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

/* ---------------------------------------------------------------------------
 * 板块表：定义一次，展开为数据表
 * ------------------------------------------------------------------------- */

static const board_rule_t k_boards[] = {
#define AS_ROW(id, label, lim, minq, incr, cage) \
    [BOARD_##id] = { BOARD_##id, label, lim, minq, incr, cage },
    CQS_BOARD_TABLE(AS_ROW)
#undef AS_ROW
};

/* 编译期闸门：表长度必须等于枚举数量。
 * 往 CQS_BOARD_TABLE 加一行却忘了同步别处，编译直接失败。 */
_Static_assert(sizeof k_boards / sizeof k_boards[0] == (size_t)BOARD_COUNT,
               "board table size != BOARD_COUNT");

const board_rule_t *board_rule(board_t b) {
    if ((int)b < 0 || (int)b >= (int)BOARD_COUNT) return NULL;
    return &k_boards[(int)b];
}

const char *board_name(board_t b) {
    const board_rule_t *r = board_rule(b);
    return r ? r->label : "?";
}

board_t board_classify(const char *symbol) {
    if (symbol == NULL) return BOARD_CN_MAIN;
    /* 跳过可能的交易所后缀，只看代码本体 */
    if (strncmp(symbol, "688", 3) == 0 || strncmp(symbol, "689", 3) == 0)
        return BOARD_CN_STAR;
    if (strncmp(symbol, "300", 3) == 0 || strncmp(symbol, "301", 3) == 0)
        return BOARD_CN_GEM;
    if (symbol[0] == '8' || symbol[0] == '4' || strncmp(symbol, "920", 3) == 0)
        return BOARD_CN_BSE;
    return BOARD_CN_MAIN;
}

/* ---------------------------------------------------------------------------
 * 市场剖面：制度差异的唯一载体
 * ------------------------------------------------------------------------- */

static const market_profile_t k_profile_cn = {
    .kind              = MKT_CN,
    .label             = "A股",
    .t_plus_1          = true,
    .has_price_limit   = true,
    .boards            = k_boards,
    .board_count       = BOARD_COUNT,
    .price_limit_note  = "主板±10% | 科创/创业板±20% | 北交所±30%；"
                         "主板 ST/*ST 自 2026-07-06 起由 ±5% 调整为 ±10%",
};

static const market_profile_t k_profile_us = {
    .kind              = MKT_US,
    .label             = "美股",
    .t_plus_1          = false,
    .has_price_limit   = false,
    .boards            = k_boards,
    .board_count       = BOARD_COUNT,
    .price_limit_note  = "无涨跌停，实行 LULD 波动性熔断",
};

struct market_rules {
    const market_profile_t *profile;
};

static const market_rules_t k_mr_cn = { &k_profile_cn };
static const market_rules_t k_mr_us = { &k_profile_us };

const market_rules_t   *market_cn(void) { return &k_mr_cn; }
const market_rules_t   *market_us(void) { return &k_mr_us; }
const char             *mr_label(const market_rules_t *mr) { return mr->profile->label; }
const market_profile_t *mr_profile(const market_rules_t *mr) { return mr->profile; }

board_t mr_classify(const market_rules_t *mr, const char *symbol) {
    if (mr->profile->kind == MKT_US) return BOARD_US;
    return board_classify(symbol);
}

const board_rule_t *mr_board_of(const market_rules_t *mr, const char *symbol) {
    return board_rule(mr_classify(mr, symbol));
}

/* ---------------------------------------------------------------------------
 * 涨跌停
 * ------------------------------------------------------------------------- */

static double round2(double x) { return round(x * 100.0) / 100.0; }

void mr_price_limits(const market_rules_t *mr, double prev_close,
                     const char *symbol, bool is_st,
                     double *lo, double *hi) {
    const board_rule_t *r = mr_board_of(mr, symbol);
    /* is_st 当前不产生分支 —— 主板 ST 已与普通股同为 ±10%。
     * 保留该参数是为规则未来变化时留扩展点，不构成当前差异。 */
    (void)is_st;

    if (!mr->profile->has_price_limit || r == NULL || r->limit_pct <= 0.0) {
        if (lo) *lo = 0.0;
        if (hi) *hi = INFINITY;
        return;
    }
    double k = r->limit_pct / 100.0;
    if (lo) *lo = round2(prev_close * (1.0 - k) + 1e-9);
    if (hi) *hi = round2(prev_close * (1.0 + k) + 1e-9);
}

bool mr_is_limit_down(const market_rules_t *mr, double price, double prev_close,
                      const char *symbol, bool is_st) {
    double lo = 0.0;
    mr_price_limits(mr, prev_close, symbol, is_st, &lo, NULL);
    if (lo <= 0.0) return false;
    return price <= lo + 1e-9;
}

bool mr_is_limit_up(const market_rules_t *mr, double price, double prev_close,
                    const char *symbol, bool is_st) {
    double hi = 0.0;
    mr_price_limits(mr, prev_close, symbol, is_st, NULL, &hi);
    if (!isfinite(hi)) return false;
    return price >= hi - 1e-9;
}

/* ---------------------------------------------------------------------------
 * T+1
 * ------------------------------------------------------------------------- */

bool mr_can_sell(const market_rules_t *mr, long buy_day, long sell_day) {
    if (!mr->profile->t_plus_1) return true;   /* 美股 T+0 */
    return sell_day > buy_day;
}

/* ---------------------------------------------------------------------------
 * 数量取整
 * ------------------------------------------------------------------------- */

int mr_round_qty(const market_rules_t *mr, int target, const char *symbol) {
    const board_rule_t *r = mr_board_of(mr, symbol);
    if (r == NULL || target < r->min_qty) return 0;
    int excess = (target - r->min_qty) % r->qty_increment;
    return target - excess;
}

/* ---------------------------------------------------------------------------
 * 最坏损失重算（核心）
 * ------------------------------------------------------------------------- */

double mr_worst_loss(const market_rules_t *mr, double entry, int qty,
                     const char *symbol, double stop, int consec_limit_down) {
    const board_rule_t *r = mr_board_of(mr, symbol);
    if (r == NULL || qty <= 0) return 0.0;

    double planned = (entry - stop) * (double)qty;
    if (planned < 0.0) planned = 0.0;

    /* 无涨跌幅限制的市场（美股）：不存在"跌停封板卖不出"，
     * 最坏损失即计划损失。这不是特例分支，而是制度差异的正确体现。 */
    if (!mr->profile->has_price_limit || r->limit_pct <= 0.0) return planned;

    if (consec_limit_down <= 0) return planned;

    double k = r->limit_pct / 100.0;
    double floor_price = entry * pow(1.0 - k, (double)consec_limit_down);
    double limit_loss = (entry - floor_price) * (double)qty;
    if (limit_loss < 0.0) limit_loss = 0.0;

    /* 取二者较大者。consec=0 时 floor_price=entry，此处自然退化为 planned。 */
    return planned > limit_loss ? planned : limit_loss;
}

double mr_loss_amplification(const market_rules_t *mr, double entry, int qty,
                             const char *symbol, double stop, int consec_limit_down) {
    double planned = (entry - stop) * (double)qty;
    if (planned <= 0.0) return 1.0;
    return mr_worst_loss(mr, entry, qty, symbol, stop, consec_limit_down) / planned;
}

int mr_size_by_worst(const market_rules_t *mr, double capital, double risk_pct,
                     double entry, int qty_hint, const char *symbol,
                     double stop, int consec_limit_down) {
    double budget = capital * risk_pct / 100.0;
    double per_share = mr_worst_loss(mr, entry, 1, symbol, stop, consec_limit_down);
    if (per_share <= 0.0 || entry <= 0.0) return 0;

    int raw = (int)(budget / per_share);
    if (qty_hint > 0 && raw > qty_hint) raw = qty_hint;   /* 受上限约束 */
    return mr_round_qty(mr, raw, symbol);
}

/* ---------------------------------------------------------------------------
 * 交易费用
 * ------------------------------------------------------------------------- */

fee_schedule_t fee_default(void) {
    return (fee_schedule_t){
        .stamp_duty_sell_pct = 0.05,   /* 核验值 */
        .commission_pct      = -1.0,   /* 待用户提供 */
        .commission_min      = -1.0,
        .transfer_fee_pct    = -1.0,
        .slippage_pct        = -1.0,
    };
}

bool fee_complete(const fee_schedule_t *f) {
    return f->commission_pct >= 0.0 && f->commission_min >= 0.0
        && f->transfer_fee_pct >= 0.0 && f->slippage_pct >= 0.0;
}

double fee_round_trip_pct(const fee_schedule_t *f) {
    if (!fee_complete(f)) return -1.0;   /* 成本未知 —— 显式返回 -1，不返回 0 */
    return f->stamp_duty_sell_pct
         + 2.0 * f->commission_pct
         + 2.0 * f->transfer_fee_pct
         + 2.0 * f->slippage_pct;
}

/* ---------------------------------------------------------------------------
 * 停牌
 * ------------------------------------------------------------------------- */

suspension_rule_t suspension_default(void) {
    return (suspension_rule_t){
        /* 停牌日不得计入监控计时：停牌期间根本没有交易，
         * 谈不上"跌破后 N 日收回"。 */
        .count_suspended_days_in_monitor    = false,
        /* 复牌首日不适用常规止损：复牌常伴跳空缺口，止损价已失去意义。 */
        .first_day_after_resume_normal_stop = false,
        .reevaluate_window_days             = 5,
    };
}
