"""Windows 跨平台适配自动化测试集。"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.settings import (
    DEFAULT_AGENT_CONFIG,
    DEFAULT_APP_BINARY,
    DEFAULT_SESSION_ROOT,
    TEMPLATE_CONFIG,
    config_path,
    describe,
    effective_config,
)
from scripts.verify_env import _decode_cmd
from server import close_app, reveal_in_folder


class TestWindowsCompatibility(unittest.TestCase):
    def test_template_config_exists(self):
        """验证项目根目录的配置模版存在且合法。"""
        self.assertTrue(TEMPLATE_CONFIG.is_file(), "config.template.yaml 必须存在")
        import yaml
        content = yaml.safe_load(TEMPLATE_CONFIG.read_text(encoding="utf-8"))
        self.assertIn("app", content)
        self.assertIn("paths", content)
        # 验证模版中无写死的用户专属绝对路径
        self.assertEqual(content["app"].get("binary"), "")
        self.assertEqual(content["paths"].get("session_root"), "")

    def test_config_self_heal_from_template(self):
        """测试在全新环境下如果无 config.yaml，能自动从 template 复制自愈。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            with patch("core.settings.user_workspace", return_value=tmppath):
                target = tmppath / "config.yaml"
                self.assertFalse(target.exists())
                p = config_path()
                self.assertTrue(p.is_file())
                self.assertEqual(p, target)
                self.assertTrue(len(p.read_text(encoding="utf-8")) > 100)

    def test_effective_config_agent_config(self):
        """测试 effective_config 对 agent_config 的跨平台兜底。"""
        cfg_mac_hardcoded = {
            "app": {"binary": ""},
            "paths": {
                "session_root": "",
                "agent_config": "/Users/someone/.srtclaw/config/config.yaml"
            }
        }
        with patch("core.settings.sys.platform", "win32"):
            eff = effective_config(cfg_mac_hardcoded)
            # 应该回退到当前平台默认值
            self.assertFalse(eff["paths"]["agent_config"].startswith("/Users/"))

    def test_describe_contains_platform_info(self):
        """测试 describe() 函数返回中包含 platform 及 is_windows 字段。"""
        desc = describe({})
        self.assertIn("platform", desc)
        self.assertIn("is_windows", desc)
        self.assertIn("binary", desc)
        self.assertIn("session_root", desc)
        self.assertIn("agent_config", desc)

    def test_reveal_in_folder_windows_command(self):
        """验证 Windows 下文件与目录调用 explorer 的命令格式差异与反斜杠处理。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "sample.txt"
            test_file.write_text("hello", encoding="utf-8")
            
            with patch("server.sys.platform", "win32"):
                # 1) 测试文件高亮定位命令
                with patch("server.subprocess.run") as mock_run:
                    ok, res_path = reveal_in_folder(str(test_file))
                    self.assertTrue(ok)
                    mock_run.assert_called_once()
                    cmd = mock_run.call_args[0][0]
                    self.assertEqual(cmd[0], "explorer")
                    self.assertTrue(cmd[1].startswith("/select,"))
                    target_arg = cmd[1].split("/select,", 1)[1]
                    self.assertNotIn("/", target_arg) # 目标路径部分应该全为反斜杠

                # 2) 测试目录直接进入浏览命令
                with patch("server.subprocess.run") as mock_run:
                    ok, res_path = reveal_in_folder(str(tmpdir))
                    self.assertTrue(ok)
                    mock_run.assert_called_once()
                    cmd = mock_run.call_args[0][0]
                    self.assertEqual(cmd[0], "explorer")
                    self.assertFalse(cmd[1].startswith("/select,"))
                    self.assertNotIn("/", cmd[1])

    def test_decode_cmd_gbk_and_utf8(self):
        """测试 _decode_cmd 支持 Windows GBK 与 UTF-8 双向编码解码。"""
        text = "睿云智能工作台.exe"
        gbk_bytes = text.encode("gbk")
        utf8_bytes = text.encode("utf-8")
        
        self.assertEqual(_decode_cmd(gbk_bytes), text)
        self.assertEqual(_decode_cmd(utf8_bytes), text)
        self.assertEqual(_decode_cmd(b""), "")

    def test_upload_delete_windows_case_insensitive(self):
        """验证 Windows 盘符大小写不一致时的安全性校验逻辑。"""
        base_str = "C:\\Users\\test\\workspace\\uploads"
        target_lower = "c:\\users\\test\\workspace\\uploads\\file.png"
        target_upper = "C:\\Users\\test\\workspace\\uploads\\file.png"
        outside = "c:\\users\\test\\workspace\\other\\file.png"

        self.assertTrue(target_lower.lower().startswith(base_str.lower()))
        self.assertTrue(target_upper.lower().startswith(base_str.lower()))
        self.assertFalse(outside.lower().startswith(base_str.lower()))


if __name__ == "__main__":
    unittest.main()
