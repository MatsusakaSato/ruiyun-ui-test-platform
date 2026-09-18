"""单轮测试的全量轨迹导出：给可视化界面提供逐步骤量化数据。

每个用例导出：
  - 执行轨迹（thinking / tool_call 逐步，含成功/失败/截断状态与结果摘要）
  - 平台自动确认事件（按会话归属后混排进时间线；位置为消息级近似，见 _merge_confirm_events）
  - 工具调用统计（调用了什么、是否成功）
  - Skill 使用（read_skill_file 的返回里有 skill_name，可直接量化）
"""
from __future__ import annotations

import json
from typing import Any

from core.models import CaseResult, ExecutionTrace, ToolCall

# 思考内容与工具结果一律输出全文，不做长度截断
# （截断会让用户在界面上读到断句，被迫去日志里翻，体验更差）

# 规则 -> 影响说明（问题发现区块使用）
RULE_IMPACT = {
    "TOOL_CALL_FAILED":    "该步骤未获得有效数据，agent 可能基于缺失信息继续作答或转向低质量替代路径",
    "TOOL_RESULT_MISSING": "工具无返回，agent 失去该环节依据，任务链路出现空洞",
    "ORPHAN_TOOL_CALL":    "调用链路声明与实际执行不一致，问题排查与审计无法对账",
    "LOOP_CONSECUTIVE":    "重复无效调用，浪费配额与时间，可能持续到触达迭代上限",
    "LOOP_TOTAL":          "单工具高频调用，资源消耗异常，通常是策略失控的前兆",
    "MAX_ITERATIONS_HIT":  "达到迭代上限被强制终止，任务大概率未完成",
    "NO_FINAL_ANSWER":     "用户得不到任何回复，整轮任务无产出",
    "OUTPUT_TRUNCATED":    "最终答复被中途切断，用户看到的是不完整内容，任务未真正完结",
    "DUPLICATE_CALL":      "同参数重复调用，冗余消耗配额与时间",
    "EMPTY_REQUIRED_ARG":  "空参数调用被放行且返回成功，任务实际未完成，结果具有欺骗性",
    "SESSION_NOT_CLOSED":  "会话停留在流式状态，前端可能一直处于加载中",
    "CONFIRM_MANUAL_NEEDED": "确认卡片自动点击多次失败，任务卡在等待人工授权，测试流程无法自动推进",
}


