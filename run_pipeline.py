#!/usr/bin/env python3
"""睿云智能工作台 · UI 测试平台 —— 主流程编排。

三段式流水线（控制台按 [1/3] [2/3] [3/3] 报进度）：
  1  UI 自动化操作   drivers/ui_driver.py  注入用例、等待新会话落盘
  2  日志校验调用链   core/log_parser.py + core/assertor.py
  3  测试报告生成     core/metrics.py + report/builder.py

用法：
    python run_pipeline.py                    # 完整流程（UI + 日志 + 报告）
    python run_pipeline.py --cases 1          # 只跑前 1 条用例
    python run_pipeline.py --keep-app         # 已废弃（应用现在常驻，不会自动关闭）
"""
from __future__ import annotations

import argparse
import json
import plistlib
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from core.assertor import run_assertions  # noqa: E402
from core.log_parser import parse_session  # noqa: E402
from core.metrics import build_metrics  # noqa: E402
from core.models import CaseResult, Finding  # noqa: E402
from core.settings import config_path, effective_config, preset_path, rounds_dir  # noqa: E402
from report.builder import render_report  # noqa: E402


def _log(msg: str) -> None:
    print(msg, flush=True)


# 控制台分隔线宽度：与最宽的汇总行持平，够用即可
_RULE = "─" * 58

# 控制台一律说中文，不把内部状态码（PASS/FAIL/UI_FAIL）直接抛给用户
_STATUS_CN = {"PASS": "通过", "FAIL": "断言失败", "UI_FAIL": "UI 失败"}


