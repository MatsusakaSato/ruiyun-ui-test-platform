"""大模型供应商探活：一次最小请求完成「连通性 + 鉴权」校验。

设计约束（来自需求确认）：
  * 协议无关 —— 只依赖 base_url + api_key；默认走 OpenAI 兼容探活，
    同时允许用户用 probe_path 指定任意路径。
  * 以真实推理为准 —— 填了模型名时用 POST /chat/completions 做一次最小推理：
    只有真正调用成功才能证明「这个 Key 可用」。仅探测 GET /models 会漏掉
    IP 白名单、模型授权等权限问题，产生「models 200 但实际不可用」的假阳性。
    未填模型名时才退化为 GET /models 鉴权探测。
    两个标准端点互不遮挡：任一端点返回 404/405 时自动回退另一个。
  * 凭证集中 —— 探活接口只接受「随请求传入的临时 Key」（不落盘）；后端长期使用的
    Key 由本模块的密钥文件读写统一管理（独立文件、0600 权限）。
  * 不泄露 —— message / detail 一律经过 Key 脱敏，绝不回显 API Key。

仅使用标准库，供 server.py、core/evaluator.py 与后续「生成用例 / 分析结果」功能复用。
"""
from __future__ import annotations

import json
import os
import re
import socket
import ssl
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core.settings import secrets_path

DEFAULT_TIMEOUT_S = 12.0
MAX_DETAIL_CHARS = 300
USER_AGENT = "Ruiyun-UI-Test-Platform/1.0 (llm-probe)"
# 未填模型名时的占位模型（仅用于端点存在性回退，不作为成功依据）
PLACEHOLDER_MODEL = "gpt-3.5-turbo"

# 后端密钥文件：供仪表盘评估与 CLI 复用。存放在用户工作区（历史位置自动
# 迁移），权限 0600、不入版本库。密钥属用户数据，跟着工作区走而非代码目录。
_ROOT = Path(__file__).resolve().parent.parent
SECRETS_FILE = secrets_path()
# 判分响应可能很长：探活只取片段（默认），对话调用需取全文
CHAT_MAX_BYTES = 2 * 1024 * 1024

# 探活结果分类（前端按此选择提示色）
CAT_OK = "ok"
CAT_AUTH = "auth"
CAT_NOT_FOUND = "not_found"
CAT_TIMEOUT = "timeout"
CAT_NETWORK = "network"
CAT_TLS = "tls"
CAT_SERVER_ERROR = "server_error"
CAT_INVALID_INPUT = "invalid_input"

_REDIRECT_CODES = (301, 302, 303, 307, 308)


@dataclass
class ProbeResult:
    """一次探活的结构化结果。ok=True 表示连通且鉴权通过。"""

    ok: bool
    stage: str                 # custom_path / models / chat / preflight
    category: str              # 见上方 CAT_* 常量
    message: str               # 面向用户的中文提示（已脱敏）
    status: Optional[int] = None
    detail: str = ""           # 响应体摘要（截断 + 脱敏）
    latency_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "stage": self.stage,
            "category": self.category,
            "message": self.message,
            "status": self.status,
            "detail": self.detail,
            "latency_ms": self.latency_ms,
        }


# ------------------------------------------------ 脱敏与文本处理

def redact(text: str, api_key: str = "") -> str:
    """从任意文本中抹掉 API Key / Bearer 凭证，避免回显给前端或写入日志。"""
    if not text:
        return ""
    out = str(text)
    if api_key:
        out = out.replace(api_key, "***")
    out = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{6,}", r"\1***", out)
    out = re.sub(
        r"(?i)([\"']?(?:api[_-]?key|apikey|authorization|access[_-]?token)[\"']?\s*[:=]\s*[\"']?)"
        r"[^\"'\s,}]{4,}",
        r"\1***",
        out,
    )
    return out


def _clip(text: str, limit: int = MAX_DETAIL_CHARS) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _elapsed_ms(t0: float) -> int:
    return int((time.time() - t0) * 1000)


def _safe_url(url: str) -> str:
    """仅保留 scheme://host/path —— 去掉 query，避免凭证出现在提示里。"""
    p = urllib.parse.urlsplit(url)
    return f"{p.scheme}://{p.netloc}{p.path}"


