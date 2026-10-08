/* ============================================================================
 * demo.js — 交互式分析演示
 * ============================================================================
 * 本文件是 cqs/src/ 下 C 实现的前端镜像。同一套规则、同一套优先级、
 * 同一套硬约束，只是运行在浏览器里。
 *
 * 镜像对应关系：
 *   assessResearch  <-> cqs/src/analysis.c  assess_research
 *   assessIndustry  <-> cqs/src/analysis.c  assess_industry
 *   assessCashflow  <-> cqs/src/analysis.c  assess_cashflow
 *   assessMoat      <-> cqs/src/analysis.c  assess_moat
 *   adviceDecide    <-> cqs/src/analysis.c  advice_decide
 *   worstLoss       <-> cqs/src/rules.c     mr_worst_loss
 *   decideSignals   <-> cqs/src/signal.c    signals_decide
 * ==========================================================================*/

'use strict';

/* ---------------------------------------------------------------------------
 * 领域模型
 * ------------------------------------------------------------------------- */

const clamp = (v) => Math.max(0, Math.min(100, v));
const gradeABCD = (s) => (s >= 80 ? 'A' : s >= 60 ? 'B' : s >= 40 ? 'C' : 'D');
const gradeMoat = (s) => (s >= 75 ? '宽' : s >= 50 ? '窄' : '无');

function mkAssessment(module) {
  return { module, score: 0, grade: 'D', findings: [], gaps: [], caveats: [] };
}

/** 缺口过多时拒绝给分。判据按检查项数，不写死阈值 —— 写死会让
 *  检查项少的模块带着 50 分"通过"。 */
function finalize(a, score, gradeFn, nChecks) {
  if (nChecks > 0 && a.gaps.length * 2 >= nChecks) {
    a.score = 0;
    a.grade = 'D';
    return;
  }
  a.score = clamp(score);
  a.grade = gradeFn(a.score);
}

/* ---------------------------------------------------------------------------
 * 四模块评估
 * ------------------------------------------------------------------------- */

function assessResearch(i) {
  const a = mkAssessment('研报分析');
  let s = 50;

  a.caveats.push('研报为非一手来源，引用需核实原文，不得与公司公告同等处理');

  if (i.targetPrice > 0 && i.currentPrice > 0) {
    const up = (i.targetPrice / i.currentPrice - 1) * 100;
    a.findings.push(`机构一致目标价 ${i.targetPrice.toFixed(2)} 元，较现价隐含 ${up >= 0 ? '+' : ''}${up.toFixed(2)}% 空间`);
    s += 10;
  } else {
    a.gaps.push('缺一致目标价或现价');
  }

  if (i.coverage > 0) {
    s += Math.min(20, i.coverage * 0.7);
    if (i.coverage < 5) a.caveats.push(`仅 ${i.coverage} 家机构覆盖，样本过小，一致预期代表性弱`);
  } else {
    a.gaps.push('缺覆盖机构数');
  }

  if (i.buyPct >= 0) {
    if (i.buyPct >= 95) {
      s -= 10;
      a.caveats.push(`买入/增持占比 ${i.buyPct.toFixed(0)}%，接近全体一致。该信号已失去区分度 —— 当所有人都看多时，'看多'不携带信息`);
    } else {
      s += 5;
    }
    a.findings.push(`近 6 个月买入/增持占比 ${i.buyPct.toFixed(0)}%`);
  } else {
    a.gaps.push('缺评级分布');
  }

  if (i.siteVisit) s += 15;
  else a.caveats.push('未见机构调研记录，研报可能基于公开信息推演而非一手验证');

  finalize(a, s, gradeABCD, 4);
  return a;
}

function assessIndustry(i) {
  const a = mkAssessment('行业分析');

  const STAGE = { '导入': 60, '成长': 90, '成熟': 65, '下行': 25 };
  const POS = { '龙头': 95, '细分龙头': 80, '追赶者': 55, '边缘': 25 };
  const INT = { low: 85, medium: 65, high: 40 };
  const MG = { up: 85, flat: 60, down: 30 };

  if (!i.industry) a.gaps.push('缺行业名称');
  else a.findings.push(`所属行业：${i.industry}`);
  if (!i.stage) a.gaps.push('缺生命周期阶段');
  if (!i.position) a.gaps.push('缺公司位置');
  if (!i.intensity) a.gaps.push('缺竞争强度');
  if (!i.marginTrend) a.gaps.push('缺行业毛利率趋势');

  const parts = [
    STAGE[i.stage] !== undefined ? STAGE[i.stage] : 50,
    POS[i.position] !== undefined ? POS[i.position] : 50,
    INT[i.intensity] !== undefined ? INT[i.intensity] : 50,
    MG[i.marginTrend] !== undefined ? MG[i.marginTrend] : 50,
  ];
  let base = parts.reduce((x, y) => x + y, 0) / 4;

  if (i.industryGrowth) base += Math.max(-15, Math.min(15, i.industryGrowth * 0.5));

  if (i.stage === '下行') a.findings.push('行业处下行阶段 —— 逆势标的胜率显著低于顺周期');
  if (i.intensity === 'high') a.findings.push('竞争强度高 —— 需壁垒模块证明公司能维持超额收益');
  if (i.position === '边缘') a.findings.push('公司处行业边缘位置 —— 难以享受行业红利');
  if (i.relStrength) a.findings.push(`近 60 日相对行业 ${i.relStrength >= 0 ? '+' : ''}${i.relStrength.toFixed(1)}%`);

  if (!i.industry) a.caveats.push('行业未识别，本模块结论不成立（同一指标跨行业不可比）');

  finalize(a, base, gradeABCD, 5);
  return a;
}

