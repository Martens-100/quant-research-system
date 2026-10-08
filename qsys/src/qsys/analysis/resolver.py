"""
股票名称解析器
==============

手动输入的入口。为什么需要它
----------------------------
"手动输入股票名称"看起来简单，但有几个必须处理的坑：

  1. **同名与近似名**：输入"科伦"可能指科伦药业(002422)或科伦博泰(06990.HK)
  2. **简称与全称**：'贵州茅台' vs '茅台' vs '600519'
  3. **A股/港股同名**：同一集团在不同市场上市
  4. **错别字**：'科仑药业'

设计原则：**宁可要求确认，不要静默猜错。**
解析结果带 confidence，低于阈值时必须让用户确认 ——
猜错标的比报错严重得多，因为后续所有分析都会基于错误的公司。
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Optional


@dataclass
class Resolved:
    """解析结果。"""

    input_text: str
    code: Optional[str]
    name: Optional[str]
    market: Optional[str]
    confidence: float
    candidates: list[tuple[str, str, float]] = None  # type: ignore[assignment]
    warning: str = ""

    def __post_init__(self) -> None:
        if self.candidates is None:
            self.candidates = []

    @property
    def is_confident(self) -> bool:
        return self.confidence >= 0.85 and self.code is not None

    def explain(self) -> str:
        lines = [f"输入: {self.input_text}"]
        if self.code:
            lines.append(f"解析: {self.code} {self.name} ({self.market})  置信度 {self.confidence:.0%}")
        else:
            lines.append("解析: 未匹配到标的")
        if self.warning:
            lines.append(f"⚠ {self.warning}")
        if self.candidates and not self.is_confident:
            lines.append("候选:")
            for c, n, s in self.candidates[:5]:
                lines.append(f"  {c} {n}  ({s:.0%})")
        return "\n".join(lines)


# 内置基础映射表。生产环境应替换为完整证券列表（约 5000+ 只 A 股）。
_BUILTIN: list[tuple[str, str, str]] = [
    ("002422", "科伦药业", "深市主板"),
    ("6990.HK", "科伦博泰生物", "港股"),
    ("301301", "川宁生物", "创业板"),
    ("600519", "贵州茅台", "沪市主板"),
    ("300750", "宁德时代", "创业板"),
    ("688111", "金山办公", "科创板"),
    ("NVDA.US", "英伟达", "美股"),
]


class Resolver:
    """股票名称解析器。"""

    def __init__(self, table: Optional[list[tuple[str, str, str]]] = None) -> None:
        self.table = table if table is not None else list(_BUILTIN)

    @staticmethod
    def _sim(a: str, b: str) -> float:
        return SequenceMatcher(None, a, b).ratio()

    def resolve(self, text: str) -> Resolved:
        """解析用户输入。"""
        raw = text.strip()
        if not raw:
            return Resolved(raw, None, None, None, 0.0, [], "输入为空")

        # 1) 精确代码匹配
        for code, name, market in self.table:
            if raw.upper() == code.upper():
                return Resolved(raw, code, name, market, 1.0)

        # 2) 精确名称匹配
        for code, name, market in self.table:
            if raw == name:
                return Resolved(raw, code, name, market, 1.0)

        # 3) 模糊匹配
        scored: list[tuple[str, str, str, float]] = []
        for code, name, market in self.table:
            # 子串包含（"科伦" 命中 "科伦药业"）
            if raw in name or name in raw:
                base = 0.90
                # 命中多个同名前缀时降级，要求用户确认
                base -= 0.05 * len([1 for _, n, _ in self.table if raw in n]) 
                scored.append((code, name, market, max(0.5, base)))
            else:
                s = self._sim(raw, name)
                if s >= 0.5:
                    scored.append((code, name, market, s))

        if not scored:
            return Resolved(raw, None, None, None, 0.0, [],
                            "未匹配到任何标的 —— 请核对名称，或直接输入 6 位代码")

        scored.sort(key=lambda x: -x[3])
        top = scored[0]
        cands = [(c, n, s) for c, n, _, s in scored]

        warning = ""
        if len(scored) > 1 and scored[1][3] >= top[3] - 0.15:
            warning = (
                f"存在 {len(scored)} 个近似候选，置信度接近 —— "
                f"必须人工确认，猜错标的比报错严重得多"
            )
        elif top[3] < 0.85:
            warning = "匹配置信度偏低，请确认"

        return Resolved(raw, top[0], top[1], top[2], round(top[3], 2), cands, warning)


if __name__ == "__main__":
    print("=" * 70)
    print("名称解析器 · 自检")
    print("=" * 70)
    r = Resolver()
    for t in ["科伦药业", "002422", "科伦", "贵州茅台", "茅台", "宁德", "科仑药业",
              "英伟达", "NVDA.US", "不存在的公司"]:
        print()
        print(r.resolve(t).explain())
