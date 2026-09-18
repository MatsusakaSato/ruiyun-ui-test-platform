"""度量聚合 —— 评估口径：每轮测试仅统计本轮对话，不追溯历史日志。

报告结构：
  1. 客观信息：调用链路、各环节耗时、token 消耗（估算）、请求次数、错误率、响应状态
  2. 问题发现：仅本轮实际发现的问题（表现 + 影响），无问题则显式标注
"""
from __future__ import annotations

from collections import Counter

from core.assertor import RULES
from core.models import CaseResult, ExecutionTrace
from core.trajectory import RULE_IMPACT, build_objective
from core.trajectory import _skill_files as _skill_files
from core.trajectory import _skill_name as _skill_name

SEVERITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}


def _pct(a: int, b: int) -> float:
    return round(a * 100.0 / b, 1) if b else 0.0


def repro_block(recipes: list) -> tuple:
    """从复现配方（dict 或 ReproRecipe）构建报告用的复现率区块。"""
    rows = []
    for r in recipes or []:
        rd = r if isinstance(r, dict) else r.to_dict()
        if not rd.get("attempts"):
            continue
        rows.append({
            "key": rd["key"],
            "severity": rd.get("severity", "P1"),
            "name": rd.get("rule_name", ""),
            "tool": rd.get("tool", ""),
            "attempts": rd["attempts"],
            "hits": rd["hits"],
            "rate": rd.get("rate") or 0.0,
            "stability": rd.get("stability", ""),
            "sessions": [s.get("session_id") for s in rd.get("run_sessions", []) if s.get("session_id")],
        })
    summary = {
        "verified": len(rows),
        "stable": sum(1 for r in rows if r["stability"] == "必现"),
        "likely": sum(1 for r in rows if r["stability"] == "高概率复现"),
        "flaky": sum(1 for r in rows if r["stability"] == "偶发"),
        "avg_rate": round(sum(r["rate"] for r in rows) / len(rows), 3) if rows else None,
    }
    return rows, summary