function assessCashflow(i) {
  const a = mkAssessment('现金流分析');
  let s = 50;

  if (i.ocf !== null && i.netProfit !== null && i.netProfit !== 0) {
    const ratio = i.ocf / i.netProfit;
    s += Math.max(-25, Math.min(25, (ratio - 0.8) * 50));
    if (ratio < 0.8) {
      a.findings.push(`经营现金流/归母净利 = ${ratio.toFixed(2)}（<0.8）—— 利润未有效转化为现金，需查应收与存货`);
    } else {
      a.findings.push(`经营现金流/归母净利 = ${ratio.toFixed(2)}，利润含金量正常`);
    }
  } else {
    a.gaps.push('缺经营现金流或归母净利');
  }

  if (i.ocf !== null && i.capex !== null) {
    const fcf = i.ocf - i.capex;
    if (fcf > 0) {
      s += 15;
      a.findings.push(`自由现金流为正（${fcf.toFixed(2)}）`);
    } else {
      s -= 10;
      a.findings.push(`自由现金流为负（${fcf.toFixed(2)}）—— 扩张期特征，但需确认资本开支能形成未来收益而非沉没成本`);
    }
  } else {
    a.gaps.push('缺资本开支');
  }

  if (i.interestDebt !== null && i.ocf !== null && i.ocf !== 0) {
    const yrs = i.interestDebt / i.ocf;
    if (yrs < 2) s += 10;
    else if (yrs > 4) s -= 10;
    if (yrs > 3) a.findings.push(`有息负债需 ${yrs.toFixed(1)} 年经营现金流偿还，偿债压力偏高`);
    else a.findings.push(`有息负债需 ${yrs.toFixed(1)} 年经营现金流偿还`);
  } else {
    a.gaps.push('缺有息负债');
  }

  finalize(a, s, gradeABCD, 3);
  return a;
}

/**
 * 壁垒模块 —— 两条硬约束
 *   ① moat_source 为空 -> 等级强制「无」，得分 10。说不出就是没有。
 *   ② ROIC < WACC      -> 得分硬上限 45。壁垒若真实，必体现在资本回报上。
 * 第 ② 条来自一次实测：纯加权计分会让类型分+证据分+持续性淹没 ROIC 的负分，
 * 得出「壁垒宽」而发现里写着「壁垒存疑」—— 自相矛盾。
 */
function assessMoat(i) {
  const a = mkAssessment('核心壁垒');

  if (!i.moatSource || !i.moatSource.trim()) {
    a.caveats.push('未能给出可命名的壁垒来源 —— 按本模块硬约束，无论其他字段如何，等级判定为「无」');
    a.score = 10;
    a.grade = '无';
    return a;
  }

  const TYPE = { '技术专利': 85, '规模成本': 80, '网络效应': 90, '牌照': 75, '品牌': 70, '客户粘性': 75, '无': 10 };
  let base = TYPE[i.moatType] !== undefined ? TYPE[i.moatType] : 40;

  a.findings.push(`壁垒类型：${i.moatType || '未分类'}；来源：${i.moatSource}`);

  if (i.evidence > 0) {
    base += Math.min(15, i.evidence * 5);
    a.findings.push(`提供 ${i.evidence} 条可验证证据`);
  } else {
    a.caveats.push('壁垒来源已命名但无可验证证据 —— 属叙述性主张，非事实');
  }

  let roicBelowWacc = false;
  if (i.roic !== null && i.wacc !== null) {
    const spread = i.roic - i.wacc;
    base += Math.max(-20, Math.min(20, spread * 2));
    if (spread > 5) a.findings.push(`ROIC 超出 WACC ${spread.toFixed(1)}pct —— 壁垒已转化为经济优势`);
    else if (spread > 0) a.findings.push(`ROIC 略高于 WACC ${spread.toFixed(1)}pct —— 优势有限`);
    else {
      a.findings.push(`ROIC 低于 WACC ${spread.toFixed(1)}pct —— 所谓技术优势未转化为资本回报，壁垒存疑`);
      roicBelowWacc = true;
    }
  } else {
    a.gaps.push('缺 ROIC 或 WACC，无法做最硬的壁垒判据');
  }

  if (i.durability >= 5) base += 10;
  else if (i.durability < 3) base -= 10;

  if (i.substitutes >= 3) base -= 15;

  let s = clamp(base);
  if (roicBelowWacc) s = Math.min(s, 45);   /* 硬约束 ② */

  a.score = s;
  a.grade = gradeMoat(s);
  return a;
}

/* ---------------------------------------------------------------------------
 * 四维综合与因子分层
 * ------------------------------------------------------------------------- */

/** 加权综合：现金流 30% · 壁垒 25% · 行业 25% · 研报 20%
 *  慢变量（现金流/壁垒）权重高于快变量（研报/行业），避免被短期预期牵着走。 */
function composite(b) {
  return b.research.score * 0.20 + b.industry.score * 0.25
       + b.cashflow.score * 0.30 + b.moat.score * 0.25;
}

