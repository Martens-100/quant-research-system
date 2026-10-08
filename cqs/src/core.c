/* ============================================================================
 * cqs/core.c — 领域模型实现
 * ==========================================================================*/

#include "cqs/core.h"

#include <stdio.h>
#include <string.h>

/* ---------------------------------------------------------------------------
 * 时效策略表：定义一次，两处展开
 * ------------------------------------------------------------------------- */

static const double k_fresh_days[] = {
#define AS_DAYS(id, str, days) [FC_##id] = days,
    CQS_FRESH_TABLE(AS_DAYS)
#undef AS_DAYS
};

static const char *const k_fresh_names[] = {
#define AS_NAMES(id, str, days) [FC_##id] = str,
    CQS_FRESH_TABLE(AS_NAMES)
#undef AS_NAMES
};

/* 编译期闸门：表长度必须与枚举数量一致。
 * 新增一个时效类别却忘了填天数，编译直接失败 —— 而不是运行时读到 0。 */
_Static_assert(sizeof k_fresh_days / sizeof k_fresh_days[0] == (size_t)FC_COUNT,
               "freshness days table size != FC_COUNT");
_Static_assert(sizeof k_fresh_names / sizeof k_fresh_names[0] == (size_t)FC_COUNT,
               "freshness names table size != FC_COUNT");

double freshness_days(freshness_cat_t c) {
    if ((int)c < 0 || (int)c >= (int)FC_COUNT) return 0.0;
    return k_fresh_days[(int)c];
}

const char *freshness_name(freshness_cat_t c) {
    if ((int)c < 0 || (int)c >= (int)FC_COUNT) return "?";
    return k_fresh_names[(int)c];
}

/* ---------------------------------------------------------------------------
 * 字段值
 * ------------------------------------------------------------------------- */

field_value_t fv_make(const char *name, double v, time_t as_of,
                      trading_state_t st, const char *src) {
    return (field_value_t){
        .name = name, .value = v, .has_value = true,
        .as_of = as_of, .state = st, .source = src,
        .period = NULL, .announced_at = 0, .has_announced = false,
        .unit = NULL,
    };
}

double fv_age_days(field_value_t fv, time_t now) {
    return difftime(now, fv.as_of) / 86400.0;
}

/**
 * 拒绝理由缓冲区。
 * 单线程 CLI 场景下可接受；若将来引入并发，改为由调用方传入缓冲。
 */
static char g_reason[160];

const char *fv_reject(field_value_t fv, double max_age_days, time_t now) {
    if (!fv.has_value) {
        snprintf(g_reason, sizeof g_reason, "值为空（数据缺口）");
        return g_reason;
    }
    if (fv.state == TS_SUSPENDED) {
        snprintf(g_reason, sizeof g_reason, "标的停牌，价格不具备交易意义");
        return g_reason;
    }
    if (fv.state == TS_INTRADAY) {
        snprintf(g_reason, sizeof g_reason, "当前为盘中价，非收盘价");
        return g_reason;
    }
    double age = fv_age_days(fv, now);
    if (age > max_age_days) {
        snprintf(g_reason, sizeof g_reason,
                 "数据已过期 %.1f 天（上限 %.1f 天）", age, max_age_days);
        return g_reason;
    }
    return NULL;   /* 可用 */
}

/* ---------------------------------------------------------------------------
 * 区间一致性
 * ------------------------------------------------------------------------- */

bool series_compatible(field_value_t a, field_value_t b,
                       double max_gap_days, const char **why) {
    const char *sink = NULL;          /* 调用方不关心原因时的落点 */
    if (why == NULL) why = &sink;

    if (!a.has_value || !b.has_value) {
        *why = "存在空值，无法计算区间";
        return false;
    }
    if (a.source != NULL && b.source != NULL && strcmp(a.source, b.source) != 0) {
        *why = "数据源不同，不可直接比较";
        return false;
    }
    if (a.name != NULL && b.name != NULL && strcmp(a.name, b.name) != 0) {
        *why = "字段不同，不可直接比较";
        return false;
    }
    double gap = difftime(b.as_of, a.as_of) / 86400.0;
    if (gap < 0.0) gap = -gap;
    if (gap > max_gap_days) {
        static char gapbuf[192];
        snprintf(gapbuf, sizeof gapbuf,
                 "两点间隔 %.0f 天（上限 %.0f 天），不可直接相减算涨跌幅；"
                 "跨期比较须改用同一连续序列并说明中间路径",
                 gap, max_gap_days);
        *why = gapbuf;
        return false;
    }
    return true;
}

field_value_t fv_pct_change(field_value_t from, field_value_t to) {
    const char *why = NULL;
    if (!series_compatible(from, to, 1.0, &why)) return CQS_FV_NULL;
    if (from.value == 0.0) return CQS_FV_NULL;
    return fv_make("pct_change",
                   (to.value - from.value) / from.value * 100.0,
                   to.as_of, to.state, to.source);
}
