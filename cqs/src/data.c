/* ============================================================================
 * cqs/data.c — 数据层实现
 * ==========================================================================*/

#include "cqs/data.h"

#include <stdio.h>
#include <string.h>

/* ---------------------------------------------------------------------------
 * 缺口日志
 * ------------------------------------------------------------------------- */

void gap_log_init(gap_log_t *g) {
    g->count = 0;
}

void gap_log_add(gap_log_t *g, const char *symbol, const char *field, const char *reason) {
    if (g->count >= CQS_GAP_MAX) return;   /* 满则丢弃，不越界 */
    g->items[g->count] = (gap_entry_t){
        .symbol = symbol, .field = field, .reason = reason,
        .logged_at = time(NULL),
    };
    g->count++;
}

size_t gap_log_dump(const gap_log_t *g, char *buf, size_t cap) {
    if (cap == 0) return 0;
    size_t off = 0;
    int n = snprintf(buf, cap, "数据缺口 %zu 项:", g->count);
    if (n < 0) return 0;
    off = (size_t)n < cap ? (size_t)n : cap - 1;

    for (size_t i = 0; i < g->count && off + 1 < cap; i++) {
        int m = snprintf(buf + off, cap - off, "\n  [%s] %s: %s",
                         g->items[i].symbol, g->items[i].field, g->items[i].reason);
        if (m < 0) break;
        off += (size_t)m < (cap - off) ? (size_t)m : (cap - off - 1);
    }
    return g->count;
}

/* ---------------------------------------------------------------------------
 * 内置样例：002422 科伦药业
 * ---------------------------------------------------------------------------
 * 数据来源：2026-10-08 逐日 K 线接口取数（前复权收盘价）。
 * 数据表定义一次，长度由编译器算出 —— 不再手写长度常量。
 * ------------------------------------------------------------------------- */

static const double k_closes_002422[] = {
    41.95, 44.75, 47.98, 47.73, 43.61, 41.98, 42.70, 44.07,
    44.80, 48.50, 49.07, 49.90, 44.91, 47.80, 47.36, 49.22,
    51.35, 46.65, 47.51, 45.21, 43.04, 42.79, 43.44,
    41.33, 42.29, 42.90, 42.51, 45.44, 46.41, 46.42, 46.91,
    46.30, 45.11, 46.69, 46.26, 44.97, 47.15, 44.87,
    41.44, 42.57, 43.64, 42.28, 41.75, 40.58,
    40.83, 41.75, 41.82, 41.92, 41.54, 40.76, 40.37, 40.14,
    39.35, 41.38, 40.65, 40.52, 40.02, 39.77, 42.48, 42.00,
    41.70, 38.86, 38.60, 39.22, 40.85, 39.26,
};

#define CQS_N_CLOSES (sizeof k_closes_002422 / sizeof k_closes_002422[0])

/* 2026-10-08 15:00 CST 的 epoch 秒。用常量而非 time()，
 * 保证样例在任何时刻运行都得到相同结果 —— 可复现是测试的前提。 */
#define CQS_FIXTURE_ASOF ((time_t)1791442800)

time_t fixture_last_day(void) { return CQS_FIXTURE_ASOF; }

/* ---- 适配器实现：三个函数，全部走同一张数据表 ---- */

static field_value_t fx_quote(const data_adapter_t *a, const char *symbol, time_t now) {
    (void)a; (void)now;
    if (symbol == NULL || strncmp(symbol, "002422", 6) != 0) return CQS_FV_NULL;
    return fv_make("close", k_closes_002422[CQS_N_CLOSES - 1],
                   CQS_FIXTURE_ASOF, TS_CLOSED, "fixture");
}

static size_t fx_closes(const data_adapter_t *a, const char *symbol,
                        double *out, size_t want) {
    (void)a;
    if (symbol == NULL || out == NULL) return 0;
    if (strncmp(symbol, "002422", 6) != 0) return 0;

    size_t avail = CQS_N_CLOSES;
    size_t take = want < avail ? want : avail;
    /* 取最后 take 根 */
    size_t start = avail - take;
    for (size_t i = 0; i < take; i++) out[i] = k_closes_002422[start + i];
    return take;
}

static field_value_t fx_bar(const data_adapter_t *a, const char *symbol, size_t back) {
    (void)a;
    if (symbol == NULL || strncmp(symbol, "002422", 6) != 0) return CQS_FV_NULL;
    if (back >= CQS_N_CLOSES) return CQS_FV_NULL;
    return fv_make("close", k_closes_002422[CQS_N_CLOSES - 1 - back],
                   CQS_FIXTURE_ASOF - (time_t)back * 86400, TS_CLOSED, "fixture");
}

static const data_ops_t k_fixture_ops = {
    .market = "CN-fixture",
    .quote  = fx_quote,
    .closes = fx_closes,
    .bar    = fx_bar,
};

static const data_adapter_t k_fixture = { &k_fixture_ops, NULL };

const data_adapter_t *fixture_002422(void) { return &k_fixture; }