function factorLayer(p) {
  if (p >= 80) return 'Q1';
  if (p >= 60) return 'Q2';
  if (p >= 40) return 'Q3';
  if (p >= 20) return 'Q4';
  return 'Q5';
}

/* ---------------------------------------------------------------------------
 * 制度约束：最坏损失与仓位
 * ------------------------------------------------------------------------- */

const BOARD_LIMIT = { main: 10.0, star: 20.0, gem: 20.0, bse: 30.0 };
const BOARD_MINQTY = { main: 100, star: 200, gem: 100, bse: 100 };
const BOARD_INCR = { main: 100, star: 1, gem: 100, bse: 1 };

function classifyBoard(symbol) {
  const c = String(symbol || '').trim();
  if (/^68[89]/.test(c)) return 'star';
  if (/^30[01]/.test(c)) return 'gem';
  if (/^[84]/.test(c) || /^920/.test(c)) return 'bse';
  return 'main';
}

/** 最坏损失：连续跌停无法成交时的实际损失。
 *  consec = 0 时退化为计划损失（不是 0）—— 这是修过的一个缺陷。 */
function worstLoss(entry, stop, qty, board, consec) {
  const planned = Math.max(0, (entry - stop) * qty);
  const limit = BOARD_LIMIT[board];
  if (!limit || consec <= 0) return planned;
  const floor = entry * Math.pow(1 - limit / 100, consec);
  return Math.max(planned, (entry - floor) * qty);
}

function roundQty(target, board) {
  const min = BOARD_MINQTY[board];
  const incr = BOARD_INCR[board];
  if (target < min) return 0;
  return target - ((target - min) % incr);
}

function sizing(capital, riskPct, entry, stop, board, consec) {
  const budget = capital * riskPct / 100;
  const perShareStd = entry - stop;
  const perShareWc = worstLoss(entry, stop, 1, board, consec);
  const stdQty = perShareStd > 0 ? roundQty(Math.floor(budget / perShareStd), board) : 0;
  const wcQty = perShareWc > 0 ? roundQty(Math.floor(budget / perShareWc), board) : 0;
  return {
    standard: { qty: stdQty, pct: stdQty * entry / capital * 100 },
    worst: { qty: wcQty, pct: wcQty * entry / capital * 100 },
    amplification: perShareStd > 0 ? perShareWc / perShareStd : 1,
    budget,
  };
}

/* ---------------------------------------------------------------------------
 * 三级信号决策树
 * ------------------------------------------------------------------------- */

/** 按趋势级优先规则产出决策。
 *  分支 3/4 是回归测试的固化对象：早期版本漏了它们，
 *  「一中性一多头」「一多一空」会落到兜底分支返回"未匹配"。 */
function decideSignals(sigs) {
  const trend = sigs.filter((s) => s.tier === 'trend');
  const mom = sigs.filter((s) => s.tier === 'momentum');

  if (trend.length === 0 || trend.some((s) => s.state === 'unknown')) {
    return { action: 'wait', by: null, reason: '趋势级数据缺失，不做判断（不猜）' };
  }

  const bull = trend.filter((s) => s.state === 'bull').length;
  const bear = trend.filter((s) => s.state === 'bear').length;
  const neutral = trend.filter((s) => s.state === 'neutral').length;

  if (bear === trend.length) {
    return { action: 'block', by: 'trend', reason: '趋势级全部空头，动量级信号不构成入场依据' };
  }
  if (neutral > 0) {
    return { action: 'wait', by: 'trend', reason: `趋势级有 ${neutral}/${trend.length} 项为中性，方向不明，等待明确` };
  }
  if (bull > 0 && bear > 0) {
    return { action: 'wait', by: 'trend', reason: `趋势级方向不一致（${bull} 多 / ${bear} 空），等待明确` };
  }
  if (bull === trend.length) {
    const momBull = mom.filter((s) => s.state === 'bull').length;
    if (mom.length > 0 && momBull === mom.length) {
      return { action: 'entry', by: 'momentum', reason: '趋势与动量一致向上' };
    }
    return { action: 'wait', by: 'momentum', reason: '趋势已转多，但动量未确认（趋势级优先的固有代价：右侧入场）' };
  }
  return { action: 'wait', by: null, reason: '未匹配规则分支 —— 请检查信号状态取值' };
}

/* ---------------------------------------------------------------------------
 * 建议映射引擎
 * ---------------------------------------------------------------------------
 * 优先级（决定权从高到低）：
 *   ① 逻辑止损   ② 量化 BLOCK/EXIT   ③ 监控 CRITICAL
 *   ④ 多因子分层  ⑤ 量化 ENTRY + 监控降级
 * ------------------------------------------------------------------------- */

const MON_ORDER = { info: 0, warning: 1, critical: 2 };

function maxMonLevel(mons) {
  let best = 'info';
  mons.forEach((m) => { if (MON_ORDER[m.level] > MON_ORDER[best]) best = m.level; });
  return best;
}