def _est_tokens(text: str) -> int:
    """字符量折算 token 的工程估算：CJK≈1.6 字符/token，其余≈4 字符/token。

    应用日志未记录官方 usage，此估算仅用于量级对比，报告中明确标注。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if '\u4e00' <= ch <= '\u9fff')
    other = len(text) - cjk
    return int(cjk / 1.6 + other / 4)


def build_objective(case: CaseResult, stage_times: dict | None = None) -> dict:
    """单用例客观信息：调用链路之外的量化数据（耗时/用量/请求/状态）。"""
    trace = case.trace
    o: dict = {"timing": {}, "volume": {}, "requests": {}, "status": {}, "coverage": {}}
    if trace is None:
        return o

    # ---- 耗时 ----
    o["timing"] = {
        "platform_s": round(case.elapsed_s, 1),          # 平台实测（发送->落盘稳定）
        "first_response_s": trace.first_response_s if trace.first_response_s >= 0 else None,
        "generation_s": trace.generation_s if trace.generation_s >= 0 else None,
        "session_s": trace.session_s if trace.session_s >= 0 else None,
        "stage_times": stage_times or {},
        "note": "日志未记录单次工具调用耗时，故仅提供会话/消息级耗时",
    }

    # ---- 用量（字符精确 + token 估算） ----
    tool_result_chars = sum(len(t.raw_result or "") for t in trace.tool_calls)
    tool_arg_chars = sum(len(t.signature()) for t in trace.tool_calls)
    prompt_chars = len(case.prompt or "")
    thinking_chars = trace.reasoning_chars
    answer_chars = len(trace.final_answer or "")
    in_chars = prompt_chars + tool_result_chars
    out_chars = thinking_chars + answer_chars + tool_arg_chars
    # 思考全文：reasoning_chars 只是长度，token 估算需要真实文本才能区分中英文。
    # 实测 thinking_steps 内容总长与 reasoning_chars 完全一致（65/65），可安全替代。
    thinking_text = "".join(x.content or "" for x in trace.thinking_steps)
    o["volume"] = {
        "prompt_chars": prompt_chars,
        "thinking_chars": thinking_chars,
        "answer_chars": answer_chars,
        "tool_result_chars": tool_result_chars,
        "input_tokens_est": (_est_tokens(case.prompt or "")
                             + sum(_est_tokens(t.body or t.raw_result or "") for t in trace.tool_calls)),
        "output_tokens_est": (_est_tokens(trace.final_answer or "")
                              + _est_tokens(thinking_text)
                              + sum(_est_tokens(t.signature()) for t in trace.tool_calls)),
        "total_tokens_est": 0,
        "note": "token 为估算值（CJK≈1.6字符/token，其余≈4字符/token）；日志未记录官方 usage，仅可观测部分：输入=提示词+工具结果回灌，输出=思考+答复+调用参数",
    }
    o["volume"]["total_tokens_est"] = o["volume"]["input_tokens_est"] + o["volume"]["output_tokens_est"]

    # ---- 请求次数 ----
    o["requests"] = {
        "turns": trace.turn_count,
        "tool_calls": len(trace.tool_calls),
        "thinking_steps": len(trace.thinking_steps),
        "distinct_tools": len({t.name for t in trace.tool_calls}),
    }

    # ---- 状态与错误率 ----
    n = len(trace.tool_calls)
    fail = sum(1 for f in case.findings if f.rule in ("TOOL_CALL_FAILED", "TOOL_RESULT_MISSING"))
    empty = sum(1 for t in trace.tool_calls if t.raw_result is not None and not t.raw_result.strip())
    # 工具结果被应用标记 truncated=true 的次数（展示口径，非 bug 判定）
    trunc = sum(1 for t in trace.tool_calls
                if isinstance(t.result_obj, dict) and t.result_obj.get("truncated") is True)
    closed = (not trace.is_streaming) and bool(trace.completed_at)
    o["status"] = {
        "tool_calls": n,
        "tool_ok": max(0, n - fail - trunc - empty),
        "tool_fail": fail,
        "tool_truncated": trunc,
        "tool_empty": empty,
        "error_rate": round(fail * 100.0 / n, 1) if n else 0.0,
        "truncated_rate": round(trunc * 100.0 / n, 1) if n else 0.0,
        "session_closed": closed,
        "closed_desc": "正常收尾" if closed else "未正常收尾（缺 completedAt 或仍在流式）",
        "chain_consistent": not trace.orphan_ids,
    }

    # ---- 工具覆盖（本轮用例声明 vs 实际调用） ----
    expected = case.expected_tools or []
    used = sorted({t.name for t in trace.tool_calls})
    o["coverage"] = {
        "expected": expected,
        "hit": [t for t in expected if t in used],
        "missing": [t for t in expected if t not in used],
        "extra": [t for t in used if t not in expected],
    }
    return o


def _arg_summary(arguments: Any, limit: int = 160) -> str:
    try:
        s = json.dumps(arguments, ensure_ascii=False)
    except Exception:
        s = str(arguments)
    return s if len(s) <= limit else s[:limit] + "…"


def _step_status(tc: ToolCall, findings_by_step: dict, session_id: str) -> str:
    """单次调用的状态：ok / fail / truncated / empty（取最严重）。

    findings_by_step 的 key 是 (session_id, step_index) 元组 —— 必须用同样形态查，
    否则永远查不到，所有步骤都会误标为 ok。
    另外做一层兜底：即使 findings 关联失败，也直接看结果本身是否失败，
    避免「明明返回 success=false 却显示成功」。
    """
    findings = findings_by_step.get((session_id, tc.index)) or []
    rules = {f.rule for f in findings}
    if "TOOL_CALL_FAILED" in rules or "TOOL_RESULT_MISSING" in rules:
        return "fail"

    # 兜底：不依赖 findings，直接按结果内容判定（防止关联失配时静默显示成功）
    if tc.raw_result is None or not str(tc.raw_result).strip():
        return "empty"
    obj = tc.result_obj
    if isinstance(obj, dict):
        if obj.get("success") is False or obj.get("is_error") is True or obj.get("isError") is True:
            return "fail"
        err = obj.get("error")
        if isinstance(err, str) and err.strip():
            return "fail"
        if obj.get("truncated") is True:
            return "truncated"
    return "ok"


def _skill_name(tc: ToolCall) -> str:
    """从 read_skill_file 的结构化返回中取 skill_name。"""
    if tc.name != "read_skill_file":
        return ""
    obj = tc.result_obj
    if isinstance(obj, dict):
        return str(obj.get("skill_name") or "")
    return ""


def _skill_files(tc: ToolCall) -> list:
    obj = tc.result_obj
    if isinstance(obj, dict) and isinstance(obj.get("read_files"), list):
        return obj["read_files"]
    rel = ""
    if isinstance(tc.arguments, dict):
        rel = str(tc.arguments.get("relative_path") or "")
    return [rel] if rel else []


def _session_log_path(session_dir: str) -> str:
    """返回会话文件夹路径（定位用），供前端在 Finder 中显示。

    指向会话目录本身而非内部的 session.messages.json：
    Finder 中选中整个会话文件夹，方便一并查看
    session.messages.json / session.meta.json / todo.md / memory/ 等全部产物。
    """
    if not session_dir:
        return ""
    from pathlib import Path
    return str(Path(session_dir))


# 自动确认触发方式 → 界面可读标签（与 drivers/ui_driver.py 的 mode 口径一一对应）
CONFIRM_MODE_LABEL = {
    "qcard-option": "选中选项",
    "qcard-confirm": "确认本题",
    "qcard-submit": "提交",
    "qcard-fill": "填写自定义答案",
    "keyword": "关键词命中授权按钮",
    "first-option": "结构兜底点首个选项",
}


def _iso_to_epoch(s) -> float | None:
    """本地无时区 ISO 串 → epoch 秒（与事件 ts 同口径）；解析失败返回 None。"""
    if not s:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(s)).timestamp()
    except Exception:
        return None


def _ev_epoch(ev: dict) -> float:
    try:
        return float(ev.get("ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def _merge_confirm_events(steps: list, trace, events: list) -> list:
    """把「平台自动确认」事件近似混排进步骤时间线。

    为什么只能是近似：会话日志只记录**消息级**时间戳，timelineSteps 内的步骤本身
    没有时间字段，因此「插到第 N 次工具调用之后」无法实现。这里以每条 assistant
    消息的 [at, done_at) 为一个响应段：

      · 事件 ts 落入某段 → 追加在该段步骤之后（段内按 ts 升序），条目上带
        approximate=True 与 anchor（该段起始消息时间），前端据此显示「近似」角标；
      · 早于首段 → 置于最前；晚于末段 / 无时间戳 / 无段可归 → 追加末尾；
      · 无步骤（会话日志解析失败）时仍然输出事件条目，不静默丢弃。
    """
    items = []
    for ev in (events or []):
        if not isinstance(ev, dict):
            continue
        mode = str(ev.get("mode") or "")
        items.append({
            "type": "auto_confirm",
            "time": str(ev.get("time") or ""),
            "ts": _ev_epoch(ev),
            "mode": mode,
            "label": CONFIRM_MODE_LABEL.get(mode, mode or "自动确认"),
            "text": str(ev.get("text") or ""),
            "q": str(ev.get("q") or ""),
            # 位置精度：日志无步骤级时间，位置一律是消息级近似
            "approximate": True,
            # 归属来源：驱动点击时记录了会话 → session；旧事件无 sess → time-window
            "attribution": "session" if str(ev.get("sess") or "") else "time-window",
            "anchor": "",
        })
    if not items:
        return list(steps or [])

    spans = []
    for sp in (getattr(trace, "message_spans", None) or []):
        if not isinstance(sp, dict):
            continue
        spans.append({
            "at_ts": _iso_to_epoch(sp.get("at")),
            "done_ts": _iso_to_epoch(sp.get("done_at")),
            "first_step": sp.get("first_step"),
            "last_step": sp.get("last_step"),
            "anchor": str(sp.get("at") or ""),
        })

    head, tail, at_seg = [], [], {}
    first_at = next((sp["at_ts"] for sp in spans if sp["at_ts"]), None)
    for it in items:
        ts = it["ts"]
        hit = None
        if ts > 0:
            for i, sp in enumerate(spans):
                lo = sp["at_ts"]
                if lo is None:
                    continue
                # 段结束：消息完成时间；缺失则顺延到下一段起点（最后一段无上界）
                hi = sp["done_ts"] or (spans[i + 1]["at_ts"]
                                       if i + 1 < len(spans) else None)
                if ts >= lo and (hi is None or ts < hi):
                    hit = i
                    break
        if hit is not None:
            it["anchor"] = spans[hit]["anchor"]
            at_seg.setdefault(hit, []).append(it)
        elif ts > 0 and first_at is not None and ts < first_at:
            head.append(it)
        else:
            tail.append(it)

    out = list(head)
    covered = set()
    for i, sp in enumerate(spans):
        lo, hi = sp["first_step"], sp["last_step"]
        if isinstance(lo, int) and isinstance(hi, int):
            for st in steps:
                if lo <= st.get("i", -1) <= hi:
                    out.append(st)
                    covered.add(id(st))
        out.extend(sorted(at_seg.get(i, []), key=lambda x: x["ts"]))
    for st in steps:                       # 未被任何消息段覆盖的步骤（保持原序）
        if id(st) not in covered:
            out.append(st)
    out.extend(sorted(tail, key=lambda x: x["ts"]))
    return out


def build_case_detail(case: CaseResult, findings_by_step: dict | None = None) -> dict:
    trace = case.trace
    if trace is None:
        return {
            "case_id": case.case_id, "name": case.name, "prompt": case.prompt,
            "status": case.status, "session_id": case.session_id,
            "session_dir": "", "session_log_path": "",
            "ui_error": case.ui_error, "elapsed_s": round(case.elapsed_s, 1),
            # 用例附件（本机绝对路径）与投递结果，供平台核对"附件是否真的带上了"
            "attachments": list(getattr(case, "attachments", []) or []),
            "attach_note": getattr(case, "attach_note", ""),
            "waited_limit": bool(getattr(case, "waited_limit", False)),
            "wait_note": getattr(case, "wait_note", ""),
            "auto_confirms": int(getattr(case, "auto_confirms", 0)),
            # 无轨迹也照样输出自动确认条目：事件本身是事实，不该因解析失败而丢
            "steps": _merge_confirm_events(
                [], None, list(getattr(case, "confirm_events", []) or [])),
            "tools": [], "skills": [], "objective": {},
        }
    objective = build_objective(case)

    findings_by_step = findings_by_step or {}
    steps = []
    thinking_no = 0
    for tc in trace.tool_calls:
        pass
    # 重建带 thinking 的完整时间线（log_parser 已按序存两列，这里按 index 合并）
    merged = []
    for tc in trace.tool_calls:
        merged.append(("tool", tc.index, tc))
    for th in trace.thinking_steps:
        merged.append(("think", th.index, th))
    merged.sort(key=lambda x: x[1])

    for kind, idx, obj in merged:
        if kind == "think":
            thinking_no += 1
            content = obj.content or ""
            steps.append({
                "i": idx, "type": "thinking", "n": thinking_no,
                # 完整内容：不做截断，用户直接在界面读全文，无需去日志里翻
                "content": content,
                "chars": len(content),
            })
        else:
            tc: ToolCall = obj
            status = _step_status(tc, findings_by_step, trace.session_id)
            raw = tc.raw_result or ""
            steps.append({
                "i": idx, "type": "tool_call",
                "name": tc.name,
                "status": status,
                "args_summary": _arg_summary(tc.arguments),
                # 完整结果：不做截断
                "result": raw,
                # 展示长度用内层正文，而不是外层 JSON 外壳 ——
                # 外壳含 success/字段名等，会让用户看到的字数虚高
                "result_len": len(tc.body) if tc.body else len(raw),
                "raw_len": len(raw),
                "body_from": tc.body_from,
                "result_empty": not raw.strip(),
                "skill_name": _skill_name(tc),
                "skill_files": _skill_files(tc) if tc.name == "read_skill_file" else [],
                "issues": sorted({f.rule for f in findings_by_step.get(tc.index, [])}),
            })

    # 工具统计
    tool_stats = {}
    for tc in trace.tool_calls:
        st = tool_stats.setdefault(tc.name, {"name": tc.name, "calls": 0, "fail": 0, "truncated": 0})
        st["calls"] += 1
        s = _step_status(tc, findings_by_step, trace.session_id)
        if s == "fail":
            st["fail"] += 1
        elif s == "truncated":
            st["truncated"] += 1

    # Skill 使用（去重聚合）
    skills = {}
    for tc in trace.tool_calls:
        sn = _skill_name(tc)
        if not sn:
            continue
        sk = skills.setdefault(sn, {"name": sn, "files": [], "steps": []})
        for f in _skill_files(tc):
            if f and f not in sk["files"]:
                sk["files"].append(f)
        sk["steps"].append(tc.index)
    # 若只读了 SKILL.md 但返回无 skill_name，回退标记为未知技能
    unknown = [tc for tc in trace.tool_calls
               if tc.name == "read_skill_file" and not _skill_name(tc)]
    for tc in unknown:
        sk = skills.setdefault("(未识别技能)", {"name": "(未识别技能)", "files": [], "steps": []})
        sk["steps"].append(tc.index)
        for f in _skill_files(tc):
            if f and f not in sk["files"]:
                sk["files"].append(f)

    # 平台自动确认事件：驱动已按会话归属挂在本用例上，这里近似混排进时间线
    steps = _merge_confirm_events(steps, trace,
                                  list(getattr(case, "confirm_events", []) or []))

    answer = trace.final_answer or ""
    return {
        "case_id": case.case_id,
        "name": case.name,
        "prompt": case.prompt,
        "status": case.status,
        "session_id": case.session_id,
        # 会话日志的绝对路径，供前端生成 file:// 链接直接打开
        "session_dir": trace.source_file,
        "session_log_path": _session_log_path(trace.source_file),
        "ui_error": case.ui_error,
        # 用例附件（本机绝对路径）与投递结果，供平台核对"附件是否真的带上了"
        "attachments": list(getattr(case, "attachments", []) or []),
        "attach_note": getattr(case, "attach_note", ""),
        "waited_limit": bool(getattr(case, "waited_limit", False)),
        "wait_note": getattr(case, "wait_note", ""),
        "auto_confirms": int(getattr(case, "auto_confirms", 0)),
        "elapsed_s": round(case.elapsed_s, 1),
        "mode": trace.mode,
        "created_at": trace.created_at,
        "answer_excerpt": answer[:600] + ("…" if len(answer) > 600 else ""),
        "answer_chars": len(answer),
        "reasoning_chars": trace.reasoning_chars,
        "tool_call_count": len(trace.tool_calls),
        "thinking_count": len(trace.thinking_steps),
        "steps": steps,
        "tools": sorted(tool_stats.values(), key=lambda x: -x["calls"]),
        "skills": list(skills.values()),
        "objective": objective,
        "findings": [f.to_dict() if hasattr(f, "to_dict") else {
            "rule": f.rule, "severity": f.severity, "detail": f.detail,
            "tool": f.tool, "evidence": f.evidence, "step_index": f.step_index,
        } for f in case.findings],
    }


def build_round_detail(case_results: list, metrics: dict | None = None,
                       repro_rows: list | None = None,
                       stage_times: dict | None = None) -> dict:
    """整轮详情：本轮全部用例的轨迹 + 客观信息 + 汇总指标。

    评估口径：只包含本轮用例会话，不追溯历史日志。
    """
    # findings 按 (session, step) 索引，用于标注轨迹状态
    by_step: dict = {}
    for c in case_results:
        for f in c.findings:
            if f.step_index is not None:
                by_step.setdefault((c.session_id, f.step_index), []).append(f)

    details = [build_case_detail(c, by_step) for c in case_results]

    # 轮次级客观汇总
    def _sum(field, key):
        return sum((d.get("objective", {}).get(field, {}) or {}).get(key) or 0
                   for d in details if d.get("objective"))

    agg_volume = {
        "input_tokens_est": _sum("volume", "input_tokens_est"),
        "output_tokens_est": _sum("volume", "output_tokens_est"),
    }
    agg_volume["total_tokens_est"] = agg_volume["input_tokens_est"] + agg_volume["output_tokens_est"]
    agg_requests = {
        "turns": _sum("requests", "turns"),
        "tool_calls": _sum("requests", "tool_calls"),
        "thinking_steps": _sum("requests", "thinking_steps"),
    }
    calls = agg_requests["tool_calls"]
    agg_status = {
        "tool_fail": _sum("status", "tool_fail"),
        "tool_truncated": _sum("status", "tool_truncated"),
        "error_rate": round(_sum("status", "tool_fail") * 100.0 / calls, 1) if calls else 0.0,
        "truncated_rate": round(_sum("status", "tool_truncated") * 100.0 / calls, 1) if calls else 0.0,
    }
    timings = [d["objective"]["timing"] for d in details
               if d.get("objective", {}).get("timing")]
    round_objective = {
        "volume": agg_volume,
        "requests": agg_requests,
        "status": agg_status,
        "timing": {
            "avg_first_response_s": (round(sum(t["first_response_s"] or 0 for t in timings) / len(timings), 2)
                                     if timings else None),
            "stage_times": stage_times or {},
        },
    }

    # 轮次级工具/skill 汇总（仅本轮用例）
    round_tools: dict = {}
    round_skills: dict = {}
    for d in details:
        for t in d.get("tools", []):
            agg = round_tools.setdefault(t["name"], {"name": t["name"], "calls": 0, "fail": 0, "truncated": 0})
            agg["calls"] += t["calls"]
            agg["fail"] += t.get("fail", 0)
            agg["truncated"] += t.get("truncated", 0)
        for s in d.get("skills", []):
            agg = round_skills.setdefault(s["name"], {"name": s["name"], "sessions": 0, "files": []})
            agg["sessions"] += 1
            for f in s.get("files", []):
                if f and f not in agg["files"]:
                    agg["files"].append(f)

    return {
        "cases": details,
        "round_tools": sorted(round_tools.values(), key=lambda x: -x["calls"]),
        "round_skills": sorted(round_skills.values(), key=lambda x: -x["sessions"]),
        "round_objective": round_objective,
        "metrics": metrics or {},
        "repro_rows": repro_rows or [],
    }