def _ev_ts(ev: dict) -> float:
    try:
        return float(ev.get("ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def _events_for_session(events: list, sess, t_send: float) -> list:
    """从全轮事件流里取出属于某会话的自动确认事件。

    口径优先级：
      1. 驱动在点击那一刻记录了归属会话（ev["sess"]）→ 按会话目录严格匹配。
         并发（max_inflight>1）下这是唯一可靠的信息源；
      2. 事件没有 sess（旧版本产物等）→ 回退「该用例发送之后」的时间窗口径，
         只能近似（条目标注归属来源 time-window）。
    """
    key = str(sess)
    owned = [e for e in (events or [])
             if isinstance(e, dict) and str(e.get("sess") or "") == key]
    if owned:
        return owned
    return [e for e in (events or [])
            if isinstance(e, dict) and not e.get("sess")
            and _ev_ts(e) >= t_send - 1]


def _read_exe_version_windows(binary: Path) -> tuple:
    """从 PE 版本资源读取版本号与产品名（Windows，纯 ctypes 标准库实现）。

    Electron 打包（electron-builder NSIS）默认写入 FileVersion / ProductName
    资源；读不到任何一项时返回空串，不阻塞流程（与 macOS 分支口径一致）。
    """
    import ctypes
    from ctypes import wintypes

    ver = ctypes.WinDLL("version")
    ver.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR,
                                            ctypes.POINTER(wintypes.DWORD)]
    ver.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                        wintypes.DWORD, wintypes.LPVOID]
    ver.VerQueryValueW.argtypes = [wintypes.LPCVOID, wintypes.LPCWSTR,
                                   ctypes.POINTER(wintypes.LPVOID),
                                   ctypes.POINTER(wintypes.UINT)]

    path = str(binary)
    size = ver.GetFileVersionInfoSizeW(path, None)
    if not size:
        return "", ""
    data = ctypes.create_string_buffer(size)
    if not ver.GetFileVersionInfoW(path, 0, size, data):
        return "", ""

    # 1) Translation 表是二进制（语言/代码页 WORD 对），先拿它拼 StringFileInfo 键
    tbuf, tlen = wintypes.LPVOID(), wintypes.UINT()
    if not ver.VerQueryValueW(ctypes.cast(data, wintypes.LPCVOID),
                              "\\VarFileInfo\\Translation",
                              ctypes.byref(tbuf), ctypes.byref(tlen)) or tlen.value < 4:
        return "", ""
    words = (wintypes.WORD * (tlen.value // 2)).from_address(tbuf.value)
    lang_cp = f"{words[0]:04X}{words[1]:04X}"

    # 2) 按「语言+代码页」逐项查字符串资源（末尾 NUL 不计入长度）
    def query_str(name: str) -> str:
        buf, blen = wintypes.LPVOID(), wintypes.UINT()
        if not ver.VerQueryValueW(ctypes.cast(data, wintypes.LPCVOID),
                                  f"\\StringFileInfo\\{lang_cp}\\{name}",
                                  ctypes.byref(buf), ctypes.byref(blen)) or blen.value <= 1:
            return ""
        return ctypes.wstring_at(buf.value, blen.value - 1)

    return query_str("FileVersion") or query_str("ProductVersion"), query_str("ProductName")


def read_app_version(cfg: dict) -> tuple:
    """读取被测应用版本号：Windows 读 exe 版本资源，macOS 读 Info.plist。"""
    binary = Path(cfg["app"]["binary"])
    try:
        if sys.platform == "win32":
            return _read_exe_version_windows(binary)
        plist = binary.parent.parent / "Info.plist"
        with plist.open("rb") as f:
            info = plistlib.load(f)
        return info.get("CFBundleShortVersionString", ""), info.get("CFBundleIdentifier", "")
    except Exception:
        return "", cfg["app"].get("bundle_id", "")


def read_max_iterations(cfg: dict) -> int:
    """从 agent 配置读取 ReAct 迭代上限，作为死循环判定的基线。"""
    p = Path(cfg["paths"].get("agent_config", ""))
    if not p.is_file():
        return 0
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return int((data.get("react") or {}).get("max_iterations") or 0)
    except Exception:
        return 0


# --------------------------------------------------------------------- Stage 1
def run_ui_cases(cfg: dict, cases: list, driver) -> list:
    """流水线执行用例：发送串行、生成并行，最多 max_inflight 条同时在途。

    实测依据（experiment_pipeline.py）：A 生成中「新建任务」0.4s 可点，
    新会话目录在发送瞬间创建，先发的任务不被后续操作打断，两者日志互不干扰。

    发送是串行的 —— 一条用例发送并确认其会话目录出现后才发下一条，
    因此「用例 ↔ 会话」绑定依然明确；等待则谁先完成先收谁（滑动窗口）。
    max_inflight=1 时退化为完全串行。
    """
    results = []
    # 等待上限：对模型工作时长不设限，仅在等满后停止等待并解析已有日志
    case_timeout = float(cfg.get("case_timeout_s", 1200))
    # 新会话目录出现的等待（与应用生成时长无关，通常几秒）
    new_sess_timeout = float(cfg.get("new_session_timeout_s", 90))
    # 同时在途的会话数上限
    max_inflight = max(1, int(cfg.get("max_inflight", 5)))

    pending = list(enumerate(cases, 1))
    inflight = []          # 每项 [res, sess, done, t_send]
    n_total = len(cases)
    drain_logged = False   # 全部发送完只提示一次，避免等待期刷屏

    def _collect(res, sess, done: bool, t_send: float):
        """收取一条完成的用例：解析日志、跑断言、入结果集。"""
        if not done:
            res.waited_limit = True
            res.wait_note = f"已达等待上限（{case_timeout / 60:.0f} 分钟），按已落盘日志出结果"
        # 全流程标记：按会话归属收集该用例的自动确认（含工具授权/计划确认）。
        # 旧口径「发送之后全部算它的」在并发下必然串台（先完成者背锅、最后一条
        # 吞掉后面全部），故改为按驱动记录的 sess 归属，见 _events_for_session。
        res.confirm_events = _events_for_session(driver.confirm_events, sess, t_send)
        res.auto_confirms = len(res.confirm_events)
        res.ui_ok = True
        res.trace = parse_session(sess)
        if res.trace is None:
            res.ui_error = "会话日志解析失败"
            res.ui_ok = False
        else:
            res.findings = run_assertions(
                res.trace, cfg["rules"], read_max_iterations(cfg)
            )
        results.append(res)
        tr = res.trace
        _log(f"        已收取 {res.case_id} · 会话 {sess.name} · 工具调用 "
             f"{len(tr.tool_calls) if tr else 0} 次 · 问题 {len(res.findings)} 个 · "
             f"{_STATUS_CN.get(res.status, res.status)}"
             + ("（等待超时，按已有日志出结果）" if not done else ""))

    while pending or inflight:
        # ---------- 1) 填满在途窗口 ----------
        while pending and len(inflight) < max_inflight:
            i, case = pending.pop(0)
            cid = case.get("id") or f"CASE-{i:03d}"
            name = case.get("name") or cid
            prompt = case.get("prompt") or ""
            _log(f"\n发送 {i}/{n_total} · {cid if name == cid else cid + ' ' + name}")
            _log(f"        提问：{prompt[:60]}{'...' if len(prompt) > 60 else ''}")
            res = CaseResult(
                case_id=cid, name=name, prompt=prompt,
                expected_tools=case.get("expect_tools") or [],
            )
            t0 = time.time()
            try:
                # 回到「新建任务」首页：生成中的其他会话不受影响（已实测）
                if not driver.reset_to_new_task():
                    res.ui_ok = False
                    # 失败原因必须可诊断：停在登录页 vs 组件没挂上，处理方式完全不同
                    st = {}
                    try:
                        st = driver.page_state() or {}
                    except Exception:
                        st = {}
                    if st.get("login_like"):
                        res.ui_error = ("应用停留在登录页（页面上有登录/密码框）："
                                        "请先在该应用里完成登录，再运行测试")
                    else:
                        res.ui_error = "没能回到「新建任务」首页"
                    _log(f"        失败：{res.ui_error}"
                         f"（页面 {st.get('href') or '未知'} · "
                         f"新建任务按钮 {'有' if st.get('has_new_task') else '无'} · "
                         f"输入区 {'有' if st.get('has_composer') else '无'}）")
                    if st.get("body_head"):
                        _log(f"        页面正文开头：{st['body_head']}")
                    res.elapsed_s = time.time() - t0
                    results.append(res)
                    continue

                # 附件投递：必须在输入文本之前 —— 应用把「引用文件」与草稿一起提交。
                # 投递失败仍继续发送（保留消息内容作为对照），但写入 ui_error 明确标为异常，
                # 避免「附件根本没送进去，用例却照常跑完」的假通过。
                attach_paths = [str(p) for p in (case.get("attachments") or []) if str(p).strip()]
                if attach_paths:
                    res.attachments = attach_paths
                    ok_att, msg_att = driver.attach_files(attach_paths)
                    res.attach_note = msg_att
                    _log(f"        附件：{'已引用' if ok_att else '投递失败 —— ' + msg_att}")
                    if not ok_att:
                        res.ui_error = f"附件未投递：{msg_att}"

                before = driver.snapshot_sessions()
                driver.type_text(prompt)
                time.sleep(0.5)
                how = driver.send()
                sess = driver.wait_new_session(before, timeout_s=new_sess_timeout)
                if not sess:
                    # 未产生会话时把输入区现状一并打印：必须能区分「字没进去」与
                    # 「字进去了但发送按钮不可点/没点到」，否则只剩一句笼统的失败。
                    st = {}
                    try:
                        st = driver.composer_state() or {}
                    except Exception:
                        st = {}
                    res.ui_error = (
                        "发送后没有产生新会话日志"
                        + (f"（发送方式 {how}；输入框里 {st.get('text_len')} 字，"
                           f"发送按钮 {st.get('send_button') or '没找到'}）" if st else ""))
                    res.elapsed_s = time.time() - t0
                    results.append(res)
                    _log(f"        失败：没有产生新会话（发送方式 {how}，输入框 "
                         f"{st.get('text_len', '?')} 字，发送按钮 "
                         f"{st.get('send_button') or '没找到'}）")
                    if st.get("input_selector"):
                        _log(f"        输入框定位：{st['input_selector']}")
                    continue

                res.session_id = sess.name
                # 事件归属：此后驱动点击的确认卡片都算在这条会话头上
                driver.set_current_sess(sess)
                inflight.append([res, sess, False, t0])
                _log(f"        已发送 · 会话 {sess.name} 已创建（进行中 "
                     f"{len(inflight)}/{max_inflight}）")
            except Exception as exc:
                res.ui_ok = False
                res.ui_error = f"{type(exc).__name__}: {exc}"
                _log(f"        失败：界面操作出错 —— {res.ui_error}")
                # 输入相关的异常一律补一份页面现状：登录页 / 组件未挂载 / 选择器失效
                # 在界面上看起来都是「发不出去」，必须能区分
                if "输入框" in str(exc) or "未写入" in str(exc):
                    try:
                        st = driver.page_state() or {}
                        _log(f"        当前页面 {st.get('href') or '未知'}，"
                             f"登录页 {'是' if st.get('login_like') else '否'}，"
                             f"输入区 {'有' if st.get('has_composer') else '无'}")
                        if st.get("body_head"):
                            _log(f"        页面正文开头：{st['body_head']}")
                    except Exception:
                        pass
                res.elapsed_s = time.time() - t0
                results.append(res)

        # ---------- 2) 等待至少一条在途会话稳定 ----------
        if not inflight:
            continue
        if not pending and not drain_logged:
            drain_logged = True
            _log(f"\n  {n_total} 条用例已全部发送，进行中 {len(inflight)} 条 —— "
                 f"等待生成完成后依次收取…")

        # 自动确认连续失败 → 请求人工介入：给最旧的在途用例挂 P0 finding（仅一次）
        if (getattr(driver, "confirm_human_needed", False)
                and not getattr(driver, "_human_reported", False)):
            driver._human_reported = True
            # 挂到「转人工那一刻所在会话」对应用例；无归属信息时退回最旧在途用例
            hs = str(getattr(driver, "confirm_human_sess", "") or "")
            owner = next((r for r, s, _, _ in inflight if hs and str(s) == hs), None)
            res0 = owner if owner is not None else inflight[0][0]
            res0.findings.append(Finding(
                rule="CONFIRM_MANUAL_NEEDED", severity="P0",
                session_id=res0.session_id,
                detail=("确认卡片自动点击连续 5 次失败，任务卡在等待人工授权。"
                        "请在应用界面手动处理；处理后的结果仍会被正常采集，"
                        "但该用例已标记需人工复核。"),
                evidence="详见控制台「确认」相关日志，以及本用例时间线里的自动确认条目",
            ))
            res0.ui_ok = True
            _log("        自动确认连续失败，需要你手动处理：用例 "
                 f"{res0.case_id} 卡在确认卡片，请在应用界面点一下")

        dirs = [sess for _, sess, _, _ in inflight]
        pairs = [(sess, res) for res, sess, _, _ in inflight]
        settled = driver.wait_some_settled(
            dirs, timeout_s=case_timeout, inflight_pairs=pairs,
            cycle_s=float((cfg.get("app") or {}).get("auto_confirm_cycle_s", 6)),
        )
        settled_set = set(settled)

        if settled_set:
            # 收走已完成的，其余留在窗口继续等
            remaining = []
            for res, sess, done, t0 in inflight:
                if sess in settled_set:
                    res.elapsed_s = time.time() - t0
                    _collect(res, sess, done=True, t_send=t0)
                else:
                    remaining.append([res, sess, done, t0])
            inflight = remaining
        else:
            # 等满上限一条都没稳定 → 全部剩余按等待上限出结果（非异常）
            for res, sess, done, t0 in inflight:
                res.elapsed_s = time.time() - t0
                _collect(res, sess, done=False, t_send=t0)
            inflight = []

    return results


def load_cases(cases_file: str) -> list:
    """载入本轮用例。

    优先使用调用方手动指定的用例文件（界面输入 → 临时文件），
    未指定时回退到用户工作区的本地 SQLite 预设数据库。
    """
    if not cases_file:
        from core.testcase_db import get_preset_cases
        return get_preset_cases()

    path = Path(cases_file)
    if not path.is_file():
        _log(f"  找不到用例文件：{path}")
        return []
    if path.suffix.lower() in (".db", ".sqlite", ".sqlite3"):
        from core.testcase_db import get_preset_cases
        return get_preset_cases(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        data = yaml.safe_load(text)
    if isinstance(data, list):          # 纯列表形式
        return data
    return (data or {}).get("cases") or []


# --------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description="睿云智能工作台 UI 测试平台")
    ap.add_argument("--config", default=str(config_path()))
    ap.add_argument("--cases", type=int, default=0, help="只执行前 N 条用例")
    ap.add_argument("--cases-file", default="",
                    help="手动指定的用例文件（YAML/JSON），供界面输入用例时使用")
    ap.add_argument("--repro-times", type=int, default=0,
                    help="对每条 bug 签名自动复现 N 次，量化复现率（0=关闭）")
    ap.add_argument("--repro-limit", type=int, default=0,
                    help="最多验证多少个 bug 签名（按 P0→P1 排序取前 N，0=全部）")
    ap.add_argument("--max-inflight", type=int, default=0,
                    help="同时在途用例上限（流水线并发数），0=沿用 config.yaml 的 max_inflight")
    ap.add_argument("--auto-confirm", choices=("on", "off"), default=None,
                    help="是否自动点击确认卡片 / 选项卡作答：on=强制开启，off=强制关闭，"
                         "不传则沿用 config.yaml 的 app.auto_confirm")
    ap.add_argument("--keep-app", action="store_true",
                    help="[已废弃] 应用现在常驻不关闭，该参数无任何作用，仅为兼容旧命令保留")
    ap.add_argument("--run-id", default="", help="轮次 ID（可视化平台传入，用于归档该轮全部数据）")
    ap.add_argument("--report-name", default="ruiyun_hardbug_report.html")
    args = ap.parse_args()

    t_start = time.time()
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    # 应用路径 / 日志路径三层合并：.app_settings.json > config.yaml > 内置默认
    cfg = effective_config(cfg)
    if args.max_inflight > 0:
        cfg["max_inflight"] = args.max_inflight
    # 自动点击：界面开关显式传值就覆盖配置；不传（None）时沿用 config.yaml，
    # 这样命令行直接跑 run_pipeline.py 的行为不变。
    if args.auto_confirm is not None:
        cfg.setdefault("app", {})["auto_confirm"] = (args.auto_confirm == "on")
    if args.keep_app:
        _log("提示：--keep-app 已失效（应用现在常驻，运行结束不会关闭）")

    cases = load_cases(args.cases_file)
    if args.cases:
        cases = cases[: args.cases]
    if not cases:
        _log("  本轮没有可用用例，已中止")
        return 2
    app_version, bundle_id = read_app_version(cfg)
    max_iter = read_max_iterations(cfg)
    run_mode = "UI 自动化"
    case_src = "手动输入" if args.cases_file else "本地 SQLite 预设库"
    _log(_RULE)
    _log("睿云智能工作台 · UI 测试平台")
    _log(f"应用版本 {app_version or '未读取到'} · 用例 {len(cases)} 条（{case_src}）")
    _log(f"并发 {max(1, int(cfg.get('max_inflight', 5)))} 条 · "
         f"迭代上限 {max_iter or '未读取到'} · 模式 {run_mode}"
         # 自动点击只在被显式指定时说明：沿用配置时不必占一行
         + (" · 自动点击 开" if args.auto_confirm == "on"
            else " · 自动点击 关" if args.auto_confirm == "off" else ""))
    if not (cfg.get("app") or {}).get("auto_confirm", False):
        # 关掉自动点击时先提醒一句：遇到确认卡片只能人工点，否则会一直等到超时
        _log("提示：自动点击已关闭，用例出现确认卡片时需要你自己在应用里点一下，"
             "否则会等到超时才出结果")
    _log(_RULE)

    driver = None
    results = []

    from drivers.ui_driver import RuiyunUIDriver
    driver = RuiyunUIDriver(cfg)
    _log("\n[1/3] 连接应用…")
    ok, how = driver.ensure_ready()
    if not ok:
        if how == "launch_failed":
            _log("  失败：应用没能启动，或调试端口未就绪")
        else:
            _log("  失败：连不上应用界面（渲染进程不可用）")
        return 2
    _log(f"  已连接（{'复用正在运行的应用' if how == 'reused' else '新启动了应用'}）")

    # 应用常驻：默认结束后不关闭，仅断开 CDP 连接（下次运行直接复用）。
    # 仅当 config 显式设 close_app_after_run=true 时才关闭「我们自己启动的」实例。
    results = run_ui_cases(cfg, cases, driver)
    driver.detach()
    if cfg.get("app", {}).get("close_app_after_run"):
        driver.kill_app()
        _log("  · 已按 close_app_after_run=true 关闭应用进程")

    t_stage1 = time.time() - t_start   # Stage 1 耗时：应用启动 + UI 用例执行

    # --------------------------------------------- Stage 2 本轮日志覆盖断言
    # 评估口径：只针对本轮用例产生的会话断言，不追溯历史日志
    _log("\n[2/3] 校验调用链路…")
    t_stage2 = time.time()
    max_iter = read_max_iterations(cfg)
    round_findings = [f for c in results for f in c.findings]
    for c in results:
        if c.trace is not None and not c.findings:
            # 兜底：UI 阶段未断言的（如回放失败路径）补一次
            c.findings = run_assertions(c.trace, cfg["rules"], max_iter)
    _log(f"  {len(results)} 条用例共命中 {len(round_findings)} 个问题"
         + ("（本轮未发现问题）" if not round_findings else ""))
    t_stage2 = time.time() - t_stage2

    # ------------------------------------------------ Stage 2.5 复现率验证
    recipes = []
    if args.repro_times > 0 and round_findings:
        from core.repro import build_recipes, verify_recipe
        round_traces = [c.trace for c in results if c.trace]
        recipes = build_recipes(round_findings, round_traces, cfg)
        if args.repro_limit and len(recipes) > args.repro_limit:
            # P0 优先验证，控制耗时
            recipes.sort(key=lambda r: (0 if r.severity == "P0" else 1, r.key))
            recipes = recipes[: args.repro_limit]
        _log(f"\n[附加] 复现率验证：{len(recipes)} 个问题 × {args.repro_times} 次")
        if not recipes:
            _log("  没有可复现的用例（缺原始提问，也无法合成）")
        for r in recipes:
            _log(f"  · {r.key}  提问：{r.prompt[:52]}")
            verify_recipe(r, driver, cfg, times=args.repro_times, log=lambda m: _log("    " + m))
            _log(f"    复现 {r.hits}/{r.attempts} = {r.rate:.0%} → {r.stability}")
        driver.detach()   # 仅断开连接，应用保持运行
        if cfg.get("app", {}).get("close_app_after_run"):
            driver.kill_app()
            _log("  已关闭应用进程")

    # ---------------------------------------------------------- Stage 3 报告
    _log("\n[3/3] 生成测试报告…")
    t_stage3 = time.time()
    elapsed = time.time() - t_start
    stage_times = {
        "ui_automation_s": round(t_stage1, 1),
        "assert_s": round(t_stage2, 1),
        "report_s": 0.0,
        "total": round(elapsed, 1),
    }
    metrics = build_metrics(results, cfg, recipes=recipes, stage_times=stage_times)
    report_path = ROOT / "report" / args.report_name
    render_report(metrics, report_path, app_version=app_version,
                  bundle_id=bundle_id, run_mode=run_mode, total_elapsed=elapsed)
    stage_times["report_s"] = round(time.time() - t_stage3, 1)
    metrics["objective"]["timing"]["stage_times"] = stage_times

    art = ROOT / "artifacts"
    if not art.is_dir():
        art.mkdir(parents=True)
    (art / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    (art / "case_results.json").write_text(
        json.dumps([c.to_dict() for c in results], ensure_ascii=False, indent=2),
        encoding="utf-8")
    if recipes:
        (art / "repro_results.json").write_text(
            json.dumps([r.to_dict() for r in recipes], ensure_ascii=False, indent=2),
            encoding="utf-8")

    # ------------------------------------------------ 轮次归档（可视化平台数据源）
    # 归档到用户工作区 rounds/（与用例、附件同处，用户可直接查看）
    if args.run_id:
        from core.trajectory import build_round_detail
        round_dir = rounds_dir() / args.run_id
        if not round_dir.is_dir():
            round_dir.mkdir(parents=True)
        detail = build_round_detail(
            results, metrics,
            [r.to_dict() for r in recipes] if recipes else [],
            stage_times=stage_times, cfg=cfg)
        (round_dir / "round_detail.json").write_text(
            json.dumps(detail, ensure_ascii=False), encoding="utf-8")
        round_summary = {
            "run_id": args.run_id,
            "run_mode": run_mode,
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "exit": "ok",
            "elapsed_s": round(elapsed, 1),
            "app_version": app_version,
            "summary": metrics["summary"],
            "repro_summary": metrics.get("repro_summary") or {},
            "cases": metrics["case_rows"],
            "round_tools": detail["round_tools"],
            "round_skills": detail["round_skills"],
            # 本轮产物清单：跑完即落盘，**不需要先做质量评估**。
            # 「这轮产出了哪些文件」是执行结果的一部分，跟有没有配模型无关，
            # 因此放在这里而不是评估产物里（评估仍会算它自己的那份，用于判分）。
            "round_artifacts": detail.get("round_artifacts") or [],
            "round_artifact_kinds": detail.get("round_artifact_kinds") or [],
            # 全流程标记：本轮所有自动确认事件（时间/按钮文本/class）
            "auto_confirm_events": getattr(driver, "confirm_events", []) if driver else [],
            # 自动确认连续失败时置 true —— 提示查看控制台并人工介入
            "auto_confirm_human_needed": bool(getattr(driver, "confirm_human_needed", False)) if driver else False,
        }
        (round_dir / "round_summary.json").write_text(
            json.dumps(round_summary, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            (round_dir / "report.html").write_text(
                report_path.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception:
            pass
        _log(f"\n  本轮数据已归档：{round_dir}")

    s = metrics["summary"]
    obj = metrics["objective"]
    _log("\n" + _RULE)
    _log("本轮结果")
    _log(f"  用例        {s['cases']} 条：通过 {s['passed']} · 断言失败 {s['failed']} · "
         f"UI 失败 {s['ui_failed']}（通过率 {s['pass_rate']}%）")
    _log(f"  问题发现    {s['findings']} 个（P0 {s['p0']} · P1 {s['p1']}）"
         + ("  ← 本轮未发现问题" if s['findings'] == 0 else ""))
    _log(f"  工具调用    {s['tool_calls_total']} 次，失败 {s['tool_calls_failed']} 次"
         f"（{s['tool_fail_rate']}%），截断 {s['tool_calls_truncated']} 次")
    _log(f"  对话用量    提问 {obj['requests']['turns']} 轮 · 思考 {obj['requests']['thinking_steps']} 步 · "
         f"估算 token {obj['tokens']['total_est']}（入 {obj['tokens']['input_est']} / 出 {obj['tokens']['output_est']}）")
    _log(f"  响应耗时    平均首响 {obj['timing']['avg_first_response_s']}s · "
         f"总耗时 {elapsed:.1f}s")
    if metrics.get("repro_summary", {}).get("verified"):
        rs = metrics["repro_summary"]
        _log(f"  复现率      已验证 {rs['verified']} 个问题：必现 {rs['stable']} · "
             f"高概率 {rs['likely']} · 偶发 {rs['flaky']}，平均 {rs['avg_rate']:.0%}")
    _log(f"  报告        {report_path}")
    if driver is not None and getattr(driver, "confirm_events", None):
        ce = driver.confirm_events
        _log(f"  自动确认    {len(ce)} 次 → " + "；".join(
            f"{e['time']} 「{e['text'][:20]}」" for e in ce[:6])
            + ("…" if len(ce) > 6 else ""))
    _log(_RULE)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
