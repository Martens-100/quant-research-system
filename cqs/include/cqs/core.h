/* ============================================================================
 * cqs/core.h — 领域模型（共享类型）
 * ============================================================================
 * 职责：定义全系统共用的值类型与不变量。
 *
 * 本模块无依赖。所有其他模块只允许依赖它，禁止反向。
 *
 * 设计要点
 * --------
 * 1) **X-macro 单一定义**：每个枚举表只写一次，同时展开为
 *    enum 成员 + 名称表。消灭"键值堆砌"—— 枚举与字符串不可能不一致。
 * 2) **Score 值类型**：把 0–100 钳位封装进构造函数，
 *    各模块不再重复写 clamp。
 * 3) **头文件只放接口**：数据表在 .c 中定义，避免多编译单元下的
 *    unused 告警，也让接口与数据分离。
 * ==========================================================================*/

#ifndef CQS_CORE_H
#define CQS_CORE_H

#include <stdbool.h>
#include <stddef.h>
#include <time.h>

/* ---------------------------------------------------------------------------
 * X-macro 展开器
 * ------------------------------------------------------------------------- */

#define CQS_X_ENUM(id, str)  id,
#define CQS_X_NAME(id, str)  str,

/**
 * CQS_ENUM(prefix, table)
 *   生成：typedef enum {...} prefix##_t;  与  prefix##_name()
 *
 * 用法：一张表 -> 枚举 + 名称函数，二者同源，不可能不一致。
 */
#define CQS_ENUM(prefix, table)                                                \
    typedef enum {                                                             \
        table(CQS_X_ENUM)                                                      \
        prefix##_COUNT                                                         \
    } prefix##_t;                                                              \
                                                                               \
    static inline const char *prefix##_name(prefix##_t v) {                    \
        static const char *const k_n[] = { table(CQS_X_NAME) };                \
        return ((int)v >= 0 && (int)v < (int)prefix##_COUNT)                   \
                   ? k_n[(int)v] : "?";                                        \
    }

/* ---------------------------------------------------------------------------
 * 评分：0–100，构造即钳位
 * ------------------------------------------------------------------------- */

typedef struct { double v; } score_t;

/** 构造评分。唯一入口，各模块不得自行 clamp。 */
static inline score_t score_new(double v) {
    if (v < 0.0) v = 0.0;
    if (v > 100.0) v = 100.0;
    return (score_t){ v };
}
static inline double score_of(score_t s)       { return s.v; }
static inline score_t score_shift(score_t s, double d) { return score_new(s.v + d); }
static inline score_t score_scale(score_t s, double k) { return score_new(s.v * k); }
static inline score_t score_min(score_t a, score_t b)  { return a.v < b.v ? a : b; }
static inline score_t score_max(score_t a, score_t b)  { return a.v > b.v ? a : b; }

/** 线性映射：x∈[lo,hi] -> [0,100]，越界钳位 */
static inline score_t score_lerp(double x, double lo, double hi) {
    if (hi <= lo) return score_new(0.0);
    return score_new((x - lo) / (hi - lo) * 100.0);
}

/** 按区间映射并按权重并入（把"加分项"统一成一行） */
static inline score_t score_apply(score_t s, bool cond, double delta) {
    return cond ? score_shift(s, delta) : s;
}

/* ---------------------------------------------------------------------------
 * 等级
 * ------------------------------------------------------------------------- */

#define CQS_GRADE_TABLE(X)                                                     \
    X(GRADE_A,      "A")                                                       \
    X(GRADE_B,      "B")                                                       \
    X(GRADE_C,      "C")                                                       \
    X(GRADE_D,      "D")                                                       \
    X(GRADE_WIDE,   "宽")                                                      \
    X(GRADE_NARROW, "窄")                                                      \
    X(GRADE_NONE,   "无")

CQS_ENUM(grade, CQS_GRADE_TABLE)

/** A/B/C/D 四档（切点 80/60/40） */
static inline grade_t grade_abcd(score_t s) {
    if (s.v >= 80.0) return GRADE_A;
    if (s.v >= 60.0) return GRADE_B;
    if (s.v >= 40.0) return GRADE_C;
    return GRADE_D;
}