function adviceDecide(opt) {
  const { quantAction, quantReason, layer, monitors, holding, logicFalsified, basePct } = opt;
  const chain = [];
  const blocked = [];
  const mlevel = maxMonLevel(monitors);

  const out = (advice, pct, by) => ({ advice, positionPct: pct, decidedBy: by, chain, blocked });

  chain.push(`输入: quant=${quantAction} factor=${layer} monitor=${mlevel} holding=${holding}`);

  /* ---- ① 逻辑止损 ---- */
  if (logicFalsified > 0) {
    chain.push('① 逻辑止损触发 —— 优先级最高，跳过其余判定');
    blocked.push(`${logicFalsified} 条买入假设被证伪`);
    return out(logicFalsified >= 2 ? '清仓' : '降仓',
               logicFalsified >= 2 ? 0 : basePct / 2,
               '逻辑止损（优先级 ①）');
  }
  chain.push('① 逻辑止损：未触发');

  /* ---- 持仓分支 ---- */
  if (holding) {
    if (quantAction === 'exit') {
      chain.push('② 量化 EXIT —— 清仓');
      return out('清仓', 0, '量化信号（优先级 ②）');
    }
    if (quantAction === 'block') {
      chain.push('② 量化 BLOCK —— 持仓状态下减至观察仓');
      return out('降仓', basePct / 3, '量化信号（优先级 ②）');
    }
    if (mlevel === 'critical') {
      chain.push('③ 监控 CRITICAL —— 转入风控观察，禁止加仓');
      blocked.push('监控 CRITICAL：禁止一切加仓动作');
      return out('持有', basePct, '监控信号（优先级 ③）');
    }
    if (layer === 'Q4' || layer === 'Q5') {
      chain.push(`④ 因子分层 ${layer} —— 相对位置落后，降仓 1/3`);
      return out('降仓', basePct * 2 / 3, '多因子信号（优先级 ④）');
    }
    chain.push('④ 因子分层可接受 —— 持有');
    return out('持有', basePct, '维持');
  }

  /* ---- 空仓分支 ---- */
  if (quantAction === 'block') {
    chain.push('② 量化 BLOCK —— 不入场');
    blocked.push(quantReason);
    return out('不入场', 0, '量化信号（优先级 ②）');
  }
  if (quantAction === 'wait') {
    chain.push('② 量化 WAIT —— 观察');
    blocked.push(quantReason);
    return out('观察', 0, '量化信号（优先级 ②）');
  }
  chain.push('② 量化 ENTRY —— 通过，进入仓位判定');

  /* ---- ③ 监控 CRITICAL ---- */
  if (mlevel === 'critical') {
    chain.push('③ 监控 CRITICAL —— 暂缓建仓');
    monitors.filter((m) => m.level === 'critical').forEach((m) => blocked.push(m.trigger));
    return out('暂缓建仓', 0, '监控信号（优先级 ③）');
  }
  chain.push('③ 监控：无 CRITICAL');

  /* ---- ④ 因子分层决定仓位 ---- */
  const top = layer === 'Q1' || layer === 'Q2';
  let pct;
  if (top) {
    pct = basePct;
    chain.push(`④ 因子 ${layer}（前 40%）—— 标准仓位`);
  } else if (layer === 'Q3') {
    pct = basePct / 4;
    chain.push('④ 因子 Q3（中位）—— 试仓 1/4');
  } else {
    chain.push(`④ 因子 ${layer}（后 40%）—— 因子不支持，不入场`);
    blocked.push(`因子分层 ${layer} 位于后 40%，即使量化信号 ENTRY 也不入场`);
    return out('不入场', 0, '多因子信号（优先级 ④）');
  }

  /* ---- ⑤ 监控 WARNING 减半 ---- */
  if (mlevel === 'warning') {
    chain.push('⑤ 监控 WARNING —— 仓位减半');
    blocked.push('监控 WARNING：存在需观察的事件');
    return out(top ? '建仓（半仓）' : '试仓', pct / 2, '监控信号（优先级 ⑤ 降级）');
  }

  chain.push('⑤ 监控 INFO —— 不降级');
  return out(top ? '建仓' : '试仓', pct, '量化 + 因子（优先级 ②④）');
}

/* ---------------------------------------------------------------------------
 * 复盘
 * ------------------------------------------------------------------------- */

/** 复盘分两种情形，因为修正方向完全相反：
 *  执行了但结果不好 -> 可能计划错（调参数）
 *  该执行时没执行   -> 可能手软（调执行机制） */
