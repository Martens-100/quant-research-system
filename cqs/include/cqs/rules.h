/* ============================================================================
 * cqs/rules.h — 制度约束引擎
 * ============================================================================
 * 职责：把 A 股 / 美股的制度约束显式建模，供风控与回测调用。
 *
 * 为什么必须是一等公民
 * --------------------
 * 风险预算公式  仓位 = 风险预算 / 止损宽度
 * 隐含假设「止损必然能在设定价位成交」。但在 A 股：
 *   T+1      -> 当日买入当日卖不掉
 *   涨跌停   -> 连续跌停时根本卖不出去，实际损失远超止损宽度
 *   停牌     -> 停牌期间无法交易，「跌破 N 日未收回」会被停牌日错误满足
 * 公式在制度约束下失效，且失效方向永远是坏的。
 *
 * 代码嵌合方式
 * ------------
 * **一份逻辑 + 两份数据**：CN / US 共用同一套算法实现，
 * 差异全部收敛到 market_profile_t 数据剖面里。
 * 不复制实现 —— 复制会导致两个市场各自演化出不一致的行为。
 *
 * 规则核验
 * --------
 * 全部规则经外部来源实时核验，核验日期见 CQS_RULES_VERIFIED_AT。
 * 禁止凭记忆修改本文件中的任何数值 —— 先核验再改。
 * ==========================================================================*/

#ifndef CQS_RULES_H
#define CQS_RULES_H

#include <stdbool.h>
#include <stddef.h>

/** 本文件全部规则的最后核验日期。修改任何规则前须更新此字段。 */
#define CQS_RULES_VERIFIED_AT "2026-10-08"

/* ---------------------------------------------------------------------------
 * 板块规则表：定义一次，展开为枚举 + 数据表
 * ---------------------------------------------------------------------------
 * 列：id, 名称, 涨跌幅%, 最小申报, 递增单位, 价格笼子
 * ------------------------------------------------------------------------- */

#define CQS_BOARD_TABLE(X)                                                     \
    X(CN_MAIN, "沪深主板", 10.0, 100, 100, false)                              \
    X(CN_STAR, "科创板",   20.0, 200,   1, true)                               \
    X(CN_GEM,  "创业板",   20.0, 100, 100, true)                               \
    X(CN_BSE,  "北交所",   30.0, 100,   1, true)                               \
    X(US,      "美股",      0.0,   1,   1, false)

typedef enum {
#define AS_E(id, label, lim, minq, incr, cage) BOARD_##id,
    CQS_BOARD_TABLE(AS_E)
#undef AS_E
    BOARD_COUNT
} board_t;

typedef struct {
    board_t     id;
    const char *label;
    double      limit_pct;      /**< 0 表示无涨跌幅限制 */
    int         min_qty;        /**< 单笔买入最小申报 */
    int         qty_increment;  /**< 申报递增单位 */
    bool        price_cage;     /**< 是否有价格笼子 */
} board_rule_t;

/** 取板块规则。id 越界返回 NULL。 */
const board_rule_t *board_rule(board_t b);
const char         *board_name(board_t b);

/** 按证券代码识别板块（688/689→科创，300/301→创业，8/4/920→北交所，其余主板） */
board_t board_classify(const char *symbol);

/* ---------------------------------------------------------------------------
 * ST 涨跌幅说明（核验记录，供文档与断言引用）
 * ---------------------------------------------------------------------------
 * 沪深主板 ST / *ST 涨跌幅限制**已由 5% 调整为 10%**，自 2026-07-06 起施行，
 * 与主板普通股一致。科创板 / 创业板 ST 股仍适用其板块的 ±20%。
 *
 * 核验来源（五源一致）：搜狐(2026-07-02)、新浪财经(2026-07-05)、
 * 东方财富(2026-07-06)、腾讯新闻(2026-07-06)、上海市地方金融监管局(2026-07-02)。
 *
 * 因此：主板 ST 与主板普通股的涨跌幅**相同**，无需分支处理。
 * 保留 is_st 参数是为了规则未来变化时可扩展，而非当前存在差异。
 * ------------------------------------------------------------------------- */

/* ---------------------------------------------------------------------------
 * 市场剖面：制度差异的唯一载体
 * ------------------------------------------------------------------------- */