/** 宽/窄/无 三档（切点 75/50）—— 壁垒模块专用 */
static inline grade_t grade_moat(score_t s) {
    if (s.v >= 75.0) return GRADE_WIDE;
    if (s.v >= 50.0) return GRADE_NARROW;
    return GRADE_NONE;
}

/* ---------------------------------------------------------------------------
 * 三态
 * ------------------------------------------------------------------------- */

#define CQS_TRI_TABLE(X)                                                       \
    X(TRI_BULL,       "bull")                                                  \
    X(TRI_NEUTRAL,    "neutral")                                               \
    X(TRI_BEAR,       "bear")                                                  \
    X(TRI_OVERBOUGHT, "overbought")                                            \
    X(TRI_OVERSOLD,   "oversold")                                              \
    X(TRI_UNKNOWN,    "unknown")

CQS_ENUM(tri_state, CQS_TRI_TABLE)

/** 方向是否明确（bull/bear）；中性/未知不算 */
static inline bool tri_directional(tri_state_t s) {
    return s == TRI_BULL || s == TRI_BEAR;
}

/* ---------------------------------------------------------------------------
 * 交易状态
 * ------------------------------------------------------------------------- */

#define CQS_TS_TABLE(X)                                                        \
    X(TS_PRE_OPEN,  "pre_open")                                                \
    X(TS_INTRADAY,  "intraday")                                                \
    X(TS_CLOSED,    "closed")                                                  \
    X(TS_SUSPENDED, "suspended")                                               \
    X(TS_UNKNOWN,   "unknown")

CQS_ENUM(trading_state, CQS_TS_TABLE)

/* ---------------------------------------------------------------------------
 * 字段值：值 + 取数时刻 + 交易状态 + 来源
 * ------------------------------------------------------------------------- */

typedef struct {
    const char      *name;
    double           value;
    bool             has_value;    /**< false = 数据缺口，禁止当 0 用 */
    time_t           as_of;        /**< 取数时刻（非报告期） */
    trading_state_t  state;
    const char      *source;
    const char      *period;       /**< 统计周期，如 "2026H1"；无则 NULL */
    time_t           announced_at; /**< 财务字段公告日（Point-in-Time 关键） */
    bool             has_announced;
    const char      *unit;
} field_value_t;

/** 空值哨兵：所有"没有这个数"统一用它 */
#define CQS_FV_NULL ((field_value_t){ .name = NULL, .has_value = false, \
                                      .state = TS_UNKNOWN })

field_value_t fv_make(const char *name, double v, time_t as_of,
                      trading_state_t st, const char *src);

/** 距取数时刻的天数 */
double fv_age_days(field_value_t fv, time_t now);

/**
 * 字段是否可用于决策。
 * 返回 NULL = 可用；否则返回**拒绝原因**（静态字符串）。
 * 显式返回理由，而不是静默 false。
 */
const char *fv_reject(field_value_t fv, double max_age_days, time_t now);

/* ---------------------------------------------------------------------------
 * 时效策略
 * ------------------------------------------------------------------------- */

#define CQS_FRESH_TABLE(X)                                                     \
    X(PRICE,        "price",        1.0)                                       \
    X(CAPITAL_FLOW, "capital_flow", 2.0)                                       \
    X(VALUATION,    "valuation",    7.0)                                       \
    X(FUNDAMENTAL,  "fundamental",  200.0)                                     \
    X(MARKET_RULE,  "market_rule",  180.0)

typedef enum {
#define AS_E(id, str, days) FC_##id,
    CQS_FRESH_TABLE(AS_E)
#undef AS_E
    FC_COUNT
} freshness_cat_t;

double      freshness_days(freshness_cat_t c);
const char *freshness_name(freshness_cat_t c);

/* ---------------------------------------------------------------------------
 * 区间一致性：防止跨时点拼接
 * ------------------------------------------------------------------------- */

/**
 * 校验两字段能否直接相减算涨跌幅。
 * false 时 *why 指向静态原因串。
 *
 * 修正对象："38.52 → 45.44 涨 18%" 那类错误 ——
 * 两点间隔 37 天，中间经历 −12.6% 回撤，不可直接相减。
 */
bool series_compatible(field_value_t a, field_value_t b,
                       double max_gap_days, const char **why);

/** 区间涨跌幅。不兼容时返回 CQS_FV_NULL。 */
field_value_t fv_pct_change(field_value_t from, field_value_t to);

#endif /* CQS_CORE_H */
