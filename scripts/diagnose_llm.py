#!/usr/bin/env python3
"""一条命令自查大模型接入：配置 / 出口 IP / 供应商原文。

评估因「判分调用失败」中止时，用它替代猜测：
  1) 打印服务端已保存的配置（Key 只显示前缀与长度，绝不回显明文）；
  2) 打印本机**直连**出口 IP（多源比对，显式绕过系统代理）；
  3) 用已保存配置真实探活一次，原样回显供应商返回体（含它看到的来源 IP）；
  4) 给出结论：该把哪个 IP 加入白名单、是否漏填模型名。

用法：
  python scripts/diagnose_llm.py            # 完整自查（会真实调用一次模型）
  python scripts/diagnose_llm.py --ip-only  # 只看出口 IP，不调用模型（不产生费用）

退出码：0 = 探活通过；1 = 探活失败；2 = 未配置 / 参数错误。
"""
import argparse
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.llm_client import load_config, probe_provider, public_config  # noqa: E402

# 出口 IP 回显源：任一个可用即可，多源比对能识别「多出口 / 代理」导致的 IP 漂移
IP_ECHO_URLS = (
    "https://api-ipv4.ip.sb/ip",
    "http://ip.3322.net",
    "https://myip.ipip.net",
)

# 显式绕过代理：诊断的是「供应商最终看到的源 IP」，
# 走代理时看到的会是代理 IP —— 这正是 403 白名单最常见的成因。
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def egress_ips(timeout_s: float = 6.0) -> list:
    """取本机直连出口 IP，返回 [(来源, 文本), ...]。"""
    out = []
    for url in IP_ECHO_URLS:
        try:
            with _DIRECT.open(url, timeout=timeout_s) as resp:
                text = resp.read(200).decode("utf-8", "replace").strip()
            out.append((url, text))
        except Exception as exc:
            out.append((url, f"失败（{type(exc).__name__}）"))
    return out


def _ipv4_in(text) -> list:
    return re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", str(text or ""))


def main() -> int:
    ap = argparse.ArgumentParser(description="大模型接入自查（评估判分失败的排障入口）")
    ap.add_argument("--ip-only", action="store_true", help="只查出口 IP，不调用模型")
    args = ap.parse_args()

    cfg = load_config()
    pub = public_config()

    print("=" * 64)
    print("1) 已保存的配置（服务端独立密钥文件，0600）")
    print("=" * 64)
    if not pub.get("configured"):
        print("  未配置（缺少供应商地址或 API Key）")
    print(f"  供应商地址 : {pub.get('base_url') or '（空）'}")
    model = pub.get("model") or ""
    print(f"  模型名     : {model or '（空）← 必填；留空会用占位模型名发判分请求，必然失败'}")
    print(f"  API Key    : {pub.get('key_hint') or '（空）'}")
    print(f"  保存时间   : {pub.get('saved_at') or '—'}")

    print()
    print("=" * 64)
    print("2) 本机直连出口 IP（已绕过系统代理）")
    print("=" * 64)
    seen: list = []
    for url, text in egress_ips():
        print(f"  {url:<30} -> {text}")
        seen.extend(_ipv4_in(text))
    uniq = sorted(set(seen))
    local_ip = uniq[0] if uniq else ""
    print(f"  → 本机出口 IPv4：{local_ip or '（未取到，可能被网络策略拦截）'}")

    if args.ip_only:
        print("\n（--ip-only：未调用模型，未产生费用）")
        return 0

    print()
    print("=" * 64)
    print("3) 真实探活（用已保存的配置调用一次模型）")
    print("=" * 64)
    if not (cfg.get("base_url") and cfg.get("api_key")):
        print("  跳过：未配置供应商地址或 API Key")
        return 2
    res = probe_provider(cfg["base_url"], cfg["api_key"],
                         model=cfg.get("model") or "").to_dict()
    print(f"  结果 : {'✓ 可用' if res.get('ok') else '✗ 失败'}"
          f"（{res.get('latency_ms')}ms · 阶段 {res.get('stage')} · 分类 {res.get('category')}）")
    print(f"  提示 : {res.get('message')}")
    if res.get("status"):
        print(f"  HTTP : {res['status']}")
    if res.get("detail"):
        print("  供应商原文：")
        for line in str(res["detail"]).splitlines() or [""]:
            print(f"      {line}")

    print()
    print("=" * 64)
    print("4) 结论与下一步")
    print("=" * 64)
    if res.get("ok"):
        print("  ✓ 配置可用。若评估仍失败，请查看 evaluation.json 的 summary.judge_errors。")
        return 0

    detail = str(res.get("detail") or "")
    supplier_ips = _ipv4_in(detail)
    denied = (res.get("status") in (401, 403)
              or "allowlist" in detail.lower() or "白名单" in detail)
    if denied and supplier_ips:
        print(f"  ✗ 供应商按「来源 IP 未放行」拒绝了请求，它看到的源 IP 是：{supplier_ips[0]}")
        if local_ip and local_ip != supplier_ips[0]:
            print(f"    注意：本机直连出口 IP 是 {local_ip}，与供应商所见**不一致**"
                  "（可能存在代理 / VPN / 多出口线路）。")
        print(f"    请把 {supplier_ips[0]} 加入该 API Key 的 IP 白名单"
              "（出口 IP 可能动态变化，必要时按 CIDR 放行）。")
        print("    这属于供应商侧配置，本地代码无法绕过。")
        return 1
    if denied:
        print("  ✗ 鉴权 / 权限被拒，但原文中没有可识别的 IP。")
        print("    请核对该 API Key 的归属账号、项目与白名单设置。")
        return 1
    if not model:
        print("  ✗ 未配置模型名：请先在「模型设置」填写模型名或端点 ID。")
        return 1
    print(f"  ✗ 未通过：{res.get('message')}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
