#!/usr/bin/env python3
"""注入式测试：core/llm_client 探活逻辑（不联网、不驱动 UI）。

用假 transport 替换真实 HTTP，覆盖：
  * 判定语义：填了模型名走「真实推理」POST /chat/completions；未填才用 GET /models
  * 端点回退：chat 404/405 → /models，反之亦然
  * 分类映射：ok / auth / not_found / timeout / network / tls / server_error / invalid_input
  * 真实缺陷回归：403 IP 白名单权限错误必须判失败并回显原文；
    真实推理 400（模型名问题）不得误判为通过（该缺陷曾导致「models 200 假通过」）
  * 地址不含 /v1 时的多前缀候选、自定义探活路径
  * 响应摘要与提示中绝不出现 API Key

沿用 scripts/test_cycle_confirm.py 的纯 assert + __main__ 风格（仓库无 pytest）。
"""
import json
import socket
import ssl
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import llm_client as lc  # noqa: E402

CALLS = []


def _make_transport(plan):
    """plan: [(status, body) | Exception, ...]，按调用顺序消费，超出则复用最后一项。"""
    state = {"i": 0}

    def transport(url, api_key, timeout_s, method, body, max_bytes=None):
        CALLS.append({"url": url, "method": method, "body": body, "api_key": api_key})
        item = plan[min(state["i"], len(plan) - 1)]
        state["i"] += 1
        if isinstance(item, BaseException):
            return None, "", item
        status, text = item
        return status, text, None

    return transport


def with_transport(plan, fn):
    orig = lc._do_request
    lc._do_request = _make_transport(plan)
    CALLS.clear()
    try:
        return fn()
    finally:
        lc._do_request = orig


def test_prefix_candidates():
    assert lc.prefix_candidates("https://api.example.com") == [
        "https://api.example.com", "https://api.example.com/v1"], CALLS
    assert lc.prefix_candidates("https://api.example.com/v1") == ["https://api.example.com/v1"]
    # 主机名含 v1 不应误判为已带版本段
    assert lc.prefix_candidates("https://v1.api.example.com") == [
        "https://v1.api.example.com", "https://v1.api.example.com/v1"]
    print("PASS 1: base_url 前缀候选（含主机名 v1 不误判）")


def test_invalid_input():
    r = lc.probe_provider("api.example.com", "sk-x")
    assert (not r.ok) and r.category == "invalid_input" and r.stage == "preflight", r
    r = lc.probe_provider("ftp://api.example.com", "sk-x")
    assert r.category == "invalid_input", r
    r = lc.probe_provider("https://user:pass@api.example.com", "sk-x")
    assert r.category == "invalid_input", r
    r = lc.probe_provider("https://api.example.com", "   ")
    assert r.category == "invalid_input" and "API Key" in r.message, r
    # 真实缺陷回归：Key 与模型名被当成一段文本一起粘贴（实测 63 字符）
    # 必须判为入参错误，而不是发出去换回一个含义模糊的 401
    r = lc.probe_provider("https://api.example.com", "sk-abc ep-mu4acuxw")
    assert r.category == "invalid_input" and "空格" in r.message, r
    print("PASS 2: 入参校验（缺 scheme / 非 http(s) / 带账号密码 / 空 Key / Key 混入模型名）")


def test_no_model_uses_models():
    r = with_transport([(200, '{"data":[]}')],
                       lambda: lc.probe_provider("https://api.example.com/v1", "sk-secret"))
    assert r.ok and r.stage == "models" and r.status == 200, r
    assert len(CALLS) == 1, CALLS
    assert CALLS[0]["method"] == "GET" and CALLS[0]["url"].endswith("/v1/models"), CALLS
    print("PASS 3: 未填模型名 → 退化为 GET /models 鉴权探测")


def test_chat_is_primary():
    r = with_transport([(200, '{"choices":[]}')],
                       lambda: lc.probe_provider("https://api.example.com/v1", "sk-secret", model="m1"))
    assert r.ok and r.stage == "chat", r
    assert len(CALLS) == 1, CALLS
    assert CALLS[0]["method"] == "POST" and CALLS[0]["url"].endswith("/v1/chat/completions"), CALLS
    assert CALLS[0]["body"].get("model") == "m1", CALLS
    print("PASS 4: 填了模型名 → 以真实推理 POST /chat/completions 判定")


def test_chat_fallback_to_models():
    r = with_transport([(404, ""), (200, '{"data":[]}')],
                       lambda: lc.probe_provider("https://api.example.com/v1", "sk-secret", model="m1"))
    assert r.ok and r.stage == "models", r
    assert len(CALLS) == 2, CALLS
    assert CALLS[0]["method"] == "POST" and CALLS[1]["method"] == "GET", CALLS
    print("PASS 5: chat 端点 404 → 回退 GET /models")