function runReview(opt) {
  const { outcome, advice, mandatory, noExecReason, plannedPrice, actualPrice,
          plannedQty, actualQty, priceT20, hypotheses, dataGaps, emotionBad, emotionTotal } = opt;
  const dims = [];
  const corrections = [];
  let attribution = '';
  let takeOver = false;

  /* D1 信号一致性 */
  const adviceIsIdle = ['不入场', '观察', '暂缓建仓'].includes(advice);
  if (outcome === 'not_executed') {
    dims.push({
      code: 'D1', name: '信号一致性',
      verdict: adviceIsIdle ? '一致（建议本就是不动）' : '不一致（建议动作未执行）',
      score: adviceIsIdle ? 100 : 0,
    });
    if (!adviceIsIdle) corrections.push('[D1] 见 B1 未执行归因');
  } else {
    dims.push({ code: 'D1', name: '信号一致性', verdict: '已执行', score: 100 });
  }

  /* D2 逻辑验证 */
  if (hypotheses && hypotheses.length) {
    const v = hypotheses.filter((h) => h.status === 'verified').length;
    const f = hypotheses.filter((h) => h.status === 'falsified').length;
    const u = hypotheses.length - v - f;
    const score = v / hypotheses.length * 100;
    dims.push({ code: 'D2', name: '逻辑验证', verdict: `${f} 条证伪 / ${v} 条验证 / ${u} 条待验证`, score });
    if (f > 0) corrections.push('[D2] 触发逻辑止损 —— 优先级高于价格止损');
  } else {
    dims.push({ code: 'D2', name: '逻辑验证', verdict: '无假设可验证', score: null });
    corrections.push('[D2] 补录买入时的核心假设');
  }

  /* D3 数据质量 */
  const dg = dataGaps || 0;
  dims.push({
    code: 'D3', name: '数据质量',
    verdict: dg === 0 ? '无缺口' : `${dg} 项缺口`,
    score: Math.max(0, 100 - dg * 20),
  });
  if (dg > 0) corrections.push('[D3] 缺口字段不得进入决策；补充取数后重跑分析');

  /* 情形专属维度 */
  if (outcome === 'executed') {
    if (plannedPrice && actualPrice) {
      const dev = Math.abs(actualPrice - plannedPrice) / plannedPrice * 100;
      dims.push({
        code: 'A1', name: '执行偏差',
        verdict: dev <= 1 ? '偏差可接受' : dev <= 3 ? '偏差偏大' : '偏差过大',
        score: Math.max(0, 100 - dev * 10),
      });
      if (dev > 1) corrections.push('[A1] 复核下单方式（限价单 vs 市价单）');
    } else {
      dims.push({ code: 'A1', name: '执行偏差', verdict: '缺计划价或成交价', score: null });
    }
    if (plannedQty && actualQty) {
      const ratio = actualQty / plannedQty;
      const over = ratio > 1.1, under = ratio < 0.9;
      dims.push({
        code: 'A2', name: '仓位合规',
        verdict: over ? '实际仓位超建议' : under ? '实际仓位低于建议' : '与建议一致',
        score: over ? 60 : under ? 70 : 100,
      });
      if (over) corrections.push('[A2] 超配会放大风险敞口，复核是否偏离风险预算');
    } else {
      dims.push({ code: 'A2', name: '仓位合规', verdict: '数据不足', score: null });
    }
  } else {
    /* B1 未执行归因 —— 本模块最关键 */
    const REASON = {
      signal_not_triggered: { v: '信号未触发', s: 100, c: '' },
      triggered_not_acted: { v: '信号已触发但未执行', s: 0,
        c: '[B1] 若理由是长期逻辑，追问：这个理由在信号触发日成立，在下一次触发时是否也成立？永远成立的理由等于没有理由' },
      condition_unmet: { v: '执行条件不满足', s: 50,
        c: '[B1] 修执行条件（增加不可成交情形的最坏损失重算），而非扣纪律分' },
      missed_monitoring: { v: '未跟踪标的', s: 30, c: '[B1] 把标的加入财报日历与日频监控' },
    };
    const r = REASON[noExecReason] || { v: '未提供原因', s: null, c: '[B1] 必须归因' };
    dims.push({ code: 'B1', name: '未执行归因', verdict: r.v, score: r.s });
    if (r.c) corrections.push(r.c);

    /* B3 纪律扣分：客观条件不允许时不构成纪律问题 */
    if (noExecReason === 'condition_unmet') {
      dims.push({ code: 'B3', name: '纪律扣分', verdict: '不适用（客观条件不允许）', score: null });
      corrections.push('[B3] 改用最坏损失重算降低此类标的仓位上限');
    } else if (mandatory) {
      dims.push({ code: 'B3', name: '纪律扣分', verdict: '硬性动作未执行 → 归零', score: 0 });
      corrections.push('[B3] 升级为系统接管下单权限');
    } else {
      dims.push({ code: 'B3', name: '纪律扣分', verdict: '非硬性动作，不扣分', score: 100 });
    }

    /* B2 信号有效性 */
    if (plannedPrice && priceT20) {
      const r20 = (priceT20 / plannedPrice - 1) * 100;
      const wantExit = ['清仓', '降仓', '不入场'].includes(advice);
      const fell = r20 < 0;
      const impossible = noExecReason === 'condition_unmet';
      let verdict, score, corr = '';
      if (impossible) {
        verdict = '信号正确，但客观上无法执行';
        score = 50;
        corr = '[B2] 用最坏损失重算降低此类标的仓位上限';
      } else if (wantExit && fell) {
        verdict = '信号正确，未执行是错的'; score = 0;
        corr = '[B2] 强化执行机制（考虑系统接管下单权限）';
      } else if (wantExit && !fell) {
        verdict = '信号偏保守，未执行反而更好'; score = 80;
        corr = '[B2] 复核止损宽度与信号阈值是否过紧';
      } else {
        verdict = '信号正确，未执行错过机会'; score = 0;
        corr = '[B2] 强化执行机制';
      }
      dims.push({ code: 'B2', name: '信号有效性', verdict, score });
      if (corr) corrections.push(corr);
    } else {
      dims.push({ code: 'B2', name: '信号有效性', verdict: '窗口未到', score: null });
      corrections.push('[B2] T+20 到期后补跑');
    }
  }

  /* D4 事后验证 */
  const ref = outcome === 'executed' ? actualPrice : plannedPrice;
  if (ref && priceT20) {
    const r20 = (priceT20 / ref - 1) * 100;
    const up = r20 > 0;
    dims.push({
      code: 'D4', name: '事后验证',
      verdict: outcome === 'executed'
        ? (up ? '执行方向事后看偏错（价格反向）' : '执行方向事后看正确（价格同向）')
        : (up ? '未操作事后看偏错（错过了上涨）' : '未操作事后看正确（避开了下跌）'),
      score: null,
      detail: `T+20：${ref.toFixed(2)} → ${priceT20.toFixed(2)}（${r20 >= 0 ? '+' : ''}${r20.toFixed(2)}%）`,
    });
  } else {
    dims.push({ code: 'D4', name: '事后验证', verdict: '窗口未到或数据缺失', score: null });
  }

  /* E1 情绪 */
  if (emotionTotal > 0) {
    const ratio = emotionBad / emotionTotal;
    dims.push({
      code: 'E1', name: '情绪标签',
      verdict: ratio > 0.5 ? '情绪主导操作' : '情绪影响可控',
      score: ratio > 0.5 ? 30 : 80,
    });
    if (ratio > 0.5) corrections.push('[E1] 建议暂停交易，待情绪平复');
  } else {
    dims.push({ code: 'E1', name: '情绪标签', verdict: '无记录', score: null });
    corrections.push('[E1] 每次手动加减仓时打标签：贪婪/恐惧/焦虑/不甘/从众');
  }

  /* 归因 —— 必须区分"计划错"与"手软"，两者修正方向相反 */
  if (outcome === 'not_executed' && noExecReason === 'triggered_not_acted') {
    attribution = '【权限问题】信号已触发但未执行 —— 修正方向是执行机制，不是参数';
    takeOver = true;
  } else if (outcome === 'not_executed' && noExecReason === 'condition_unmet') {
    attribution = '【设计问题】执行条件不满足 —— 修正方向是执行条件，不是纪律';
  } else if (outcome === 'not_executed' && noExecReason === 'signal_not_triggered') {
    attribution = '【正常】信号未触发，未操作是正确行为';
  } else if (outcome === 'executed') {
    const a1 = dims.find((d) => d.code === 'A1');
    attribution = (a1 && a1.score !== null && a1.score < 90)
      ? '【参数问题】已执行但偏差大 —— 修正方向是下单方式与参数'
      : '【正常】执行偏差在可接受范围';
  } else {
    attribution = '【正常】本轮各维度未见显著异常';
  }

  /* 去重 */
  const seen = new Set();
  const deduped = [];
  corrections.forEach((c) => {
    const key = c.split('] ').slice(1).join('] ');
    if (!seen.has(key)) { seen.add(key); deduped.push(c); }
  });

  if (takeOver) deduped.unshift('[最高优先] 未执行硬性动作 → 系统接管下单权限');

  return { dims, attribution, corrections: deduped, takeOver };
}

