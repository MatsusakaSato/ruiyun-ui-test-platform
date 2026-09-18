#!/usr/bin/env python3
"""dev 环境生效验证 —— 通过后端进程的实际外联 IP 判定当前环境。

原理：dev 与生产的 API 域名解析到不同 IP，
  api-dev.3ren.cn            → 123.59.168.122   (dev manage)
  ruiyun-api-dev.3ren.cn     → 123.59.168.121   (dev institution)
  api.3ren.cn / ruiyun-api…  → 123.59.168.79    (生产)
后端只在有云端请求时才外联，因此脚本会先刷新主窗口触发请求，再持续抓取连接。

用法：
    python scripts/verify_env.py            # 刷新主窗口并判定
    python scripts/verify_env.py --no-click # 不触发刷新，只被动抓取 15s
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

IP2LABEL = {
    "123.59.168.122": "dev  (api-dev.3ren.cn)",
    "123.59.168.121": "dev  (ruiyun-api-dev.3ren.cn)",
    "123.59.168.79":  "生产 (api.3ren.cn / ruiyun-api.3ren.cn)",
}
MAIN_HOSTS = ("127.0.0.1", "::1")


def sniff(seconds: float) -> list:
    hits, seen = [], set()
    end = time.time() + seconds
    while time.time() < end:
        if sys.platform == "win32":
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True).stdout.decode("utf-8", "replace")
            pids_ips = []
            for line in out.splitlines():
                if "ESTABLISHED" not in line:
                    continue
                parts = line.split()
                if len(parts) >= 5:
                    ip = parts[2].split(":")[0]
                    pid = parts[-1]
                    if ip not in MAIN_HOSTS:
                        pids_ips.append((ip, pid))
            if pids_ips:
                tasklist_out = subprocess.run(["tasklist", "/NH"], capture_output=True).stdout.decode("utf-8", "replace")
                pid_to_name = {}
                for line in tasklist_out.splitlines():
                    parts = line.split()
                    if len(parts) >= 2 and parts[1].isdigit():
                        pid_to_name[parts[1]] = parts[0]
                for ip, pid in pids_ips:
                    name = pid_to_name.get(pid, "unknown")
                    if ("srtclaw" not in name.lower()) and ("睿云" not in name):
                        continue
                    if (ip, pid) in seen:
                        continue
                    seen.add((ip, pid))
                    hits.append((ip, pid, name))
        else:
            out = subprocess.run(["lsof", "-nP", "-iTCP", "-sTCP:ESTABLISHED"],
                                 capture_output=True).stdout.decode("utf-8", "replace")
            for line in out.splitlines():
                if ("srtclaw" not in line) and ("睿云" not in line):
                    continue
                parts = line.split()
                remote = next((p for p in parts if "->" in p), None)
                if not remote:
                    continue
                pid, name = parts[1], parts[0]
                ip = remote.split("->")[1].rsplit(":", 1)[0].strip("[]")
                if ip in MAIN_HOSTS or (ip, pid) in seen:
                    continue
                seen.add((ip, pid))
                hits.append((ip, pid, name))
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-click", action="store_true", help="不触发刷新，被动抓取")
    ap.add_argument("--seconds", type=float, default=12.0)
    args = ap.parse_args()

    if not args.no_click:
        from drivers.cdp import CDPSession, list_targets
        main = next((t for t in list_targets(9222)
                     if t.get("type") == "page" and "5173" in (t.get("url") or "")), None)
        if main:
            c = CDPSession(main["webSocketDebuggerUrl"])
            c.enable_runtime()
            hits: list = []
            th = threading.Thread(target=lambda: hits.extend(sniff(args.seconds)))
            th.start()
            try:
                c.eval_js("location.reload()", timeout=10)
            except Exception:
                pass
            c.close()
            th.join()
        else:
            print("未找到主窗口，改为被动抓取")
            hits = sniff(args.seconds)
    else:
        hits = sniff(args.seconds)

    print(f"=== {args.seconds:.0f}s 内应用进程外联判定 ===")
    envs = set()
    for ip, pid, name in hits:
        label = IP2LABEL.get(ip)
        if label is None:
            continue
        envs.add(label.split()[0])
        print(f"  pid={pid:6s} {name[:16]:18s} → {ip:16s} {label}")
    if not envs:
        print("  未捕获到 3ren API 外联（应用可能全部命中本地缓存）。\n"
              "  可在应用里触发一次云端操作（如刷新工作空间）后重试，\n"
              "  或改用配置层证据：启动日志中的「环境档案: dev（注入 N 个变量）」。")
        return 1
    if envs == {"dev"}:
        print("\n✅ 结论：当前运行实例为 dev 环境")
        return 0
    if envs == {"生产"}:
        print("\n❌ 结论：当前运行实例为生产环境（dev 未生效，检查启动方式）")
        return 2
    print(f"\n⚠️ 同时出现多种环境连接: {envs}")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
