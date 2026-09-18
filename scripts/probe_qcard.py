#!/usr/bin/env python3
"""选项卡（agent-question-composer）真实复现与抓取探针。

用途：让 agent 主动弹出「选择卡片」，把卡片的真实 DOM 事实逐帧落盘，并可选用
驱动自身的 `_qcard_handle()` 循环推进，用来复现 / 回归「反复点但状态不推进」。

与既有探针的分工：
  * probe_composer.py / probe_sidebar.py —— 输入区、侧边栏结构
  * 本脚本 —— 选项卡（多题 / 单选多选 / 提交·确认·跳过三态）

关键事实（依据 app.asar 内组件源码，非猜测）：
  * 容器：`<section class="agent-question-composer" role="dialog">`
  * 选项：`button.agent-question-option`，选中态为附加类 `is-selected`
  * 多题：`.agent-question-composer__counter` 显示 `i+1/u`
  * 底部按钮三态：
        末题          → `.agent-question-composer__primary`，文案「提交」
        非末题已作答  → `.agent-question-composer__primary`，文案「确认」
        非末题未作答  → `.agent-question-composer__skip`，文案「跳过」（**不可点**）
  * `disabled` 表示「提交中」：此时选项与底部按钮一起禁用，文案变「提交中…」
  * 单选且非末题时，点选项会**立即自动跳到下一题**

用法：
    python scripts/probe_qcard.py                  # 复现 + 抓帧（默认不驱动点击）
    python scripts/probe_qcard.py --drive          # 额外用 _qcard_handle() 循环推进
    python scripts/probe_qcard.py --task "…"       # 换提示词
    python scripts/probe_qcard.py --file /abs/x.pdf --attempts 3

产出：artifacts/qcard_probe.json（逐帧事实 + 驱动事件 + 结论），不改动应用。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from core.settings import config_path  # noqa: E402

from drivers.ui_driver import RuiyunUIDriver  # noqa: E402

DEFAULT_FILE = "/Users/amano/Downloads/1尚睿通科技考勤制度V6.1.pdf"
# 刻意留出「格式 / 篇幅 / 对象」的模糊度 —— 这是 agent 主动出选择题的常见触发点
DEFAULT_TASK = "读取我引用的这份文档，把里面的要点总结给我"

OUT = ROOT / "artifacts" / "qcard_probe.json"

# ---------------------------------------------------------------- 探测 JS
# 单次往返取回全部事实，避免多次 eval 之间状态漂移（多题会自动跳题）。
QCARD_FACTS_JS = r"""
(() => {
  const vis = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const clsList = (el) => ((el && el.className) || '').toString().split(/\s+/);
  const txt = (el) => ((el && el.innerText) || '').trim();
  // 只认真正的容器类（子元素是 agent-question-composer__body 等，靠前缀过滤）
  const roots = [...document.querySelectorAll('[class*="agent-question-composer"]')]
    .filter(el => clsList(el).includes('agent-question-composer') && vis(el));
  if (!roots.length) {
    // 探测失败的自证材料：把页面上所有 agent-question* 节点报出来，
    // 便于区分「确实没出卡片」与「卡片出了但选择器没匹配上」
    const hint = [...document.querySelectorAll('[class*="agent-question"]')]
      .slice(0, 10)
      .map(el => el.tagName.toLowerCase() + '.' + ((el.className || '').toString().trim().slice(0, 60)));
    return {found: false, rootCount: 0, hint};
  }
  // 取最后一个（最新）卡片：历史残影在前，不该挡住当前这张
  const composer = roots[roots.length - 1];

  const titleEl = composer.querySelector('#agent-question-title')
              || composer.querySelector('.agent-question-composer__heading h2');
  const counterEl = composer.querySelector('.agent-question-composer__counter');
  const counter = txt(counterEl);
  const m = /^(\d+)\s*\/\s*(\d+)$/.exec(counter);
  const questionIndex = m ? Number(m[1]) - 1 : null;
  const questionCount = m ? Number(m[2]) : null;

  const options = [...composer.querySelectorAll('button.agent-question-option')].map(el => ({
    label: (txt(el.querySelector('.agent-question-option__label')) || txt(el)).slice(0, 80),
    description: txt(el.querySelector('.agent-question-option__description')).slice(0, 100),
    selected: clsList(el).includes('is-selected'),
    disabled: !!el.disabled,
    visible: vis(el),
  }));

  const otherInput = composer.querySelector('.agent-question-other input');
  const customInput = otherInput ? (otherInput.value || '') : null;

  const buttons = [
    ...composer.querySelectorAll('.agent-question-composer__footer button'),
    ...composer.querySelectorAll('.agent-question-composer__nav button'),
  ].map(el => {
    const c = (el.className || '').toString();
    const kind = c.includes('composer__primary') ? 'primary'
               : c.includes('composer__skip') ? 'skip'
               : c.includes('composer__close') ? 'close' : 'nav';
    return {kind, text: txt(el).slice(0, 20), disabled: !!el.disabled, visible: vis(el),
            aria: el.getAttribute('aria-label') || ''};
  });

  const primary = buttons.find(b => b.kind === 'primary' && b.visible) || null;
  const skip = buttons.find(b => b.kind === 'skip' && b.visible) || null;
  // 「提交中」没有独立标志位，用「文案变提交中」或「primary 与全部选项一起禁用」交叉推断
  const submitting = !!(primary && (
    primary.text.includes('提交中')
    || (primary.disabled && options.length > 0 && options.every(o => o.disabled))
  ));

  return {
    found: true,
    rootCount: roots.length,
    question: txt(titleEl).slice(0, 150),
    counter,
    questionIndex, questionCount,
    isLast: (questionIndex !== null && questionCount !== null)
              ? questionIndex === questionCount - 1 : null,
    options, customInput, buttons, primary, skip, submitting,
  };
})()
"""


# 只读走查用：点「下一题」翻页，用来把多题卡片逐题结构抓全（不提交、不改答案）
CLICK_NEXT_JS = r"""
(() => {
  const vis = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const b = document.querySelector('button[aria-label="下一题"]');
  if (!b || b.disabled || !vis(b)) return false;
  b.click();
  return true;
})()
"""


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def brief(f: dict) -> str:
    """一行摘要，日志用（不打印整段对话，只留短文本）。"""
    if not f or not f.get("found"):
        return "卡片不在"
    opts = "、".join(
        f"{o['label'][:12]}{'✓' if o['selected'] else ''}{'✗禁用' if o['disabled'] else ''}"
        for o in f.get("options") or [])
    btn = f.get("primary") or f.get("skip") or {}
    return (f"题 {f.get('counter') or '?'} 末题={f.get('isLast')} 提交中={f.get('submitting')} "
            f"选项[{opts}] 按钮[{btn.get('kind')}:{btn.get('text')}"
            f"{'✗禁用' if btn.get('disabled') else ''}]")


def fingerprint(f: dict) -> str:
    """进度指纹：题目 + 题号 + 选项标签集 + 选中态。指纹变化 == 真的推进。"""
    if not f or not f.get("found"):
        return "(无卡片)"
    labels = "|".join(
        f"{o['label']}{'#' if o['selected'] else ''}" for o in (f.get("options") or []))
    return f"{f.get('counter')}::{f.get('question','')}::{labels}"


def main() -> int:
    ap = argparse.ArgumentParser(description="选项卡真实复现与抓帧探针")
    ap.add_argument("--file", default=DEFAULT_FILE, help="要投递的本机文件（默认考勤制度 PDF）")
    ap.add_argument("--task", default=DEFAULT_TASK, help="发送的提示词")
    ap.add_argument("--attempts", type=int, default=3, help="最多尝试几次以让卡片出现")
    ap.add_argument("--card-timeout", type=float, default=150.0, help="每次尝试等卡片出现的秒数")
    ap.add_argument("--drive", action="store_true",
                    help="发现卡片后用驱动自身 _qcard_handle() 循环推进（复现/回归用）")
    ap.add_argument("--walk", action="store_true",
                    help="逐题点「下一题」只读走查多题卡片结构（不提交、不改答案）")
    ap.add_argument("--switch-test", action="store_true",
                    help="先点一格答案，再切到其它会话并切回，验证卡片是否被重置（答案丢失）")
    ap.add_argument("--drive-seconds", type=float, default=90.0, help="--drive 的最长推进秒数")
    ap.add_argument("--poll", type=float, default=0.5, help="轮询间隔（与流水线一致）")
    ap.add_argument("--out", default=str(OUT), help="证据落盘路径")
    args = ap.parse_args()

    cfg = yaml.safe_load(config_path().read_text(encoding="utf-8"))
    if args.file:
        cfg.setdefault("app", {})
    drv = RuiyunUIDriver(cfg)

    evidence: dict = {
        "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "file": args.file, "task": args.task, "drive": bool(args.drive),
        "attempts": [],
    }

    ok, how = drv.ensure_ready()
    log(f"接入应用: ok={ok} how={how}")
    if not ok:
        evidence["fatal"] = "应用未就绪（ensure_ready 失败）"
        Path(args.out).write_text(json.dumps(evidence, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        return 2
    evidence["attach_mode"] = how

    card_found = False
    try:
        for attempt in range(1, args.attempts + 1):
            rec = {"attempt": attempt, "frames": [], "events": [], "attach_note": ""}
            evidence["attempts"].append(rec)
            log(f"—— 第 {attempt}/{args.attempts} 次尝试 ——")

            if not drv.reset_to_new_task():
                rec["error"] = "无法回到新建任务首页"
                log("  ✗ 无法回到新建任务首页")
                continue

            paths = [p for p in (args.file or "").split(",") if p.strip()]
            if paths:
                ok_att, msg_att = drv.attach_files(paths)
                rec["attach_ok"] = ok_att
                rec["attach_note"] = msg_att
                log(f"  附件{'已引用' if ok_att else '投递失败'}: {msg_att}")

            before = drv.snapshot_sessions()
            try:
                drv.type_text(args.task)
            except Exception as exc:
                rec["error"] = f"输入失败: {type(exc).__name__}: {exc}"
                log(f"  ✗ {rec['error']}")
                continue
            how_send = drv.send()
            sess = drv.wait_new_session(before, timeout_s=90)
            rec["send"] = how_send
            rec["session"] = sess.name if sess else None
            log(f"  已发送（{how_send}），会话 {rec['session']}")

            # ---------- 等卡片出现 ----------
            deadline = time.time() + args.card_timeout
            last_brief = None
            last_hint = None
            while time.time() < deadline:
                try:
                    f = drv.cdp.eval_js(QCARD_FACTS_JS, timeout=10)
                except Exception as exc:
                    rec["frames"].append({"t": datetime.now().strftime("%H:%M:%S"),
                                          "error": f"{type(exc).__name__}: {exc}"})
                    break
                if f and f.get("found"):
                    card_found = True
                    rec["frames"].append({
                        "t": datetime.now().strftime("%H:%M:%S"), "facts": f,
                        "fingerprint": fingerprint(f),
                    })
                    b = brief(f)
                    if b != last_brief:
                        log(f"  ★ 卡片: {b}")
                        last_brief = b
                    break
                # 未命中时留下自证材料：区分「确实没出卡」与「出了但没匹配上」
                hint = (f or {}).get("hint") or []
                if hint and hint != last_hint:
                    log(f"  · 疑似卡片节点但未命中容器: {hint[:4]}")
                    rec["frames"].append({"t": datetime.now().strftime("%H:%M:%S"),
                                          "facts": f, "note": "未命中容器"})
                    last_hint = hint
            if not card_found and not rec["frames"]:
                rec["frames"].append({"t": datetime.now().strftime("%H:%M:%S"),
                                      "facts": {"found": False}, "note": "整轮未见卡片"})

            if not card_found:
                log("  本次未出现卡片（会话可能直接执行），重试")
                time.sleep(3)
                continue

            if args.switch_test:
                # 关键实验：多会话巡检会切走视图，若卡片是组件内状态，
                # 切走再切回就会丢答案、退回第 1 题 —— 那才是「反复点不推进」的根因。
                log("  会话切换实验：先答一格，再切走并切回，看卡片是否被重置")
                st = {"before_click": f}
                ev0 = drv._qcard_handle()
                time.sleep(0.8)
                fa = drv.cdp.eval_js(QCARD_FACTS_JS, timeout=10)
                st["after_click"] = fa
                st["click_event"] = {k: (ev0 or {}).get(k) for k in ("mode", "text")}
                log(f"    答题后: {brief(fa)}  事件={st['click_event']}")

                items = drv._sidebar_items() or []
                st["sidebar"] = [f"{it['idx']}:{it['title'][:18]}" for it in items[:6]]
                log(f"    会话列表: {st['sidebar']}")
                if len(items) >= 2:
                    away = items[-1]
                    drv._open_conversation(away)
                    time.sleep(1.6)
                    st["away_title"] = away.get("title", "")[:24]
                    st["after_away"] = drv.cdp.eval_js(QCARD_FACTS_JS, timeout=10)
                    log(f"    切到「{st['away_title']}」后: {brief(st['after_away'])}")
                    back = drv._find_item(items, args.task) or next(
                        (it for it in items if it.get("idx") == 0), None)
                    if back:
                        drv._open_conversation(back)
                        time.sleep(1.6)
                        st["after_back"] = drv.cdp.eval_js(QCARD_FACTS_JS, timeout=10)
                        log(f"    切回本会话后: {brief(st['after_back'])}")
                        fb = st["after_back"] or {}
                        st["reset"] = (
                            fb.get("counter") != (fa or {}).get("counter")
                            or [o.get("selected") for o in (fb.get("options") or [])]
                               != [o.get("selected") for o in (fa or {}).get("options") or []])
                        log(f"    → 卡片被重置（答案丢失）: {st['reset']}")
                    else:
                        log("    ✗ 未在列表中找到本会话，无法切回")
                rec["switch_test"] = st
                break

            if args.walk:
                # 只读走查：逐题点「下一题」把多题卡片结构抓全（不提交、不改答案）
                log("  开始逐题走查（只点「下一题」）…")
                for step in range(12):
                    f = drv.cdp.eval_js(QCARD_FACTS_JS, timeout=10)
                    rec["frames"].append({"t": datetime.now().strftime("%H:%M:%S"),
                                          "walk": step, "facts": f,
                                          "fingerprint": fingerprint(f)})
                    if not (f or {}).get("found"):
                        log(f"    [{step}] 卡片已不在")
                        break
                    log(f"    [{step}] {brief(f)}")
                    opts = " | ".join(o["label"][:20] for o in (f.get("options") or []))
                    log(f"        题目: {f.get('question','')[:60]} / 选项: {opts or '（无选项，纯输入题）'}")
                    if f.get("isLast"):
                        log("        已到末题")
                        break
                    okc = drv.cdp.eval_js(CLICK_NEXT_JS, timeout=10)
                    if not okc:
                        log("        下一题不可点，停止走查")
                        break
                    time.sleep(0.7)
                break

            if not args.drive:
                log("  抓帧完成（未驱动点击；加 --drive 用驱动自身循环推进，--walk 逐题走查）")
                break

            # ---------- 用驱动自身推进（复现/回归） ----------
            log("  开始用 _qcard_handle() 推进…")
            t_end = time.time() + args.drive_seconds
            prev_fp = fingerprint(rec["frames"][-1]["facts"])
            stuck_since = time.time()
            n_calls = n_events = 0
            miss_streak = 0
            while time.time() < t_end:
                n_calls += 1
                try:
                    ev = drv._qcard_handle()
                except Exception as exc:
                    ev = {"error": f"{type(exc).__name__}: {exc}"}
                try:
                    f = drv.cdp.eval_js(QCARD_FACTS_JS, timeout=10)
                except Exception as exc:
                    f = {"found": False, "error": f"{type(exc).__name__}: {exc}"}
                fp = fingerprint(f)
                frame = {
                    "t": datetime.now().strftime("%H:%M:%S"),
                    "call": n_calls,
                    "event": {k: ev.get(k) for k in ("mode", "text", "q", "kind",
                                                     "reason", "error")} if isinstance(ev, dict) else None,
                    "facts": f, "fingerprint": fp,
                    "progress": fp != prev_fp,
                }
                rec["frames"].append(frame)
                if ev:
                    n_events += 1
                    log(f"    → {frame['event']}")
                if fp != prev_fp:
                    log(f"    ✓ 进度变化: {brief(f)}")
                    prev_fp, stuck_since = fp, time.time()
                # React 重渲染会让卡片短暂读不到，连续 3 次未命中才认定「真的消失」
                if not (f or {}).get("found"):
                    miss_streak += 1
                    if miss_streak >= 3:
                        log("  ✓ 卡片已消失（推进到任务继续）")
                        break
                else:
                    miss_streak = 0
                if getattr(drv, "confirm_human_needed", False):
                    log("  ‼️ 驱动已请求人工介入（熔断）—— 复现到失效路径")
                    break
                if time.time() - stuck_since > 30 and n_calls > 3:
                    log("  ‼️ 30s 内指纹未变化 —— 复现到「点了没用」")
                    break
                time.sleep(args.poll)

            rec["drive_calls"] = n_calls
            rec["drive_events"] = n_events
            rec["human_needed"] = bool(getattr(drv, "confirm_human_needed", False))
            rec["auto_confirm_still_on"] = bool(drv.auto_confirm)
            rec["card_gone"] = not bool((rec["frames"][-1].get("facts") or {}).get("found"))
            break
    finally:
        evidence["confirm_events"] = list(getattr(drv, "confirm_events", []))
        evidence["confirm_human_needed"] = bool(getattr(drv, "confirm_human_needed", False))
        evidence["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        evidence["card_found"] = card_found
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(evidence, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        log(f"证据已落盘 → {args.out}（卡片出现={card_found}，"
            f"自动确认事件={len(evidence['confirm_events'])}）")
        drv.detach()
    return 0 if card_found else 1


if __name__ == "__main__":
    raise SystemExit(main())