# ------------------------------------------------ 入参校验与候选前缀

def normalize_base_url(base_url: str) -> tuple[str, str]:
    """校验并规范化 base_url，返回 (规范化地址, 错误信息)。"""
    raw = (base_url or "").strip().rstrip("/")
    if not raw:
        return "", "请填写模型供应商地址"
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme not in ("http", "https"):
        return "", "地址必须以 http:// 或 https:// 开头"
    if not parsed.hostname:
        return "", "地址缺少主机名（例如 https://api.example.com/v1）"
    if parsed.username or parsed.password:
        return "", "地址中不允许携带账号密码，请把凭证填入 API Key"
    return raw, ""


def prefix_candidates(base: str) -> list:
    """地址未包含 /v1 版本段时，额外纳入 {base}/v1 作为候选前缀。"""
    base = base.rstrip("/")
    path = urllib.parse.urlsplit(base).path
    cands = [base]
    if "/v1" not in path:
        cands.append(base + "/v1")
    seen, out = set(), []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


# ------------------------------------------------ HTTP 执行

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁用自动跳转：避免 Authorization 头被重定向带到第三方主机。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


# 注意：这里【不】使用 ProxyHandler({})。
# server.py 中 app_is_running() 刻意绕开代理是为了访问 127.0.0.1；
# 而探活访问的是外网供应商，必须遵循环境里的 HTTP(S)_PROXY，
# 否则企业代理环境下会直接连不通。两者语义不同，不要照搬。
_OPENER = urllib.request.build_opener(_NoRedirect())


def _do_request(url: str, api_key: str, timeout_s: float,
                method: str, body: Optional[dict],
                max_bytes: int = MAX_DETAIL_CHARS * 4) -> tuple[Optional[int], str, Optional[BaseException]]:
    """发一次请求，返回 (状态码, 响应文本, 异常)。

    max_bytes：响应体读取上限。探活只需片段（默认值），对话调用需传大值取全文。
    3xx 因禁用自动跳转会被 urllib 以 HTTPError 抛出，此处照常返回其状态码。
    """
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(req, timeout=timeout_s) as resp:
            raw = resp.read(max_bytes)
            return resp.status, raw.decode("utf-8", "replace"), None
    except urllib.error.HTTPError as exc:
        raw = b""
        try:
            raw = exc.read(max_bytes)
        except Exception:
            pass
        return exc.code, raw.decode("utf-8", "replace"), None
    except Exception as exc:  # URLError / 超时 / TLS ...
        return None, "", exc


def classify_exception(exc: BaseException) -> tuple[str, str]:
    """把网络异常映射为 (分类, 可读中文提示)。"""
    reason = getattr(exc, "reason", None) or exc
    text = f"{type(reason).__name__}: {reason}".lower()

    if isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in text:
        return CAT_TIMEOUT, "连接超时：地址不可达或网络过慢"
    if isinstance(reason, ssl.SSLError) or "certificate" in text or "ssl:" in text:
        return CAT_TLS, "TLS/证书校验失败"
    if isinstance(reason, socket.gaierror) or "name or service not known" in text \
            or "nodename nor servname" in text or "getaddrinfo" in text:
        return CAT_NETWORK, "DNS 解析失败：域名可能写错"
    if isinstance(reason, ConnectionRefusedError) or "refused" in text:
        return CAT_NETWORK, "连接被拒绝：地址或端口不对"
    return CAT_NETWORK, "网络不可达（请检查网络与代理设置）"


# ------------------------------------------------ 探活入口

