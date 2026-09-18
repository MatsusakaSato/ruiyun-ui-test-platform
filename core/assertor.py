"""硬 bug 断言引擎。

只判定「应用本身存在的硬性 bug」，不对输出内容质量做任何评判。
规则全部基于日志中的客观证据（错误串、ID 一致性、长度特征、调用序列）。
"""
from __future__ import annotations

import json
from typing import Any

from core.models import ExecutionTrace, Finding, ToolCall


RULES = {
    "TOOL_CALL_FAILED":    ("P0", "工具调用失败"),
    "TOOL_RESULT_MISSING": ("P0", "工具调用无返回"),
    "ORPHAN_TOOL_CALL":    ("P0", "调用链路 ID 不一致"),
    "LOOP_CONSECUTIVE":    ("P0", "死循环（同工具连续调用）"),
    "MAX_ITERATIONS_HIT":  ("P0", "触达 ReAct 迭代上限"),
    "NO_FINAL_ANSWER":     ("P0", "会话无最终答复"),
    "OUTPUT_TRUNCATED":    ("P1", "最终答案被强制截断（完结率不足）"),
    "DUPLICATE_CALL":      ("P1", "同参数重复调用"),
    "LOOP_TOTAL":          ("P1", "同工具高频调用"),
    "EMPTY_REQUIRED_ARG":  ("P1", "必填参数为空被放行"),
    "SESSION_NOT_CLOSED":  ("P1", "会话未正常收尾"),
    "CONFIRM_MANUAL_NEEDED": ("P0", "确认卡片自动点击失败，需人工介入"),
}


def _mk(rule: str, trace: ExecutionTrace, detail: str, *,
        tool: str = "", step: int | None = None, evidence: str = "") -> Finding:
    sev = RULES.get(rule, ("P2", ""))[0]
    return Finding(
        rule=rule, severity=sev, session_id=trace.session_id,
        detail=detail, tool=tool, step_index=step, evidence=evidence[:400],
    )


def _norm(s: str) -> str:
    return (s or "").strip().lower()


# ------------------------------ 单条规则 ------------------------------

def check_tool_failed(tc: ToolCall, trace: ExecutionTrace, cfg: dict) -> list:
    markers = [m.lower() for m in cfg.get("error_markers", [])]
    text = _norm(tc.raw_result)
    hits = []

    # 1) 文本特征词
    for m in markers:
        if m in text:
            hits.append(m)

    # 2) 结构化错误字段
    obj = tc.result_obj
    if isinstance(obj, dict):
        if obj.get("success") is False:
            hits.append("success=false")
        if obj.get("is_error") is True or obj.get("isError") is True:
            hits.append("is_error=true")
        err = obj.get("error")
        if isinstance(err, str) and err.strip():
            hits.append("error field")
        elif err not in (None, "", {}, []):
            hits.append("error field")

    # 3) 空结果
    if not hits and (tc.raw_result is None or not tc.raw_result.strip()):
        return [_mk("TOOL_RESULT_MISSING", trace, f"{tc.name} 返回空结果",
                    tool=tc.name, step=tc.index)]

    if hits:
        head = (tc.raw_result or "").replace("\n", " ")[:200]
        return [_mk("TOOL_CALL_FAILED", trace,
                    f"{tc.name} 调用失败（命中特征：{', '.join(sorted(set(hits))) }）",
                    tool=tc.name, step=tc.index, evidence=head)]
    return []


def check_consecutive_loop(trace: ExecutionTrace, cfg: dict) -> list:
    th = cfg.get("loop_consecutive_threshold", 5)
    names = [t.name for t in trace.tool_calls]
    out, run, prev = [], 1, None
    for i, nm in enumerate(names):
        if nm == prev:
            run += 1
        else:
            run, prev = 1, nm
        if run == th:
            out.append(_mk("LOOP_CONSECUTIVE", trace,
                           f"{nm} 连续调用 {run} 次（阈值 {th}）",
                           tool=nm, step=trace.tool_calls[i].index))
    return out


