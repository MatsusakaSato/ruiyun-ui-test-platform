"""平台数据模型：把原始日志规范化为可断言的执行轨迹。"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


@dataclass
class ToolCall:
    """一次工具调用的完整快照。"""
    index: int                      # 在 timelineSteps 中的序号
    tool_call_id: str
    name: str
    arguments: Any                  # 原始参数
    raw_result: Optional[str]       # 结果文本（字符串化后）
    result_obj: Any = None          # 若结果是 JSON / dict，保留结构化形态
    # 内层正文（如 file_content / content）与其来源字段名。
    # 长度与截断判定用它，避免被外层 JSON 外壳干扰。
    body: str = ""
    body_from: str = ""
    event_type: str = ""
    session_id: str = ""
    request_id: str = ""
    duration_ms: Optional[float] = None
    # 触发本次调用的用户提问（该调用之前最近的一条 user 消息）。
    # 会话可能包含多轮对话，首条提问不等于本次调用的触发源。
    turn_prompt: str = ""
    # 消息级时间锚：所属 assistant 消息的 timestamp / completedAt。
    # 日志没有步骤级时间戳，这两个字段是「自动确认事件混排时间线」能拿到的最细粒度。
    msg_at: str = ""
    msg_done_at: str = ""
    # 派生判定
    failed: bool = False
    fail_reason: str = ""
    truncated: bool = False
    empty_required_arg: list = field(default_factory=list)

    @property
    def result_len(self) -> int:
        return len(self.raw_result) if isinstance(self.raw_result, str) else 0

    @property
    def body_len(self) -> int:
        """内层正文长度（无正文时退回结果外壳长度）。"""
        return len(self.body) if self.body else self.result_len

    def signature(self) -> str:
        """参数指纹，用于重复调用检测（忽略键顺序）。"""
        import json
        try:
            return json.dumps(self.arguments, ensure_ascii=False, sort_keys=True)
        except Exception:
            return str(self.arguments)


@dataclass
class ThinkingStep:
    index: int
    content: str
    # 消息级时间锚（同 ToolCall）：日志只到消息粒度，步骤级时间戳不存在
    msg_at: str = ""
    msg_done_at: str = ""


@dataclass
class ExecutionTrace:
    """一个会话（一次端到端执行）的规范化轨迹。"""
    session_id: str
    title: str = ""
    status: str = ""
    mode: str = ""
    created_at: str = ""
    updated_at: str = ""
    user_prompt: str = ""
    final_answer: str = ""
    reasoning_chars: int = 0
    tool_calls: list = field(default_factory=list)      # list[ToolCall]
    thinking_steps: list = field(default_factory=list)   # list[ThinkingStep]
    # 每条 assistant 消息的时间区间与步骤区间：{"at","done_at","first_step","last_step"}
    # 用于把自动确认事件（epoch 秒）近似落到所属响应段 —— 步骤级时间戳日志未记录
    message_spans: list = field(default_factory=list)
    associated_tool_call_ids: list = field(default_factory=list)
    is_streaming: bool = False
    completed_at: str = ""
    raw_message_count: int = 0
    source_file: str = ""
    error: str = ""
    # --- 客观耗时（日志时间戳直接可得；粒度到会话/消息级） ---
    user_at: str = ""            # 用户提问时间
    assistant_at: str = ""       # 模型开始响应时间
    # 派生（秒），解析时计算；日志无单步耗时，不做步骤级耗时
    first_response_s: float = -1.0   # 首响延迟：user_at -> assistant_at
    generation_s: float = -1.0       # 生成时长：assistant_at -> completed_at
    session_s: float = -1.0          # 会话时长：created_at -> updated_at
    turn_count: int = 0              # 用户提问轮数（= 模型响应次数）

    @property
    def tool_names(self) -> list:
        return [t.name for t in self.tool_calls]

    @property
    def orphan_ids(self) -> list:
        """关联 ID 与实际调用 ID 的差集（链路完整性）。"""
        declared = set(self.associated_tool_call_ids or [])
        actual = {t.tool_call_id for t in self.tool_calls}
        return sorted(declared.symmetric_difference(actual))


@dataclass
class Finding:
    """一条命中的硬 bug。"""
    rule: str            # 规则 ID
    severity: str        # P0 / P1 / P2
    session_id: str
    detail: str
    tool: str = ""
    step_index: Optional[int] = None
    evidence: str = ""


@dataclass
class CaseResult:
    """一个测试用例的执行与断言结果。"""
    case_id: str
    name: str
    prompt: str = ""
    expected_tools: list = field(default_factory=list)
    session_id: str = ""
    trace: Optional[ExecutionTrace] = None
    findings: list = field(default_factory=list)   # list[Finding]
    ui_ok: bool = False
    ui_error: str = ""
    # 本轮投递给该用例的附件（本机绝对路径）与投递结果说明
    attachments: list = field(default_factory=list)
    attach_note: str = ""
    # 等满等待上限（非异常，仅表示本次没等到日志稳定就出结果）
    waited_limit: bool = False
    wait_note: str = ""
    # 该用例在途期间平台自动点击确认的次数（含计划确认 / 工具授权）
    auto_confirms: int = 0
    # 归属到本用例的自动确认事件（驱动层按会话归属后填充），供轨迹层混排进时间线。
    # 不进入 to_dict 输出：事件明细已由 round_detail 的时间线条目承载。
    confirm_events: list = field(default_factory=list)
    elapsed_s: float = 0.0

    @property
    def passed(self) -> bool:
        return self.ui_ok and not self.findings

    @property
    def status(self) -> str:
        if not self.ui_ok:
            return "UI_FAIL"
        return "PASS" if not self.findings else "FAIL"

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("trace", None)
        d.pop("confirm_events", None)   # 事件明细由 round_detail 的时间线条目承载
        d["passed"] = self.passed
        d["status"] = self.status
        return d
