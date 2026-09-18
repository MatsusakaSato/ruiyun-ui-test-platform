"""格式遵循度客观判定：产物类型一致率 + 要求项覆盖率。

设计要点（对应「规则先行、模型兜底」）：
  * 产物类型一致率：以用例 labels.targets 为**格式契约**，核验是否真的产出了对应类型
    产物（产物由 core/artifacts 从工具调用抽取）——完全客观。
  * 要求项覆盖率：模型**只负责**把问题里的显式要求抽成清单（如「包含教学目标」），
    命中判定由本模块的确定性规则执行 —— 保证同一输入结果可复现。
  * 两者都不可得（无 targets 且未抽到显式要求）→ **不适用**，不伪造分值。

比例 → 分值复用 core.eval_rubric.ratio_to_score 的档位（≥95%→5 … <50%→1）。
"""
from __future__ import annotations

import re

from core.artifacts import kinds_from_targets
from core.eval_rubric import ratio_to_score

_SPLIT = re.compile(r"[，,、；;：:/（）()\[\]【】和与及\s]+")
_PUNCT = re.compile(r"[\s，,。.、；;：:！!？?“”\"'（）()\[\]【】《》<>—\-_/\\|]+")
# 要求项常带动词前缀（「包含教学目标」）。去掉后按核心词再匹配一次，
# 否则整句会成为一个 token，「本文有教学目标」这类写法永远判不中。
_LEAD = re.compile(r"^(?:包含|包括|具有|具备|提供|给出|需要|必须|要有|应有|涵盖|涉及|有)")


def _norm(s) -> str:
    return _PUNCT.sub("", str(s or "")).lower()


def build_contract(labels, kinds=None) -> dict:
    """由用例标签构建格式契约。

    kinds 非 None 时覆盖由 labels.targets 翻译出的类型集合 ——
    调用方（core/evaluator）会在用例未声明 targets 时**按问题原文推断**产物类型，
    并需要把同一份契约同时用于「格式遵循度」与「任务完成率」，
    否则两个维度会对同一个问题给出互相矛盾的要求。
    """
    labels = labels if isinstance(labels, dict) else {}
    targets = [str(t).strip().lower() for t in (labels.get("targets") or []) if str(t).strip()]
    return {
        "targets": targets,
        "kinds": set(kinds) if kinds is not None else kinds_from_targets(targets),
        "attachment": bool(labels.get("attachment")),
    }


def type_consistency(contract: dict, artifact_kinds) -> tuple:
    """产物类型一致率。返回 (rate|None, detail)。"""
    kinds = contract.get("kinds") or set()
    if not kinds:
        return None, {"reason": "未声明也未从问题原文推断出产物类型"}
    got = set(artifact_kinds or set())
    hit = kinds & got
    return len(hit) / len(kinds), {
        "expected": sorted(kinds),
        "produced": sorted(got),
        "hit": sorted(hit),
    }


def _requirement_hit(req: str, hay_norm: str) -> bool:
    r = _norm(req)
    if not r:
        return False
    if r in hay_norm:
        return True
    core = _LEAD.sub("", r)              # 去掉「包含/具有」等动词前缀后再匹配核心词
    if len(core) >= 2 and core in hay_norm:
        return True
    toks = [t for t in _SPLIT.split(str(req)) if len(_norm(t)) >= 2]
    if not toks:
        return False
    return sum(1 for t in toks if _norm(t) in hay_norm or _norm(_LEAD.sub("", t)) in hay_norm) \
        / len(toks) >= 0.6


def requirement_coverage(requirements, haystack: str) -> tuple:
    """要求项覆盖率。requirements 由模型抽取，命中判定在本地做。返回 (rate|None, detail)。"""
    reqs = [str(r).strip() for r in (requirements or []) if str(r).strip()]
    if not reqs:
        return None, {"reason": "未抽取到显式格式要求"}
    hay = _norm(haystack)
    hit = [r for r in reqs if _requirement_hit(r, hay)]
    return len(hit) / len(reqs), {
        "total": len(reqs),
        "hit": hit,
        "missing": [r for r in reqs if r not in hit],
    }


def combine(type_rate, coverage_rate, w_type: float = 0.5, w_coverage: float = 0.5) -> tuple:
    """两个子比例加权 → 1-5 分。两者都缺 → (None, 原因)。"""
    parts = []
    if type_rate is not None:
        parts.append((type_rate, max(0.0, float(w_type))))
    if coverage_rate is not None:
        parts.append((coverage_rate, max(0.0, float(w_coverage))))
    if not parts:
        return None, "无格式契约且未抽取到显式要求，不适用"
    total_w = sum(w for _, w in parts) or 1.0
    rate = sum(r * w for r, w in parts) / total_w
    return ratio_to_score(rate), f"加权比例 {rate:.0%}"


def evaluate(labels, artifact_kinds, requirements=None, haystack: str = "",
             weights: dict | None = None, kinds=None) -> dict:
    """格式遵循度的完整判定结果。score=None 表示不适用。

    kinds：产物类型集合覆盖（用于「未声明但已从问题原文推断」的情形）。
    """
    w = weights or {}
    contract = build_contract(labels, kinds)
    type_rate, type_detail = type_consistency(contract, artifact_kinds)
    cov_rate, cov_detail = requirement_coverage(requirements, haystack)
    score, note = combine(type_rate, cov_rate,
                          w.get("type", 0.5), w.get("coverage", 0.5))
    return {
        "score": score,
        "basis": "objective",
        "na_reason": "" if score is not None else note,
        "note": note,
        "type_rate": type_rate,
        "coverage_rate": cov_rate,
        "attachment": contract["attachment"],
        "detail": {
            "type_consistency": type_detail,
            "requirement_coverage": cov_detail,
        },
    }
