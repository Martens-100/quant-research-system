/* ============================================================================
 * cqs/signal.c — 三层指标 + 三级信号实现
 * ==========================================================================*/

#include "cqs/signal.h"

#include <math.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

/* CQS_SERIES_MAX 定义在 signal.h（main 也要用），此处只做编译期校验 */
_Static_assert(CQS_SERIES_MAX >= 260, "series buffer must hold MA250 + margin");

/* ---------------------------------------------------------------------------
 * 基础计算
 * ------------------------------------------------------------------------- */

/** 最近 period 根的简单均值 */
static bool sma_last(const double *c, size_t n, int period, double *out) {
    if (period <= 0 || n < (size_t)period) return false;
    double s = 0.0;
    for (size_t i = n - (size_t)period; i < n; i++) s += c[i];
    *out = s / (double)period;
    return true;
}

/** 单步 EMA 迭代：k = 2/(p+1) */
static double ema_step(double prev, double x, int period) {
    double k = 2.0 / ((double)period + 1.0);
    return x * k + prev * (1.0 - k);
}

/** RSI（Wilder 简化版：等权平均） */
static bool rsi_last(const double *c, size_t n, int period, double *out) {
    if (period <= 0 || n < (size_t)period + 1u) return false;
    double gain = 0.0, loss = 0.0;
    for (size_t i = n - (size_t)period; i < n; i++) {
        double d = c[i] - c[i - 1];
        if (d > 0.0) gain += d; else loss -= d;
    }
    gain /= (double)period;
    loss /= (double)period;
    if (loss == 0.0) { *out = 100.0; return true; }
    *out = 100.0 - 100.0 / (1.0 + gain / loss);
    return true;
}

/** 收盘价年化波动率（60 日窗口，252 交易日） */
static bool annualized_vol(const double *c, size_t n, int window, double *out) {
    if (window <= 1 || n < (size_t)window + 1u) return false;
    size_t start = n - (size_t)window;
    double sum = 0.0;
    for (size_t i = start; i < n; i++) sum += c[i] / c[i - 1] - 1.0;
    double mean = sum / (double)window;

    double var = 0.0;
    for (size_t i = start; i < n; i++) {
        double r = c[i] / c[i - 1] - 1.0 - mean;
        var += r * r;
    }
    var /= (double)window;
    *out = sqrt(var) * sqrt(252.0) * 100.0;
    return true;
}

/* ---------------------------------------------------------------------------
 * 技术面快照
 * ------------------------------------------------------------------------- */

tech_snapshot_t tech_compute(const double *closes, size_t n) {
    tech_snapshot_t s;
    memset(&s, 0, sizeof s);

    if (closes == NULL || n < 2) return s;

    /* --- 均线 --- */
    for (int i = 0; i < CQS_MA_N; i++) {
        s.ma_ok[i] = sma_last(closes, n, k_ma_periods[i], &s.ma[i]);
    }

    /* --- RSI12 --- */
    s.rsi_ok = rsi_last(closes, n, 12, &s.rsi12);

    /* --- MACD(12,26,9) --- */
    if (n >= 35) {
        double e12 = closes[0], e26 = closes[0];
        double dif_buf[CQS_SERIES_MAX];
        size_t nb = 0;
        for (size_t i = 1; i < n && nb < CQS_SERIES_MAX; i++) {
            e12 = ema_step(e12, closes[i], 12);
            e26 = ema_step(e26, closes[i], 26);
            dif_buf[nb++] = e12 - e26;
        }
        if (nb > 0) {
            double dea = dif_buf[0];
            for (size_t i = 1; i < nb; i++) dea = ema_step(dea, dif_buf[i], 9);
            s.macd_dif  = dif_buf[nb - 1];
            s.macd_dea  = dea;
            s.macd_hist = (s.macd_dif - dea) * 2.0;
            s.macd_ok   = true;
        }
    }

    /* --- KDJ(9,3,3) --- */
    if (n >= 9) {
        double k = 50.0, d = 50.0;
        for (size_t i = 8; i < n; i++) {
            double lo = closes[i - 8], hi = closes[i - 8];
            for (size_t j = i - 8; j <= i; j++) {
                if (closes[j] < lo) lo = closes[j];
                if (closes[j] > hi) hi = closes[j];
            }
            double rsv = (hi == lo) ? 50.0 : (closes[i] - lo) / (hi - lo) * 100.0;
            k = 2.0 / 3.0 * k + 1.0 / 3.0 * rsv;
            d = 2.0 / 3.0 * d + 1.0 / 3.0 * k;
        }
        s.kdj_k = k; s.kdj_d = d; s.kdj_j = 3.0 * k - 2.0 * d;
        s.kdj_ok = true;
    }

    /* --- BOLL(20,2) --- */
    if (n >= 20) {
        double mid = 0.0;
        if (sma_last(closes, n, 20, &mid)) {
            double var = 0.0;
            for (size_t i = n - 20; i < n; i++) {
                double d = closes[i] - mid;
                var += d * d;
            }
            var /= 20.0;
            double sd = sqrt(var);
            s.boll_mid = mid;
            s.boll_up  = mid + 2.0 * sd;
            s.boll_low = mid - 2.0 * sd;
            s.boll_ok  = true;
        }
    }

    /* --- ATR20 ---
     * ⚠️ 口径说明：本实现只接收收盘价序列，没有 high/low。
     * 因此 ATR 退化为「近 20 日收盘价绝对变动的均值」，
     * 是真实 ATR 的下界近似，不是真 ATR。
     * 若数据源能提供 OHLC，应在 data 层补 high/low 后改用真 TR。
     * 这里显式标注，不假装它是 ATR。 */
    if (n >= 21) {
        double sum = 0.0;
        for (size_t i = n - 20; i < n; i++) sum += fabs(closes[i] - closes[i - 1]);
        s.atr20 = sum / 20.0;
        s.atr_ok = true;
    }

    return s;
}

