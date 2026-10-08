/* ============================================================================
 * cqs/data.h — 数据层
 * ============================================================================
 * 职责：数据源插拔 + 数据缺口记录。
 *
 * 为什么缺口要显式记录
 * --------------------
 * 取数失败时返回 0 是最危险的一类 bug —— 它不报错，
 * 只把"没取到"悄悄变成"值为零"，让结论在无人察觉时变错。
 * 因此本层规定：**取不到就记缺口，缺口字段不得进入决策。**
 *
 * 代码嵌合方式
 * ------------
 * data_ops_t 是一张函数指针表。换数据源 = 换一张表，
 * 调用方代码一行不改。这是 C 里最接近 trait 的写法。
 * ==========================================================================*/

#ifndef CQS_DATA_H
#define CQS_DATA_H

#include "cqs/core.h"

#include <stddef.h>
#include <time.h>

/* ---------------------------------------------------------------------------
 * 缺口日志
 * ------------------------------------------------------------------------- */

#define CQS_GAP_MAX 64

typedef struct {
    const char *symbol;
    const char *field;
    const char *reason;
    time_t      logged_at;
} gap_entry_t;

typedef struct {
    gap_entry_t items[CQS_GAP_MAX];
    size_t      count;
} gap_log_t;

void gap_log_init(gap_log_t *g);
void gap_log_add(gap_log_t *g, const char *symbol, const char *field, const char *reason);
/** 返回实际写入的条数（缓冲不足时截断，不越界） */
size_t gap_log_dump(const gap_log_t *g, char *buf, size_t cap);

/* ---------------------------------------------------------------------------
 * 数据源适配器
 * ------------------------------------------------------------------------- */

typedef struct data_adapter data_adapter_t;

typedef struct {
    const char *market;

    /** 最新报价。必须标注 TradingState，否则调用方无法判断能否用于决策。 */
    field_value_t (*quote)(const data_adapter_t *a, const char *symbol, time_t now);

    /** 取最近 n 根收盘价（前复权）。返回实际根数，不足时按实际返回。 */
    size_t (*closes)(const data_adapter_t *a, const char *symbol,
                     double *out, size_t want);

    /** 取第 back 根 K 线（back=0 为最新）。无数据返回 CQS_FV_NULL。 */
    field_value_t (*bar)(const data_adapter_t *a, const char *symbol, size_t back);
} data_ops_t;

struct data_adapter {
    const data_ops_t *ops;
    const void       *ctx;   /**< 具体数据源的上下文 */
};

/** 便捷转发，避免调用方到处写 a->ops->xxx(a, ...) */
static inline field_value_t da_quote(const data_adapter_t *a, const char *sym, time_t now) {
    return a->ops->quote(a, sym, now);
}
static inline size_t da_closes(const data_adapter_t *a, const char *sym,
                               double *out, size_t want) {
    return a->ops->closes(a, sym, out, want);
}
static inline field_value_t da_bar(const data_adapter_t *a, const char *sym, size_t back) {
    return a->ops->bar(a, sym, back);
}

/* ---------------------------------------------------------------------------
 * 内置样例适配器
 * ---------------------------------------------------------------------------
 * 内嵌 002422 科伦药业 2026-07-01 ~ 2026-10-08 的 66 根前复权收盘价，
 * 用于离线自检与测试。生产环境应替换为真实数据源（akshare 等）。
 * ------------------------------------------------------------------------- */

const data_adapter_t *fixture_002422(void);

/** 样例数据的时间基准（最后一根 K 线的交易日） */
time_t fixture_last_day(void);

#endif /* CQS_DATA_H */
