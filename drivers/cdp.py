"""极简 CDP（Chrome DevTools Protocol）客户端 —— 用于驱动 Electron 渲染进程。

只用标准库 + websocket-client，零额外框架依赖。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from websocket import create_connection


class CDPError(RuntimeError):
    pass


# 本机 CDP 必须绕过系统代理，否则会被 HTTP_PROXY 拦成 HTTPError
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_json(url: str, timeout: float = 3.0) -> Optional[Any]:
    try:
        with _LOCAL_OPENER.open(url, timeout=timeout) as r:
            return json.load(r)
    except (urllib.error.URLError, OSError, ValueError):
        return None


def wait_for_port(port: int, timeout_s: float = 30.0) -> bool:
    """等待 Electron 调试端口就绪。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if http_json(f"http://127.0.0.1:{port}/json/version"):
            return True
        time.sleep(0.5)
    return False


def list_targets(port: int) -> list:
    return http_json(f"http://127.0.0.1:{port}/json/list") or []


class CDPSession:
    """一条渲染进程的 CDP 连接。"""

    def __init__(self, ws_url: str, timeout: float = 30.0):
        self.ws_url = ws_url
        # 本机连接：显式排除代理，避免 websocket-client 读取 HTTP_PROXY 后
        # 把 ws://127.0.0.1 请求转发给代理导致连接失败。
        self.ws = create_connection(
            ws_url,
            timeout=timeout,
            suppress_origin=True,
            max_size=None,
            http_no_proxy=["127.0.0.1", "localhost", "::1"],
        )
        self._id = 0
        self.events: list = []

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---------------------------------------------------------------- 基础
    def call(self, method: str, params: Optional[dict] = None, timeout: float = 30.0) -> dict:
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while True:
            remain = deadline - time.time()
            if remain <= 0:
                raise TimeoutError(f"CDP 调用超时: {method}")
            try:
                self.ws.settimeout(remain)
                raw = self.ws.recv()
            except Exception as exc:  # 连接被关闭等
                raise CDPError(f"CDP 连接异常: {exc}") from exc
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if msg.get("id") == mid:
                if "error" in msg:
                    raise CDPError(f"{method} -> {msg['error']}")
                return msg.get("result", {}) or {}
            if "method" in msg:
                self.events.append(msg)

    def enable_runtime(self) -> None:
        try:
            self.call("Runtime.enable", timeout=10)
        except CDPError:
            pass

    # ---------------------------------------------------------------- 求值
    def eval_js(self, expression: str, timeout: float = 30.0, await_promise: bool = True) -> Any:
        res = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
                "userGesture": True,
            },
            timeout=timeout,
        )
        if "exceptionDetails" in res:
            desc = (res["exceptionDetails"].get("exception") or {}).get("description", "")
            raise CDPError(f"JS 异常: {desc[:300]}")
        return (res.get("result") or {}).get("value")

    # ---------------------------------------------------------------- 输入
    def insert_text(self, text: str) -> None:
        self.call("Input.insertText", {"text": text}, timeout=15)

    def press_key(self, key: str = "Enter", code: str = "Enter",
                  windows_vk: int = 13, native_vk: int = 13,
                  text: str = "") -> None:
        """按键：rawKeyDown →（有文本时）char → keyUp。

        为什么必须显式发 char：CDP 的 keyDown 在 text 为空时等价于 rawKeyDown，
        Chromium **不会**合成 keypress / beforeinput —— 只监听 keydown 的应用能收到，
        靠 keypress / beforeinput / textInput 提交的应用则完全收不到。
        这正是「回车发不出去、但界面上文本明明在输入框里」的一类成因，
        且在 Windows 的新版构建上比 macOS 更容易踩到（键位映射与编辑管线不同）。

        keyDown 用 rawKeyDown（显式表示「不合成字符」）+ 独立的 char：
        这样无论如何都只产生一次字符事件，不会重复提交。
        """
        base = {
            "key": key,
            "code": code,
            "windowsVirtualKeyCode": windows_vk,
            "nativeVirtualKeyCode": native_vk,
        }
        self.call("Input.dispatchKeyEvent", {**base, "type": "rawKeyDown"}, timeout=10)
        if text:
            self.call("Input.dispatchKeyEvent",
                      {**base, "type": "char", "text": text,
                       "unmodifiedText": text}, timeout=10)
        self.call("Input.dispatchKeyEvent", {**base, "type": "keyUp"}, timeout=10)


def pick_page_target(targets: list, prefer_keywords=("srtclaw", "睿云", "index.html", "localhost")) -> Optional[dict]:
    """从 targets 中挑出主界面渲染进程。"""
    pages = [t for t in targets if t.get("type") == "page"]
    if not pages:
        return None
    for t in pages:
        blob = f"{t.get('url','')} {t.get('title','')}".lower()
        if any(k.lower() in blob for k in prefer_keywords):
            return t
    return pages[0]