def probe_provider(base_url: str, api_key: str, *, model: str = "",
                   probe_path: str = "", timeout_s: float = DEFAULT_TIMEOUT_S) -> ProbeResult:
    """对任意供应商做一次最小连通性 + 鉴权探活，返回归一化结果。

    尝试顺序：
      1) 用户填了 probe_path → 按其路径 GET（最高优先级，不再走默认）
      2) 填了模型名 → POST {prefix}/chat/completions（真实推理，最能代表「可用」）
         未填模型名 → GET {prefix}/models
      3) 上述端点返回 404/405 时，自动回退到另一个标准端点
    """
    t0 = time.time()

    base, err = normalize_base_url(base_url)
    if err:
        return ProbeResult(False, "preflight", CAT_INVALID_INPUT, err,
                           latency_ms=_elapsed_ms(t0))

    key = (api_key or "").strip()
    if not key:
        return ProbeResult(False, "preflight", CAT_INVALID_INPUT, "请填写 API Key",
                           latency_ms=_elapsed_ms(t0))

    # API Key 绝不含空白字符。把「Key + 模型名/地址」一起粘进来是最常见的坑：
    # 直接判入参错误，比发出去换回一个含义模糊的 401 更容易定位。
    if re.search(r"\s", key):
        return ProbeResult(
            False, "preflight", CAT_INVALID_INPUT,
            f"API Key 中不能含空格或换行（当前 {len(key)} 字符）——"
            "很可能把「模型名」一起粘进来了。请只填 Key 原文，模型名填到「模型名」字段",
            latency_ms=_elapsed_ms(t0))

    try:
        timeout = max(1.0, float(timeout_s))
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_S

    prefixes = prefix_candidates(base)
    custom = (probe_path or "").strip()
    has_model = bool((model or "").strip())
    chat_body = {
        "model": (model or "").strip() or PLACEHOLDER_MODEL,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
    }

    attempts: list = []
    if custom:
        p = custom if custom.startswith("/") else "/" + custom
        for pref in prefixes:
            attempts.append(("custom_path", pref + p, "GET", None))
    else:
        # 有模型名 → 真实推理优先（唯一能证明「Key 可调用模型」的方式）；
        # 无模型名 → 只能退化为 /models 鉴权探测。
        for pref in prefixes:
            if has_model:
                attempts.append(("chat", pref + "/chat/completions", "POST", chat_body))
            else:
                attempts.append(("models", pref + "/models", "GET", None))
        # 回退：标准端点不存在（404/405）时改试另一个
        for pref in prefixes:
            if has_model:
                attempts.append(("models", pref + "/models", "GET", None))
            else:
                attempts.append(("chat", pref + "/chat/completions", "POST", chat_body))

    last = ProbeResult(False, "preflight", CAT_NETWORK, "未发起任何探活请求",
                       latency_ms=_elapsed_ms(t0))

    for stage, url, method, body in attempts:
        status, raw, exc = _do_request(url, key, timeout, method, body)
        detail = _clip(redact(raw, key))

        if exc is not None:
            cat, msg = classify_exception(exc)
            # 网络层失败与具体路径无关，换前缀也救不回来 → 立即返回
            return ProbeResult(False, stage, cat, f"{msg}（{_safe_url(url)}）",
                               detail=detail, latency_ms=_elapsed_ms(t0))

        if status is None:
            return ProbeResult(False, stage, CAT_NETWORK, "未获得任何响应",
                               detail=detail, latency_ms=_elapsed_ms(t0))

        if 200 <= status < 300:
            return ProbeResult(True, stage, CAT_OK, f"连接成功（HTTP {status}）",
                               status=status, detail=detail, latency_ms=_elapsed_ms(t0))

        if status in (401, 403):
            # 403 常见于「来源 IP 不在 API Key 白名单」等权限问题，
            # 具体原因在 detail（供应商原文）里，前端会原样展示。
            return ProbeResult(False, stage, CAT_AUTH,
                               f"API Key 无效、无权限，或来源 IP 未在白名单（HTTP {status}）",
                               status=status, detail=detail, latency_ms=_elapsed_ms(t0))

        if status == 429:
            # 429 说明请求已到达且凭证被识别（否则会是 401）→ 判定为可用
            return ProbeResult(True, stage, CAT_OK, f"连接成功，但供应商限流（HTTP {status}）",
                               status=status, detail=detail, latency_ms=_elapsed_ms(t0))

        if status in (404, 405):
            last = ProbeResult(False, stage, CAT_NOT_FOUND, f"探活路径不存在（HTTP {status}）",
                               status=status, detail=detail, latency_ms=_elapsed_ms(t0))
            continue

        if status in _REDIRECT_CODES:
            return ProbeResult(False, stage, CAT_SERVER_ERROR,
                               f"供应商返回重定向 HTTP {status}（已禁用自动跳转以保护凭证）",
                               status=status, detail=detail, latency_ms=_elapsed_ms(t0))

        if 400 <= status < 500:
            # 真实推理被拒（400/422）通常是模型名不存在或不被该 Key 允许。
            # 这里必须判失败：否则「Key 用不了这个模型」会被误报成验证通过。
            if stage == "chat" and status in (400, 422):
                return ProbeResult(False, stage, CAT_SERVER_ERROR,
                                   f"模型调用被拒绝（HTTP {status}）：模型名可能不存在或不被该 Key 允许",
                                   status=status, detail=detail, latency_ms=_elapsed_ms(t0))
            last = ProbeResult(False, stage, CAT_SERVER_ERROR, f"请求被拒绝（HTTP {status}）",
                               status=status, detail=detail, latency_ms=_elapsed_ms(t0))
            continue

        if status >= 500:
            return ProbeResult(False, stage, CAT_SERVER_ERROR, f"供应商服务端错误（HTTP {status}）",
                               status=status, detail=detail, latency_ms=_elapsed_ms(t0))

        last = ProbeResult(False, stage, CAT_SERVER_ERROR, f"未预期的响应（HTTP {status}）",
                           status=status, detail=detail, latency_ms=_elapsed_ms(t0))

    return last


