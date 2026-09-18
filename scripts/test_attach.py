#!/usr/bin/env python3
"""附件投递的注入式自测（不联网、不驱动真实应用）。

覆盖：
  1. 入参护栏：空列表直接通过；文件不存在直接判失败且不发起任何 CDP 调用
  2. 核心判定：**引用计数没有真的增加 → 必须判失败**
     （只看「有没有抛异常」会漏掉"拖了但没进去"，那会让用例在无附件的情况下跑完，
      即假通过 —— 这正是本模块存在的意义）
  3. 计数增加 → 通过，且文案含投递数量

为什么不用 mock 之外的方案：真实拖拽的端到端验证在开发期已实测
（`button.chat-input-btn--attach` 的 title 由「引用本地文件 (0/10)」变为「(1/10)」）；
本脚本负责把判定逻辑固定在 CI 可跑的形式里。

用法：python scripts/test_attach.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from drivers.ui_driver import RuiyunUIDriver  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
from core.settings import config_path, uploads_dir  # noqa: E402
REAL_FILE = uploads_dir() / "_selftest_probe.md"
COUNT_EXPR_KEY = "chat-input-btn--attach"


class _StubCDP:
    """假 CDP：把「引用计数」按预设序列返回，其余求值返回拖拽落点。"""

    def __init__(self, counts):
        self.counts = list(counts)
        self.i = 0
        self.calls = []

    def eval_js(self, expr, timeout=30, await_promise=True):
        if COUNT_EXPR_KEY in expr:
            v = self.counts[min(self.i, len(self.counts) - 1)]
            self.i += 1
            return v
        return {"x": 10, "y": 20}          # _composer_drop_point

    def call(self, method, params=None, timeout=30.0):
        self.calls.append(method)
        return {}


def _driver(counts):
    cfg = yaml.safe_load(config_path().read_text(encoding="utf-8"))
    drv = RuiyunUIDriver(cfg)
    drv.cdp = _StubCDP(counts)
    return drv


def test_guards():
    drv = _driver([0])
    assert drv.attach_files([]) == (True, ""), "空附件列表应直接通过"
    assert drv.cdp.calls == [], "空列表不应发起任何 CDP 调用"

    import tempfile
    fake_path = str(Path(tempfile.gettempdir()) / "definitely_not_here_9f3a.md")
    ok, msg = drv.attach_files([fake_path])
    assert ok is False, msg
    assert "不存在" in msg, msg
    assert drv.cdp.calls == [], "文件不存在时不应发起拖拽"
    print("PASS 1: 入参护栏（空列表通过 / 不存在直接失败且无副作用）")


def test_count_unchanged_is_failure():
    """拖拽发了但计数没动 → 必须判失败（这是防假通过的关键一条）。"""
    drv = _driver([0, 0, 0, 0, 0])
    ok, msg = drv.attach_files([str(REAL_FILE)], timeout_s=1.0)
    assert ok is False, f"计数未增加却判成功：{msg}"
    assert "引用计数未增加" in msg, msg
    assert "Input.dispatchDragEvent" in drv.cdp.calls, "应真的发起过拖拽事件"
    print("PASS 2: 计数未增加 → 判失败并说明原因（防假通过）")


def test_count_increased_is_success():
    drv = _driver([0, 1])
    ok, msg = drv.attach_files([str(REAL_FILE)], timeout_s=1.0)
    assert ok is True, msg
    assert "1" in msg and "投递 1 个" in msg, msg
    print("PASS 3: 计数增加 → 判成功且文案含投递数量")


def test_partial_batch_is_failure():
    """投 2 个只进去 1 个 → 判失败（不能按"至少进了一个"糊过去）。"""
    drv = _driver([0, 1, 1, 1])
    other = REAL_FILE.parent / "_selftest_probe2.md"
    other.write_text("x", encoding="utf-8")
    try:
        ok, msg = drv.attach_files([str(REAL_FILE), str(other)], timeout_s=1.0)
        assert ok is False, f"只进去 1/2 却判成功：{msg}"
        assert "期望 +2" in msg, msg
    finally:
        other.unlink(missing_ok=True)
    print("PASS 4: 部分投递 → 判失败（期望数量必须全部到位）")


if __name__ == "__main__":
    REAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    REAL_FILE.write_text("# selftest probe\n", encoding="utf-8")
    try:
        test_guards()
        test_count_unchanged_is_failure()
        test_count_increased_is_success()
        test_partial_batch_is_failure()
    finally:
        REAL_FILE.unlink(missing_ok=True)
    print("\n附件投递自测全部通过 ✓")