/* ---------------------------------------------------------------------------
 * 预设标的
 * ------------------------------------------------------------------------- */

const PRESETS = {
  '002422': {
    name: '科伦药业', symbol: '002422', board: 'main',
    currentPrice: 39.26, entry: 45.44, stop: 40.00, consec: 3,
    research: { targetPrice: 52.10, coverage: 29, buyPct: 100, siteVisit: true },
    industry: { industry: '医药生物-化学制药', stage: '成长', position: '细分龙头',
                intensity: 'high', marginTrend: 'down', industryGrowth: 3.5, relStrength: -6.5 },
    cashflow: { ocf: 23.41, netProfit: 11.28, capex: 23.90, interestDebt: 44.18 },
    moat: { moatType: '技术专利',
            moatSource: 'ADC 平台 OptiDC 与默沙东的 17 项全球 III 期临床绑定',
            durability: 5, roic: 5.94, wacc: 9.0, substitutes: 2, evidence: 2 },
  },
  '002487': {
    name: '大金重工', symbol: '002487', board: 'main',
    currentPrice: 48.10, entry: 48.10, stop: 43.00, consec: 2,
    research: { targetPrice: 56.00, coverage: 18, buyPct: 88, siteVisit: true },
    industry: { industry: '电力设备-风电设备', stage: '成长', position: '细分龙头',
                intensity: 'medium', marginTrend: 'up', industryGrowth: 18.0, relStrength: 12.4 },
    cashflow: { ocf: 26.50, netProfit: 15.20, capex: 18.00, interestDebt: 20.00 },
    moat: { moatType: '规模成本',
            moatSource: '欧洲单桩市占率 29.1% + 自有远洋船队与欧洲风电母港布局',
            durability: 6, roic: 14.8, wacc: 9.5, substitutes: 2, evidence: 3 },
  },
};

/* ---------------------------------------------------------------------------
 * 渲染
 * ------------------------------------------------------------------------- */

const $ = (id) => document.getElementById(id);
const num = (id) => {
  const v = $(id).value;
  return v === '' ? null : Number(v);
};
const str = (id) => {
  const v = $(id).value;
  return v === '' ? null : v;
};

function scoreClass(s) {
  if (s >= 75) return 'good';
  if (s >= 50) return 'mid';
  if (s >= 30) return 'warn';
  return 'bad';
}