# ------------------------------------------------ 对话调用（判分 / 生成）
@dataclass
class ChatResult:
    ok: bool
    content: str = ""
    error: str = ""
    status: Optional[int] = None
    stage: str = ""
    usage: dict = field(default_factory=dict)   # input_tokens / output_tokens / total_tokens
    latency_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "content": self.content,
            "error": self.error,
            "status": self.status,
            "stage": self.stage,
            "usage": self.usage,
            "latency_ms": self.latency_ms,
        }


def _extract_usage(obj) -> dict:
    u = obj.get("usage") if isinstance(obj, dict) else None
    u = u if isinstance(u, dict) else {}

    def _int(*keys) -> int:
        for k in keys:
            v = u.get(k)
            if isinstance(v, (int, float)):
                return int(v)
        return 0

    inp = _int("prompt_tokens", "input_tokens")
    out = _int("completion_tokens", "output_tokens")
    return {"input_tokens": inp, "output_tokens": out,
            "total_tokens": _int("total_tokens") or (inp + out)}


def chat(base_url: str, api_key: str, model: str, messages: list, *,
         timeout_s: float = 60.0, temperature: float = 0.0,
         json_mode: bool = False, max_tokens: Optional[int] = None,
         allow_placeholder: bool = False) -> ChatResult:
    """OpenAI 兼容对话调用，返回正文与 usage；失败原因可读且已脱敏。

    复用探活的候选前缀逻辑：地址未含版本段时自动尝试 {base} 与 {base}/v1。

    allow_placeholder=False（默认）：model 为空时**直接判失败**。旧实现会静默回退成
    占位模型名发出去，把「模型名没填」变成一句与真实原因无关的供应商报错，极难定位。
    仅探活（probe_provider）为试端点存在性才允许占位名。

    json_mode 回退：部分供应商不支持 response_format（返回 400/422），
    此时自动去掉该字段重试一次，而不是让整轮判分一起失败。
    """
    t0 = time.time()
    base, err = normalize_base_url(base_url)
    if err:
        return ChatResult(False, error=err, stage="preflight", latency_ms=_elapsed_ms(t0))
    key = (api_key or "").strip()
    if not key:
        return ChatResult(False, error="未配置 API Key", stage="preflight",
                          latency_ms=_elapsed_ms(t0))
    if re.search(r"\s", key):
        return ChatResult(False, error="API Key 含空格或换行，请检查是否粘贴了多余内容",
                          stage="preflight", latency_ms=_elapsed_ms(t0))
    mdl = (model or "").strip()
    if not mdl and not allow_placeholder:
        return ChatResult(False, error="未配置模型名（供应商通常要求真实的模型名或端点 ID）",
                          stage="preflight", latency_ms=_elapsed_ms(t0))

    body = {
        "model": mdl or PLACEHOLDER_MODEL,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }
    if max_tokens:
        body["max_tokens"] = int(max_tokens)
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    last = ChatResult(False, error="未发起请求", stage="preflight", latency_ms=_elapsed_ms(t0))
    for pref in prefix_candidates(base):
        url = pref + "/chat/completions"
        status, text, exc = _do_request(url, key, timeout_s, "POST", body,
                                        max_bytes=CHAT_MAX_BYTES)
        if exc is not None:
            _, msg = classify_exception(exc)
            return ChatResult(False, error=msg, stage="network", latency_ms=_elapsed_ms(t0))
        if status is None:
            return ChatResult(False, error="未获得任何响应", stage="network",
                              latency_ms=_elapsed_ms(t0))
        if status in (404, 405):
            last = ChatResult(False, error=f"端点不存在（HTTP {status}）", status=status,
                              stage="not_found", latency_ms=_elapsed_ms(t0))
            continue
        if status in (400, 422) and "response_format" in body:
            # 供应商不支持 JSON 模式 → 去掉该字段重试一次（同前缀，不再循环尝试）
            body.pop("response_format", None)
            status, text, exc = _do_request(url, key, timeout_s, "POST", body,
                                            max_bytes=CHAT_MAX_BYTES)
            if exc is not None:
                _, msg = classify_exception(exc)
                return ChatResult(False, error=msg, stage="network", latency_ms=_elapsed_ms(t0))
            if status is None:
                return ChatResult(False, error="未获得任何响应", stage="network",
                                  latency_ms=_elapsed_ms(t0))
        if not 200 <= status < 300:
            return ChatResult(False,
                              error=f"模型调用失败（HTTP {status}）：{_clip(redact(text, key))}",
                              status=status, stage="http_error", latency_ms=_elapsed_ms(t0))
        try:
            obj = json.loads(text)
        except ValueError:
            return ChatResult(False, error="响应不是合法 JSON", status=status,
                              stage="parse", latency_ms=_elapsed_ms(t0))
        choices = obj.get("choices") if isinstance(obj, dict) else None
        if not isinstance(choices, list) or not choices:
            return ChatResult(False, error="响应缺少 choices", status=status,
                              stage="parse", latency_ms=_elapsed_ms(t0))
        msg0 = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = msg0.get("content") if isinstance(msg0, dict) else None
        if not isinstance(content, str) or not content.strip():
            return ChatResult(False, error="响应内容为空", status=status,
                              stage="parse", latency_ms=_elapsed_ms(t0))
        return ChatResult(True, content=content, status=status, stage="chat",
                          usage=_extract_usage(obj), latency_ms=_elapsed_ms(t0))
    return last