/* ---------------------------------------------------------------------------
 * 三层指标（技术面层）
 * ------------------------------------------------------------------------- */

static indicator_t mk(const char *name, tri_state_t st, double v, const char *fmt, ...) {
    indicator_t r;
    r.name = name;
    r.state = st;
    r.value = v;
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(r.reason, sizeof r.reason, fmt, ap);
    va_end(ap);
    return r;
}

static indicator_t ind_ma_align(const tech_snapshot_t *s) {
    if (!s->ma_ok[0] || !s->ma_ok[4])
        return mk("均线排列", TRI_UNKNOWN, 0.0, "需 250 根 K 线");

    bool asc = true, desc = true;
    for (int i = 0; i + 1 < CQS_MA_N; i++) {
        if (!(s->ma[i] > s->ma[i + 1])) asc = false;
        if (!(s->ma[i] < s->ma[i + 1])) desc = false;
    }
    bool above = s->ma_ok[4] && (s->ma[0] > s->ma[4]);
    (void)above;

    tri_state_t st;
    if (asc)                       st = TRI_BULL;
    else if (desc)                 st = TRI_BEAR;
    else                           st = TRI_NEUTRAL;

    return mk("均线排列", st, s->ma[0],
              "MA5=%.2f MA20=%.2f MA60=%.2f MA250=%.2f",
              s->ma[0], s->ma[2], s->ma[3], s->ma[4]);
}

static indicator_t ind_price_vs_ma250(const tech_snapshot_t *s) {
    if (!s->ma_ok[4])
        return mk("价格vs年线", TRI_UNKNOWN, 0.0, "需 250 根 K 线");
    double pct = (s->ma[0] / s->ma[4] - 1.0) * 100.0;
    return mk("价格vs年线", pct > 0.0 ? TRI_BULL : TRI_BEAR, pct,
              "MA5 %.2f vs MA250 %.2f（%+.2f%%）", s->ma[0], s->ma[4], pct);
}

static indicator_t ind_macd(const tech_snapshot_t *s) {
    if (!s->macd_ok)
        return mk("MACD", TRI_UNKNOWN, 0.0, "需 35 根 K 线");
    tri_state_t st;
    if (s->macd_dif > s->macd_dea && s->macd_hist > 0.0)      st = TRI_BULL;
    else if (s->macd_dif < s->macd_dea && s->macd_hist < 0.0) st = TRI_BEAR;
    else                                                       st = TRI_NEUTRAL;
    return mk("MACD", st, s->macd_hist,
              "DIF %.4f / DEA %.4f / 柱 %+.4f", s->macd_dif, s->macd_dea, s->macd_hist);
}

static indicator_t ind_rsi(const tech_snapshot_t *s) {
    if (!s->rsi_ok)
        return mk("RSI12", TRI_UNKNOWN, 0.0, "数据不足");
    tri_state_t st = (s->rsi12 > 70.0) ? TRI_OVERBOUGHT
                  : (s->rsi12 < 30.0) ? TRI_OVERSOLD
                  : TRI_NEUTRAL;
    return mk("RSI12", st, s->rsi12, "RSI12 = %.2f", s->rsi12);
}