function renderAssessment(a) {
  const pct = a.score;
  return `
    <div class="assess">
      <div class="assess-head">
        <span class="assess-name">${a.module}</span>
        <span class="assess-grade g-${scoreClass(pct)}">${a.grade}</span>
        <span class="assess-score ${scoreClass(pct)}">${pct.toFixed(0)}<small>/100</small></span>
      </div>
      <div class="bar"><i class="${scoreClass(pct)}" style="width:${pct}%"></i></div>
      ${a.findings.length ? `<div class="assess-block"><b>关键发现</b><ul>${a.findings.map((f) => `<li>${f}</li>`).join('')}</ul></div>` : ''}
      ${a.gaps.length ? `<div class="assess-block gap"><b>数据缺口</b><ul>${a.gaps.map((f) => `<li>${f}</li>`).join('')}</ul></div>` : ''}
      ${a.caveats.length ? `<div class="assess-block caveat"><b>警示</b><ul>${a.caveats.map((f) => `<li>${f}</li>`).join('')}</ul></div>` : ''}
    </div>`;
}

function analyze() {
  const board = classifyBoard($('symbol').value || '002422');

  const research = assessResearch({
    targetPrice: num('r_target') || 0,
    currentPrice: num('currentPrice') || 0,
    coverage: num('r_coverage') || 0,
    buyPct: num('r_buyPct') === null ? -1 : num('r_buyPct'),
    siteVisit: $('r_siteVisit').checked,
  });
  const industry = assessIndustry({
    industry: str('i_industry'),
    stage: str('i_stage'),
    position: str('i_position'),
    intensity: str('i_intensity'),
    marginTrend: str('i_margin'),
    industryGrowth: num('i_growth') || 0,
    relStrength: num('i_rel') || 0,
  });
  const cashflow = assessCashflow({
    ocf: num('c_ocf'), netProfit: num('c_np'),
    capex: num('c_capex'), interestDebt: num('c_debt'),
  });
  const moat = assessMoat({
    moatType: str('m_type'), moatSource: str('m_source'),
    durability: num('m_dur') || 0,
    roic: num('m_roic'), wacc: num('m_wacc'),
    substitutes: num('m_sub') || 0, evidence: num('m_ev') || 0,
  });

  const bundle = { research, industry, cashflow, moat };
  const comp = composite(bundle);
  const layer = factorLayer(comp);

  /* ---- 三类信号 ---- */
  const entry = num('entry') || 0;
  const stop = num('stop') || 0;
  const consec = num('consec') || 0;
  const capital = num('capital') || 1000000;
  const riskPct = num('riskPct') || 1;

  const sz = sizing(capital, riskPct, entry, stop, board, consec);

  /* 简化版三级信号：用四模块结果推演，而非真实 K 线。
   * 真实实现见 cqs/src/signal.c —— 需要 250 根 K 线才能算趋势级。 */
  const quantAction = $('quantAction').value;
  const quantReason = {
    entry: '趋势与动量一致向上',
    wait: '趋势已转多，但动量未确认（趋势级优先的固有代价：右侧入场）',
    block: '趋势级全部空头，动量级信号不构成入场依据',
    exit: '趋势破位，触发退出',
  }[quantAction];

  const monitors = [];
  if ($('mon_logic').checked) monitors.push({ level: 'critical', trigger: '买入假设被证伪', window: '即时', implication: '触发逻辑止损，优先级高于价格止损' });
  if ($('mon_gap').checked) monitors.push({ level: 'warning', trigger: '存在数据缺口', window: '本轮', implication: '缺口字段不得进入决策' });
  if ($('mon_report').checked) monitors.push({ level: 'warning', trigger: '财报临近披露', window: '3 天', implication: '禁止新增仓位' });
  if ($('mon_calib').checked) monitors.push({ level: 'info', trigger: '拥挤度阈值待校准', window: '长期', implication: '该维度暂不出结论' });

  const logicFalsified = Number($('logicFalsified').value) || 0;
  const holding = $('holding').checked;

  const adv = adviceDecide({
    quantAction, quantReason, layer, monitors, holding, logicFalsified,
    basePct: sz.worst.pct,
  });

  /* ---- 渲染 ---- */
  $('out-assess').innerHTML = [research, industry, cashflow, moat]
    .map(renderAssessment).join('');

  $('out-composite').innerHTML = `
    <div class="kpi-row">
      <div class="kpi"><div class="n">${comp.toFixed(1)}</div><div class="l">四维综合</div></div>
      <div class="kpi"><div class="n">${layer}</div><div class="l">因子分层</div></div>
      <div class="kpi"><div class="n">${sz.amplification.toFixed(2)}x</div><div class="l">最坏损失放大</div></div>
      <div class="kpi"><div class="n">${sz.worst.pct.toFixed(2)}%</div><div class="l">制度修正仓位</div></div>
    </div>
    <div class="note">综合权重：现金流 30% · 壁垒 25% · 行业 25% · 研报 20%。
    慢变量（现金流/壁垒）权重高于快变量（研报/行业），避免被短期预期牵着走。</div>`;

  $('out-signals').innerHTML = `
    <div class="sig-group">
      <div class="sig-title">① 监控信号 <span class="sig-sub">回答「有什么变化需要我知道」—— 不直接产生买卖动作</span></div>
      ${monitors.length ? monitors.map((m) => `
        <div class="sig-row lv-${m.level}">
          <span class="lv">${m.level}</span>
          <span class="tr">${m.trigger}</span>
          <span class="wd">${m.window}</span>
          <span class="im">${m.implication}</span>
        </div>`).join('') : '<div class="empty">（无）</div>'}
    </div>
    <div class="sig-group">
      <div class="sig-title">② 量化信号 <span class="sig-sub">回答「规则说该做什么」—— 可执行、有明确价位</span></div>
      <div class="sig-row quant">
        <span class="lv">${quantAction}</span>
        <span class="tr">${quantReason}</span>
      </div>
      <div class="sig-detail">
        止损位 ${stop.toFixed(2)} · 标准仓位 ${sz.standard.pct.toFixed(2)}%（${sz.standard.qty} 股）
        · 制度修正 ${sz.worst.pct.toFixed(2)}%（${sz.worst.qty} 股）
      </div>
    </div>
    <div class="sig-group">
      <div class="sig-title">③ 多因子信号 <span class="sig-sub">回答「在同类里排第几」—— 只影响仓位大小，不影响方向</span></div>
      <div class="sig-row factor">
        <span class="lv">${layer}</span>
        <span class="tr">综合 ${comp.toFixed(1)} / 100</span>
      </div>
    </div>`;

  $('out-advice').innerHTML = `
    <div class="advice-box">
      <div class="advice-main">${adv.advice}</div>
      <div class="advice-meta">建议仓位 <b>${adv.positionPct.toFixed(2)}%</b> · 决定者 ${adv.decidedBy}</div>
    </div>
    ${adv.blocked.length ? `<div class="blocked"><b>否决 / 约束</b><ul>${adv.blocked.map((b) => `<li>${b}</li>`).join('')}</ul></div>` : ''}
    <div class="chain"><b>判定链（可审计）</b><ol>${adv.chain.map((c) => `<li>${c}</li>`).join('')}</ol></div>`;

  /* 复盘 */
  const hypotheses = [];
  if ($('h1s').value) hypotheses.push({ statement: '假设一', status: $('h1s').value });
  if ($('h2s').value) hypotheses.push({ statement: '假设二', status: $('h2s').value });
  if ($('h3s').value) hypotheses.push({ statement: '假设三', status: $('h3s').value });

  const rev = runReview({
    outcome: $('outcome').value,
    advice: adv.advice,
    mandatory: $('mandatory').checked,
    noExecReason: $('noExecReason').value,
    plannedPrice: num('plannedPrice'),
    actualPrice: num('actualPrice'),
    plannedQty: num('plannedQty'),
    actualQty: num('actualQty'),
    priceT20: num('priceT20'),
    hypotheses,
    dataGaps: (research.gaps.length + industry.gaps.length + cashflow.gaps.length + moat.gaps.length),
    emotionBad: num('emotionBad') || 0,
    emotionTotal: num('emotionTotal') || 0,
  });

  $('out-review').innerHTML = `
    <div class="rev-attr ${rev.takeOver ? 'bad' : ''}">归因结论：${rev.attribution}</div>
    <table class="rev-table">
      <thead><tr><th>维度</th><th>判定</th><th>得分</th></tr></thead>
      <tbody>${rev.dims.map((d) => `
        <tr>
          <td><code>${d.code}</code> ${d.name}</td>
          <td>${d.verdict}${d.detail ? `<div class="dim-detail">${d.detail}</div>` : ''}</td>
          <td>${d.score === null ? '—' : d.score.toFixed(0)}</td>
        </tr>`).join('')}</tbody>
    </table>
    ${rev.corrections.length ? `<div class="corr"><b>修正动作</b><ul>${rev.corrections.map((c) => `<li>${c}</li>`).join('')}</ul></div>` : ''}`;
}

