"""复现配方生成与复现率验证。

核心思想：bug 只有能被稳定复现才有修复价值。这里做两件事：
  1. ReproRecipe —— 从日志反推每条 bug 的最小复现路径
     （提示词 → 预期触发工具 → 验证标准），优先使用原始会话的真实提问。
  2. verify()   —— 用 UI 自动化把同一配方跑 N 次，统计断言再次命中率，
     量化出「必现 / 高概率 / 偶发」。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Optional

from core.assertor import RULES
from core.models import CaseResult, ExecutionTrace, Finding


# 复现率分级
STABLE_THRESHOLD = 1.0     # 100% → 必现
LIKELY_THRESHOLD = 0.6     # ≥60% → 高概率复现，否则偶发


@dataclass
class ReproRecipe:
    """一条 bug 的最小复现路径。"""
    key: str                  # rule:tool 唯一键
    rule: str
    rule_name: str
    severity: str
    tool: str = ""
    # 复现输入
    prompt: str = ""
    prompt_source: str = ""   # original（原始会话提问）/ synthesized（按工具合成）
    source_session: str = ""
    # 触发点与证据
    trigger_args: dict = field(default_factory=dict)
    evidence: str = ""
    # 验证标准
    verify_rule: str = ""
    verify_desc: str = ""
    expected: str = ""        # 预期正确行为
    actual: str = ""          # 实际错误行为
    # UI 操作步骤
    steps: list = field(default_factory=list)
    # 验证结果（跑过才有）
    attempts: int = 0
    hits: int = 0
    rate: Optional[float] = None
    stability: str = ""       # 必现 / 高概率复现 / 偶发
    run_sessions: list = field(default_factory=list)   # [{session_id, hit}]
    occurrences: int = 0      # 该签名在日志中的出现次数
    error: str = ""

    @property
    def key_tuple(self) -> tuple:
        return (self.rule, self.tool)

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------- 提示词合成 ---------------------------

# 工具 → 最易触发它的提示词（原始会话提问缺失时的兜底）
TOOL_PROMPTS = {
    "read_memory": "请读取你的记忆文件，然后告诉我里面当前有多少条记录。",
    "mcp_write_workspace_file": "请在工作区新建一个文件，文件名 repro_test.txt，内容写「复现验证」，完成后告诉我完整路径。",
    "mcp_paid_search": "请搜索李白的详细生平资料并整理介绍，内容尽量详细完整。",
    "mcp_fetch_webpage": "请帮我抓取 https://baike.baidu.com/item/李白/1043 这个网页的正文内容。",
    "convert_markdown_to_docx": "请把一段 Markdown 文档转换成 docx 并保存到工作区。",
    "academic_resource_collector": "请帮我检索「李白生平研究」相关的学术文献综述。",
    "todo_create": "请帮我列一个 3 步的任务清单来规划这次资料整理。",
    "todo_complete": "请帮我列一个 3 步的任务清单并逐项完成它。",
}


def _verify_desc(rule: str, tool: str, cfg: dict) -> str:
    r = cfg.get("rules", {})
    if rule == "TOOL_CALL_FAILED":
        return f"复现会话日志中出现 {tool} 调用，且结果命中错误特征（success=false / error 字段 / [ERROR] 等）"
    if rule == "EMPTY_REQUIRED_ARG":
        return f"复现会话日志中 {tool} 被调用时必填参数为空，且调用未被拦截"
    if rule in ("LOOP_CONSECUTIVE", "LOOP_TOTAL"):
        return (f"复现会话日志中 {tool} 调用次数达到阈值"
                f"（连续 ≥ {r.get('loop_consecutive_threshold', 5)} 或总数 ≥ {r.get('loop_total_threshold', 8)}）")
    if rule == "DUPLICATE_CALL":
        return f"复现会话日志中 {tool} 出现完全相同参数的重复调用"
    return f"复现会话日志中再次命中断言规则 {rule}"


def _expected_vs_actual(rule: str, tool: str, f: Finding) -> tuple:
    if rule == "TOOL_CALL_FAILED":
        return (f"{tool} 返回调用成功或明确的业务结果",
                f"{tool} 返回失败：{(f.evidence or '')[:120]}")
    if rule == "EMPTY_REQUIRED_ARG":
        return (f"{tool} 校验必填参数并拒绝空值调用",
                f"空参数调用被放行：{(f.evidence or '')[:120]}")
    return ("调用行为正常收敛", f.detail)


def build_recipe(finding: Finding, traces_by_sid: dict, cfg: dict) -> ReproRecipe:
    """从一条 Finding 构建最小复现配方。优先取原始会话的真实提问。"""
    rule, tool = finding.rule, finding.tool
    trace: Optional[ExecutionTrace] = traces_by_sid.get(finding.session_id)

    prompt, source = "", ""
    trigger_args: dict = {}
    if trace:
        # 取触发该调用的那一轮提问（多轮会话下 ≠ 首条提问）；
        # 同一工具多次命中时选提问最短的一次，最接近最小复现路径
        candidates = [tc for tc in trace.tool_calls
                      if not tool or tc.name == tool]
        candidates = [tc for tc in candidates if (tc.turn_prompt or "").strip()] or candidates
        if candidates:
            best = min(candidates, key=lambda tc: len(tc.turn_prompt or ""))
            trigger_args = best.arguments if isinstance(best.arguments, dict) else {}
            if (best.turn_prompt or "").strip():
                prompt, source = best.turn_prompt.strip(), "original"
        if not prompt and trace.user_prompt.strip():
            prompt, source = trace.user_prompt.strip(), "original"

    if not prompt:
        prompt = TOOL_PROMPTS.get(tool, trace.user_prompt if trace else "")
        source = "synthesized" if prompt else "unknown"


    recipe = ReproRecipe(
        key=f"{rule}:{tool or '-'}",
        rule=rule,
        rule_name=RULES.get(rule, ("", rule))[1],
        severity=finding.severity,
        tool=tool,
        prompt=prompt,
        prompt_source=source,
        source_session=finding.session_id,
        trigger_args=trigger_args,
        evidence=finding.evidence,
        verify_rule=rule,
        verify_desc=_verify_desc(rule, tool, cfg),
    )
    recipe.expected, recipe.actual = _expected_vs_actual(rule, tool, finding)
    recipe.steps = [
        "启动睿云智能工作台，进入「新建任务」首页",
        f"在输入框原样输入以下提示词：{prompt}",
        "发送后等待任务执行结束（输入框清空且回复停止滚动）",
        f"打开日志目录 {cfg['paths']['session_root']} 下最新会话的 session.messages.json",
        "按验证标准核对（也可直接用 run_repro.py 自动核对）",
    ]
    return recipe


def _hit_signature(finding: Finding, trace: ExecutionTrace) -> bool:
    """该会话是否真的命中了这条 bug 签名（存在对应工具调用）。"""
    if not finding.tool:
        return True
    return any(tc.name == finding.tool for tc in trace.tool_calls)


def build_recipes(findings: list, traces: list, cfg: dict) -> list:
    """按 (rule, tool) 去重聚合配方，一条 bug 签名一份配方。

    同一签名可能被多个会话命中：优先取「提问最短」的会话作为复现源，
    因为越短的提示词越接近最小复现路径，复现成本与不确定性都更低。
    """
    from core.assertor import run_assertions
    from run_pipeline import read_max_iterations

    traces_by_sid = {t.session_id: t for t in traces}
    max_iter = read_max_iterations(cfg)

    # 先对每个会话跑一遍断言，建立 签名 → [会话] 的倒排表
    sig_sessions: dict = {}
    for t in traces:
        for f in run_assertions(t, cfg["rules"], max_iter):
            key = f"{f.rule}:{f.tool or '-'}"
            sig_sessions.setdefault(key, []).append((f, t))

    # 补上本轮用例的命中项（可能来自尚未落盘完成的 trace）
    for f in findings:
        key = f"{f.rule}:{f.tool or '-'}"
        t = traces_by_sid.get(f.session_id)
        if t is not None:
            pairs = sig_sessions.setdefault(key, [])
            if not any(x[1].session_id == t.session_id for x in pairs):
                pairs.append((f, t))

    recipes = []
    for key, pairs in sig_sessions.items():
        # 同签名下选「提问最短」的会话作为最小复现源
        def _rank(pair):
            f, t = pair
            p = (t.user_prompt or "").strip()
            return (0, len(p)) if p else (1, 9999)

        pairs = sorted(pairs, key=_rank)
        rep_finding, src_trace = pairs[0]
        recipe = build_recipe(rep_finding, {src_trace.session_id: src_trace}, cfg)
        if not recipe.prompt:
            continue
        recipe.occurrences = len(pairs)   # 该签名在日志中的出现次数
        recipes.append(recipe)
    return recipes


# --------------------------- 复现率验证 ---------------------------

def classify(rate: float) -> str:
    if rate >= STABLE_THRESHOLD:
        return "必现"
    if rate >= LIKELY_THRESHOLD:
        return "高概率复现"
    return "偶发"


def verify_recipe(recipe: ReproRecipe, driver, cfg: dict,
                  times: int = 3, log=print) -> ReproRecipe:
    """用 UI 自动化重复执行配方，统计断言命中率。

    判定口径：新会话再次命中同规则断言（工具相同时还要求同工具）即算复现。
    """
    from core.assertor import run_assertions
    from core.log_parser import parse_session

    recipe.attempts = 0
    recipe.hits = 0
    recipe.run_sessions = []
    max_iter = _read_max_iterations(cfg)

    for i in range(times):
        recipe.attempts += 1
        tag = f"[复现 {i + 1}/{times}] {recipe.key}"
        try:
            if not driver.reset_to_new_task():
                recipe.run_sessions.append({"session_id": "", "hit": False, "note": "无法回到首页"})
                log(f"    {tag} ✗ 无法回到新建任务首页")
                continue

            before = driver.snapshot_sessions()
            driver.type_text(recipe.prompt)
            time_sleep(0.5)
            driver.send()
            sess = driver.wait_new_session(before, timeout_s=90)
            if not sess:
                recipe.run_sessions.append({"session_id": "", "hit": False, "note": "未产生新会话"})
                log(f"    {tag} ✗ 未产生新会话")
                continue

            driver.wait_settled(sess, timeout_s=float(cfg.get("case_timeout_s", 1200)))
            trace = parse_session(sess)
            if trace is None:
                recipe.run_sessions.append({"session_id": sess.name, "hit": False, "note": "日志解析失败"})
                log(f"    {tag} ✗ 日志解析失败")
                continue

            findings = run_assertions(trace, cfg["rules"], max_iter)
            hit = any(f.rule == recipe.rule and
                      (not recipe.tool or f.tool == recipe.tool)
                      for f in findings)
            recipe.run_sessions.append({"session_id": sess.name, "hit": hit})
            if hit:
                recipe.hits += 1
            log(f"    {tag} {'✓ 复现' if hit else '✗ 未复现'}"
                f"（会话 {sess.name}，工具调用 {len(trace.tool_calls)} 次）")
        except Exception as exc:
            recipe.run_sessions.append({"session_id": "", "hit": False, "note": f"{type(exc).__name__}: {exc}"})
            log(f"    {tag} ✗ 异常 {type(exc).__name__}: {exc}")

    recipe.rate = round(recipe.hits / recipe.attempts, 3) if recipe.attempts else 0.0
    recipe.stability = classify(recipe.rate) if recipe.attempts else ""
    return recipe


def time_sleep(s: float) -> None:
    import time
    time.sleep(s)


def _read_max_iterations(cfg: dict) -> int:
    from pathlib import Path
    import yaml
    p = Path(cfg.get("paths", {}).get("agent_config", ""))
    if not p.is_file():
        return 0
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return int((data.get("react") or {}).get("max_iterations") or 0)
    except Exception:
        return 0
