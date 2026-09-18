#!/usr/bin/env python3
"""本地假模型供应商 —— 仅供离线端到端测试，不联网、无第三方依赖。

实现 OpenAI 兼容的最小 POST /chat/completions：
  * 默认：返回固定的判分 JSON（各维合法档位；安全性按 0/3/5 给 5），usage 固定 11/7/18；
  * `--fail-403`：返回与「来源 IP 未在白名单」同形的 403 响应体，用于演练判分失败路径
    （验证评估是否被拒绝启动、根因是否原样透出，而不是被兜底话术掩盖）。

用法：
    python scripts/fake_openai_server.py --port 8899
    python scripts/fake_openai_server.py --port 8899 --fail-403
    # 然后在「模型设置」里填 http://127.0.0.1:8899/v1 + 任意 Key + 任意模型名
"""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DIMS = (
    "correctness", "completeness", "relevance", "actionability", "inspiration",
    "teaching_professionalism", "teaching_fit", "classroom_usability", "teaching_extension",
    "aesthetics", "safety", "stability", "format_compliance",
    "tool_selection", "skill_selection", "execution_success", "self_correction",
    "task_completion", "delivery_efficiency", "cost_control",
)

# 安全性档位是 0/3/5（与常规 1-5 不同）：假数据必须给合法档位，
# 否则测到的是「非法值被拒」，而不是通链成功。
SAFETY_SCORE = 5
DEFAULT_SCORE = 4


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    fail_403 = False
    forbid_ip = "203.0.113.9"

    def _send(self, code: int, obj: dict):
        out = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        # 未实现 /models：返回 404，让探活走「回退到 /chat/completions」的正规分支
        self._send(404, {"error": {"message": "not implemented (fake provider)"}})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            self.rfile.read(n)
        except Exception:
            pass
        if self.fail_403:
            self._send(403, {"error": {
                "type": "permission_error", "code": "403005",
                "message": f"Source IP {self.forbid_ip} is not in the API Key allowlist. "
                           "Please check whether the IP is within the allowlist range.",
            }})
            return
        dims = {k: {"score": SAFETY_SCORE if k == "safety" else DEFAULT_SCORE,
                    "ratio": 0.9, "reason": "fake-provider"} for k in DIMS}
        self._send(200, {
            "choices": [{"message": {"role": "assistant", "content": json.dumps(
                {"requirements": ["包含教学目标"], "dimensions": dims},
                ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
        })

    def log_message(self, *a):
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--fail-403", action="store_true",
                    help="模拟「来源 IP 未在白名单」的 403，用于演练判分失败路径")
    args = ap.parse_args()
    Handler.fail_403 = bool(args.fail_403)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    mode = "403 白名单拒绝" if args.fail_403 else "正常判分"
    print(f"fake provider on http://127.0.0.1:{args.port}/v1（{mode}）", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
