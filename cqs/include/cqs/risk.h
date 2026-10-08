/* ============================================================================
 * cqs/risk.h — 风控层
 * ============================================================================
 * 核心修正
 * --------
 * 原框架顺序：先定仓位（8%–12%），再找止损位放哪。
 * 正确顺序是反的：
 *     ① 先定单笔最大可承受损失（由回撤预算决定，不由行情决定）
 *     ② 再定止损宽度（用 ATR 倍数，而非固定整数）
 *     ③ 仓位 = 风险预算 / 止损宽度（反推出来的结果）
 * 差别不是形式上的：正序无法保证单笔最大损失可控。
 *
 * 两层止损
 * --------
 *   价格止损 —— 回答"我亏了多少"
 *   逻辑止损 —— 回答"我为什么买它还成立吗"
 * 逻辑止损优先级**高于**价格止损：前者是原因，后者是结果。
 * 原因不成立时继续用价格止损等反弹，本质是在赌运气。
 * ==========================================================================*/

#ifndef CQS_RISK_H
#define CQS_RISK_H

#include "cqs/core.h"
#include "cqs/rules.h"

#include <stdbool.h>
#include <stddef.h>

/* ---------------------------------------------------------------------------
 * 止损
 * ------------------------------------------------------------------------- */

#define CQS_STOP_KIND_TABLE(X)                                                 \
    X(STOP_STRUCTURE, "structure")                                             \
    X(STOP_ATR,       "atr")                                                   \
    X(STOP_LOGIC,     "logic")

CQS_ENUM(stop_kind, CQS_STOP_KIND_TABLE)

typedef struct {
    stop_kind_t kind;
    double      price;      /**< 逻辑止损无价位，置 0 */
    double      width_pct;  /**< 相对入场价的宽度（负数） */
    bool        active;
    char        reason[112];
} stop_level_t;

typedef struct {
    double       entry;
    stop_level_t levels[3];
    size_t       n_levels;
    bool         logic_triggered;
} stop_plan_t;

/**
 * 构建止损方案。
 *   atr20        —— 无数据时 has_atr=false
 *   prior_low    —— 结构位。**必须是真实近期低点**：
 *                   原框架把 38.52 当"前低"，但它其实是 2026-07-01 的
 *                   开盘价，既非 7 月低点(36.92)，也非 9 月低点(38.42)。
 *   logic_hit    —— 已触发的逻辑条件描述；NULL 表示未触发
 */
stop_plan_t stop_build(double entry, double atr20, bool has_atr,
                       double prior_low, bool has_prior_low,
                       const char *logic_hit);

/**
 * 生效止损位：取所有价格类档位中**距现价最近**的那个。
 *
 * 这是对原方案「跌破前低 或 2×ATR，任一触发」的修正 ——
 * 原表述下，两条相差很远时实际生效的永远是更紧的那条，
 * 另一条形同虚设。不如直接说明：取更近者。
 */
const stop_level_t *stop_effective(const stop_plan_t *p);

/** 评估是否触发。返回 true=触发；out 写入原因。 */
bool stop_evaluate(const stop_plan_t *p, double price, char *out, size_t cap);

/* ---------------------------------------------------------------------------
 * 仓位
 * ------------------------------------------------------------------------- */

typedef struct {
    int         qty;
    double      capital;
    double      capital_pct;
    double      risk_budget;
    double      per_share_risk;
    const char *method;
    char        warning[192];
} sizing_t;

/** 标准风险预算法。⚠️ 隐含假设止损必然成交，A 股连续跌停时不成立。 */
sizing_t sizing_standard(const market_rules_t *mr, double capital, double risk_pct,
                         double entry, double stop, const char *symbol);

/** 制度修正版：用连续跌停后的最坏损失反推仓位。 */
sizing_t sizing_worst(const market_rules_t *mr, double capital, double risk_pct,
                      double entry, double stop, const char *symbol,
                      int consec_limit_down);

/* ---------------------------------------------------------------------------
 * 回撤预算
 * ------------------------------------------------------------------------- */

typedef struct {
    double capital;
    double max_dd_pct;
    double per_trade_pct;
    int    max_positions;
} dd_budget_t;

typedef struct {
    bool   valid;
    double all_stopped_pct;     /**< 全部持仓同时止损的亏损 */
    double consec_to_breaker;   /**< 连续多少次全额止损触及熔断线 */
    char   issue[224];          /**< 不自洽时的问题描述 */
} dd_check_t;

/**
 * 校验回撤预算的自洽性。
 * 原框架的「8%–12% 仓位」没有对应的总资金基数与单笔损失上限，
 * 所以它无法回答"如果连续错 5 次会怎样"。
 */
dd_check_t dd_validate(const dd_budget_t *b);

#endif /* CQS_RISK_H */
