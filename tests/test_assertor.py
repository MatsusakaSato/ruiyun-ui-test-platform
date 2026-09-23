"""assertor 工具错误判定与误判防护单元测试。"""
import json
import unittest

from core.assertor import check_tool_failed
from core.models import ExecutionTrace, ToolCall


class TestAssertorToolFailed(unittest.TestCase):
    def setUp(self):
        self.trace = ExecutionTrace(session_id="test_sess")
        self.cfg = {
            "error_markers": [
                "[ERROR]",
                "[error]",
                "failed to",
                "traceback",
                "timed out",
                "timeout",
                "connection aborted",
                "max retries exceeded",
                "tool_call_failed",
                "exception",  # 保持即使配置带 exception，也能准确识别不误报
            ]
        }

    def test_read_skill_file_prose_exception_not_failed(self):
        """测试 read_skill_file 返回的内容中包含英文单词 'exception'，绝不能误判为失败。"""
        raw_json = json.dumps({
            "success": True,
            "skill_ref": "skill_ref_3a2ce0a39ae3",
            "skill_name": "local-doc-edit",
            "relative_path": "SKILL.md",
            "file_content": (
                "The host recognizes same-request, unpublished Word drafts and "
                "classifies matching-version continuation as low risk. No extra draft "
                "flag or finalize tool is needed. This exception ends with the request "
                "and does not cover existing user files, prior deliveries..."
            ),
        }, ensure_ascii=False)

        tc = ToolCall(
            index=1,
            tool_call_id="call_1",
            name="read_skill_file",
            arguments={"skill_name": "local-doc-edit", "relative_path": "SKILL.md"},
            raw_result=raw_json,
            result_obj=json.loads(raw_json),
        )

        findings = check_tool_failed(tc, self.trace, self.cfg)
        self.assertEqual(len(findings), 0, f"显式 success: True 的结果不应报警: {findings}")

    def test_content_payload_exclusion_without_explicit_success(self):
        """测试未带 success 字段的对象，正文中的 exception 同样不应误报。"""
        raw_json = json.dumps({
            "skill_ref": "skill_ref_123",
            "relative_path": "SKILL.md",
            "file_content": "With one minor exception, the document is complete.",
        })
        tc = ToolCall(
            index=1,
            tool_call_id="call_2",
            name="read_file",
            arguments={"path": "SKILL.md"},
            raw_result=raw_json,
            result_obj=json.loads(raw_json),
        )
        findings = check_tool_failed(tc, self.trace, self.cfg)
        self.assertEqual(len(findings), 0, "正文字段包含普通 exception 不应误报")

    def test_real_exception_in_text_triggers_failure(self):
        """测试文本中真实的程序异常抛出（带冒号或异常上下文），能正常报警。"""
        raw_err = "Unhandled Exception: database connection failed\n  at db.connect()"
        tc = ToolCall(
            index=1,
            tool_call_id="call_3",
            name="query_db",
            arguments={"sql": "SELECT 1"},
            raw_result=raw_err,
            result_obj=None,
        )
        findings = check_tool_failed(tc, self.trace, self.cfg)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].rule, "TOOL_CALL_FAILED")

    def test_explicit_error_in_dict_triggers_failure(self):
        """测试字典中显式 success=False 或包含 error 字段能够正确报警。"""
        raw_json = json.dumps({
            "success": False,
            "error": "File not found: document.docx",
        })
        tc = ToolCall(
            index=1,
            tool_call_id="call_4",
            name="read_file",
            arguments={"path": "document.docx"},
            raw_result=raw_json,
            result_obj=json.loads(raw_json),
        )
        findings = check_tool_failed(tc, self.trace, self.cfg)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].rule, "TOOL_CALL_FAILED")
        self.assertIn("success=false", findings[0].detail)

    def test_empty_result_triggers_missing(self):
        """测试空结果触发 TOOL_RESULT_MISSING。"""
        tc = ToolCall(
            index=1,
            tool_call_id="call_5",
            name="some_tool",
            arguments={},
            raw_result="",
            result_obj=None,
        )
        findings = check_tool_failed(tc, self.trace, self.cfg)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].rule, "TOOL_RESULT_MISSING")


if __name__ == "__main__":
    unittest.main()