# ------------------------------------------------ 后端密钥文件（0600，不进版本库）
def _empty_cfg() -> dict:
    return {"base_url": "", "api_key": "", "model": "", "saved_at": ""}


def load_config() -> dict:
    """读取后端密钥配置；文件缺失或损坏一律返回空配置，不向调用方抛异常。"""
    if not SECRETS_FILE.is_file():
        return _empty_cfg()
    try:
        data = json.loads(SECRETS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return _empty_cfg()
    if not isinstance(data, dict):
        return _empty_cfg()
    out = _empty_cfg()
    for k in out:
        v = data.get(k)
        out[k] = v.strip() if isinstance(v, str) else ""
    return out


def save_config(base_url: str, api_key: str, model: str = "") -> dict:
    """原子写入密钥文件并设为 0600。返回含明文 Key 的配置，仅供内部使用。"""
    payload = {
        "base_url": (base_url or "").strip(),
        "api_key": (api_key or "").strip(),
        "model": (model or "").strip(),
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    SECRETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(SECRETS_FILE.parent),
                               prefix=".llm_secrets.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, SECRETS_FILE)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
    try:
        os.chmod(SECRETS_FILE, 0o600)
    except OSError:
        pass
    return payload


def clear_config() -> None:
    """删除密钥文件（清空配置）。"""
    try:
        SECRETS_FILE.unlink()
    except OSError:
        pass


def public_config() -> dict:
    """可安全返回给前端的配置信息：不含明文 Key。"""
    cfg = load_config()
    key = cfg.get("api_key") or ""
    if len(key) >= 14:
        hint = f"{key[:7]}…{key[-4:]}（{len(key)} 字符）"
    elif key:
        hint = "（已配置）"
    else:
        hint = ""
    return {
        "configured": bool(cfg.get("base_url") and key),
        "base_url": cfg.get("base_url") or "",
        "model": cfg.get("model") or "",
        "key_hint": hint,
        "saved_at": cfg.get("saved_at") or "",
    }