/** 市场类型。显式区分，不用 t_plus_1 之类的字段当代理。 */
typedef enum {
    MKT_CN,   /**< A 股 */
    MKT_US    /**< 美股 */
} market_kind_t;

typedef struct {
    market_kind_t       kind;
    const char         *label;
    bool                t_plus_1;         /**< A 股 true */
    bool                has_price_limit;  /**< 美股 false */
    const board_rule_t *boards;           /**< 该市场板块表 */
    size_t              board_count;
    const char         *price_limit_note;
} market_profile_t;

/** 不透明句柄：调用方只通过下面的访问器使用，不直接触碰内部结构 */
typedef struct market_rules market_rules_t;

const market_rules_t *market_cn(void);
const market_rules_t *market_us(void);
const char           *mr_label(const market_rules_t *mr);
const market_profile_t *mr_profile(const market_rules_t *mr);

board_t mr_classify(const market_rules_t *mr, const char *symbol);
const board_rule_t *mr_board_of(const market_rules_t *mr, const char *symbol);

/** 涨跌停价。无限制时 *lo=0, *hi=+inf。四舍五入到 2 位，与交易所口径一致。 */
void mr_price_limits(const market_rules_t *mr, double prev_close,
                     const char *symbol, bool is_st,
                     double *lo, double *hi);

bool mr_is_limit_down(const market_rules_t *mr, double price, double prev_close,
                      const char *symbol, bool is_st);
bool mr_is_limit_up(const market_rules_t *mr, double price, double prev_close,
                    const char *symbol, bool is_st);

/** T+1 判定。用"第几天"的整数序号而非 time_t，避免自然日/交易日混淆。 */
bool mr_can_sell(const market_rules_t *mr, long buy_day, long sell_day);

/** 按板块最小申报单位向下取整。返回 0 表示无法建立该仓位。 */
int mr_round_qty(const market_rules_t *mr, int target, const char *symbol);

/**
 * 最坏损失重算（本模块核心）。
 *
 * 在「连续跌停无法成交」情形下重算真实损失。
 * consec_limit_down 由调用方提供 —— 是该标的的历史极端值，
 * 不同标的差异极大，无法由系统推定。
 */
double mr_worst_loss(const market_rules_t *mr, double entry, int qty,
                     const char *symbol, double stop, int consec_limit_down);

/** 放大倍数 = 最坏损失 / 计划损失。>1.5 时判定风险预算公式失效。 */
double mr_loss_amplification(const market_rules_t *mr, double entry, int qty,
                             const char *symbol, double stop, int consec_limit_down);

/** 用最坏损失反推仓位（制度修正版风险预算法）。 */
int mr_size_by_worst(const market_rules_t *mr, double capital, double risk_pct,
                     double entry, int qty_hint, const char *symbol,
                     double stop, int consec_limit_down);

/* ---------------------------------------------------------------------------
 * 交易费用
 * ---------------------------------------------------------------------------
 * 印花税已核验；佣金 / 过户费 / 滑点因人而异，必须由用户从对账单提供。
 * 未填齐时 round_trip_pct() 返回 -1 —— 调用方必须显式处理"成本未知"，
 * 而不是拿 0 当成本静默算下去。
 * ------------------------------------------------------------------------- */

typedef struct {
    double stamp_duty_sell_pct;  /**< 印花税，卖出单边。核验值 0.05 */
    double commission_pct;       /**< 佣金，双向。-1 = 未提供 */
    double commission_min;       /**< 单笔最低佣金。-1 = 未提供 */
    double transfer_fee_pct;     /**< 过户费，双向。-1 = 未提供 */
    double slippage_pct;         /**< 滑点假设。-1 = 未提供 */
} fee_schedule_t;

fee_schedule_t fee_default(void);
bool  fee_complete(const fee_schedule_t *f);
/** 往返成本占比（%）。未填齐返回 -1。 */
double fee_round_trip_pct(const fee_schedule_t *f);

/* ---------------------------------------------------------------------------
 * 停牌规则
 * ------------------------------------------------------------------------- */

typedef struct {
    bool count_suspended_days_in_monitor;   /**< 必须 false */
    bool first_day_after_resume_normal_stop;/**< 必须 false */
    int  reevaluate_window_days;
} suspension_rule_t;

suspension_rule_t suspension_default(void);

#endif /* CQS_RULES_H */
