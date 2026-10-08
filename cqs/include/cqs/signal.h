/* ============================================================================
 * cqs/signal.h — 三层指标 + 三级信号
 * ============================================================================
 * 术语（避免与"三层指标"混淆）
 * ----------------------------
 *   三层指标 indicator_layer：基本面 / 技术面 / 资金面  —— 按**信息类型**分
 *   三级信号 signal_tier     ：趋势 / 动量 / 极值      —— 按**决策用途**分
 * 两者正交：技术面层同时供给趋势级与动量级。
 *
 * 核心规则
 * --------
 * **趋势级优先**：趋势级为空头时，动量级金叉不构成入场信号。
 * 这条规则的作用不是提高胜率，而是让你不必在盘中现场裁决 ——
 * 现场裁决就是情绪决策的入口。
 *
 * 代价必须说清楚：趋势级是慢变量，从空头转多头时价格往往已涨一段，
 * 系统会系统性右侧晚入场。这是"用收益换确定性"的交易，不是免费优势。
 * ==========================================================================*/

#ifndef CQS_SIGNAL_H
#define CQS_SIGNAL_H

#include "cqs/core.h"

#include <stdbool.h>
#include <stddef.h>

/* ---------------------------------------------------------------------------
 * 技术面快照
 * ------------------------------------------------------------------------- */

#define CQS_MA_N 5

/** 单次可处理的最大 K 线根数。MA250 + 波动率分位窗口都需要它。 */
#define CQS_SERIES_MAX 512

typedef struct {
    double ma[CQS_MA_N];         /**< MA5/10/20/60/250 */
    bool   ma_ok[CQS_MA_N];
    double rsi12;
    bool   rsi_ok;
    double macd_dif, macd_dea, macd_hist;
    bool   macd_ok;
    double kdj_k, kdj_d, kdj_j;
    bool   kdj_ok;
    double boll_up, boll_mid, boll_low;
    bool   boll_ok;
    double atr20;
    bool   atr_ok;
} tech_snapshot_t;

/** MA 周期表：定义一次，供计算与展示共用 */
#define CQS_MA_PERIODS(X) X(5) X(10) X(20) X(60) X(250)
static const int k_ma_periods[CQS_MA_N] = {
#define AS_P(p) p,
    CQS_MA_PERIODS(AS_P)
#undef AS_P
};

tech_snapshot_t tech_compute(const double *closes, size_t n);

/* ---------------------------------------------------------------------------
 * 指标结果
 * ------------------------------------------------------------------------- */

typedef struct {
    const char *name;
    tri_state_t state;
    double      value;
    char        reason[128];
} indicator_t;

/** 三层指标（技术面层） */
typedef struct {
    indicator_t ma_align;      /**< 均线排列 */
    indicator_t price_vs_ma250;/**< 价格 vs 年线 */
    indicator_t macd;
    indicator_t rsi;
    indicator_t kdj;
    indicator_t boll;
    indicator_t vol_pct;       /**< 波动率分位 */
} tech_indicators_t;

tech_indicators_t tech_indicators(const tech_snapshot_t *s, const double *closes, size_t n);

/* ---------------------------------------------------------------------------
 * 三级信号
 * ------------------------------------------------------------------------- */

#define CQS_TIER_TABLE(X)                                                      \
    X(TIER_TREND,    "trend")                                                  \
    X(TIER_MOMENTUM, "momentum")                                               \
    X(TIER_EXTREME,  "extreme")

/* 由表生成 signal_tier_t 与 signal_tier_name() —— 不再手写 switch */
CQS_ENUM(signal_tier, CQS_TIER_TABLE)

/** 信号有效期（交易日）。信号生成后 N 日内未触发动作即失效。 */
#define CQS_SIGNAL_DECAY_DAYS 3

typedef struct {
    signal_tier_t tier;
    const char   *name;
    tri_state_t   state;
    bool          expired;      /**< 过期信号不参与决策 */
    const char   *reason;
} signal_t;

#define CQS_MAX_SIGNALS 8

/* ---------------------------------------------------------------------------
 * 决策
 * ------------------------------------------------------------------------- */

#define CQS_ACTION_TABLE(X)                                                    \
    X(ACT_ENTRY, "entry")                                                      \
    X(ACT_WAIT,  "wait")                                                       \
    X(ACT_BLOCK, "block")                                                      \
    X(ACT_EXIT,  "exit")

CQS_ENUM(action, CQS_ACTION_TABLE)

typedef struct {
    action_t      action;
    signal_tier_t decided_by;
    bool          has_decided_by;
    char          reason[192];
    signal_t      signals[CQS_MAX_SIGNALS];
    size_t        n_signals;
} decision_t;

/**
 * 按趋势级优先规则产出决策。
 *
 * 决策树（严格按序，不跳级）：
 *   1. 趋势级存在 UNKNOWN  -> WAIT（不知道就不动，不猜）
 *   2. 趋势级全空头        -> BLOCK
 *   3. 趋势级含中性        -> WAIT（方向不明）
 *   4. 趋势级多空混合      -> WAIT（方向不一致）
 *   5. 趋势级全多头 + 动量全多头 -> ENTRY
 *   6. 趋势级全多头 + 动量未确认 -> WAIT（右侧入场代价）
 *   7. 兜底                -> WAIT（并提示分支未覆盖）
 *
 * 分支 3/4 是回归测试的固化对象：早期版本漏了它们，
 * 导致「一中性一多头」「一多一空」落到兜底分支，返回"未匹配"。
 */
decision_t signals_decide(const signal_t *sigs, size_t n);

/** 把三级指标装配成信号数组，返回写入条数 */
size_t signals_build(const tech_indicators_t *ind, signal_t *out, size_t cap);

/** 取舍声明（必须显式输出，否则使用者会以为趋势优先是免费优势） */
const char *signals_tradeoff_note(void);

#endif /* CQS_SIGNAL_H */