def check_total_loop(trace: ExecutionTrace, cfg: dict) -> list:
    th = cfg.get("loop_total_threshold", 8)
    counts: dict[str, int] = {}
    for t in trace.tool_calls:
        counts[t.name] = counts.get(t.name, 0) + 1
    out = []
    for nm, c in counts.items():
        if c >= th:
            out.append(_mk("LOOP_TOTAL", trace,
                           f"{nm} 单会话共调用 {c} 次（阈值 {th}）", tool=nm))
    return out


def check_duplicate(trace: ExecutionTrace, cfg: dict) -> list:
    seen: dict[tuple, list] = {}
    for t in trace.tool_calls:
        seen.setdefault((t.name, t.signature()), []).append(t)
    out = []
    for (nm, sig), items in seen.items():
        if len(items) >= 2:
            out.append(_mk("DUPLICATE_CALL", trace,
                           f"{nm} 使用完全相同的参数重复调用 {len(items)} 次",
                           tool=nm, step=items[1].index,
                           evidence=sig[:200]))
    return out


def check_empty_args(trace: ExecutionTrace, cfg: dict) -> list:
    out = []
    for t in trace.tool_calls:
        if t.empty_required_arg:
            out.append(_mk("EMPTY_REQUIRED_ARG", trace,
                           f"{t.name} 必填参数为空仍被放行：{', '.join(t.empty_required_arg)}",
                           tool=t.name, step=t.index,
                           evidence=json.dumps(t.arguments, ensure_ascii=False)[:300]))
    return out


def check_orphan(trace: ExecutionTrace, cfg: dict) -> list:
    if not trace.associated_tool_call_ids and not trace.tool_calls:
        return []
    diff = trace.orphan_ids
    if diff:
        return [_mk("ORPHAN_TOOL_CALL", trace,
                    f"associatedToolCallIds 与 timelineSteps 的调用 ID 不匹配（"
                    f"声明 {len(trace.associated_tool_call_ids)} / 实际 {len(trace.tool_calls)}）",
                    evidence=", ".join(diff[:6]))]
    return []


def check_max_iterations(trace: ExecutionTrace, limit: int, cfg: dict) -> list:
    if limit and len(trace.tool_calls) >= limit:
        return [_mk("MAX_ITERATIONS_HIT", trace,
                    f"工具调用步数 {len(trace.tool_calls)} 已触达 max_iterations={limit}",
                    step=trace.tool_calls[-1].index)]
    return []


def check_final_answer(trace: ExecutionTrace, cfg: dict) -> list:
    if not (trace.final_answer or "").strip():
        return [_mk("NO_FINAL_ANSWER", trace, "会话结束但未产出任何最终答复")]
    return []


# --------------------- 输出截断：思考内容 / 最终答案 ---------------------

def _unclosed_structure(text: str) -> str:
    """检测未闭合的结构标记，返回命中描述（无则空串）。

    代码块与 Markdown 表格必须以成对/完整形式收尾，
    数量为奇数（代码块）或末行是残破表头，都说明被切断。
    """
    fence = text.count("```")
    if fence % 2 == 1:
        return f"代码块 ``` 出现 {fence} 次（奇数，未闭合）"
    lines = [l for l in text.split("\n") if l.strip()]
    if lines and lines[-1].lstrip().startswith("|") and lines[-1].rstrip().endswith("|"):
        return "结尾停在 Markdown 表格行，表格未收尾"
    if text.count("![") > text.count(")") and text.count("![") > 0:
        return "存在未闭合的图片语法 ![...](...)"
    return ""


