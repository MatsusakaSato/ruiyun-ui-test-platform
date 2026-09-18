"""日志解析器：把 .srtclaw 会话日志规范化为 ExecutionTrace。

实测日志结构（v2.0.14）:
  session/sess_xxx/session.meta.json      -> 会话元信息
  session/sess_xxx/session.messages.json  -> {"messages": [user, assistant]}
  assistant 消息含 timelineSteps: [{type:"thinking"|"tool_call", ...}]
  tool_call 步骤: {toolCallId, name, arguments, result:{event_type, result, ...}}

工具 result 实际存在三种形态，必须全部兼容：
  1) 纯文本      "[ERROR]: failed to fetch webpage ..."
  2) JSON 字符串 '{"success": true, "md_file_name": "..."}'
  3) Python repr "{'success': False, 'error': '路径必须为 ...'}"
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
from typing import Any, Optional

from core.models import ExecutionTrace, ToolCall, ThinkingStep


# ----------------------------- 结果形态解包 -----------------------------

# 工具结果外层包装的标识字段：命中说明这是「信封」，
# 应继续下钻到 result 取真正的业务返回。
_WRAPPER_KEYS = ("event_type", "tool_name", "tool_call_id")


def unwrap_result(raw: Any) -> tuple[Optional[str], Any]:
    """返回 (文本形态, 结构化形态)。文本形态统一用于长度/截断/错误判定。

    日志里的 result 常见两层包装：
      {"event_type": "...", "result": "<业务返回>", "tool_name": ...}
    必须下钻取出 <业务返回>，否则长度/截断判定会算在外壳上。
    （早期用 len(raw)<=8 判断，但信封含 metadata 时键数会超过 8，导致漏下钻。）
    """
    if raw is None:
        return None, None

    if isinstance(raw, dict):
        inner = raw.get("result")
        is_wrapper = (inner is not None
                      and any(k in raw for k in _WRAPPER_KEYS))
        if is_wrapper:
            return unwrap_result(inner)
        text = json.dumps(raw, ensure_ascii=False)
        return text, raw

    if isinstance(raw, (int, float, bool)):
        return str(raw), raw

    if not isinstance(raw, str):
        return str(raw), raw

    # 字符串：尝试解出结构化形态（保持原文作为文本形态）
    stripped = raw.strip()
    if stripped[:1] in "{[":
        for loader in (json.loads, ast.literal_eval):
            try:
                obj = loader(stripped)
                if isinstance(obj, (dict, list)):
                    return raw, obj
            except Exception:
                continue
    return raw, None


# 结构化返回中承载「正文」的字段（按优先级）。
# 截断/长度判定必须针对这些内层正文，而不是外层 JSON 外壳 ——
# 否则外壳的 `"}` 会被当成自然收尾，导致 JSON 类结果永不判截断。
BODY_FIELDS = ("file_content", "content", "text", "markdown", "markdown_content",
               "body", "data", "answer", "result_text")


def extract_body(text: Optional[str], obj: Any) -> tuple[str, str]:
    """从工具返回中取出「正文」，用于长度与截断判定。

    返回 (正文, 来源说明)。取不到则退回原始文本，来源标注 raw。
    """
    if isinstance(obj, dict):
        for k in BODY_FIELDS:
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                return v, k
        # 列表型正文（如搜索结果数组）
        for k in BODY_FIELDS:
            v = obj.get(k)
            if isinstance(v, list) and v:
                return json.dumps(v, ensure_ascii=False), f"{k}[]"
    return (text or ""), "raw"


def _detect_empty_required_args(name: str, arguments: Any) -> list:
    """检测「必填参数为空」被放行的情况（真实案例：
    convert_markdown_to_docx 传 markdown_content=""，接口仍返回 success=true）。"""
    empty = []
    if not isinstance(arguments, dict):
        return empty
    # 各工具的关键必填参数映射
    required = {
        "mcp_write_workspace_file": ["relative_path", "content"],
        "convert_markdown_to_docx": ["md_file_name", "markdown_content"],
        "read_memory": ["path"],
        "mcp_fetch_webpage": ["url"],
        "mcp_paid_search": ["query"],
        "todo_complete": ["idx"],
    }.get(name)
    if not required:
        # 兜底：任意名为 content/query/url/path 的参数为空
        required = [k for k in ("content", "query", "url", "path") if k in arguments]
    for key in required:
        if key in arguments:
            val = arguments[key]
            if val is None or (isinstance(val, str) and not val.strip()):
                empty.append(key)
    return empty


# ----------------------------- 会话解析 -----------------------------

def _load_json(path: Path) -> Optional[Any]:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def parse_session(session_dir: Path) -> Optional[ExecutionTrace]:
    """解析单个会话文件夹。"""
    meta = _load_json(session_dir / "session.meta.json") or {}
    payload = _load_json(session_dir / "session.messages.json")
    if not isinstance(payload, dict):
        return None

    msgs = payload.get("messages") or []
    trace = ExecutionTrace(
        session_id=meta.get("session_id") or session_dir.name,
        title=meta.get("title", ""),
        status=meta.get("status", ""),
        mode=meta.get("mode", ""),
        created_at=meta.get("created_at", ""),
        updated_at=meta.get("updated_at", ""),
        raw_message_count=len(msgs),
        source_file=str(session_dir),
    )

    step_index = 0
    current_turn_prompt = ""   # 触发当前步骤的用户提问（多轮会话下逐轮更新）
    last_user_at = ""          # 最近一条用户提问时间（首响延迟计算用）
    for msg in msgs:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")

        if role == "user":
            # 多轮会话：后发消息会追加到同一会话，需跟随更新触发源
            current_turn_prompt = msg.get("content") or ""
            last_user_at = msg.get("timestamp") or ""
            trace.turn_count += 1
            if not trace.user_prompt:
                trace.user_prompt = current_turn_prompt
                trace.user_at = last_user_at
            continue

        if role != "assistant":
            continue

        trace.reasoning_chars += len(msg.get("reasoningContent") or "")
        trace.associated_tool_call_ids.extend(msg.get("associatedToolCallIds") or [])
        if msg.get("isStreaming"):
            trace.is_streaming = True
        if msg.get("completedAt"):
            trace.completed_at = msg["completedAt"]
        if msg.get("timestamp"):
            # 多轮会话取最早/最新响应时间：首条用于首响延迟
            if not trace.assistant_at:
                trace.assistant_at = msg["timestamp"]
        # 最后一条 assistant 的 content 视为最终答复
        content = msg.get("content")
        if content is not None:
            trace.final_answer = content

        span_first = step_index        # 本条 assistant 消息的步骤起点（消息级时间锚）
        for step in (msg.get("timelineSteps") or []):
            if not isinstance(step, dict):
                continue
            stype = step.get("type")
            if stype == "thinking":
                trace.thinking_steps.append(
                    ThinkingStep(index=step_index, content=step.get("content") or "",
                                 msg_at=msg.get("timestamp") or "",
                                 msg_done_at=msg.get("completedAt") or "")
                )
            elif stype == "tool_call":
                raw_res = step.get("result")
                text, obj = unwrap_result(raw_res)
                body, body_from = extract_body(text, obj)
                meta_res = raw_res if isinstance(raw_res, dict) else {}
                tc = ToolCall(
                    index=step_index,
                    tool_call_id=step.get("toolCallId") or "",
                    name=step.get("name") or "",
                    arguments=step.get("arguments"),
                    raw_result=text,
                    result_obj=obj,
                    body=body,
                    body_from=body_from,
                    event_type=meta_res.get("event_type", ""),
                    session_id=meta_res.get("session_id", "") or trace.session_id,
                    request_id=meta_res.get("request_id", "") or msg.get("request_id", ""),
                    empty_required_arg=_detect_empty_required_args(
                        step.get("name") or "", step.get("arguments")
                    ),
                    turn_prompt=current_turn_prompt,
                    msg_at=msg.get("timestamp") or "",
                    msg_done_at=msg.get("completedAt") or "",
                )
                trace.tool_calls.append(tc)
            step_index += 1

        # 记录本条响应的时间区间与步骤区间：自动确认事件只能按消息粒度近似落段
        # （日志未记录步骤级时间戳，见 core/trajectory._merge_confirm_events）
        span_last = step_index - 1
        if span_last >= span_first:
            trace.message_spans.append({
                "at": msg.get("timestamp") or "",
                "done_at": msg.get("completedAt") or "",
                "first_step": span_first,
                "last_step": span_last,
            })

    # ---- 派生耗时（秒）：日志仅有会话/消息级时间戳，步骤级耗时不可得 ----
    def _ts(s: str):
        if not s:
            return None
        try:
            from datetime import datetime
            return datetime.fromisoformat(s)
        except Exception:
            return None

    t_user, t_asst, t_done = _ts(trace.user_at), _ts(trace.assistant_at), _ts(trace.completed_at)
    t_created, t_updated = _ts(trace.created_at), _ts(trace.updated_at)
    if t_user and t_asst:
        trace.first_response_s = round((t_asst - t_user).total_seconds(), 2)
    if t_asst and t_done:
        trace.generation_s = round((t_done - t_asst).total_seconds(), 2)
    elif t_user and t_done:
        trace.generation_s = round((t_done - t_user).total_seconds(), 2)
    if t_created and t_updated:
        trace.session_s = round((t_updated - t_created).total_seconds(), 2)

    return trace


def discover_sessions(session_root: str, since_mtime: Optional[float] = None) -> list:
    """列出会话目录（按创建时间倒序）。since_mtime 用于只取新增会话。"""
    root = Path(session_root)
    if not root.is_dir():
        return []
    out = []
    for child in root.iterdir():
        if not child.is_dir() or not child.name.startswith("sess_"):
            continue
        try:
            mt = child.stat().st_mtime
        except OSError:
            continue
        if since_mtime is not None and mt <= since_mtime:
            continue
        out.append((mt, child))
    out.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in out]


def latest_session_id(session_root: str) -> Optional[str]:
    sessions = discover_sessions(session_root)
    return sessions[0].name if sessions else None


def load_all(session_root: str) -> list:
    """批量解析全部会话，跳过损坏文件。"""
    traces = []
    for d in discover_sessions(session_root):
        t = parse_session(d)
        if t is not None:
            traces.append(t)
    return traces