static indicator_t ind_kdj(const tech_snapshot_t *s) {
    if (!s->kdj_ok)
        return mk("KDJ", TRI_UNKNOWN, 0.0, "数据不足");
    tri_state_t st = (s->kdj_j > 100.0) ? TRI_OVERBOUGHT
                  : (s->kdj_j < 0.0)   ? TRI_OVERSOLD
                  : TRI_NEUTRAL;
    return mk("KDJ", st, s->kdj_j, "K=%.2f D=%.2f J=%.2f",
              s->kdj_k, s->kdj_d, s->kdj_j);
}

static indicator_t ind_boll(const tech_snapshot_t *s, const double *closes, size_t n) {
    if (!s->boll_ok || n == 0)
        return mk("BOLL", TRI_UNKNOWN, 0.0, "数据不足");
    double price = closes[n - 1];
    tri_state_t st = (price >= s->boll_up)  ? TRI_OVERBOUGHT
                   : (price <= s->boll_low) ? TRI_OVERSOLD
                   : TRI_NEUTRAL;
    return mk("BOLL", st, price, "价 %.2f / 下轨 %.2f / 上轨 %.2f",
              price, s->boll_low, s->boll_up);
}

static indicator_t ind_vol_pct(const double *closes, size_t n) {
    /* A 股波动率替代方案。
     * 注意理由已修正：原方案称「中国波指已停止发布」，该断言证据不足
     * （CEIC 数据止于 2018-02-14，另有 2025-06 新指数消息，
     *  新浪 2026-09-28 仍有行情页），已撤回。
     * 本方法成立不依赖指数是否存在 —— 它回答
     * 「这只股票比它自己平时更波动吗」。 */
    const int win = 60, lookback = 250;
    if (n < (size_t)(win + 20))
        return mk("波动率分位", TRI_UNKNOWN, 0.0, "需 %d 根 K 线", win + 20);

    size_t start = (n > (size_t)lookback) ? n - (size_t)lookback : 0;
    double cur = 0.0;
    if (!annualized_vol(closes, n, win, &cur))
        return mk("波动率分位", TRI_UNKNOWN, 0.0, "波动率计算失败");

    size_t cnt = 0, below = 0;
    for (size_t end = start + (size_t)win; end <= n; end++) {
        double v = 0.0;
        if (!annualized_vol(closes, end, win, &v)) continue;
        cnt++;
        if (v <= cur) below++;
    }
    if (cnt == 0) return mk("波动率分位", TRI_UNKNOWN, 0.0, "样本不足");

    double pct = (double)below / (double)cnt * 100.0;
    tri_state_t st = (pct > 80.0) ? TRI_BEAR : (pct < 30.0) ? TRI_BULL : TRI_NEUTRAL;
    return mk("波动率分位", st, pct,
              "年化波动率 %.1f%%，处于近 %zu 日 %.0f%% 分位", cur, cnt, pct);
}

tech_indicators_t tech_indicators(const tech_snapshot_t *s,
                                  const double *closes, size_t n) {
    tech_indicators_t r;
    r.ma_align       = ind_ma_align(s);
    r.price_vs_ma250 = ind_price_vs_ma250(s);
    r.macd           = ind_macd(s);
    r.rsi            = ind_rsi(s);
    r.kdj            = ind_kdj(s);
    r.boll           = ind_boll(s, closes, n);
    r.vol_pct        = ind_vol_pct(closes, n);
    return r;
}

/* ---------------------------------------------------------------------------
 * 装配三级信号
 * ------------------------------------------------------------------------- */

static size_t push(signal_t *out, size_t cap, size_t i,
                   signal_tier_t tier, const indicator_t *ind) {
    if (i >= cap) return i;
    out[i] = (signal_t){
        .tier = tier, .name = ind->name, .state = ind->state,
        .expired = false, .reason = ind->reason,
    };
    return i + 1;
}

size_t signals_build(const tech_indicators_t *ind, signal_t *out, size_t cap) {
    size_t i = 0;
    i = push(out, cap, i, TIER_TREND,    &ind->ma_align);
    i = push(out, cap, i, TIER_TREND,    &ind->price_vs_ma250);
    i = push(out, cap, i, TIER_MOMENTUM, &ind->macd);
    i = push(out, cap, i, TIER_MOMENTUM, &ind->rsi);
    i = push(out, cap, i, TIER_EXTREME,  &ind->kdj);
    i = push(out, cap, i, TIER_EXTREME,  &ind->boll);
    return i;
}