/* ---------------------------------------------------------------------------
 * 预设装载
 * ------------------------------------------------------------------------- */

function loadPreset(key) {
  const p = PRESETS[key];
  if (!p) return;
  $('stockName').value = p.name;
  $('symbol').value = p.symbol;
  $('currentPrice').value = p.currentPrice;
  $('entry').value = p.entry;
  $('stop').value = p.stop;
  $('consec').value = p.consec;

  $('r_target').value = p.research.targetPrice;
  $('r_coverage').value = p.research.coverage;
  $('r_buyPct').value = p.research.buyPct;
  $('r_siteVisit').checked = p.research.siteVisit;

  $('i_industry').value = p.industry.industry;
  $('i_stage').value = p.industry.stage;
  $('i_position').value = p.industry.position;
  $('i_intensity').value = p.industry.intensity;
  $('i_margin').value = p.industry.marginTrend;
  $('i_growth').value = p.industry.industryGrowth;
  $('i_rel').value = p.industry.relStrength;

  $('c_ocf').value = p.cashflow.ocf;
  $('c_np').value = p.cashflow.netProfit;
  $('c_capex').value = p.cashflow.capex;
  $('c_debt').value = p.cashflow.interestDebt;

  $('m_type').value = p.moat.moatType;
  $('m_source').value = p.moat.moatSource;
  $('m_dur').value = p.moat.durability;
  $('m_roic').value = p.moat.roic;
  $('m_wacc').value = p.moat.wacc;
  $('m_sub').value = p.moat.substitutes;
  $('m_ev').value = p.moat.evidence;

  analyze();
}

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('[data-preset]').forEach((btn) => {
    btn.addEventListener('click', () => loadPreset(btn.dataset.preset));
  });
  document.querySelectorAll('input, select').forEach((el) => {
    el.addEventListener('change', analyze);
    el.addEventListener('input', analyze);
  });
  loadPreset('002422');
});
