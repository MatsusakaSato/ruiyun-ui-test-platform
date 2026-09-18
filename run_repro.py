#!/usr/bin/env python3
"""复现率验证器 —— 对指定 bug 签名做 N 次 UI 自动化复现，量化稳定性。

用法：
    # 列出当前日志里所有 bug 的复现配方（不执行，零成本）
    python run_repro.py --list

    # 对某条 bug 自动复现 3 次，输出复现率（需要驱动 UI，产生真实对话）
    python run_repro.py --rule TOOL_CALL_FAILED --tool read_memory --times 3

    # 对全部 bug 签名逐一验证（耗时 = 签名数 × times × 单次耗时）
    python run_repro.py --all --times 2

    # 验证结果落盘 + 合并进 HTML 报告
    python run_repro.py --rule TOOL_CALL_FAILED --tool mcp_paid_search --times 3 --update-report
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from core.assertor import run_assertions  # noqa: E402
from core.log_parser import parse_session  # noqa: E402
from core.repro import build_recipes, verify_recipe  # noqa: E402
from core.settings import config_path, effective_config  # noqa: E402


def _log(msg):
    print(msg, flush=True)


def load_traces(cfg):
    root = Path(cfg["paths"]["session_root"])
    traces = []
    if root.is_dir():
        for d in sorted(root.glob("sess_*")):
            if d.is_dir():
                t = parse_session(d)
                if t:
                    traces.append(t)
    return traces


def all_findings(cfg, traces):
    from run_pipeline import read_max_iterations
    out = []
    for t in traces:
        out.extend(run_assertions(t, cfg["rules"], read_max_iterations(cfg)))
    return out


def save(recipes, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True) if not path.parent.is_dir() else None
    path.write_text(json.dumps([r.to_dict() for r in recipes], ensure_ascii=False, indent=2),
                    encoding="utf-8")


def render(cfg, traces, findings, recipes, run_mode="复现验证"):
    """重渲染报告：以上次完整运行的 metrics 为基底，注入复现数据。

    所有 bug 签名都会带上复现方法；已验证的签名额外叠加复现率结果。
    """
    from report.builder import render_report
    from core.metrics import build_metrics, repro_block
    from core.repro import build_recipes
    from run_pipeline import read_app_version

    mp = ROOT / "artifacts" / "metrics.json"
    if mp.is_file():
        metrics = json.loads(mp.read_text(encoding="utf-8"))
    else:
        metrics = build_metrics([], cfg)

    # 全量配方打底，再用已验证结果叠加复现率
    all_recipes = {r.key: r for r in build_recipes(findings, traces, cfg)}
    for r in recipes:
        all_recipes[r.key] = r
    by_key = {k: r.to_dict() for k, r in all_recipes.items()}

    for row in metrics.get("findings_rows", []):
        rep = by_key.get(f"{row['rule']}:{row['tool'] or '-'}")
        if rep:
            row["repro"] = rep
    repro_rows, repro_summary = repro_block(list(all_recipes.values()))
    metrics["repro_rows"] = repro_rows
    metrics["repro_summary"] = repro_summary

    ver, bundle = read_app_version(cfg)
    rp = ROOT / "report" / "ruiyun_hardbug_report.html"
    render_report(metrics, rp, app_version=ver, bundle_id=bundle,
                  run_mode=run_mode, total_elapsed=0)
    return rp


def main() -> int:
    ap = argparse.ArgumentParser(description="bug 复现率验证器")
    ap.add_argument("--config", default=str(config_path()))
    ap.add_argument("--list", action="store_true", help="仅列出复现配方，不执行")
    ap.add_argument("--rule", help="限定断言规则，如 TOOL_CALL_FAILED")
    ap.add_argument("--tool", help="限定工具名，如 read_memory")
    ap.add_argument("--all", action="store_true", help="验证全部 bug 签名")
    ap.add_argument("--times", type=int, default=3, help="每个配方重复次数（默认 3）")
    ap.add_argument("--keep-app", action="store_true",
                    help="[已废弃] 应用现在常驻不关闭，该参数无任何作用")
    ap.add_argument("--update-report", action="store_true", help="验证后重渲染 HTML 报告")
    ap.add_argument("--render-only", action="store_true",
                    help="不驱动 UI，用上次验证结果（artifacts/repro_results.json）重渲染报告")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    # 应用路径 / 日志路径三层合并：.app_settings.json > config.yaml > 内置默认
    cfg = effective_config(cfg)
    traces = load_traces(cfg)
    findings = all_findings(cfg, traces)
    recipes = build_recipes(findings, traces, cfg)

    if args.rule:
        recipes = [r for r in recipes if r.rule == args.rule]
    if args.tool:
        recipes = [r for r in recipes if r.tool == args.tool]

    if not recipes:
        _log("没有匹配的 bug 签名（日志可能已全部修复）")
        return 0

    # ---------------- 仅重渲染 ----------------
    if args.render_only:
        saved = ROOT / "artifacts" / "repro_results.json"
        recipes = []
        if saved.is_file():
            from core.repro import ReproRecipe
            recipes = [ReproRecipe(**d) for d in json.loads(saved.read_text(encoding="utf-8"))]
        findings = all_findings(cfg, traces)
        rp = render(cfg, traces, findings, recipes)
        _log(f"报告已更新 {rp}")
        return 0

    # ---------------- 列表模式 ----------------
    if args.list:
        _log(f"共 {len(recipes)} 个 bug 签名的复现配方：\n")
        for r in recipes:
            _log(f"[{r.severity}] {r.rule_name}  {r.key}")
            _log(f"  来源会话 : {r.source_session}（提示词取自{'原始提问' if r.prompt_source == 'original' else '合成'}）")
            _log(f"  复现提示词: {r.prompt}")
            if r.trigger_args:
                _log(f"  触发参数  : {json.dumps(r.trigger_args, ensure_ascii=False)[:160]}")
            _log(f"  验证标准  : {r.verify_desc}")
            _log(f"  预期/实际 : {r.expected} ↔ {r.actual[:80]}")
            _log("")
        return 0

    # ---------------- 验证模式 ----------------
    targets = recipes if args.all else recipes[:1]
    _log(f"待验证签名 {len(targets)} 个 × {args.times} 次 = 至多 {len(targets) * args.times} 轮 UI 对话\n")

    from drivers.ui_driver import RuiyunUIDriver
    driver = RuiyunUIDriver(cfg)
    ok, how = driver.ensure_ready()
    if not ok:
        _log("✗ 应用启动失败或无法接入渲染进程" if how == "launch_failed"
             else "✗ 无法接入渲染进程")
        return 2
    _log(f"✓ {'复用已运行的应用' if how == 'reused' else '已启动应用'} | "
         f"界面 {driver.target_url}\n")

    for r in targets:
        _log(f"▶ 验证 {r.key}（{r.rule_name}）")
        _log(f"  提示词: {r.prompt[:70]}")
        verify_recipe(r, driver, cfg, times=args.times, log=_log)
        _log(f"  ⇒ 复现 {r.hits}/{r.attempts} = {r.rate:.0%} → {r.stability}\n")
    driver.detach()   # 仅断开连接，应用保持运行

    out = ROOT / "artifacts" / "repro_results.json"
    save(targets, out)
    _log(f"结果已写入 {out}")

    if args.update_report:
        rp = render(cfg, traces, findings, targets)
        _log(f"报告已更新 {rp}")

    # ---------------- 汇总 ----------------
    _log("=" * 70)
    for r in targets:
        _log(f"[{r.severity}] {r.key:48s} {r.hits}/{r.attempts} = "
             f"{(r.rate or 0):.0%}  {r.stability}")
    _log("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
