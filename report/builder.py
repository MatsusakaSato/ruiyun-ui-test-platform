"""报告生成器：把度量渲染成自包含的可视化 HTML。"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATE_DIR = Path(__file__).resolve().parent


def render_report(metrics: dict, out_path: Path, *,
                  app_version: str = "",
                  bundle_id: str = "",
                  run_mode: str = "",
                  total_elapsed: float = 0.0) -> Path:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )

    nonzero_rules = [r["count"] for r in metrics["rule_rows"] if r["count"] > 0]

    # 按用例归集问题：报告逐用例展示，不做跨用例汇总
    case_findings: dict = {}
    for f in metrics.get("findings_rows") or []:
        case_findings.setdefault(f.get("case_id") or "", []).append(f)

    html = env.get_template("template.html").render(
        s=metrics["summary"],
        rule_rows=metrics["rule_rows"],
        severity=metrics["severity"],
        case_rows=metrics["case_rows"],
        case_objective=metrics.get("case_objective") or {},
        case_findings=case_findings,
        repro_summary=metrics.get("repro_summary") or {"verified": 0},
        max_rule=max(nonzero_rules) if nonzero_rules else 0,
        app_version=app_version,
        bundle_id=bundle_id,
        run_mode=run_mode,
        generated_at=_dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        total_elapsed=round(total_elapsed, 1),
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path