def test_multi_prefix():
    r = with_transport([(404, ""), (404, ""), (200, "{}")],
                       lambda: lc.probe_provider("https://api.example.com", "sk-secret"))
    assert r.ok and r.stage == "chat", r
    assert CALLS[0]["url"].endswith("/models"), CALLS
    assert CALLS[1]["url"].endswith("/v1/models"), CALLS
    print("PASS 6: 地址不含 /v1 时自动追加 /v1 候选")


def test_custom_path_overrides():
    r = with_transport([(200, "ok")],
                       lambda: lc.probe_provider("https://api.example.com/v1", "k", probe_path="/health"))
    assert r.ok and r.stage == "custom_path", r
    assert len(CALLS) == 1 and CALLS[0]["url"].endswith("/v1/health"), CALLS
    print("PASS 7: 自定义探活路径覆盖默认探测")


def test_auth_failed():
    r = with_transport([(401, '{"error":"invalid key"}')],
                       lambda: lc.probe_provider("https://api.example.com/v1", "sk-secret", model="m1"))
    assert (not r.ok) and r.category == "auth" and r.status == 401, r
    print("PASS 8: 401 → auth")


def test_permission_error_surfaces():
    """真实缺陷回归：TokenHub 的 403 permission_error（来源 IP 不在白名单）
    必须判失败，并把供应商原文带出来，而不是因为 /models 200 就假通过。"""
    body = ('{"error":{"type":"permission_error","code":"403005",'
            '"message":"Source IP 1.2.3.4 is not in the API Key allowlist."}}')
    r = with_transport([(403, body)],
                       lambda: lc.probe_provider("https://api.example.com/v1", "sk-secret", model="ep-abc"))
    assert (not r.ok) and r.category == "auth" and r.status == 403, r
    assert "allowlist" in r.detail, r.detail
    assert "白名单" in r.message, r
    print("PASS 9: 403 权限错误（IP 白名单）判失败并回显供应商原文")


def test_chat_400_not_ok():
    r = with_transport([(400, '{"error":{"message":"model not found"}}')],
                       lambda: lc.probe_provider("https://api.example.com/v1", "sk-secret", model="bad-model"))
    assert (not r.ok) and r.status == 400 and "模型" in r.message, r
    print("PASS 10: 真实推理 400（模型名问题）不再误判为通过")


def test_timeout_and_network():
    r = with_transport([urllib.error.URLError(socket.timeout("timed out"))],
                       lambda: lc.probe_provider("https://api.example.com/v1", "k"))
    assert (not r.ok) and r.category == "timeout", r

    r = with_transport([urllib.error.URLError(socket.gaierror(-2, "Name or service not known"))],
                       lambda: lc.probe_provider("https://api.example.com/v1", "k"))
    assert r.category == "network", r

    r = with_transport([urllib.error.URLError(ssl.SSLError("certificate verify failed"))],
                       lambda: lc.probe_provider("https://api.example.com/v1", "k"))
    assert r.category == "tls", r
    print("PASS 11: 超时 / DNS / TLS 分类正确")


def test_server_error_and_redirect():
    r = with_transport([(500, "boom")], lambda: lc.probe_provider("https://api.example.com/v1", "k"))
    assert (not r.ok) and r.category == "server_error" and r.status == 500, r

    r = with_transport([(302, "")], lambda: lc.probe_provider("https://api.example.com/v1", "k"))
    assert (not r.ok) and r.status == 302 and "重定向" in r.message, r
    print("PASS 12: 5xx 与 3xx(禁用跳转) 处理正确")


def test_rate_limited_is_ok():
    r = with_transport([(429, "rate limited")],
                       lambda: lc.probe_provider("https://api.example.com/v1", "k"))
    assert r.ok and "限流" in r.message, r
    print("PASS 13: 429（限流）视为可达且鉴权通过")


def test_key_never_leaked():
    secret = "sk-supersecret-1234567890"
    body = ('{"error":"invalid api_key ' + secret + '",'
            '"authorization":"Bearer ' + secret + '"}')
    r = with_transport([(401, body)],
                       lambda: lc.probe_provider("https://api.example.com/v1", secret, model="m1"))
    blob = json.dumps(r.to_dict(), ensure_ascii=False)
    assert secret not in blob, blob
    assert secret not in r.detail and secret not in r.message, blob
    assert "***" in r.detail, r.detail

    # Bearer 形态（未显式提供 key 时的兜底脱敏）
    assert secret not in lc.redact("Authorization: Bearer " + secret), lc.redact("Authorization: Bearer " + secret)
    print("PASS 14: 响应摘要 / 提示中绝不出现 API Key")


if __name__ == "__main__":
    test_prefix_candidates()
    test_invalid_input()
    test_no_model_uses_models()
    test_chat_is_primary()
    test_chat_fallback_to_models()
    test_multi_prefix()
    test_custom_path_overrides()
    test_auth_failed()
    test_permission_error_surfaces()
    test_chat_400_not_ok()
    test_timeout_and_network()
    test_server_error_and_redirect()
    test_rate_limited_is_ok()
    test_key_never_leaked()
    print("\n全部 14 项注入式测试通过 ✓")
