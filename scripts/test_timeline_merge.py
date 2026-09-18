#!/usr/bin/env python3
"""注入式测试：平台自动确认事件 → 用例时间线的会话归属与近似混排。

不驱动应用、不联网：只构造「会话消息时间段 + 事件流」，验证
  * 事件按会话归属到正确的用例（并发下不串台）
  * 事件在所属消息段内按时间升序落在段尾，anchor / approximate 口径正确
  * 早于首段置顶、晚于末段或无可归段置底，绝不静默丢弃
  * 无轨迹（会话日志解析失败）时事件条目仍然输出
  * 无 message_spans 的历史轨迹退化为「步骤原序 + 事件置底」，行为可解释

为什么只能近似定位：会话日志只记录**消息级**时间戳，timelineSteps 内的步骤
本身没有时间字段（见 core/log_parser.parse_session），因此不存在「精确插到
第 N 次工具调用之后」的方案，这是当前数据的物理上限。
"""
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.trajectory import _merge_confirm_events  # noqa: E402
from run_pipeline import _events_for_session  # noqa: E402

SESS_A = "/w/session/sess_a"
SESS_B = "/w/session/sess_b"


def iso(ts: float) -> str:
    """epoch 秒 → 会话日志里那种「本地无时区 ISO 串」。"""
    return datetime.fromtimestamp(ts).isoformat()


def steps_of(*specs):
    """(index, type, name) → 与 build_case_detail 同构的 steps 列表。"""
    out = []
    for i, kind, name in specs:
        if kind == "thinking":
            out.append({"i": i, "type": "thinking", "n": 1,
                        "content": name, "chars": len(name)})
        else:
            out.append({"i": i, "type": "tool_call", "name": name, "status": "ok",
                        "result_len": 0, "raw_len": 0, "issues": []})
    return out


def trace_with(spans):
    return SimpleNamespace(message_spans=spans)


def ev(ts, text="本次允许", mode="keyword", sess=None):
    d = {"time": datetime.fromtimestamp(ts).strftime("%H:%M:%S"), "ts": ts,
         "mode": mode, "text": text, "cls": "", "q": ""}
    if sess:
        d["sess"] = sess
    return d


# --------------------------------------------------------------------- 测试
def test_segment_insertion():
    t0 = time.time()
    spans = [
        {"at": iso(t0), "done_at": iso(t0 + 2), "first_step": 0, "last_step": 1},
        {"at": iso(t0 + 2), "done_at": iso(t0 + 4), "first_step": 2, "last_step": 3},
    ]
    steps = steps_of((0, "thinking", "想一下"), (1, "tool", "read_skill_file"),
                     (2, "thinking", "再想一下"), (3, "tool", "mcp_write_workspace_file"))
    events = [ev(t0 + 3.5, text="本次允许"), ev(t0 + 3.0, text="仅本次授权执行")]
    merged = _merge_confirm_events(steps, trace_with(spans), events)
    got = [(m["type"], m.get("name") or m.get("text") or m.get("content")) for m in merged]
    assert got == [("thinking", "想一下"), ("tool_call", "read_skill_file"),
                   ("thinking", "再想一下"), ("tool_call", "mcp_write_workspace_file"),
                   ("auto_confirm", "仅本次授权执行"),
                   ("auto_confirm", "本次允许")], got
    a, b = merged[-2], merged[-1]
    assert a["approximate"] is True and b["approximate"] is True
    assert a["anchor"] == iso(t0 + 2) and b["anchor"] == iso(t0 + 2), (a["anchor"], b["anchor"])
    assert a["attribution"] == "time-window", a
    assert b["label"] == "关键词命中授权按钮", b
    print("PASS 1: 事件按消息段落入段尾 / 段内按时间升序 / 近似与锚点标注正确")


