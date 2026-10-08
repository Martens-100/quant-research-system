/* ============================================================================
 * cqs/analysis.h — 四模块评估 + 三类信号 + 建议映射
 * ============================================================================
 * 代码嵌合
 * --------
 * 四个评估模块（研报/行业/现金流/壁垒）返回**同一个** assessment_t，
 * 由各自的 assess_*() 填充。信号层与建议层因此只需处理一种结构，
 * 不必为每个模块写适配代码。
 *
 * 建议映射的优先级（决定权从高到低）
 * ----------------------------------
 *   ① 逻辑止损（基本面证伪）—— 原因层；原因不成立时动作层无意义
 *   ② 量化信号 BLOCK / EXIT —— 确定性规则
 *   ③ 监控信号 CRITICAL     —— 暂缓一切加仓
 *   ④ 多因子分层            —— 只影响仓位大小，不影响方向
 *   ⑤ 量化 ENTRY + 监控降级
 * ==========================================================================*/

#ifndef CQS_ANALYSIS_H
#define CQS_ANALYSIS_H

#include "cqs/core.h"
#include "cqs/signal.h"

#include <stdbool.h>
#include <stddef.h>

/* ---------------------------------------------------------------------------
 * 统一评估结构
 * ------------------------------------------------------------------------- */

#define CQS_MAX_FINDINGS 5
#define CQS_MAX_GAPS     5
#define CQS_MAX_CAVEATS  4

typedef struct {
    const char *module;
    score_t     score;
    grade_t     grade;
    char        findings[CQS_MAX_FINDINGS][176];
    size_t      n_findings;
    char        gaps[CQS_MAX_GAPS][144];
    size_t      n_gaps;
    char        caveats[CQS_MAX_CAVEATS][176];
    size_t      n_caveats;
} assessment_t;

void assess_init(assessment_t *a, const char *module);
void assess_finding(assessment_t *a, const char *fmt, ...);
void assess_gap(assessment_t *a, const char *fmt, ...);
void assess_caveat(assessment_t *a, const char *fmt, ...);

/* ---------------------------------------------------------------------------
 * 四个模块的输入
 * ------------------------------------------------------------------------- */

typedef struct {
    double target_price_mean;
    double current_price;
    int    coverage_count;
    double rating_buy_pct;      /**< 买入/增持占比；-1 = 缺失 */
    bool   has_site_visit;
} research_in_t;

typedef struct {
    const char *industry;
    const char *stage;          /**< 导入/成长/成熟/下行 */
    const char *position;       /**< 龙头/细分龙头/追赶者/边缘 */
    const char *intensity;      /**< low/medium/high */
    const char *margin_trend;   /**< up/flat/down */
    double      industry_growth;/**< 行业中位营收增速 % */
    double      relative_strength; /**< 相对行业强弱 % */
} industry_in_t;

typedef struct {
    double ocf;            /**< 经营现金流净额 */
    double net_profit;     /**< 归母净利 */
    double capex;
    double interest_debt;
    bool   has_ocf, has_np, has_capex, has_debt;
} cashflow_in_t;

typedef struct {
    const char *moat_type;
    const char *moat_source;   /**< **必须可命名**，否则等级强制为「无」 */
    int         durability_years;
    double      roic, wacc;
    bool        has_roic, has_wacc;
    int         substitute_count;
    int         evidence_count;
} moat_in_t;

assessment_t assess_research(const research_in_t *in);
assessment_t assess_industry(const industry_in_t *in);
assessment_t assess_cashflow(const cashflow_in_t *in);
assessment_t assess_moat(const moat_in_t *in);

/* ---------------------------------------------------------------------------
 * 四维汇总
 * ------------------------------------------------------------------------- */

typedef struct {
    assessment_t research;
    assessment_t industry;
    assessment_t cashflow;
    assessment_t moat;
} bundle_t;

/** 加权综合：现金流 30% · 壁垒 25% · 行业 25% · 研报 20%
 *  慢变量（现金流/壁垒）权重高于快变量（研报/行业），
 *  避免被短期预期牵着走。 */
score_t bundle_composite(const bundle_t *b);

/** 多因子信号：只影响仓位大小，不影响方向 */
typedef struct {
    score_t     composite;
    const char *layer;       /**< Q1..Q5 */
    double      percentile;
} factor_signal_t;

factor_signal_t factor_from_bundle(const bundle_t *b);

/* ---------------------------------------------------------------------------
 * 监控信号
 * ------------------------------------------------------------------------- */

#define CQS_MON_TABLE(X)                                                       \
    X(MON_INFO,     "info")                                                    \
    X(MON_WARNING,  "warning")                                                 \
    X(MON_CRITICAL, "critical")

CQS_ENUM(mon_level, CQS_MON_TABLE)

typedef struct {
    mon_level_t level;
    const char *trigger;
    const char *window;       /**< **必须提供**，无时间窗则无法排期 */
    const char *implication;
} monitor_signal_t;

/** 汇总最高级别 */
mon_level_t monitors_max_level(const monitor_signal_t *m, size_t n);

/* ---------------------------------------------------------------------------
 * 操作建议
 * ------------------------------------------------------------------------- */

#define CQS_ADVICE_TABLE(X)                                                    \
    X(ADV_NO_ENTRY,   "不入场")                                                \
    X(ADV_OBSERVE,    "观察")                                                  \
    X(ADV_PROBE,      "试仓")                                                  \
    X(ADV_BUILD,      "建仓")                                                  \
    X(ADV_BUILD_HALF, "建仓（半仓）")                                          \
    X(ADV_SUSPEND,    "暂缓建仓")                                              \
    X(ADV_HOLD,       "持有")                                                  \
    X(ADV_REDUCE,     "降仓")                                                  \
    X(ADV_CLEAR,      "清仓")

CQS_ENUM(advice, CQS_ADVICE_TABLE)

typedef struct {
    advice_t    advice;
    double      position_pct;
    const char *decided_by;
    char        chain[6][176];
    size_t      n_chain;
    char        blocked[2][176];
    size_t      n_blocked;
} advice_result_t;

typedef struct {
    double worst_case_position_pct;   /**< 标准仓位基准（已按制度修正） */
} advice_engine_t;

/**
 * 三类信号 -> 操作建议。
 * 严格按优先级判定，每步写入 chain，便于事后审计
 * "这个建议是基于什么得出的"。
 */
advice_result_t advice_decide(const advice_engine_t *eng,
                              action_t quant_action, const char *quant_reason,
                              const char *factor_layer,
                              const monitor_signal_t *mons, size_t n_mons,
                              bool holding, int logic_falsified);

#endif /* CQS_ANALYSIS_H */