/* ---------------------------------------------------------------------------
 * 决策树
 * ------------------------------------------------------------------------- */

static decision_t make(action_t act, bool has_by, signal_tier_t by,
                       const signal_t *sigs, size_t n, const char *fmt, ...) {
    decision_t d;
    memset(&d, 0, sizeof d);
    d.action = act;
    d.has_decided_by = has_by;
    d.decided_by = by;
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(d.reason, sizeof d.reason, fmt, ap);
    va_end(ap);

    for (size_t i = 0; i < n && i < CQS_MAX_SIGNALS; i++) d.signals[i] = sigs[i];
    d.n_signals = (n < CQS_MAX_SIGNALS) ? n : CQS_MAX_SIGNALS;
    return d;
}

decision_t signals_decide(const signal_t *sigs, size_t n) {
    /* 1. 剔除过期信号 —— 防止"三天前的金叉今天才执行" */
    signal_t live[CQS_MAX_SIGNALS];
    size_t nl = 0;
    for (size_t i = 0; i < n && nl < CQS_MAX_SIGNALS; i++) {
        if (!sigs[i].expired) live[nl++] = sigs[i];
    }

    size_t n_trend = 0, bull = 0, bear = 0, neutral = 0, unknown = 0;
    size_t n_mom = 0, mom_bull = 0;

    for (size_t i = 0; i < nl; i++) {
        if (live[i].tier == TIER_TREND) {
            n_trend++;
            switch (live[i].state) {
                case TRI_BULL:    bull++;    break;
                case TRI_BEAR:    bear++;    break;
                case TRI_NEUTRAL: neutral++; break;
                default:          unknown++; break;
            }
        } else if (live[i].tier == TIER_MOMENTUM) {
            n_mom++;
            if (live[i].state == TRI_BULL) mom_bull++;
        }
    }

    /* 分支 2：趋势级缺失或含 UNKNOWN —— 不知道就不动，不猜 */
    if (n_trend == 0 || unknown > 0)
        return make(ACT_WAIT, false, TIER_TREND, live, nl,
                    "趋势级数据缺失，不做判断（不猜）");

    /* 分支 3：趋势级全空头 —— 否决，不看其他级 */
    if (bear == n_trend)
        return make(ACT_BLOCK, true, TIER_TREND, live, nl,
                    "趋势级全部空头，动量级信号不构成入场依据");

    /* 分支 4：趋势级含中性 —— 方向不明。中性不是"默认看多"。 */
    if (neutral > 0)
        return make(ACT_WAIT, true, TIER_TREND, live, nl,
                    "趋势级有 %zu/%zu 项为中性，方向不明，等待明确",
                    neutral, n_trend);

    /* 分支 5：趋势级多空混合 —— 方向不一致 */
    if (bull > 0 && bear > 0)
        return make(ACT_WAIT, true, TIER_TREND, live, nl,
                    "趋势级方向不一致（%zu 多 / %zu 空），等待明确", bull, bear);

    /* 分支 6：趋势级全多头 */
    if (bull == n_trend) {
        if (n_mom > 0 && mom_bull == n_mom)
            return make(ACT_ENTRY, true, TIER_MOMENTUM, live, nl,
                        "趋势与动量一致向上");
        return make(ACT_WAIT, true, TIER_MOMENTUM, live, nl,
                    "趋势已转多，但动量未确认（趋势级优先的固有代价：右侧入场）");
    }

    /* 分支 7：兜底 —— 正常不应到达 */
    return make(ACT_WAIT, false, TIER_TREND, live, nl,
                "未匹配规则分支（bull=%zu bear=%zu neutral=%zu n=%zu）—— 请检查信号状态取值",
                bull, bear, neutral, n_trend);
}

const char *signals_tradeoff_note(void) {
    return
        "取舍声明：趋势级优先规则用收益换确定性。\n"
        "  得到：不必在盘中现场裁决，杜绝情绪决策；避免在下跌趋势中接飞刀。\n"
        "  付出：趋势级是慢变量，从空头转多头时价格往往已涨一段，\n"
        "        系统会系统性右侧晚入场，牺牲部分启动段收益。\n"
        "  这是设计选择，不是缺陷。";
}