def test_edges_head_tail():
    t0 = time.time()
    spans = [{"at": iso(t0), "done_at": iso(t0 + 2), "first_step": 0, "last_step": 0}]
    steps = steps_of((0, "thinking", "开始"))
    merged = _merge_confirm_events(
        steps, trace_with(spans), [ev(t0 - 1, text="早于首段"), ev(t0 + 5, text="晚于末段")])
    assert merged[0]["type"] == "auto_confirm" and merged[0]["text"] == "早于首段", merged
    assert merged[1]["type"] == "thinking", merged
    assert merged[-1]["type"] == "auto_confirm" and merged[-1]["text"] == "晚于末段", merged
    print("PASS 2: 早于首段置顶 / 晚于末段置底")


def test_no_anchor_fallback():
    steps = steps_of((0, "thinking", "开始"), (1, "tool", "read_skill_file"))
    merged = _merge_confirm_events(steps, trace_with([]), [ev(0.0, text="无时间戳事件")])
    assert [m["type"] for m in merged] == ["thinking", "tool_call", "auto_confirm"], merged
    assert merged[-1]["approximate"] is True and merged[-1]["anchor"] == "", merged[-1]
    # 无事件 → 步骤原样返回（trace 为 None 也不例外）
    assert _merge_confirm_events(steps, None, []) == steps
    assert _merge_confirm_events(steps, None, None) == steps
    print("PASS 3: 无时间锚 → 置底且不丢 / 无事件时步骤原样")


def test_no_steps_still_emits():
    t0 = time.time()
    merged = _merge_confirm_events([], None, [ev(t0, sess=SESS_A)])
    assert len(merged) == 1 and merged[0]["type"] == "auto_confirm", merged
    assert merged[0]["attribution"] == "session", merged[0]
    print("PASS 4: 无轨迹（解析失败）仍输出事件条目并保留归属来源")


def test_session_attribution():
    t_send = time.time()
    events = [ev(t_send + 1, text="A 的点击", sess=SESS_A),
              ev(t_send + 2, text="B 的点击", sess=SESS_B),
              ev(t_send + 3, text="A 的第二次", sess=SESS_A)]
    a = _events_for_session(events, SESS_A, t_send)
    b = _events_for_session(events, SESS_B, t_send)
    assert [e["text"] for e in a] == ["A 的点击", "A 的第二次"], a
    assert [e["text"] for e in b] == ["B 的点击"], b
    # 旧事件（无 sess）回退「发送之后」时间窗，且绝不把带 sess 的事件误收进来
    legacy = [ev(t_send - 5, text="更早的旧事件"),
              ev(t_send + 1, text="旧事件"),
              ev(t_send + 1, text="新事件", sess=SESS_A)]
    old = _events_for_session(legacy, "/w/session/sess_c", t_send)
    assert [e["text"] for e in old] == ["旧事件"], old
    print("PASS 5: 并发下按会话归属互不串台 / 旧事件回退时间窗且不误收带 sess 事件")


def test_timeline_monotonic():
    t0 = time.time()
    spans = [{"at": iso(t0), "done_at": iso(t0 + 1), "first_step": 0, "last_step": 0},
             {"at": iso(t0 + 1), "done_at": iso(t0 + 2), "first_step": 1, "last_step": 1},
             {"at": iso(t0 + 2), "done_at": None, "first_step": 2, "last_step": 2}]
    steps = steps_of((0, "thinking", "a"), (1, "tool", "b"), (2, "thinking", "c"))
    events = [ev(t0 + 0.5, text="e1"), ev(t0 + 1.5, text="e2"), ev(t0 + 2.5, text="e3")]
    merged = _merge_confirm_events(steps, trace_with(spans), events)
    assert [m["type"] for m in merged] == ["thinking", "auto_confirm", "tool_call",
                                           "auto_confirm", "thinking", "auto_confirm"], merged
    print("PASS 6: 整条时间线按段单调（每段步骤 + 本段事件）")


if __name__ == "__main__":
    t0 = time.time()
    test_segment_insertion()
    test_edges_head_tail()
    test_no_anchor_fallback()
    test_no_steps_still_emits()
    test_session_attribution()
    test_timeline_monotonic()
    print(f"\n全部 6 项注入式测试通过 ✓（{time.time() - t0:.2f}s）")