def _truncation_signals(text: str, trace: ExecutionTrace, cfg: dict) -> list:
    """对一段文本做多信号判定，返回 [(信号名, 判定依据), ...]。

    目前只用于【最终答案】的完结率判定（见 check_output_truncated），
    不用于思考过程与工具结果。
    """
    ocfg = cfg.get("output_truncation") or {}
    caps = cfg.get("truncation_caps", [])
    endings = tuple(cfg.get("natural_endings", []))
    tol = float(ocfg.get("cap_tolerance", 0.02))
    min_len = int(ocfg.get("min_len_for_dangling", 300))
    dangling = list(ocfg.get("dangling_tails", []))

    body = text.rstrip()
    n = len(body)
    hits = []

    # 信号 A：会话仍在流式（内容必然未收完）
    if trace.is_streaming:
        hits.append(("流式未结束", "会话 isStreaming=true，内容尚未生成完就被采集"))

    # 信号 B：缺 completedAt 但有内容（生成未正常结束）
    if not trace.completed_at:
        hits.append(("缺完成标记",
                     "消息无 completedAt 字段，说明生成未走完正常结束流程"))

    # 信号 C：接续型字符收尾（必须接下文才成立）
    if n >= min_len and body:
        for d in dangling:
            if body.endswith(d):
                hits.append(("结尾为接续词",
                             f"以 {d!r} 收尾（共 {n} 字），该字/词后必须还有下文，"
                             f"实际结尾：…{body[-60:]!r}"))
                break

    # 信号 D：长度精确（或近似）落在截断上限
    for c in caps:
        lo, hi = c * (1 - tol), c * (1 + tol)
        if lo <= n <= hi and not body.endswith(endings):
            pct = (n - c) / c * 100
            hits.append(("命中截断上限",
                         f"长度 {n} 字落在上限 {c} 的 ±{tol:.0%} 范围内"
                         f"（偏差 {pct:+.1f}%），且结尾无自然收尾符，"
                         f"实际结尾：…{body[-60:]!r}"))
            break

    # 信号 E：结构未闭合
    unclosed = _unclosed_structure(text)
    if unclosed:
        hits.append(("结构未闭合",
                     f"{unclosed}，实际结尾：…{body[-60:]!r}"))

    return hits


def check_output_truncated(trace: ExecutionTrace, cfg: dict) -> list:
    """检测【最终答案】是否被强制截断 —— 即完结率。

    范围限定为最终答复：思考过程与工具调用结果不参与本规则。
    日志不记录 finish_reason，无法确证截断，因此用多信号交叉推断；
    每条 finding 都会写明命中了哪个信号与具体证据，便于复核。
    """
    out = []
    ans = (trace.final_answer or "").strip()
    if not ans:
        return out

    signals = _truncation_signals(ans, trace, cfg)
    if not signals:
        return out
    names = "、".join(s[0] for s in signals)
    detail = (f"最终答案疑似被强制截断（命中信号：{names}；共 {len(ans)} 字）。"
              + " ".join(f"【{s[0]}】{s[1]}" for s in signals))
    out.append(_mk("OUTPUT_TRUNCATED", trace, detail, evidence=ans[-200:]))
    return out


def check_closed(trace: ExecutionTrace, cfg: dict) -> list:
    if trace.is_streaming:
        return [_mk("SESSION_NOT_CLOSED", trace, "会话仍处于流式未结束状态（isStreaming=true）")]
    return []


# ------------------------------ 编排入口 ------------------------------

def run_assertions(trace: ExecutionTrace, cfg: dict, max_iterations: int = 0) -> list:
    """对单个会话执行全部断言，返回命中的 Finding 列表。"""
    findings: list[Finding] = []

    for tc in trace.tool_calls:
        findings += check_tool_failed(tc, trace, cfg)

    findings += check_consecutive_loop(trace, cfg)
    findings += check_total_loop(trace, cfg)
    findings += check_duplicate(trace, cfg)
    findings += check_empty_args(trace, cfg)
    findings += check_orphan(trace, cfg)
    findings += check_max_iterations(trace, max_iterations, cfg)
    findings += check_final_answer(trace, cfg)
    findings += check_output_truncated(trace, cfg)   # 最终答案完结率（不含思考）
    findings += check_closed(trace, cfg)

    return findings