def build_metrics(case_results: list, cfg: dict,
                  recipes: list | None = None,
                  stage_times: dict | None = None) -> dict:
    """生成报告所需全部指标。case_results 必须只含本轮用例。"""
    findings = [f for c in case_results for f in c.findings]
    ui_failed = [c for c in case_results if not c.ui_ok]
    passed = [c for c in case_results if c.passed]
    failed = [c for c in case_results if c.ui_ok and c.findings]

    # ---------- 规则维度（仅本轮命中） ----------
    rule_counter = Counter(f.rule for f in findings)
    rule_rows = []
    for rule, name in RULES.items():
        cnt = rule_counter.get(rule, 0)
        rule_rows.append({
            "rule": rule,
            "name": name[1],
            "severity": name[0],
            "count": cnt,
        })
    rule_rows.sort(key=lambda r: (SEVERITY_ORDER.get(r["severity"], 9), -r["count"]))

    sev_counter = Counter(f.severity for f in findings)
    severity = [{"severity": s, "count": sev_counter.get(s, 0)}
                for s in ("P0", "P1", "P2")]

    # ---------- 工具维度（仅本轮用例会话） ----------
    call_total = 0
    tool_counter = Counter()
    tool_failed = Counter()
    tool_truncated = Counter()
    for c in case_results:
        for tc in (c.trace.tool_calls if c.trace else []):
            call_total += 1
            tool_counter[tc.name] += 1
    for f in findings:
        if f.rule in ("TOOL_CALL_FAILED", "TOOL_RESULT_MISSING"):
            tool_failed[f.tool] += 1

    tool_rows = []
    for name, cnt in tool_counter.most_common():
        tool_rows.append({
            "tool": name,
            "calls": cnt,
            "failed": tool_failed.get(name, 0),
            "truncated": tool_truncated.get(name, 0),
            "fail_rate": _pct(tool_failed.get(name, 0), cnt),
        })

    # ---------- 工具覆盖度（本轮声明期望 vs 实际调用） ----------
    expected, used = [], set(tool_counter)
    for c in case_results:
        for t in (c.expected_tools or []):
            if t not in expected:
                expected.append(t)
    covered = [t for t in expected if t in used]
    coverage = {
        "expected": expected,
        "used": sorted(used),
        "covered": covered,
        "missing": [t for t in expected if t not in used],
        "rate": _pct(len(covered), len(expected)),
        "distinct_used": len(used),
    }

    # ---------- Skill 使用（仅本轮，从 read_skill_file 返回解析） ----------
    skill_rows = {}
    for c in case_results:
        for tc in (c.trace.tool_calls if c.trace else []):
            sn = _skill_name(tc)
            if not sn and tc.name != "read_skill_file":
                continue
            key = sn or "(未识别技能)"
            row = skill_rows.setdefault(key, {
                "name": key, "sessions": set(), "files": [], "calls": 0})
            row["calls"] += 1
            row["sessions"].add(c.case_id)
            for fp in _skill_files(tc):
                if fp and fp not in row["files"]:
                    row["files"].append(fp)
    skill_rows = [
        {**v, "sessions": sorted(v["sessions"])} for v in skill_rows.values()
    ]

    # ---------- 客观信息（逐用例 + 轮次汇总） ----------
    case_objective = {}
    for c in case_results:
        case_objective[c.case_id] = build_objective(c, stage_times)

    all_obj = [v for v in case_objective.values() if v]
    calls = sum(o.get("requests", {}).get("tool_calls", 0) for o in all_obj)
    fails = sum(o.get("status", {}).get("tool_fail", 0) for o in all_obj)
    truncs = sum(o.get("status", {}).get("tool_truncated", 0) for o in all_obj)
    in_tok = sum(o.get("volume", {}).get("input_tokens_est", 0) for o in all_obj)
    out_tok = sum(o.get("volume", {}).get("output_tokens_est", 0) for o in all_obj)
    turns = sum(o.get("requests", {}).get("turns", 0) for o in all_obj)
    think = sum(o.get("requests", {}).get("thinking_steps", 0) for o in all_obj)
    fr = [o.get("timing", {}).get("first_response_s") for o in all_obj]
    fr = [x for x in fr if x is not None and x >= 0]

    objective = {
        "requests": {
            "turns": turns,
            "tool_calls": calls,
            "thinking_steps": think,
            "llm_responses": turns,   # 每轮用户提问对应至少一次模型响应
        },
        "status": {
            "tool_fail": fails,
            "tool_truncated": truncs,
            "error_rate": _pct(fails, calls),
            "truncated_rate": _pct(truncs, calls),
            "sessions_closed": sum(1 for o in all_obj if o.get("status", {}).get("session_closed")),
            "sessions_total": len(all_obj),
        },
        "tokens": {
            "input_est": in_tok,
            "output_est": out_tok,
            "total_est": in_tok + out_tok,
            "note": "估算值：日志未记录官方 usage；输入=提示词+工具结果回灌，输出=思考+答复+调用参数（CJK≈1.6字符/token）",
        },
        "timing": {
            "avg_first_response_s": round(sum(fr) / len(fr), 2) if fr else None,
            "stage_times": stage_times or {},
            "note": "步骤级耗时日志未记录，仅提供会话/消息级耗时与平台实测耗时",
        },
    }

    # ---------- 用例明细 ----------
    case_rows = []
    for c in case_results:
        tr = c.trace
        case_rows.append({
            "case_id": c.case_id,
            "name": c.name,
            # 提示词全文：供轮次列表做摘要预览，快速定位是哪一轮
            "prompt": c.prompt or "",
            "status": c.status,
            "session_id": c.session_id,
            "tool_calls": len(tr.tool_calls) if tr else 0,
            "thinking_steps": len(tr.thinking_steps) if tr else 0,
            "findings": len(c.findings),
            "p0": sum(1 for f in c.findings if f.severity == "P0"),
            "p1": sum(1 for f in c.findings if f.severity == "P1"),
            "elapsed_s": round(c.elapsed_s, 1),
            "ui_error": c.ui_error,
            # 等满等待上限属中性信息（非异常），报告里单独以灰色提示展示
            "waited_limit": bool(getattr(c, "waited_limit", False)),
            "wait_note": getattr(c, "wait_note", ""),
            # 平台在该用例在途期间自动点击确认的次数（全流程标记）
            "auto_confirms": int(getattr(c, "auto_confirms", 0)),
            "tools": tr.tool_names if tr else [],
            "answer_chars": len(tr.final_answer) if tr else 0,
        })

    # ---------- 问题发现（仅本轮，含表现与影响） ----------
    recipe_by_key = {}
    for r in recipes or []:
        recipe_by_key[r.key if hasattr(r, "key") else r.get("key")] = \
            r.to_dict() if hasattr(r, "to_dict") else r

    findings_rows = []
    for f in sorted(findings, key=lambda x: (SEVERITY_ORDER.get(x.severity, 9), x.rule)):
        row = {
            "rule": f.rule,
            "name": RULES.get(f.rule, ("", ""))[1],
            "severity": f.severity,
            "session_id": f.session_id,
            "tool": f.tool,
            "detail": f.detail,          # 具体表现
            "impact": RULE_IMPACT.get(f.rule, ""),   # 影响
            "evidence": f.evidence,
            "step_index": f.step_index,
            "case_id": next((c.case_id for c in case_results if c.session_id == f.session_id), ""),
        }
        rep = recipe_by_key.get(f"{f.rule}:{f.tool or '-'}")
        if rep:
            row["repro"] = {
                "prompt": rep.get("prompt", ""),
                "prompt_source": rep.get("prompt_source", ""),
                "verify_desc": rep.get("verify_desc", ""),
                "expected": rep.get("expected", ""),
                "actual": rep.get("actual", ""),
                "attempts": rep.get("attempts", 0),
                "hits": rep.get("hits", 0),
                "rate": rep.get("rate"),
                "stability": rep.get("stability", ""),
                "run_sessions": rep.get("run_sessions", []),
            }
        findings_rows.append(row)

    repro_rows, repro_summary = repro_block(recipes)

    return {
        "generated_at": "",
        "summary": {
            "cases": len(case_results),
            "passed": len(passed),
            "failed": len(failed),
            "ui_failed": len(ui_failed),
            "pass_rate": _pct(len(passed), len(case_results)),
            "findings": len(findings),
            "p0": sev_counter.get("P0", 0),
            "p1": sev_counter.get("P1", 0),
            "tool_calls_total": call_total,
            "tool_calls_failed": fails,
            "tool_fail_rate": _pct(fails, call_total),
            "tool_calls_truncated": truncs,
            "elapsed_total_s": round((stage_times or {}).get("total", 0), 1),
        },
        "objective": objective,
        "case_objective": case_objective,
        "skill_rows": skill_rows,
        "rule_rows": rule_rows,
        "severity": severity,
        "tool_rows": tool_rows,
        "coverage": coverage,
        "case_rows": case_rows,
        "findings_rows": findings_rows,
        "repro_rows": repro_rows,
        "repro_summary": repro_summary,
    }
